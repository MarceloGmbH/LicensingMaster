"""Response envelope — `{meta, data, errors}` (mirrors apps/backend, ADR-0013)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Meta(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(default_factory=_now_iso)


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    context: dict[str, Any] | None = None


class Envelope(BaseModel):
    meta: Meta = Field(default_factory=Meta)
    data: Any | None = None
    errors: list[ErrorDetail] = Field(default_factory=list)


def ok(data: Any = None) -> Envelope:
    return Envelope(data=data, errors=[])


def fail(errors: list[ErrorDetail] | ErrorDetail) -> Envelope:
    if isinstance(errors, ErrorDetail):
        errors = [errors]
    return Envelope(data=None, errors=errors)
