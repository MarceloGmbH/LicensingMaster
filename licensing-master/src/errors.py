"""Typed application errors + HTTP status mapping (ADR-0013)."""

from __future__ import annotations

from typing import Any

from src.envelope import ErrorDetail


class AppError(Exception):
    status_code: int = 400
    code: str = "APP_ERROR"
    message: str = "Application error"

    def __init__(
        self,
        message: str | None = None,
        field: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if message:
            self.message = message
        self.field = field
        self.context = context
        super().__init__(self.message)

    def to_error_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code, message=self.message, field=self.field, context=self.context
        )


class ValidationError(AppError):
    status_code = 400
    code = "VALIDATION_ERROR"
    message = "Validation failed"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "Resource not found"


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "Authentication required"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    message = "Insufficient permissions"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "Resource conflict"


class SeatLimitReachedError(ConflictError):
    code = "SEAT_LIMIT_REACHED"
    message = "The subscription seat limit has been reached"


class ActivationTokenInvalidError(ForbiddenError):
    code = "ACTIVATION_TOKEN_INVALID"
    message = "The activation token is invalid, revoked or exhausted"
