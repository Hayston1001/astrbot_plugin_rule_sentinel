"""Detect rule-set changes after an isolated dry-run decision."""

from .async_io import run_sync_safely


async def snapshot_is_current(store, item, original_rules) -> bool:
    current = await run_sync_safely(store.current_for_group, item.platform_id, item.group_id)
    original_versions = {rule.rule_id: rule.version for rule in original_rules if rule.enabled}
    current_versions = {rule.rule_id: rule.version for rule in current if rule.enabled}
    return original_versions == current_versions
