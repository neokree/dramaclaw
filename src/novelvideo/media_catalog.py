"""The media model catalog, built from the engines that are actually installed.

Same entry contract the freezone canvas and the model pickers already read
(`catalogId`, `apiModel`, `supportedModes`, `ratioOptions`, ...), but the
rows come from Higgsfield's own model schemas, Draw Things and h3.c instead of
a gateway's static list.
"""

from __future__ import annotations

import logging
from typing import Any

from novelvideo.engines import drawthings, h3c, higgsfield

logger = logging.getLogger(__name__)

DRAWTHINGS_SELECTION = "drawthings"
DRAWTHINGS_RATIOS = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9"]
H3C_RATIOS = ["9:16", "16:9", "1:1", "4:3", "3:4", "4:5"]


def _entry(
    *,
    catalog_id: str,
    provider: str,
    label: str,
    media_type: str,
    sort_order: int,
    **config: Any,
) -> dict[str, Any]:
    return {
        "request": {
            "endpoint": "images/generations" if media_type == "image" else "video/generations",
            "parameters": [],
        },
        **{key: value for key, value in config.items() if value not in (None, [])},
        "catalogId": catalog_id,
        "catalog_id": catalog_id,
        "id": catalog_id,
        "providerId": provider,
        "provider": provider,
        "apiModel": catalog_id,
        "api_model": catalog_id,
        "gatewayModel": catalog_id,
        "gateway_model": catalog_id,
        "aliases": [],
        "label": label,
        "sortOrder": sort_order,
    }


def _video_modes(model: dict[str, Any]) -> list[str]:
    modes = ["text_to_video"]
    if model["start_image"]:
        modes += ["first_frame", "image_to_video"]
    if model["end_image"]:
        modes.append("first_last_frame")
    if model["image_references"]:
        modes += ["image_reference", "all_reference"]
    elif model["video_references"]:
        modes.append("all_reference")
    if "video_edit" in model["modes"]:
        modes.append("video_edit")
    if "video_extension" in model["modes"]:
        modes.append("video_extend")
    return modes


def _higgsfield_video_entries() -> list[dict[str, Any]]:
    from novelvideo.generators.video_generator import higgsfield_backend
    from novelvideo.video_duration import video_duration_bounds_for_backend

    entries = []
    for index, model in enumerate(higgsfield.catalog("video")):
        backend = higgsfield_backend(model["ref"])
        low, high = video_duration_bounds_for_backend(backend)
        entries.append(
            _entry(
                catalog_id=backend,
                provider="higgsfield",
                label=model["label"],
                media_type="video",
                sort_order=index,
                minDuration=low,
                maxDuration=high,
                durationOptions=model["durations"],
                ratioOptions=model["aspect_ratios"],
                resolutionOptions=model["resolutions"],
                supportedModes=_video_modes(model),
                referenceImageMax=(model["max_images"] or 9) if model["image_references"] else 0,
                referenceVideoMax=3 if model["video_references"] else 0,
                referenceAudioMax=3 if model["audio_references"] else 0,
                supportsAudio=model["audio"],
            )
        )
    return entries


def _higgsfield_image_entries() -> list[dict[str, Any]]:
    entries = []
    for index, model in enumerate(higgsfield.catalog("image")):
        selection = f"higgsfield:{model['ref']}"
        sch = higgsfield.schema(model["job_type"])
        quality = (higgsfield.param_specs(sch).get("quality") or {}).get("enum") or []
        entries.append(
            _entry(
                catalog_id=selection,
                provider="higgsfield",
                label=model["label"],
                media_type="image",
                sort_order=100 + index,
                ratioOptions=model["aspect_ratios"],
                resolutionOptions=list(
                    (higgsfield.param_specs(sch).get("resolution") or {}).get("enum") or []
                ),
                qualityOptions=quality,
                referenceImageMax=(model["max_images"] or 14) if model["image_references"] else 0,
            )
        )
    return entries


def media_model_catalog(media_type: str) -> list[dict[str, Any]]:
    """Installed models of `media_type` ("image" or "video"), best first."""
    entries: list[dict[str, Any]] = []
    if media_type == "video":
        try:
            entries += _higgsfield_video_entries()
        except Exception as exc:  # noqa: BLE001 - an engine offline must not hide the others
            logger.warning("Higgsfield video catalog unavailable: %s", exc)
        if h3c.available()["available"]:
            entries.append(
                _entry(
                    catalog_id="h3c",
                    provider="h3c",
                    label="h3.c · MiniMax H3 (locale)",
                    media_type="video",
                    sort_order=1000,
                    minDuration=1,
                    maxDuration=15,
                    ratioOptions=H3C_RATIOS,
                    resolutionOptions=["720p"],
                    supportedModes=["text_to_video", "first_frame", "image_to_video", "first_last_frame"],
                    referenceImageMax=0,
                    referenceVideoMax=0,
                    referenceAudioMax=0,
                    supportsAudio=True,
                )
            )
    elif media_type == "image":
        entries.append(
            _entry(
                catalog_id=DRAWTHINGS_SELECTION,
                provider="drawthings",
                label="Draw Things (locale)",
                media_type="image",
                sort_order=0,
                ratioOptions=DRAWTHINGS_RATIOS,
                referenceImageMax=1,
            )
        )
        try:
            entries += _higgsfield_image_entries()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Higgsfield image catalog unavailable: %s", exc)
    return entries


async def engines_status() -> dict[str, dict[str, Any]]:
    """Reachability of every engine, for the settings screen."""
    from novelvideo.engines import mtplx

    return {
        "higgsfield": await higgsfield.status(),
        "h3c": h3c.available(),
        "drawthings": await drawthings.status(),
        "mtplx": mtplx.status(),
    }
