"""Resolve group-specific defaults without weakening global safety switches."""

import hashlib
import json
import re


GROUP_KEYS = frozenset({"model", "strategy", "execution_mode", "protected_policy",
                        "protected_template", "protected_quote", "llm_reason_language"})


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
    selected = group.get("model")
    if "model" in group and (not isinstance(selected, dict) or set(selected) != {"backend", "model"}
                                 or selected["backend"] not in ("llm", "jev")
                                 or not isinstance(selected["model"], str) or not selected["model"].strip()
                                 or len(selected["model"]) > 128):
        raise ValueError("invalid group model")
    backend = selected["backend"] if selected else config.get("decision_backend", "")
    model = selected["model"] if selected else (config.get("llm_provider_id", "") if backend == "llm"
                                                else config.get("jev_model", "") if backend == "jev" else "")
    strategy = group.get("strategy", config.get("rule_selection_strategy", "model"))
    mode = group.get("execution_mode", config.get("default_execution_mode", "execute"))
    reason_language = validate_reason_language(group.get("llm_reason_language", config.get("llm_reason_language", "")))
    if strategy not in ("model", "priority") or mode not in ("record", "execute"):
        raise ValueError("invalid group processing")
    if backend not in ("", "llm", "jev") or not isinstance(model, str) or (backend and not model.strip()):
        raise ValueError("invalid group model")
    if backend == "jev":
        provider = config.get("jev_provider", "typesafe")
        pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}" if provider == "typesafe" else r"typesafe/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        if provider not in ("typesafe", "openrouter") or not re.fullmatch(pattern, model):
            raise ValueError("invalid Jev group model")
        if not isinstance(config.get("jev_api_key"), str) or not config["jev_api_key"].strip():
            raise ValueError("missing Jev key")
    policy = group.get("protected_policy", config.get("protected_target_policy", "exempt"))
    template = group.get("protected_template", config.get("protected_target_template", ""))
    quote = group.get("protected_quote", config.get("protected_target_quote", False))
    from .settings_store import validate_protected_target_settings
    validate_protected_target_settings({"protected_target_policy": policy,
                                        "protected_target_template": template,
                                        "protected_target_quote": quote})
    return {"backend": backend, "model": model, "strategy": strategy, "execution_mode": mode,
            "protected_policy": policy, "protected_template": template, "protected_quote": quote,
            "llm_reason_language": reason_language}


def execution_allowed(config, platform_id, group_id):
    return (config.get("action_execution_enabled") is True and
            resolve_group_settings(config, platform_id, group_id)["execution_mode"] == "execute")


def decision_fingerprint(config, platform_id, group_id):
    group = resolve_group_settings(config, platform_id, group_id)
    if group["backend"] != "llm":
        group["llm_reason_language"] = None
    payload = {"group": group, "monitoring_mode": config.get("monitoring_mode", "allowlist"),
               "monitoring_groups": config.get("monitoring_groups", []),
               "action_execution_enabled": config.get("action_execution_enabled", False),
               "default_action_mode": config.get("default_action_mode", "auto"),
               "max_duration_seconds": config.get("max_duration_seconds", 2592000),
               "jev_provider": config.get("jev_provider", "typesafe") if group["backend"] == "jev" else None,
               "jev_api_key": config.get("jev_api_key", "") if group["backend"] == "jev" else None}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
