"""Look up recorded AstrBot private sessions without reading message contents."""

PAGE_SIZE = 25
MAX_PAGE = 400
MAX_DISPLAY_NAME_LENGTH = 80


def recipient_platforms(context):
    platforms = {}
    for instance in context.platform_manager.get_insts():
        meta = instance.meta()
        if (isinstance(meta.id, str) and meta.id and meta.id == meta.id.strip()
                and ":" not in meta.id and meta.name != "webchat"):
            platforms[meta.id] = meta.name
    return platforms


def _clean_name(value, session_id):
    """Keep a recorded name that cannot be mistaken for the session identifier."""
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or name in (session_id,):
        return ""
    return name[:MAX_DISPLAY_NAME_LENGTH]


async def _recorded_sender_names(db_helper, wanted):
    """Find names the platform recorded for the same sender identifier.

    A QQ official private conversation carries only the user openid, while the
    same member may have a visible name in recorded group activity. Names come
    from the same platform instance and the same sender identifier only; message
    contents are never read. A missing or failed lookup simply leaves the
    session identifier in place.
    """
    pairs = {(platform, session_id) for platform, sessions in wanted.items() for session_id in sessions}
    if not pairs:
        return {}
    ids = {session_id for _, session_id in pairs}
    names = {}

    from astrbot.core.db import po
    from sqlalchemy import select

    sender = getattr(po, "UmoSender", None)
    if sender is not None:
        try:
            async with db_helper.get_db() as session:
                rows = (await session.execute(
                    select(sender.umo, sender.sender_id, sender.sender_name).where(
                        sender.sender_id.in_(ids), sender.sender_name.is_not(None),
                        sender.sender_name != "",
                    ).order_by(sender.updated_at.desc())
                )).all()
            for umo, sender_id, value in rows:
                platform = umo.split(":", 1)[0] if isinstance(umo, str) else ""
                key = (platform, sender_id)
                if key in pairs and key not in names:
                    name = _clean_name(value, sender_id)
                    if name:
                        names[key] = name
        except Exception:
            pass

    history = getattr(po, "PlatformMessageHistory", None)
    if history is not None:
        try:
            async with db_helper.get_db() as session:
                rows = (await session.execute(
                    select(history.platform_id, history.sender_id, history.sender_name).where(
                        history.sender_id.in_(ids), history.sender_name.is_not(None),
                        history.sender_name != "",
                    ).order_by(history.updated_at.desc())
                )).all()
            for platform, sender_id, value in rows:
                key = (platform, sender_id)
                if key in pairs and key not in names:
                    name = _clean_name(value, sender_id)
                    if name:
                        names[key] = name
        except Exception:
            pass

    return names


async def _add_recorded_names(db_helper, recipients):
    wanted = {}
    for item in recipients:
        if item["nickname"] or item["alias"]:
            continue
        wanted.setdefault(item["platform"], set()).add(item["session_id"])
    if not wanted:
        return recipients
    names = await _recorded_sender_names(db_helper, wanted)
    for item in recipients:
        if item["nickname"] or item["alias"]:
            continue
        item["nickname"] = names.get((item["platform"], item["session_id"]), "")
    return recipients


async def find_notification_recipients(db_helper, platforms, *, search="", page=1, selected="", targets=()):
    if not platforms:
        if page is None:
            return []
        return {"recipients": [], "selected": None, "has_more": False}

    from astrbot.core.db import po
    from sqlalchemy import func, literal, or_, select, union_all

    def private(umo):
        # Match the full platform/type prefix exactly, including instance IDs
        # that contain SQL LIKE wildcards. Never turn a group sender into a DM.
        prefixes = [f"{platform}:FriendMessage:" for platform in platforms]
        return or_(*((func.instr(umo, prefix) == 1)
                     & (func.length(func.trim(func.substr(umo, len(prefix) + 1))) > 0)
                     for prefix in prefixes))

    alias = po.UmoAlias
    conversation = po.ConversationV2
    sources = [
        select(alias.umo.label("umo"), func.nullif(alias.auto_name, "").label("nickname"),
               func.nullif(alias.user_alias, "").label("alias"), alias.updated_at.label("seen"),
               literal(2).label("priority")).where(private(alias.umo)),
        select(conversation.user_id, literal(None), literal(None), func.max(conversation.updated_at), literal(0))
        .where(conversation.platform_id.in_(list(platforms)), private(conversation.user_id)).group_by(conversation.user_id),
    ]
    sender = getattr(po, "UmoSender", None)  # AstrBot 4.29.4 and later.
    if sender is not None:
        sources.append(select(sender.umo, func.nullif(sender.sender_name, ""), literal(None),
                              sender.updated_at, literal(1)).where(private(sender.umo)))
    known = union_all(*sources).subquery()
    ranked = select(
        known, func.max(known.c.seen).over(partition_by=known.c.umo).label("latest"),
        func.row_number().over(partition_by=known.c.umo, order_by=(
            (known.c.nickname.is_not(None) | known.c.alias.is_not(None)).desc(),
            known.c.priority.desc(), known.c.seen.desc(), known.c.nickname.asc(),
        )).label("rank"),
    ).subquery()
    base = select(ranked.c.umo, ranked.c.nickname, ranked.c.alias).where(ranked.c.rank == 1)

    def project(row):
        target, nickname, alias_name = row
        platform, _, session_id = target.split(":", 2)
        if nickname in (session_id, target):
            nickname = None
        return {"target": target, "nickname": (nickname or "").strip(), "alias": (alias_name or "").strip(),
                "platform": platform, "platform_type": platforms[platform], "session_id": session_id}

    async with db_helper.get_db() as session:
        if page is None:
            if not targets:
                return []
            rows = (await session.execute(base.where(ranked.c.umo.in_(targets)))).all()
            return await _add_recorded_names(db_helper, [project(row) for row in rows])
        matches = base
        if search:
            matches = matches.where(or_(*(column.icontains(search, autoescape=True) for column in
                                          (ranked.c.umo, ranked.c.nickname, ranked.c.alias))))
        matches = matches.order_by(ranked.c.latest.desc(), ranked.c.umo).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)
        rows = (await session.execute(matches)).all()
        selected_row = next((row for row in rows if row.umo == selected), None)
        if selected and selected_row is None:
            selected_row = (await session.execute(base.where(ranked.c.umo == selected))).first()
    recipients = await _add_recorded_names(db_helper, [project(row) for row in rows[:PAGE_SIZE]])
    selected_recipient = None
    if selected_row is not None:
        selected_recipient = (await _add_recorded_names(db_helper, [project(selected_row)]))[0]
    return {"recipients": recipients, "selected": selected_recipient,
            "has_more": page < MAX_PAGE and len(rows) > PAGE_SIZE}
