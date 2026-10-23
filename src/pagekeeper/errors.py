"""Error types shared by the storage layer, the engine and the CLI."""


class PagekeeperError(Exception):
    """Base class for everything pagekeeper reports to a human."""


class NotFound(PagekeeperError):
    """A referenced user, schedule, policy or alert does not exist."""


class UsageError(PagekeeperError):
    """The operator asked for something pagekeeper cannot do; exit code 1."""
