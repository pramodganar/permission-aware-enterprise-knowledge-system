"""The HTTP service.

    uvicorn app.main:serve --factory --no-access-log

``serve`` reads the settings, sets up JSON logging and builds the app.
uvicorn's own access log is switched off because it writes the full URL,
query string included; the service logs each request itself, without it.

``create_app`` is what tests use: anything slow can be passed in ready-made,
and whatever isn't is loaded at startup.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from app.api import admin, audit, auth, documents, health, questions
from app.api.components import Components, close_components, load_components
from app.api.errors import install_error_handlers
from app.api.middleware import install_middleware
from app.audit.seals import seal_key
from app.auth.throttle import LoginThrottle
from app.core.config import Settings, get_settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.core.logging_setup import configure_logging
from app.core.rate_limit import RateLimiter, Window
from app.models import give_public_ids

FORM_OVERHEAD_BYTES = 64 * 1024


# What /docs says first. Everything else it says comes from the routes themselves.
DESCRIPTION = (
    "Answers questions from company documents, using only the documents the person asking "
    "may read. Sign in at `/auth/login` and send the token as `Authorization: Bearer <token>`. "
    'Every error has the same body, `{"error": {"code", "message", "request_id"}}`. '
    "Any route may also answer 405 for the wrong method, 411 or 413 for a body without a "
    "length or over the limit, and 500 with the request ID. The whole description, with the "
    "reasons behind it, is docs/api.md."
)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    components: Components | None = None,
) -> FastAPI:
    current = settings or get_settings()
    owns_engine = engine is None
    database = engine or create_db_engine(current.database_url)
    session_factory = create_session_factory(database, seal_key(current))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Creates tables that are missing, such as the audit trail in a
        # database seeded before it existed. Never changes an existing one.
        Base.metadata.create_all(database)
        with session_factory() as db:
            give_public_ids(db)
            if components is None:
                app.state.components = load_components(current, db)
        if components is not None:
            app.state.components = components
        try:
            yield
        finally:
            close_components(app.state.components)
            if owns_engine:
                database.dispose()

    production = current.environment == "production"
    app = FastAPI(
        title="Permission-Aware Enterprise Knowledge System",
        description=DESCRIPTION,
        lifespan=lifespan,
        # The interactive docs list every route; not something to publish.
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.session_factory = session_factory
    app.state.login_throttle = LoginThrottle(
        current.login_max_failures, current.login_failure_window_seconds
    )
    app.state.ask_limiter = RateLimiter(
        [Window(current.ask_limit_per_minute, 60), Window(current.ask_limit_per_day, 86_400)]
    )
    app.state.search_limiter = RateLimiter([Window(current.search_limit_per_minute, 60)])
    app.dependency_overrides[get_settings] = lambda: current
    install_error_handlers(app)
    # The largest file, and room for the form around it.
    install_middleware(app, current.max_upload_bytes + FORM_OVERHEAD_BYTES)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(questions.router)
    app.include_router(admin.router)
    app.include_router(audit.router)
    return app


def serve() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)
