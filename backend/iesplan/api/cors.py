"""CORS allowlist for the HTTP transport."""

import os


_DEFAULT_DEV_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)


def cors_origin_list() -> list[str]:
    raw = os.environ.get("IESPLAN_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()] or list(_DEFAULT_DEV_ORIGINS)
