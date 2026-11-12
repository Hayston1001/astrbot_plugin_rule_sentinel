"""Safe error categories for decision providers."""


class ContextLimitExceeded(Exception):
    """The provider explicitly reported an input context limit."""


class AuditUnavailable(Exception):
    """A decision could not be started because its audit could not be saved."""


class ProviderIntegrityUnverified(Exception):
    """The selected provider has not proven that it preserves all inputs."""


class AuditSnapshotTooLarge(Exception):
    """The complete rule snapshot cannot fit within the audit record limit."""
