class ServiceError(RuntimeError):
    """A user-facing domain error with a stable HTTP mapping."""

    def __init__(self, message: str, *, code: str = "invalid_request", status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
