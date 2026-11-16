"""Extract a minimal QQ official group message snapshot without retaining events.

AstrBot 4.29.3 can use placeholder self IDs, so this module never treats event
self_id as proof. Only explicit QQ author.bot=false with a matching member
identifier is accepted. Missing or ambiguous bot metadata is skipped. The
result contains no platform client and cannot perform moderation actions.

A quote reply carries the referenced message in its own segment. Only the
sender's new text is judged; the quoted content belongs to another message and
is never attributed to the sender.

QQ replaces an @mention in the message text with an opaque member id. When the
payload's mention list provides a display name for that id, the snapshot shows
the name instead. An id without a name keeps its original markup.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .dry_run_ingest import IncomingMessage

# Both forms are produced by QQ clients: `<@openid>` and the tag form.
_MENTION_MARKUP = re.compile(r"<@!?([^<>\s\"]{1,128})>|<qqbot-at-user\s+id=\"([^<>\s\"]{1,128})\"\s*/>")


def _mention_names(raw_data):
    """Map mention ids to display names using the payload's mention list."""
    mentions = raw_data.get("mentions") if isinstance(raw_data, dict) else None
    if not isinstance(mentions, list):
        return {}
    names = {}
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        name = mention.get("username")
        if not isinstance(name, str) or not name.strip():
            continue
        for key in ("member_openid", "id", "user_openid"):
            value = mention.get(key)
            if isinstance(value, str) and value.strip():
                names[value] = name.strip()
    return names


def _render_mentions(text, names):
    """Replace mention markup for known ids with the member's display name."""
    if not names:
        return text

    def replace(match):
        name = names.get(match.group(1) or match.group(2))
        return f"@{name}" if name else match.group(0)

    return _MENTION_MARKUP.sub(replace, text)


@dataclass(frozen=True)
class Extraction:
    status: str
    message: IncomingMessage | None = None


def _valid_image_reference(value):
    """Accept a bounded http(s) URL or a local image file set by the framework."""
    if not isinstance(value, str) or not value:
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if value.startswith(("http://", "https://")):
        return len(value) <= 8192
    if len(value) > 4096:
        return False
    try:
        import os
        if value.startswith("file://"):
            from urllib.parse import unquote, urlsplit
            from urllib.request import url2pathname
            path = url2pathname(unquote(urlsplit(value).path))
        else:
            path = value
        return os.path.isabs(path) and os.path.isfile(path)
    except (OSError, ValueError):
        return False


def extract(event, *, is_management_command, text_segment_types,
            quote_segment_types=(), image_segment_types=()) -> Extraction:
    if event.get_platform_name() not in ("qq_official", "qq_official_webhook"):
        return Extraction("unsupported_platform")
    obj = event.message_obj
    raw = getattr(obj, "raw_message", None)
    raw_group = getattr(raw, "group_openid", None)
    group = getattr(obj, "group_id", None)
    if not isinstance(raw_group, str) or not raw_group or raw_group != group:
        return Extraction("not_verified_qq_group")
    platform = event.get_platform_id()
    sender = event.get_sender_id()
    message_id = getattr(obj, "message_id", None)
    if any(not isinstance(value, str) or not value.strip() for value in (platform, group, sender, message_id)):
        return Extraction("invalid_message_identity")
    origin = getattr(event, "unified_msg_origin", None)
    if not isinstance(origin, str) or not origin.strip():
        return Extraction("session_identity_unverified")
    raw_data = getattr(raw, "raw_data", None)
    author = raw_data.get("author") if isinstance(raw_data, dict) else None
    if not isinstance(author, dict) or author.get("member_openid") != sender:
        return Extraction("sender_identity_unverified")
    if author.get("bot") is True:
        return Extraction("bot_message")
    if author.get("bot") is not False:
        return Extraction("bot_identity_unverified")
    chain = getattr(obj, "message", None)
    if not isinstance(chain, list):
        return Extraction("invalid_message_chain")
    # At segments do not disqualify an otherwise textual message. A quoted
    # reply adds a segment that only references another message, so it is
    # ignored here; the new text still has to be purely textual and any media
    # or rich payload on the current message keeps it non-text.
    if not isinstance(text_segment_types, tuple) or not text_segment_types or not all(isinstance(t, type) for t in text_segment_types):
        return Extraction("invalid_text_component_types")
    if not isinstance(quote_segment_types, tuple) or not all(isinstance(t, type) for t in quote_segment_types):
        return Extraction("invalid_text_component_types")
    if not isinstance(image_segment_types, tuple) or not all(isinstance(t, type) for t in image_segment_types):
        return Extraction("invalid_text_component_types")
    images = []
    has_other_media = False
    for segment in chain:
        if isinstance(segment, text_segment_types) or isinstance(segment, quote_segment_types):
            continue
        if image_segment_types and isinstance(segment, image_segment_types):
            reference = getattr(segment, "file", None) or getattr(segment, "url", None)
            if not _valid_image_reference(reference):
                return Extraction("invalid_image_reference")
            images.append(reference)
            continue
        has_other_media = True
        break
    raw_type = raw_data.get("message_type")
    quoted = type(raw_type) is int and raw_type == 103
    has_quote_segment = any(isinstance(segment, quote_segment_types) for segment in chain)
    # The normalized message chain alone may omit rich payload fields. Treat
    # unknown, card and attachment payloads as non-text. A quote payload
    # (message_type 103) legitimately carries msg_elements for the referenced
    # message; it is accepted only when a quote segment proves the framework
    # separated the quoted content from the sender's own text. A caption or a
    # face is not media; images count as removable media only when the whole
    # chain is otherwise textual and the attachment list agrees with the parsed
    # image segments.
    attachments = raw_data.get("attachments")
    payload_ok = (type(raw_type) is int and raw_type in (0, 103)
                  and not raw_data.get("ark_data")
                  and (has_quote_segment if quoted else not raw_data.get("msg_elements"))
                  and not has_other_media)
    if payload_ok and images:
        if isinstance(attachments, list) and len(attachments) != len(images):
            payload_ok = False
        elif attachments and not isinstance(attachments, list):
            payload_ok = False
    is_text = payload_ok and not images and not attachments
    if not payload_ok:
        images = []
    text = getattr(obj, "message_str", None)
    if not isinstance(text, str):
        return Extraction("invalid_text")
    text = _render_mentions(text, _mention_names(raw_data))
    command = is_management_command(text)
    if type(command) is not bool:
        return Extraction("command_classification_unknown")
    message_time = None
    try:
        timestamp = getattr(raw, "timestamp", None) or raw_data.get("timestamp")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else None
        if parsed is not None and parsed.tzinfo is not None and parsed <= datetime.now(timezone.utc):
            message_time = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, TypeError, OverflowError):
        pass
    return Extraction("ok", IncomingMessage(platform, group, message_id, sender, None,
                                             text, is_text, command, sender_is_verified_non_bot=True,
                                             session_origin=origin, message_time=message_time, original_text=text,
                                             image_urls=tuple(images)))
