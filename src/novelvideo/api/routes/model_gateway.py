"""Engine status endpoint for the settings UI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from novelvideo import config as app_config

router = APIRouter(prefix="/model-gateway")


@router.get("/engines")
async def get_engines_status() -> dict[str, Any]:
    """Read-only reachability of the local/remote engines, for the settings UI."""
    from novelvideo.media_catalog import engines_status

    return {
        "ok": True,
        "data": {
            "textEngine": app_config.get_text_engine(),
            "engines": await engines_status(),
        },
    }
