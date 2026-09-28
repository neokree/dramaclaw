"""Canonical video-duration normalization for quotes and provider requests."""

from __future__ import annotations

import math


def video_duration_bounds_for_backend(
    backend: str | None,
) -> tuple[int | None, int | None]:
    """Seconds the selected video engine accepts."""
    from novelvideo.engines import higgsfield
    from novelvideo.engines._proc import EngineError
    from novelvideo.generators.video_generator import video_engine

    engine, job_type = video_engine(backend)
    if engine == "h3c":
        return (1, 15)  # frames are rounded up to 5 + 17n at 24 fps
    if engine != "higgsfield" or not job_type:
        return (None, None)
    try:
        spec = higgsfield.param_specs(higgsfield.schema(job_type)).get("duration") or {}
    except (EngineError, OSError, ValueError):
        spec = {}
    enum = sorted(int(v) for v in spec.get("enum") or [])
    if enum:
        return (enum[0], enum[-1])
    # ponytail: bounds of integer durations are not in the schema; Seedance's are
    # measured, and `generate` moves any other model's duration inside its bounds.
    return (higgsfield.MIN_SECONDS, higgsfield.MAX_SECONDS)


def normalize_video_duration_for_backend(
    backend: str | None,
    value: int | float | str | None,
    configured_min_duration: int | None = None,
    configured_max_duration: int | None = None,
) -> int:
    """Resolve the exact integer seconds used for billing and generation."""
    try:
        duration = max(int(math.ceil(float(value if value is not None else 5))), 1)
    except (TypeError, ValueError):
        duration = 5

    min_duration, max_duration = video_duration_bounds_for_backend(backend)
    if configured_min_duration is not None:
        min_duration = configured_min_duration
    if configured_max_duration is not None:
        max_duration = configured_max_duration
    if min_duration is not None:
        duration = max(duration, min_duration)
    if max_duration is not None:
        duration = min(duration, max_duration)
    return duration
