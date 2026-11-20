"""Resolve group-specific defaults without weakening global safety switches."""

import hashlib
import json

from .chat_context import validate_context_messages


CHAT_ACTIONS = ("mute", "recall", "reply")
CHAT_QUERY_ITEMS = ("status", "rules", *CHAT_ACTIONS, "mute_target")
DEFAULT_CHAT_QUERY_ITEMS = ("status", *CHAT_ACTIONS)
LEGACY_CHAT_FLAGS = ("chat_show_rules", "chat_show_mute_target")
GROUP_CHAT_ITEM_KEYS = frozenset((*LEGACY_CHAT_FLAGS, "chat_query_items"))
GROUP_CHAT_KEYS = GROUP_CHAT_ITEM_KEYS | {"chat_query_inherit"}
GROUP_KEYS = GROUP_CHAT_KEYS | {"model", "strategy", "execution_mode", "protected_policy",
                                "protected_template", "protected_quote", "llm_reason_language",
                                "image_enabled", "image_model", "shared_violation_rules", "shared_violation_groups",
                                "context_messages"}


def validate_chat_query_items(items):
    if (not isinstance(items, list) or len(items) > len(CHAT_QUERY_ITEMS)
            or any(not isinstance(item, str) or item not in CHAT_QUERY_ITEMS for item in items)
            or len(set(items)) != len(items) or ("mute_target" in items and "mute" not in items)):
        raise ValueError("invalid chat query items")
    return tuple(item for item in CHAT_QUERY_ITEMS if item in items)


def chat_query_items(overrides):
    if "chat_query_items" in overrides:
        if any(flag in overrides for flag in LEGACY_CHAT_FLAGS):
            raise ValueError("invalid group chat query items")
        return validate_chat_query_items(overrides["chat_query_items"])
    if any(type(overrides.get(flag, False)) is not bool for flag in LEGACY_CHAT_FLAGS):
        raise ValueError("invalid group chat disclosure")
    return (*DEFAULT_CHAT_QUERY_ITEMS,
            *(("rules",) if overrides.get("chat_show_rules") else ()),
            *(("mute_target",) if overrides.get("chat_show_mute_target") else ()))


def effective_chat_query_items(config, platform_id, group_id):
    """Select one disclosure policy; never merge global and group allowlists."""
    key = f"{platform_id}/{group_id}"
    validate_group_key(key)
    group = config.get("group_overrides", {}).get(key, {})
    if not chat_query_inherits(group):
        return chat_query_items(group)
    return validate_chat_query_items(config.get("chat_query_items", list(DEFAULT_CHAT_QUERY_ITEMS)))


def chat_query_inherits(group):
    value = group.get("chat_query_inherit", True)
    if type(value) is not bool:
        raise ValueError("invalid group chat query source")
    return value


def merge_group_chat_settings(previous, submitted):
    """Keep the dormant local choice when the dashboard saves unrelated fields."""
    old_chat = {key: value for key, value in previous.items() if key in GROUP_CHAT_KEYS}
    new_chat = {key: value for key, value in submitted.items() if key in GROUP_CHAT_KEYS}
    if not new_chat:
        return old_chat
    updated = {**old_chat, **new_chat}
    item_fields = GROUP_CHAT_ITEM_KEYS & new_chat.keys()
    if item_fields:
        if chat_query_inherits(updated):
            raise ValueError("group inherits global chat queries")
        # The submitted list replaces the old representation (including legacy flags).
        updated = {**{key: value for key, value in updated.items() if key not in GROUP_CHAT_ITEM_KEYS},
                   **{key: new_chat[key] for key in item_fields}}
    if chat_query_inherits(updated):
        updated.pop("chat_query_inherit", None)
    return updated


def resolve_violation_groups(group):
    """Resolve disjoint groups, including the old single-list format."""
    def valid_rules(rules):
        return (isinstance(rules, list) and len(rules) <= 100
                and all(isinstance(rule, str) and rule.strip() for rule in rules)
                and len(set(rules)) == len(rules))

    if "shared_violation_groups" not in group:
        rules = group.get("shared_violation_rules", [])
        if not valid_rules(rules):
            raise ValueError("invalid shared violation rules")
        return [{"name": "", "rule_ids": sorted(rules)}] if len(rules) > 1 else []
    groups = group["shared_violation_groups"]
    if "shared_violation_rules" in group or not isinstance(groups, list) or len(groups) > 50:
        raise ValueError("invalid shared violation groups")
    names, assigned, result = set(), set(), []
    for item in groups:
        if not isinstance(item, dict) or set(item) not in ({"name", "rule_ids"}, {"rule_ids"}):
            raise ValueError("invalid shared violation group")
        name, rules = item.get("name", ""), item["rule_ids"]
        if (not isinstance(name, str) or len(name) > 60 or name.strip() != name
                or any(ord(char) < 32 or ord(char) == 127 for char in name) or (name and name in names)):
            raise ValueError("invalid or duplicate shared group name")
        if not valid_rules(rules) or len(rules) < 2 or assigned.intersection(rules):
            raise ValueError("invalid or overlapping shared group rules")
        names.add(name)
        assigned.update(rules)
        result.append({"name": name, "rule_ids": sorted(rules)})
    if len(assigned) > 100:
        raise ValueError("too many shared violation rules")
    return result


