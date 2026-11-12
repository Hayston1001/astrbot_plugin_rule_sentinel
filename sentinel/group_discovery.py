"""Discover QQ groups already seen by AstrBot without reading message content."""

import re


QQ_TYPES = frozenset({"qq_official", "qq_official_webhook"})
MAX_HISTORY_GROUPS = 5000


def _valid_scope(platform_id, group_id):
    from .group_settings import validate_group_key

    if not isinstance(platform_id, str) or not isinstance(group_id, str):
        return False
    try:
        validate_group_key(f"{platform_id}/{group_id}")
    except ValueError:
        return False
    return re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_id) is not None


def _history_scope(umo, platforms):
    if not isinstance(umo, str):
        return None
    parts = umo.split(":", 2)
    if (len(parts) != 3 or parts[0] not in platforms or parts[1] != "GroupMessage"
            or re.fullmatch(r"[0-9a-fA-F]{32}", parts[2]) is None
            or not _valid_scope(parts[0], parts[2])):
        return None
    return parts[0], parts[2]


def _display_name(value, group_id):
    if (not isinstance(value, str) or not value.strip() or len(value) > 100
            or value.strip() == group_id or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return ""
    return value.strip()


async def _history_umos(database, platform_ids):
    # Only select session identities, never message contents. The QQ adapter
    # also labels channel messages GroupMessage, so those are filtered below.
    from sqlalchemy import select
    from astrbot.core.db.po import PlatformMessageHistory

    statement = (select(PlatformMessageHistory.platform_id, PlatformMessageHistory.user_id)
                 .where(PlatformMessageHistory.platform_id.in_(platform_ids),
                        PlatformMessageHistory.user_id.like("%:GroupMessage:%"))
                 .distinct().limit(MAX_HISTORY_GROUPS + 1))
    async with database.get_db() as session:
        rows = (await session.execute(statement)).all()
    return [(platform, umo) for platform, umo in rows]


async def discover_groups(context):
    platforms = {}
    groups = {}
    for platform in list(context.platform_manager.get_insts()):
        metadata = platform.meta()
        if metadata.name not in QQ_TYPES or not isinstance(metadata.id, str) or not metadata.id:
            continue
        platforms[metadata.id] = platform
        scenes = getattr(platform, "_session_scene", None)
        if isinstance(scenes, dict):
            for group_id, scene in list(scenes.items()):
                if scene == "group" and _valid_scope(metadata.id, group_id):
                    groups[(metadata.id, group_id)] = ""

    history_available = True
    truncated = False
    if platforms:
        try:
            database = context.get_db()
            rows = await _history_umos(database, list(platforms))
            truncated = len(rows) > MAX_HISTORY_GROUPS
            for platform_id, umo in rows[:MAX_HISTORY_GROUPS]:
                scope = _history_scope(umo, platforms)
                if scope and scope[0] == platform_id:
                    groups.setdefault(scope, "")
            umos = [f"{platform}:GroupMessage:{group}" for platform, group in groups]
            for offset in range(0, len(umos), 500):
                for alias in await database.get_umo_aliases(umos[offset:offset + 500]):
                    scope = _history_scope(getattr(alias, "umo", None), platforms)
                    if scope in groups:
                        groups[scope] = (_display_name(getattr(alias, "user_alias", None), scope[1])
                                         or _display_name(getattr(alias, "auto_name", None), scope[1]))
        except Exception:
            history_available = False

    return {"groups": [{"platform_id": platform, "group_id": group, "name": name}
                       for (platform, group), name in sorted(groups.items())],
            "platforms_available": bool(platforms), "history_available": history_available, "truncated": truncated}
