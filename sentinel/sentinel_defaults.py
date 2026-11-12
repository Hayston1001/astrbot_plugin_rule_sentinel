"""Default policy values; these do not start monitoring or select a model."""

from .monitoring_policy import MonitoringPolicy


DEFAULT_QUEUE_CAPACITY_PER_GROUP = 100
DEFAULT_MONITORING_POLICY = MonitoringPolicy(
    mode="allowlist",
    groups=frozenset(),
)