def validate_violation_group_members(groups, previous, rules):
    """Only saved cumulative rules can join; inactive members may stay or leave."""
    known = {rule["rule_id"] for rule in rules}
    cumulative = {rule["rule_id"] for rule in rules if any(
        action["type"] == "mute" and action.get("duration_mode") == "cumulative"
        for action in rule["actions"])}
    old_members = {rule_id: set(item["rule_ids"]) for item in previous for rule_id in item["rule_ids"]}
    for item in groups:
        members = set(item["rule_ids"])
        if not members <= known:
            raise ValueError("shared violation rules must belong to the group")
        for rule_id in members - cumulative:
            if not ((members - {rule_id}) & old_members.get(rule_id, set())):
                raise ValueError("shared violation rules must use cumulative mute")


def validate_reason_language(value):
    if (not isinstance(value, str) or len(value) > 80
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid reason language")
    return value


def validate_group_key(key):
    if not isinstance(key, str) or key.count("/") != 1:
        raise ValueError("invalid group identity")
    if any(not value or value != value.strip() or len(value) > 128 for value in key.split("/")):
        raise ValueError("invalid group identity")
    if key.split("/")[0] == "__rule_sentinel_system__":
        raise ValueError("reserved platform identity")


def resolve_group_settings(config, platform_id, group_id):
    if not isinstance(config, dict):
        raise ValueError("invalid settings")
    key = f"{platform_id}/{group_id}"
    validate_group_key(key)
    overrides = config.get("group_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("invalid group overrides")
    group = overrides.get(key, {})
    if not isinstance(group, dict) or set(group) - GROUP_KEYS:
        raise ValueError("invalid group settings")
    shared = resolve_violation_groups(group)
    chat_query_inherits(group)
    query_items = chat_query_items(group)
    selected = group.get("model")
    if "model" in group and (not isinstance(selected, dict) or set(selected) != {"backend", "model"}
                                 or selected["backend"] != "llm"
                                 or not isinstance(selected["model"], str) or not selected["model"].strip()
                                 or len(selected["model"]) > 128):
        raise ValueError("invalid group model")
    backend = selected["backend"] if selected else config.get("decision_backend", "")
    model = selected["model"] if selected else (config.get("llm_provider_id", "") if backend == "llm" else "")
    image_selected = group.get("image_model")
    if "image_model" in group and (not isinstance(image_selected, dict) or set(image_selected) != {"backend", "model"}
                                   or image_selected["backend"] != "llm"
                                   or not isinstance(image_selected["model"], str)
                                   or not image_selected["model"].strip()
                                   or len(image_selected["model"]) > 128):
        raise ValueError("invalid group image model")
    image_enabled = group.get("image_enabled", config.get("image_judging_enabled", False))
    if type(image_enabled) is not bool:
        raise ValueError("invalid group image setting")
    image_model = (image_selected["model"] if "image_model" in group
                   else config.get("image_provider_id", ""))
    image_mode = config.get("image_understanding_mode", "direct")
    if image_mode not in ("caption", "direct"):
        raise ValueError("invalid image understanding mode")
    strategy = group.get("strategy", config.get("rule_selection_strategy", "model"))
    mode = group.get("execution_mode", config.get("default_execution_mode", "execute"))
    reason_language = validate_reason_language(group.get("llm_reason_language", config.get("llm_reason_language", "")))
    if strategy not in ("model", "priority") or mode not in ("record", "execute"):
        raise ValueError("invalid group processing")
    if backend not in ("", "llm") or not isinstance(model, str):
        raise ValueError("invalid group model")
    policy = group.get("protected_policy", config.get("protected_target_policy", "exempt"))
    template = group.get("protected_template", config.get("protected_target_template", ""))
    quote = group.get("protected_quote", config.get("protected_target_quote", False))
    from .settings_store import validate_protected_target_settings
    validate_protected_target_settings({"protected_target_policy": policy,
                                        "protected_target_template": template,
                                        "protected_target_quote": quote})
    return {"backend": backend, "model": model, "strategy": strategy, "execution_mode": mode,
            "protected_policy": policy, "protected_template": template, "protected_quote": quote,
            "llm_reason_language": reason_language, "image_enabled": image_enabled,
             "image_model": image_model, "image_understanding_mode": image_mode,
               "shared_violation_groups": shared,
                "context_messages": validate_context_messages(group.get("context_messages", 0)),
                "chat_query_items": query_items}


def execution_allowed(config, platform_id, group_id):
    return (config.get("action_execution_enabled") is True and
            resolve_group_settings(config, platform_id, group_id)["execution_mode"] == "execute")


def decision_fingerprint(config, platform_id, group_id):
    group = resolve_group_settings(config, platform_id, group_id)
    # Names and order do not change the rules contributing to a count.
    group["shared_violation_groups"] = sorted(item["rule_ids"] for item in group["shared_violation_groups"])
    del group["chat_query_items"]
    if not group["context_messages"]:
        del group["context_messages"]  # Keep existing fingerprints when context is off.
    if group["backend"] != "llm":
        group["llm_reason_language"] = None
    payload = {"group": group, "monitoring_mode": config.get("monitoring_mode", "allowlist"),
               "monitoring_groups": config.get("monitoring_groups", []),
               "action_execution_enabled": config.get("action_execution_enabled", False),
               "default_action_mode": config.get("default_action_mode", "auto"),
               "max_duration_seconds": config.get("max_duration_seconds", 2592000)}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
