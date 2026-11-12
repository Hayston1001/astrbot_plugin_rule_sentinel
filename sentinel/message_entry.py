"""Classify framework handlers and prepare copied message text."""


def is_activated_command(event, own_handler):
    """Skip only a recognized activated command handler, never on uncertainty."""
    try:
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.command_group import CommandGroupFilter
        from astrbot.core.star.filter.regex import RegexFilter

        handlers = event.get_extra("activated_handlers")
        if not isinstance(handlers, (list, tuple)):
            return False
        own = getattr(own_handler, "__func__", own_handler)
        for entry in handlers:
            handler = getattr(entry, "handler", None)
            if handler is not None and getattr(handler, "__func__", handler) is own:
                continue
            filters = getattr(entry, "event_filters", None)
            if isinstance(filters, (list, tuple)) and any(
                    isinstance(item, (CommandFilter, CommandGroupFilter, RegexFilter)) for item in filters):
                return True
    except Exception:
        return False
    return False


def strip_wake_prefix(event, context, text, at_type):
    """Strip one configured prefix from a copy, unless the bot was mentioned."""
    if not isinstance(text, str):
        return text
    try:
        chain = event.message_obj.message
        if not isinstance(chain, list):
            return text
        mentions = [segment for segment in chain if isinstance(segment, at_type)]
        if mentions:
            try:
                self_id = str(event.get_self_id())
            except Exception:
                return text
            if any(str(getattr(segment, "qq", "")) in (self_id, "all") for segment in mentions):
                return text
        config = context.get_config(umo=event.unified_msg_origin)
        prefixes = config.get("wake_prefix", [])
        if not isinstance(prefixes, (list, tuple)):
            return text
        for prefix in prefixes:
            if isinstance(prefix, str) and prefix and text.startswith(prefix):
                return text[len(prefix):].strip()
    except Exception:
        return text
    return text
