"""Fail-closed parsing of administrator-provided monitoring settings."""

from dataclasses import dataclass

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
    image_judging_enabled: bool
    image_understanding_mode: str
    image_provider_id: str
    image_caption_prompt: str
    image_max_count: int
    image_max_bytes_per_image: int
    image_max_total_bytes: int
    image_fetch_timeout_seconds: int


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
    image_enabled = config.get("image_judging_enabled", False)
    image_mode = config.get("image_understanding_mode", "direct")
    image_provider = config.get("image_provider_id", "")
    image_caption_prompt = config.get("image_caption_prompt", "")
    image_max_count = config.get("image_max_count", 9)
    image_max_bytes = config.get("image_max_bytes_per_image", 10485760)
    image_max_total = config.get("image_max_total_bytes", 20971520)
    image_timeout = config.get("image_fetch_timeout_seconds", 15)
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
    if backend not in ("", "llm"):
        raise ValueError("invalid decision backend")
    if strategy not in ("model", "priority"):
        raise ValueError("invalid rule selection strategy")
    if type(no_action_reason) is not bool:
        raise ValueError("invalid no-action reason setting")
    if type(image_enabled) is not bool or image_mode not in ("caption", "direct"):
        raise ValueError("invalid image judging setting")
    if not isinstance(image_provider, str) or len(image_provider) > 128:
        raise ValueError("invalid image provider")
    if (not isinstance(image_caption_prompt, str) or len(image_caption_prompt) > 1000
            or any(ord(char) < 32 and char not in "\n\t" or ord(char) == 127
                   for char in image_caption_prompt)):
        raise ValueError("invalid image caption prompt")
    if type(image_max_count) is not int or not 1 <= image_max_count <= 9:
        raise ValueError("invalid image count limit")
    if (type(image_max_bytes) is not int or not 65536 <= image_max_bytes <= 33554432
            or type(image_max_total) is not int or not image_max_bytes <= image_max_total <= 67108864):
        raise ValueError("invalid image size limit")
    if type(image_timeout) is not int or not 1 <= image_timeout <= 60:
        raise ValueError("invalid image timeout")
    if image_enabled:
        if not image_provider.strip():
            raise ValueError("image provider is required")
        if image_mode == "caption" and not image_caption_prompt.strip():
            raise ValueError("image caption prompt is required")
    model = config.get("llm_provider_id", "") if backend == "llm" else ""
    if not isinstance(model, str) or (require_model and (not backend or not model.strip())):
        raise ValueError("explicit decision model is required")
    return Settings(MonitoringPolicy(mode, frozenset(groups)), capacity, backend,
                    model, duration, concurrency, timeout, global_rate, group_rate, failure_threshold, cooldown, ingress_capacity, strategy,
                    no_action_reason, image_enabled, image_mode, image_provider.strip(), image_caption_prompt,
                    image_max_count, image_max_bytes, image_max_total, image_timeout)
