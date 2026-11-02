"""FastAPI application: API routes, middleware, lifespan (deps, store, checkpointer) and the built frontend.

Run with ``uv run uvicorn app.main:app`` from ``backend/``. Tests call ``create_app(settings, deps)``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import FileResponse, Response

from app.api.routes import router
from app.api.security import BodySizeLimitMiddleware, ErrorShieldMiddleware, SecurityHeadersMiddleware
from app.api.sse import SafeJSONResponse
from app.checkpoint import open_checkpointer
from app.config import Settings, get_settings
from app.deps import Deps, build_deps
from app.observability import setup_observability
from app.store.factory import create_store

log = logging.getLogger("app")

SHUTDOWN_GRACE_S = 10


def validation_messages(exc: RequestValidationError) -> list[str]:
    """Field-level messages without echoing the submitted values."""
    out: list[str] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        if loc and loc[0] in {"body", "path", "query", "header"}:
            loc = loc[1:]
        msg = str(err.get("msg", "Invalid value"))
        out.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return out[:20]


def resolve_frontend_dist(value: str) -> Path | None:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    return path if (path / "index.html").is_file() else None


async def _drain(tasks: set[asyncio.Task[Any]], timeout: float) -> None:
    """Let in-flight planner runs finish (and persist) before the store closes."""
    pending = [t for t in tasks if not t.done()]
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=timeout)
    for task in still:
        task.cancel()
    if still:
        await asyncio.gather(*still, return_exceptions=True)


def create_app(settings: Settings | None = None, deps: Deps | None = None) -> FastAPI:
    settings = settings or (deps.settings if deps is not None else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_observability(settings)
        async with AsyncExitStack() as stack:
            owned = deps is None
            d = build_deps(settings) if deps is None else deps
            if owned and d.http is not None:
                stack.push_async_callback(d.http.aclose)
            created_store = d.store is None
            if created_store:
                store = create_store(settings.database_url)
                await store.init()
                stack.push_async_callback(store.close)
                d.store = store
            created_checkpointer = d.checkpointer is None
            if created_checkpointer:
                d.checkpointer = await stack.enter_async_context(open_checkpointer(settings.database_url))
            app.state.deps = d
            log.info("startup", extra={"env": settings.app_env, "version": settings.git_sha,
                                       "places_provider": settings.resolved_places_provider(),
                                       "events_provider": settings.resolved_events_provider()})
            try:
                yield
            finally:
                await _drain(app.state.background_tasks, timeout=settings.run_deadline_s + SHUTDOWN_GRACE_S)
                app.state.deps = None
                if not owned:  # hand injected deps back as we found them
                    if created_store:
                        stack.callback(setattr, d, "store", None)
                    if created_checkpointer:
                        stack.callback(setattr, d, "checkpointer", None)

    app = FastAPI(
        title="Perfect Saturday Planner",
        version=settings.git_sha,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/api/openapi.json",
        default_response_class=SafeJSONResponse,
    )
    app.state.settings = settings
    app.state.deps = None
    app.state.background_tasks = set()

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        return SafeJSONResponse({"status": "rejected", "detail": validation_messages(exc)}, status_code=400)

    app.include_router(router)
    _mount_frontend(app, settings)

    # Last added is outermost: SecurityHeaders -> ErrorShield -> BodySizeLimit -> routes.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(ErrorShieldMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    dist = resolve_frontend_dist(settings.frontend_dist)
    if dist is None:

        @app.get("/", include_in_schema=False)
        async def root() -> Response:
            return SafeJSONResponse({
                "service": "saturday-planner",
                "detail": "Frontend not built. Run `npm run build` in frontend/ (or set FRONTEND_DIST). "
                          "The API is under /api (try /api/health).",
            })

        return

    index = dist / "index.html"

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            return SafeJSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path:
            target = (dist / full_path).resolve()
            if target.is_relative_to(dist) and target.is_file():
                cache = ("public, max-age=31536000, immutable" if full_path.startswith("assets/")
                         else "public, max-age=3600")
                return FileResponse(target, headers={"Cache-Control": cache})
            if full_path.startswith("assets/"):
                return SafeJSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()
