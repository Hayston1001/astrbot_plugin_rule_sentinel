"""Validate administrator-owned action configuration without executing it."""

from string import Formatter


ACTION_TYPES = frozenset({"reply", "mute", "recall"})
CONCRETE_EXECUTION_MODES = frozenset({"auto", "confirm"})
EXECUTION_MODES = CONCRETE_EXECUTION_MODES | {"inherit"}
TEMPLATE_FIELDS = frozenset({"sender_id", "rule_id"})
REPLY_OPTION_KEYS = ("quote", "mention_sender")
REPLY_DEFAULTS = {"reply_quote": True, "reply_mention_sender": False}
MAX_TEMPLATE_LENGTH = 2000
DURATION_UNITS = frozenset({1, 60, 3600, 86400})


def _validate_duration_unit(seconds, unit):
    if type(unit) is not int or unit not in DURATION_UNITS or seconds % unit:
        raise ValueError("invalid display unit for duration")


def resolve_action_mode(mode, default_mode=None):
    """Resolve inherited mode only from a valid, trusted global setting."""
    if isinstance(mode, str) and mode in CONCRETE_EXECUTION_MODES:
        return mode
    if mode == "inherit" and isinstance(default_mode, str) and default_mode in CONCRETE_EXECUTION_MODES:
        return default_mode
    raise ValueError("invalid action mode")


def resolve_reply_options(action, config):
    defaults = {key: config.get(f"reply_{key}") for key in REPLY_OPTION_KEYS}
    options = {key: action.get(key, defaults[key]) for key in REPLY_OPTION_KEYS}
    if any(type(value) is not bool for value in (*defaults.values(), *options.values())):
        raise ValueError("invalid reply options")
    return options


def validate_actions(actions):
    """Return a normalized copy of an action list, rejecting unknown fields.

    An empty list represents no allowed actions; callers decide whether that is
    valid for a newly enabled rule. It must never be upgraded to a mute.
    """
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    result = []
    seen = set()
    for item in actions:
        if not isinstance(item, dict):
            raise ValueError("invalid action")
        kind = item.get("type")
        mode = item.get("mode")
        if not isinstance(kind, str) or kind not in ACTION_TYPES or kind in seen:
            raise ValueError("invalid or duplicate action type")
        if not isinstance(mode, str) or mode not in EXECUTION_MODES:
            raise ValueError("invalid action mode")
        seen.add(kind)
        if kind == "reply":
            fields = {"type", "mode", "template"}
            if not fields <= set(item) <= fields | set(REPLY_OPTION_KEYS):
                raise ValueError("invalid reply fields")
            template = item["template"]
            if not isinstance(template, str) or not template.strip() or len(template) > MAX_TEMPLATE_LENGTH:
                raise ValueError("invalid reply template")
            try:
                for _, field, spec, conversion in Formatter().parse(template):
                    if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                        raise ValueError("invalid reply placeholder")
            except (ValueError, TypeError) as error:
                raise ValueError("invalid reply template") from error
            options = {key: item[key] for key in REPLY_OPTION_KEYS if key in item}
            if any(type(value) is not bool for value in options.values()):
                raise ValueError("invalid reply options")
            result.append({"type": kind, "mode": mode, "template": template, **options})
        elif kind == "mute":
            if item.get("duration_mode") == "cumulative":
                fields = {"type", "mode", "duration_mode", "window_seconds", "tiers_seconds"}
                if not fields <= set(item) <= fields | {"window_unit", "tier_units"}:
                    raise ValueError("invalid cumulative mute fields")
                from .violation_store import validate_ladder
                window, tiers = item["window_seconds"], item["tiers_seconds"]
                if not isinstance(tiers, list):
                    raise ValueError("invalid violation tiers")
                validate_ladder(window, tuple(tiers))
                action = {"type": kind, "mode": mode, "duration_mode": "cumulative",
                          "window_seconds": window, "tiers_seconds": list(tiers)}
                if "window_unit" in item:
                    _validate_duration_unit(window, item["window_unit"])
                    action["window_unit"] = item["window_unit"]
                if "tier_units" in item:
                    units = item["tier_units"]
                    if not isinstance(units, list) or len(units) != len(tiers):
                        raise ValueError("invalid tier display units")
                    for seconds, unit in zip(tiers, units):
                        _validate_duration_unit(seconds, unit)
                    action["tier_units"] = list(units)
                result.append(action)
                continue
            fields = {"type", "mode", "duration_mode", "severity_seconds"}
            if not fields <= set(item) <= fields | {"severity_units"}:
                raise ValueError("invalid mute fields")
            duration_mode, mapping = item["duration_mode"], item["severity_seconds"]
            if duration_mode not in ("strict", "autonomous") or not isinstance(mapping, dict):
                raise ValueError("invalid duration policy")
            if (duration_mode == "strict" and not mapping) or (duration_mode == "autonomous" and mapping):
                raise ValueError("invalid severity mapping")
            if any(not isinstance(key, str) or not key.strip() or type(value) is not int or value <= 0
                   for key, value in mapping.items()):
                raise ValueError("invalid severity mapping")
            action = {"type": kind, "mode": mode, "duration_mode": duration_mode,
                      "severity_seconds": dict(mapping)}
            if "severity_units" in item:
                units = item["severity_units"]
                if duration_mode != "strict" or not isinstance(units, dict) or set(units) != set(mapping):
                    raise ValueError("invalid severity display units")
                for level, seconds in mapping.items():
                    _validate_duration_unit(seconds, units[level])
                action["severity_units"] = dict(units)
            result.append(action)
        else:
            if set(item) != {"type", "mode"}:
                raise ValueError("invalid recall fields")
            result.append({"type": kind, "mode": mode})
    return result


def migrate_legacy_actions(allowed_actions, duration_mode, severity_seconds):
    """Convert legacy mute permissions without enabling absent actions."""
    if not isinstance(allowed_actions, list) or any(action != "mute" for action in allowed_actions):
        raise ValueError("invalid legacy actions")
    if len(allowed_actions) != len(set(allowed_actions)):
        raise ValueError("duplicate legacy action")
    if not allowed_actions:
        return []
    return validate_actions([{"type": "mute", "mode": "auto", "duration_mode": duration_mode,
                              "severity_seconds": severity_seconds}])
