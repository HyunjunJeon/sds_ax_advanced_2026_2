"""Failures are distinct from an empty retrieval result."""


class Day02Error(RuntimeError):
    pass


class ConfigurationError(Day02Error):
    pass


class ServiceError(Day02Error):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BudgetExceeded(Day02Error):
    pass


class ValidationFailure(Day02Error):
    pass
