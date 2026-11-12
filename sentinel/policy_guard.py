"""Recheck group monitoring policy immediately before dry-run judgment."""

import json
from types import SimpleNamespace

from .dry_run_worker import Decision


class PolicyGuard:
    def __init__(self, decide, current_policy, *, settings_match=None, session_gate=None):
        self.decide = decide
        self.current_policy = current_policy
        self.settings_match = settings_match
        self.session_gate = session_gate

    async def _denial_reason(self, item):
        policy = self.current_policy()
        key = (item.platform_id, item.group_id)
        if self.settings_match is not None and not self.settings_match():
            return "settings_changed_reload_required"
        if policy.mode == "allowlist" and key not in policy.groups:
            return "group_not_allowed"
        if policy.mode == "blocklist" and key in policy.groups:
            return "group_blocked"
        if policy.mode not in ("allowlist", "blocklist"):
            return "invalid_list_mode"
        if self.session_gate is not None:
            session = SimpleNamespace(session_origin=item.payload.get("session_origin"), group_id=item.group_id,
                                      platform_id=item.platform_id)
            if await self.session_gate(session) is not True:
                return "framework_session_denied"
        return None

    async def __call__(self, item):
        denial = await self._denial_reason(item)
        if denial is not None:
            return Decision("skipped", denial, model_attempted=False)
        result = await self.decide(item)
        if not isinstance(result, Decision) or result.status not in ("proposed", "no_action"):
            return result
        try:
            denial = await self._denial_reason(item)
        except Exception:
            denial = "policy_recheck_failed"
        if denial is None:
            return result
        try:
            prior = json.loads(result.detail)
            if not isinstance(prior, dict):
                prior = {}
        except (TypeError, ValueError):
            prior = {}
        return Decision("review", json.dumps({"review_reason": denial, "original_status": result.status,
                                              "decision": prior}, ensure_ascii=False),
                        model_attempted=result.model_attempted)
