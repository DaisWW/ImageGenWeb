class ServiceError(RuntimeError):
    """面向用户的领域错误，并映射到稳定的 HTTP 响应。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_request",
        status_code: int = 400,
        error_id: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.error_id = error_id
