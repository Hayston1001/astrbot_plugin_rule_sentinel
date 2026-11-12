"""Extract a minimal QQ official group message snapshot without retaining events.

AstrBot 4.29.3 can use placeholder self IDs, so this module never treats event
self_id as proof. Only explicit QQ author.bot=false with a matching member
identifier is accepted. Missing or ambiguous bot metadata is skipped. The
result contains no platform client and cannot perform moderation actions.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from .dry_run_ingest import IncomingMessage


@dataclass(frozen=True)
class Extraction:
    status: str
    message: IncomingMessage | None = None


def extract(event, *, is_management_command, text_segment_types) -> Extraction:
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
    # At segments do not disqualify an otherwise textual message. Quoted or
    # media segments are conservatively skipped until their context is handled.
    if not isinstance(text_segment_types, tuple) or not text_segment_types or not all(isinstance(t, type) for t in text_segment_types):
        return Extraction("invalid_text_component_types")
    raw_type = raw_data.get("message_type")
    # The normalized message chain alone may omit rich payload fields. Treat
    # unknown, quoted, card, attachment and nested-element payloads as non-text.
    is_text = (type(raw_type) is int and raw_type == 0
               and not raw_data.get("attachments") and not raw_data.get("ark_data")
               and not raw_data.get("msg_elements")
               and all(isinstance(segment, text_segment_types) for segment in chain))
    text = getattr(obj, "message_str", None)
    if not isinstance(text, str):
        return Extraction("invalid_text")
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
                                             session_origin=origin, message_time=message_time, original_text=text))
