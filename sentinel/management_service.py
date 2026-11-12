"""Authenticated dashboard-only access to the local rule and audit stores."""

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
        mappings = ([action["severity_seconds"] for action in actions if action["type"] == "mute"]
                    if isinstance(actions, list) else [fields.get("severity_seconds")])
        if any(not isinstance(mapping, dict) or any(type(seconds) is not int or not 0 < seconds <= limit
                   for seconds in mapping.values()) for mapping in mappings):
            raise ValueError("severity exceeds global duration limit")

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
        if not isinstance(fields, dict) or set(fields) != {"platform_id", "group_id"}:
            raise ValueError("invalid UI state")
        if fields != {"platform_id": "", "group_id": ""}:
            for value in fields.values():
                if not isinstance(value, str) or not value.strip() or len(value) > 128:
                    raise ValueError("invalid UI state")
        return self.queue.save_ui_state(actor, fields["platform_id"], fields["group_id"])

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
        return {"content": self.queue.message_content(platform_id=platform_id, group_id=group_id, message_id=message_id)}

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
        return self.queue.list_notifications(after_id=after_id, limit=limit)
