"""Authorized, preview-then-confirm rule management service.

Authorization must be supplied by the host from authenticated server-side
identity, never from model output or a client-supplied administrator flag.
No HTTP or chat endpoint is exposed by this module.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time


class RuleManagement:
    def __init__(self, store, authorize, *, confirmation_seconds: int, clock=time.time, validate_policy=None,
                 before_save=None, after_save=None):
        if type(confirmation_seconds) is not int or confirmation_seconds <= 0:
            raise ValueError("invalid confirmation lifetime")
        self.store = store
        self.authorize = authorize
        self.lifetime = confirmation_seconds
        self.clock = clock
        self.validate_policy = validate_policy
        self.before_save = before_save
        self.after_save = after_save
        self.secret = secrets.token_bytes(32)

    def _authorize(self, actor, fields):
        if not isinstance(actor, str) or not actor.strip():
            raise PermissionError("authenticated identity required")
        if self.authorize(actor, fields.get("platform_id"), fields.get("group_id")) is not True:
            raise PermissionError("rule management not authorized")

    @staticmethod
    def _encoded(actor, fields, expires, default_action_mode=None):
        return json.dumps({"actor": actor, "fields": fields, "expires": expires, "default_action_mode": default_action_mode},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

    @staticmethod
    def _inherits_action_mode(fields):
        actions = fields.get("actions")
        return isinstance(actions, list) and any(isinstance(item, dict) and item.get("mode") == "inherit" for item in actions)

    def preview(self, actor: str, fields: dict) -> dict:
        if not isinstance(fields, dict):
            raise ValueError("invalid rule fields")
        self._authorize(actor, fields)
        if "modified_by" in fields:
            raise ValueError("modifier is derived from authenticated identity")
        fields = {**fields, "priority": fields.get("priority", 0)}
        self.store.validate_fields(**fields, modified_by=actor)
        if self.validate_policy is not None:
            self.validate_policy(fields)
        # JSON copying freezes the preview, rather than retaining mutable input.
        frozen = json.loads(json.dumps(fields, ensure_ascii=False, allow_nan=False))
        expires = int(self.clock()) + self.lifetime
        default_mode = self.store.current_action_mode() if self._inherits_action_mode(frozen) else None
        signature = hmac.new(self.secret, self._encoded(actor, frozen, expires, default_mode), hashlib.sha256).hexdigest()
        result = {"fields": frozen, "expires": expires, "signature": signature}
        if default_mode is not None:
            result["default_action_mode"] = default_mode
        return result

    def confirm(self, actor: str, preview: dict):
        if not isinstance(preview, dict) or set(preview) not in ({"fields", "expires", "signature"},
                                                                {"fields", "expires", "signature", "default_action_mode"}):
            raise ValueError("invalid confirmation")
        fields, expires, signature = preview["fields"], preview["expires"], preview["signature"]
        if not isinstance(fields, dict) or type(expires) is not int or not isinstance(signature, str):
            raise ValueError("invalid confirmation")
        self._authorize(actor, fields)
        default_mode = preview.get("default_action_mode")
        if self._inherits_action_mode(fields) != (default_mode in ("auto", "confirm")):
            raise ValueError("invalid global action mode")
        expected = hmac.new(self.secret, self._encoded(actor, fields, expires, default_mode), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or self.clock() >= expires:
            raise ValueError("confirmation expired or changed")
        if "modified_by" in fields:
            raise ValueError("modifier is derived from authenticated identity")
        if self.validate_policy is not None:
            self.validate_policy(fields)
        if self.before_save is not None:
            self.before_save(fields)
        success = False
        try:
            result = self.store.save(**fields, modified_by=actor, expected_action_mode=default_mode)
            success = True
            return result
        finally:
            if self.after_save is not None:
                try:
                    self.after_save(fields, success)
                except Exception:
                    # A failed cache refresh must not re-enable the group.
                    logging.getLogger(__name__).warning("Rule index refresh failed; capture remains disabled")
