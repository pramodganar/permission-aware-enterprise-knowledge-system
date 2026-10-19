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

from app.api import admin, auth, documents, health, questions
from app.api.components import Components, close_components, load_components
from app.api.errors import install_error_handlers
from app.api.middleware import install_middleware
from app.auth.throttle import LoginThrottle
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging_setup import configure_logging

FORM_OVERHEAD_BYTES = 64 * 1024


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    components: Components | None = None,
) -> FastAPI:
    current = settings or get_settings()
    owns_engine = engine is None
    database = engine or create_db_engine(current.database_url)
    session_factory = create_session_factory(database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if components is None:
            with session_factory() as db:
                app.state.components = load_components(current, db)
        else:
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
    app.dependency_overrides[get_settings] = lambda: current
    install_error_handlers(app)
    # The largest file, and room for the form around it.
    install_middleware(app, current.max_upload_bytes + FORM_OVERHEAD_BYTES)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(questions.router)
    app.include_router(admin.router)
    return app


def serve() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)
