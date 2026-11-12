"""Fail-closed parsing of administrator-provided monitoring settings."""

from dataclasses import dataclass
import re

from .monitoring_policy import MonitoringPolicy
from .dry_run_queue import SYSTEM_PLATFORM


@dataclass(frozen=True)
class Settings:
    policy: MonitoringPolicy
    capacity: int
    backend: str
    model: str
    max_duration_seconds: int
    concurrency: int
    timeout_seconds: int
    global_rate_per_minute: int
    group_rate_per_minute: int
    failure_threshold: int
    cooldown_seconds: int
    ingress_capacity: int
    rule_selection_strategy: str
    llm_no_action_reason_enabled: bool


def decision_model_unconfigured(config):
    """Recognize an explicitly empty selection without accepting invalid types."""
    backend = config.get("decision_backend", "")
    model = config.get("llm_provider_id", "")
    return backend == "" or (backend == "llm" and isinstance(model, str) and not model.strip())


def read_settings(config, *, require_model=True) -> Settings:
    mode = config.get("monitoring_mode", "allowlist")
    entries = config.get("monitoring_groups", [])
    capacity = config.get("queue_capacity_per_group", 100)
    backend = config.get("decision_backend", "")
    duration = config.get("max_duration_seconds", 2592000)
    concurrency = config.get("model_concurrency", 20)
    timeout = config.get("model_timeout_seconds", 35)
    global_rate = config.get("global_rate_per_minute", 100)
    group_rate = config.get("group_rate_per_minute", 60)
    failure_threshold = config.get("model_failure_threshold", 5)
    cooldown = config.get("model_cooldown_seconds", 120)
    ingress_capacity = config.get("ingress_capacity", 256)
    strategy = config.get("rule_selection_strategy", "model")
    no_action_reason = config.get("llm_no_action_reason_enabled", True)
    if mode not in ("allowlist", "blocklist") or not isinstance(entries, list):
        raise ValueError("invalid monitoring settings")
    groups = set()
    for entry in entries:
        if not isinstance(entry, str) or entry.count("/") != 1:
            raise ValueError("group entries must use platform/group")
        platform_id, group_id = entry.split("/")
        if not platform_id.strip() or not group_id.strip() or platform_id != platform_id.strip() or group_id != group_id.strip():
            raise ValueError("invalid group identity")
        if platform_id == SYSTEM_PLATFORM:
            raise ValueError("reserved platform identity")
        groups.add((platform_id, group_id))
    if any(type(number) is not int or number <= 0 for number in
           (capacity, duration, concurrency, timeout, global_rate, group_rate, failure_threshold, cooldown, ingress_capacity)):
        raise ValueError("numeric limits must be positive integers")
    if duration > 2592000:
        raise ValueError("duration exceeds QQ official limit")
    if backend not in ("", "llm", "jev"):
        raise ValueError("invalid decision backend")
    if strategy not in ("model", "priority"):
        raise ValueError("invalid rule selection strategy")
    if type(no_action_reason) is not bool:
        raise ValueError("invalid no-action reason setting")
    model = config.get("llm_provider_id", "") if backend == "llm" else config.get("jev_model", "") if backend == "jev" else ""
    provider = config.get("jev_provider", "typesafe")
    if not isinstance(provider, str) or provider not in ("typesafe", "openrouter"):
        raise ValueError("invalid Jev provider")
    if not isinstance(model, str) or (require_model and (not backend or not model.strip())):
        raise ValueError("explicit decision model is required")
    if backend == "jev" and model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}" if provider == "typesafe"
                                                       else r"typesafe/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model):
        raise ValueError("invalid Jev model")
    return Settings(MonitoringPolicy(mode, frozenset(groups)), capacity, backend,
                    model, duration, concurrency, timeout, global_rate, group_rate, failure_threshold, cooldown, ingress_capacity, strategy,
                    no_action_reason)
