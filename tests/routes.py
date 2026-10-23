"""The routes an app serves, read off the app, for the tests that must cover every one."""

from typing import Any

from fastapi import FastAPI

# FastAPI's own pages, switched off in production (tested in test_api_app.py).
INTERACTIVE_DOCS = {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect")}


def routes_of(app: FastAPI) -> dict[tuple[str, str], Any]:
    """Every route the app serves, by method and path pattern.

    Included routers are kept whole in this FastAPI version, so their routes
    are read from the router they came from.
    """
    found: dict[tuple[str, str], Any] = {}

    def walk(routes: list[Any]) -> None:
        for route in routes:
            if hasattr(route, "original_router"):
                walk(route.original_router.routes)
                continue
            for method in getattr(route, "methods", None) or ():
                if method != "HEAD":
                    found[(method, route.path)] = route

    walk(list(app.routes))
    return found
