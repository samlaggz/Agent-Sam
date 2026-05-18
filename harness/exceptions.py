class HarnessError(Exception):
    pass


class PolicyViolationError(HarnessError):
    pass


class ApprovalRequiredError(HarnessError):
    pass


class WorkspaceBoundaryError(HarnessError):
    pass


class FilePatchError(HarnessError):
    pass


class SourceCacheError(HarnessError):
    pass


class GitHubAutomationError(HarnessError):
    pass