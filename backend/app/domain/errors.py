from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LauncherError(Exception):
    code: str
    message: str
    status_code: int = 400
    retryable: bool = False
    launch_id: str | None = None
    request_id: str | None = None
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        return self.message


class NotFoundError(LauncherError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=404)


class ConflictError(LauncherError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=409)


class ServiceUnavailableError(LauncherError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(
            code=code,
            message=message,
            status_code=503,
            retryable=retryable,
        )
