"""Construct the dry-run runtime without connecting it to platform events."""

from copy import deepcopy
from dataclasses import replace

from .astrbot_llm import AstrBotLlmInvoker
from .circuit_breaker import CircuitBreaker
from .dry_run_ingest import DryRunAdmission
from .dry_run_runtime import DryRunRuntime
from .dry_run_worker import DryRunWorker
from .llm_decider import LlmDecider
from .plugin_settings import read_settings
from .policy_guard import PolicyGuard
from .rate_limiter import RateLimiter
from .group_settings import decision_fingerprint, resolve_group_settings


class ReasonConfiguredDecider:
    """Select a stable LLM decider using the setting at decision start."""

    def __init__(self, config, make_decider):
        self.config = config
        self.make_decider = make_decider
        self.deciders = {}

    def for_setting(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid no-action reason setting")
        if enabled not in self.deciders:
            self.deciders[enabled] = self.make_decider(enabled)
        return self.deciders[enabled]

    def current(self):
        return self.for_setting(self.config.get("llm_no_action_reason_enabled", True))

    async def __call__(self, item):
        decider = self.current()
        return await decider(item)


class GroupDecider:
    """Route each group to its configured model while sharing worker limits."""

    def __init__(self, config, make_decider):
        self.config = deepcopy(config)
        self.make_decider = make_decider
        self.deciders = {}

    async def __call__(self, item):
        key = (item.platform_id, item.group_id)
        effective = resolve_group_settings(self.config, *key)
        route = (effective["backend"], effective["model"], effective["strategy"], effective["llm_reason_language"])
        if route not in self.deciders:
            self.deciders[route] = self.make_decider(*route)
        result = await self.deciders[route](item)
        return replace(result, settings_fingerprint=decision_fingerprint(self.config, *key))


def build_runtime(*, context, config, rules, queue, notify=None, session_gate=None,
                  on_failure=None):
    settings = read_settings(config)
    group_overrides = deepcopy(config.get("group_overrides", {}))
    if settings.backend != "llm":
        raise ValueError("decision backend required")
    def make_decider(backend, model, strategy, reason_language=None):
        if backend == "llm":
            language = config.get("llm_reason_language", "") if reason_language is None else reason_language
            def make_llm_decider(enabled):
                return LlmDecider(rules, AstrBotLlmInvoker(context, reason_language=language,
                                  no_action_reason_enabled=enabled), model=model,
                                  max_duration_seconds=settings.max_duration_seconds, queue=queue, strategy=strategy,
                                  no_action_reason_enabled=enabled)
            decider = ReasonConfiguredDecider(config, make_llm_decider)
            decider.current()
            return decider
        raise ValueError("decision backend required")
    decider = (GroupDecider(config, make_decider) if "group_overrides" in config
               else make_decider(settings.backend, settings.model, settings.rule_selection_strategy))
    execution_default = config.get("default_execution_mode", "execute")
    action_default = config.get("default_action_mode", "auto")
    reason_language = config.get("llm_reason_language", "")
    # If model or any safety setting changes, stop using the old captured
    # configuration until the host reloads and builds a new runtime.
    def settings_match():
        current = read_settings(config)
        return (replace(current, llm_no_action_reason_enabled=settings.llm_no_action_reason_enabled) == settings
                and config.get("group_overrides", {}) == group_overrides
                and config.get("default_execution_mode", "execute") == execution_default
                and config.get("default_action_mode", "auto") == action_default
                and config.get("llm_reason_language", "") == reason_language)
    # A message that reaches the standard plugin listener already passed the
    # framework's whitelist, session and plugin checks, so no extra gate is
    # needed. Only a caller reading events outside that pipeline has to supply
    # its own gate.
    guarded = PolicyGuard(decider, lambda: read_settings(config).policy, settings_match=settings_match,
                          session_gate=session_gate)
    limiter = RateLimiter(global_limit=settings.global_rate_per_minute,
                          group_limit=settings.group_rate_per_minute, window_seconds=60)
    breaker = CircuitBreaker(failure_threshold=settings.failure_threshold,
                             cooldown_seconds=settings.cooldown_seconds)
    admission = DryRunAdmission(queue, rules, policy=settings.policy, capacity=settings.capacity,
                                model=settings.model, rate_limiter=limiter, settings_match=settings_match,
                                notify=notify, session_gate=session_gate)
    worker = DryRunWorker(queue, guarded, concurrency=settings.concurrency,
                          timeout_seconds=settings.timeout_seconds, breaker=breaker, notify=notify)
    return DryRunRuntime(admission, worker, on_failure=on_failure)
