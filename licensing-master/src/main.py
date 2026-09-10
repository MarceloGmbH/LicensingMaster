"""licensing-master FastAPI app (ADR-0013)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.envelope import ErrorDetail, Envelope, fail
from src.errors import AppError
from src.routers import router

logging.basicConfig(
    level=logging.DEBUG if settings.APP_DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="licensing-master",
    description="Central multi-product licensing control plane (ADR-0013)",
    version=settings.VERSION,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    body: Envelope = fail(exc.to_error_detail())
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        ErrorDetail(
            code="SCHEMA_VALIDATION",
            message=e.get("msg", "Invalid value"),
            field=".".join(str(p) for p in e.get("loc", []) if p != "body"),
        )
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content=fail(details).model_dump(mode="json"))


app.include_router(router)


# The built portal (apps/licensing-portal -> dist) is copied to /app/portal by the
# Docker image. When present it is served at "/" on the same origin as /admin/*,
# so Cloudflare Access protects the UI and its API with one policy. When absent
# (bare `uvicorn` in dev — use the Vite dev server on :5190 instead) "/" just
# reports the service banner.
_PORTAL_DIR = Path(__file__).resolve().parent.parent / "portal"

if _PORTAL_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_PORTAL_DIR), html=True), name="portal")
else:

    @app.get("/")
    def root() -> dict:
        return {"service": settings.APP_NAME, "version": settings.VERSION, "env": settings.APP_ENV}
