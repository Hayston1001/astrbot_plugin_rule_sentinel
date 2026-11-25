"""Authenticated dashboard-only access to the local rule and audit stores."""

import re
from dataclasses import asdict

from .rule_management import RuleManagement


class ManagementService:
    def __init__(self, rules, queue, duration_limit=lambda: 2592000, group_index=None):
        self.rules = rules
        self.queue = queue
        self.duration_limit = duration_limit
        self.group_index = group_index
        self.rules_manager = RuleManagement(rules, self._authorized, confirmation_seconds=300,
                                            validate_policy=self._validate_duration_policy,
                                            before_save=self._before_rule_save, after_save=self._after_rule_save)

    def _before_rule_save(self, fields):
        if self.group_index is not None:
            self.group_index.invalidate(fields["platform_id"], fields["group_id"])

    def _after_rule_save(self, fields, success):
        if self.group_index is not None:
            self.group_index.finish_save(fields["platform_id"], fields["group_id"], success)

    def _validate_duration_policy(self, fields):
        limit = self.duration_limit()
        if type(limit) is not int or not 0 < limit <= 2592000:
            raise ValueError("invalid global duration limit")
        actions = fields.get("actions")
        mappings = ([action.get("severity_seconds", {}) for action in actions if action["type"] == "mute"]
                     if isinstance(actions, list) else [fields.get("severity_seconds")])
        if any(not isinstance(mapping, dict) or any(type(seconds) is not int or not 0 < seconds <= limit
                   for seconds in mapping.values()) for mapping in mappings):
            raise ValueError("severity exceeds global duration limit")
        if isinstance(actions, list) and any(
            action["type"] == "mute" and action.get("duration_mode") == "cumulative"
            and any(seconds > limit for seconds in action["tiers_seconds"]) for action in actions
        ):
            raise ValueError("violation tiers exceed global duration limit")

    def _authorized(self, actor, platform_id, group_id):
        # The AstrBot 4.29.3 extension route also accepts scoped API keys.
        # Those identities are not dashboard users and are deliberately denied.
        return (self.actor_allowed(actor)
                and isinstance(platform_id, str) and bool(platform_id.strip())
                and isinstance(group_id, str) and bool(group_id.strip()))

    def actor_allowed(self, actor):
        return isinstance(actor, str) and bool(actor.strip()) and not actor.strip().startswith("api_key:")

    def list_groups(self, actor, configured=()):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        groups = self.queue.list_known_groups()
        found = {(row["platform_id"], row["group_id"]) for row in groups}
        from .group_settings import validate_group_key
        for key in configured:
            validate_group_key(key)
            platform_id, group_id = key.split("/")
            if (platform_id, group_id) not in found:
                groups.append({"platform_id": platform_id, "group_id": group_id, "rule_count": 0,
                               "last_activity": None, "alias": "", "platform_name": ""})
                found.add((platform_id, group_id))
        return groups

    def quick_start_completed(self, actor):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        return self.queue.quick_start_completed()

    def complete_quick_start(self, actor, config):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if self.queue.quick_start_completed():
            return {"completed": True}
        from .plugin_settings import decision_model_unconfigured, read_settings
        from .settings_store import validate_settings
        validate_settings(config)
        if decision_model_unconfigured(config):
            return {"completed": False}
        policy = read_settings(config).policy
        configured = set(config["monitoring_groups"]) | set(config["group_overrides"])
        groups = self.list_groups(actor, configured)
        def allowed(item):
            listed = (item["platform_id"], item["group_id"]) in policy.groups
            return listed == (policy.mode == "allowlist")
        if not any(allowed(item) for item in groups) or not any(item["rule_count"] > 0 for item in groups):
            return {"completed": False}
        self.queue.complete_quick_start()
        return {"completed": True}

    def save_group_name(self, actor, fields, *, platform_name=False):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if not isinstance(fields, dict) or set(fields) != {"platform_id", "group_id", "name"}:
            raise ValueError("invalid group name")
        platform_id, group_id, name = fields["platform_id"], fields["group_id"], fields["name"]
        if any(not isinstance(value, str) or not value.strip() or len(value) > 128
               for value in (platform_id, group_id)):
            raise ValueError("invalid group identity")
        from .group_settings import validate_group_key
        validate_group_key(f"{platform_id}/{group_id}")
        if (not isinstance(name, str) or len(name) > 100
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError("invalid group name")
        return self.queue.save_group_name(platform_id, group_id, name.strip(), platform_name=platform_name)

    def ensure_group(self, actor, fields, platform_name=""):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if not isinstance(fields, dict) or set(fields) != {"platform_id", "group_id"}:
            raise ValueError("invalid group identity")
        platform_id, group_id = fields["platform_id"], fields["group_id"]
        if not isinstance(platform_id, str) or not isinstance(group_id, str):
            raise ValueError("invalid group identity")
        from .group_settings import validate_group_key
        validate_group_key(f"{platform_id}/{group_id}")
        if (not isinstance(platform_name, str) or len(platform_name) > 100
                or any(ord(char) < 32 or ord(char) == 127 for char in platform_name)):
            raise ValueError("invalid group name")
        return self.queue.ensure_group(platform_id, group_id, platform_name.strip())

    def get_ui_state(self, actor):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        return self.queue.get_ui_state(actor)

    def save_ui_state(self, actor, fields):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if not isinstance(fields, dict):
            raise ValueError("invalid UI state")
        if set(fields) == {"active_tab"}:
            tab = fields["active_tab"]
            if not isinstance(tab, str) or tab not in {"status", "records", "rules", "groups", "settings"}:
                raise ValueError("invalid UI state")
            return self.queue.save_active_tab(actor, tab)
        if set(fields) == {"records_platform_id", "records_group_id"}:
            if fields != {"records_platform_id": "", "records_group_id": ""}:
                for value in fields.values():
                    if not isinstance(value, str) or not value.strip() or len(value) > 128:
                        raise ValueError("invalid UI state")
            return self.queue.save_records_filter(actor, fields["records_platform_id"], fields["records_group_id"])
        if set(fields) != {"platform_id", "group_id"}:
            raise ValueError("invalid UI state")
        if fields != {"platform_id": "", "group_id": ""}:
            for value in fields.values():
                if not isinstance(value, str) or not value.strip() or len(value) > 128:
                    raise ValueError("invalid UI state")
        return self.queue.save_ui_state(actor, fields["platform_id"], fields["group_id"])

    @staticmethod
    def _validate_overview_layout(layout):
        if not isinstance(layout, dict) or set(layout) != {"cards"} or not isinstance(layout["cards"], list):
            raise ValueError("invalid overview layout")
        if len(layout["cards"]) > 64:
            raise ValueError("invalid overview layout")
        seen = set()
        for card in layout["cards"]:
            if not isinstance(card, dict) or set(card) != {"id", "width", "visible"}:
                raise ValueError("invalid overview card")
            name = card["id"]
            if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                    or name in seen or card["width"] not in ("half", "full") or type(card["visible"]) is not bool):
                raise ValueError("invalid overview card")
            seen.add(name)
        return layout

    def get_overview_layout(self, actor):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        layout = self.queue.get_overview_layout(actor)
        # AstrBot's web response converts a top-level None into an empty object.
        # Return an explicit empty card list for an account with no saved layout.
        return {"cards": []} if layout is None else self._validate_overview_layout(layout)

    def save_overview_layout(self, actor, fields):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        return self.queue.save_overview_layout(actor, self._validate_overview_layout(fields))

    def pending_reviews(self, actor, platform_id, group_id, before_id=None, limit=20, all_groups=False):
        if all_groups:
            if not self.actor_allowed(actor):
                raise PermissionError("not authorized")
        else:
            self._check(actor, platform_id, group_id)
        return self.queue.pending_reviews(platform_id, group_id, before_id, limit, all_groups)

    def close_review_item(self, actor, fields):
        """Give up one confirmable action, all of one rule's actions, or mark a check done.

        The platform is never called.
        """
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if (not isinstance(fields, dict) or not {"target"} <= set(fields)
                or set(fields) - {"target", "intent_id", "event_id", "platform_id", "group_id", "message_id", "rule_id"}):
            raise ValueError("invalid review item")
        if fields["target"] == "rule_actions":
            if set(fields) != {"target", "platform_id", "group_id", "message_id", "rule_id"}:
                raise ValueError("invalid review item")
            platform_id, group_id, message_id, rule_id = (fields[key] for key in
                                                          ("platform_id", "group_id", "message_id", "rule_id"))
            self._check(actor, platform_id, group_id)
            return self.queue.close_rule_actions(platform_id=platform_id, group_id=group_id,
                                                 message_id=message_id, rule_id=rule_id, actor=actor)
        expected = {"target", "event_id"} if fields["target"] == "event_check" else {"target", "intent_id"}
        if set(fields) != expected:
            raise ValueError("invalid review item")
        return self.queue.close_review_item(target=fields["target"], actor=actor,
                                            intent_id=fields.get("intent_id"), event_id=fields.get("event_id"))

    def rule_confirmation_scope(self, actor, fields):
        """Validate the message and rule scope of a rule-level confirmation."""
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if (not isinstance(fields, dict) or set(fields) != {"platform_id", "group_id", "message_id", "rule_id"}
                or any(not isinstance(value, str) or not value.strip() or len(value) > 512
                       for value in fields.values())):
            raise ValueError("invalid execution request")
        self._check(actor, fields["platform_id"], fields["group_id"])
        return fields

    def confirmation_state(self, actor, intent_id):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        return self.queue.confirmation_state(intent_id)

    def _check(self, actor, platform_id, group_id):
        if self._authorized(actor, platform_id, group_id) is not True:
            raise PermissionError("not authorized")

    def preview(self, actor, fields):
        return self.rules_manager.preview(actor, fields)

    def confirm(self, actor, preview):
        return asdict(self.rules_manager.confirm(actor, preview))

    def list_rules(self, actor, platform_id, group_id):
        self._check(actor, platform_id, group_id)
        return [asdict(rule) for rule in self.rules.current_for_group(platform_id, group_id)]

    def rule_history(self, actor, platform_id, group_id, rule_id):
        self._check(actor, platform_id, group_id)
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("rule id is required")
        return [asdict(rule) for rule in self.rules.history(platform_id, group_id, rule_id)]

    def list_audit(self, actor, platform_id, group_id, after_id=0, limit=50):
        self._check(actor, platform_id, group_id)
        return self.queue.list_events(platform_id=platform_id, group_id=group_id,
                                      after_id=after_id, limit=limit)

    def list_messages(self, actor, platform_id, group_id, before_id=None, limit=50, all_groups=False):
        if all_groups:
            if not self.actor_allowed(actor):
                raise PermissionError("not authorized")
        else:
            self._check(actor, platform_id, group_id)
        return self.queue.list_messages(platform_id=platform_id, group_id=group_id,
                                        before_id=before_id, limit=limit, all_groups=all_groups)

    def message_content(self, actor, platform_id, group_id, message_id):
        self._check(actor, platform_id, group_id)
        return {"content": self.queue.message_content(platform_id=platform_id, group_id=group_id, message_id=message_id),
                 "images": self.queue.message_images(platform_id=platform_id, group_id=group_id, message_id=message_id)}

    def event_detail(self, actor, platform_id, group_id, message_id, event_id):
        self._check(actor, platform_id, group_id)
        row = self.queue.event_detail(platform_id=platform_id, group_id=group_id,
                                      message_id=message_id, event_id=event_id)
        return {"available": True, **row} if row else {"available": False}

    def revoke_count(self, actor, fields):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if (not isinstance(fields, dict) or not {"platform_id", "group_id", "message_id"} <= set(fields)
                or set(fields) - {"platform_id", "group_id", "message_id", "count_id"}):
            raise ValueError("invalid violation identity")
        platform_id, group_id, message_id = (fields[key] for key in ("platform_id", "group_id", "message_id"))
        count_id = fields.get("count_id")
        if count_id is not None and (type(count_id) is not int or count_id <= 0):
            raise ValueError("invalid violation identity")
        self._check(actor, platform_id, group_id)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512
               for value in (platform_id, group_id, message_id)):
            raise ValueError("invalid violation identity")
        return self.queue.revoke_violation_count(platform_id, group_id, message_id, actor, count_id)

    def summarize_group(self, actor, platform_id, group_id, all_groups=False):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        if not all_groups and any(not isinstance(value, str) or not value.strip() or len(value) > 128
               for value in (platform_id, group_id)):
            raise ValueError("invalid group identity")
        return self.queue.summarize_group(platform_id=platform_id, group_id=group_id, all_groups=all_groups)

    def list_notifications(self, actor, after_id=0, limit=50):
        if not self.actor_allowed(actor):
            raise PermissionError("not authorized")
        from .notifications import NOTICE_SPECS
        rows = self.queue.list_notifications(after_id=after_id, limit=limit)
        for row in rows:
            spec = NOTICE_SPECS.get(row["code"])
            # The label lets the page name the failure without exposing internal codes.
            row["label"] = spec.label if spec is not None else "测试通知"
        return rows
