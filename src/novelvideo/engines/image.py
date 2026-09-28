"""The one door every image request goes through: Draw Things or Higgsfield.

A selection is `"drawthings"` (local, A1111 API) or `"higgsfield:<model_ref>"`
(cloud, schema-shaped). Callers hand over a prompt, reference images and the
geometry they already computed; they get back `(image_bytes|None, "", error)`,
the tuple every retired provider call used to return.

Each request lands in the project's image ledger (`image_request_usage`) with
the credits Higgsfield quoted before the job was paid; Draw Things costs 0.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable

from novelvideo.engines import drawthings, higgsfield
from novelvideo.engines._proc import EngineError

logger = logging.getLogger(__name__)

DRAWTHINGS = "drawthings"
HIGGSFIELD_PREFIX = "higgsfield:"

# path, raw bytes, (bytes, mime|path hint) or (name, bytes, mime)
ImageRef = Any


def provider_of(selection: str) -> str:
    return DRAWTHINGS if selection == DRAWTHINGS else "higgsfield"


def model_of(selection: str) -> str:
    """The model ref a selection points at (`drawthings` for Draw Things)."""
    return selection.removeprefix(HIGGSFIELD_PREFIX) if selection != DRAWTHINGS else DRAWTHINGS


def is_selection(value: str | None) -> bool:
    value = str(value or "").strip()
    return value == DRAWTHINGS or (
        value.startswith(HIGGSFIELD_PREFIX) and bool(value[len(HIGGSFIELD_PREFIX):].strip())
    )


def _ext(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _ref_path(ref: ImageRef, tmp: Path, index: int) -> str:
    if isinstance(ref, (str, Path)):
        return str(ref)
    if isinstance(ref, tuple):
        data = ref[1] if len(ref) == 3 else ref[0]
    else:
        data = ref
    data = bytes(data)
    path = tmp / f"ref_{index}{_ext(data)}"
    path.write_bytes(data)
    return str(path)


def _ratio(value: str) -> float | None:
    try:
        a, b = (float(x) for x in str(value).replace("-", ":").split(":"))
        return a / b if a > 0 and b > 0 else None
    except ValueError:
        return None


def closest_ratio(wanted: str | None, allowed: list[str]) -> str | None:
    """`wanted` if the model takes it, else the allowed ratio nearest to it."""
    if not wanted or not allowed:
        return None
    if wanted in allowed:
        return wanted
    target = _ratio(wanted)
    usable = [(r, _ratio(r)) for r in allowed if _ratio(r)]
    if target is None or not usable:
        return "auto" if "auto" in allowed else None
    return min(usable, key=lambda item: abs(item[1] - target))[0]


def _higgsfield_shape(
    model_ref: str, aspect_ratio: str | None, image_size: str | None, quality: str | None
) -> tuple[str | None, str | None, dict[str, Any]]:
    """-> (aspect ratio, resolution, extra) the model's schema accepts."""
    job_type, _ = higgsfield.parse_model_ref(model_ref)
    specs = higgsfield.param_specs(higgsfield.schema(job_type))
    ratio = closest_ratio(aspect_ratio, (specs.get("aspect_ratio") or {}).get("enum") or [])
    size = {"0.5k": "1k", "512": "1k"}.get(str(image_size or "").lower(), str(image_size or "").lower())
    resolution = next(
        (
            v
            for v in (specs.get("resolution") or {}).get("enum") or []
            if str(v).lower() == size
        ),
        None,
    )
    extra: dict[str, Any] = {}
    if quality and quality in ((specs.get("quality") or {}).get("enum") or []):
        extra["quality"] = quality
    return ratio, resolution, extra


def _ledger(project_output_dir, action: str, **fields) -> None:
    """Write the image ledger; a failed write is logged, never raised."""
    if not project_output_dir:
        return
    from novelvideo import image_request_usage as usage

    try:
        if action == "record":
            usage.record_image_request(project_output_dir=project_output_dir, **fields)
        else:
            usage.update_image_request_status(project_output_dir=project_output_dir, **fields)
    except Exception:  # noqa: BLE001
        logger.warning("could not record image usage", exc_info=True)


async def generate_image(
    selection: str,
    prompt: str,
    *,
    refs: Iterable[ImageRef] = (),
    aspect_ratio: str | None = "1:1",
    image_size: str | None = None,
    quality: str | None = None,
    width: int | None = None,
    height: int | None = None,
    output_path: str | Path | None = None,
    usage: dict[str, Any] | None = None,
) -> tuple[bytes | None, str, str]:
    """Generate one image. Never raises for engine failures: `(None, "", why)`.

    `refs`: first one is the img2img init image on Draw Things; every one is an
    `image_references` entry on Higgsfield (the CLI uploads local paths).
    `output_path`: where the engine writes (stable paths let a paid Higgsfield
    job be reattached after a crash); a temp file otherwise.
    `usage`: ledger fields (`task_type`, `scope`, `episode`, `beat_num`,
    `character_name`, `identity_name`, `project_output_dir`); the project is
    inferred from `output_path` when not given.
    """
    from novelvideo.image_request_usage import (
        infer_episode_from_path,
        infer_project_output_dir,
    )

    selection = str(selection or "").strip()
    if not is_selection(selection):
        return None, "", f"Selezione immagine non valida: {selection!r}"
    usage = dict(usage or {})
    project_dir = usage.pop("project_output_dir", None) or infer_project_output_dir(output_path)
    fields = {
        "provider": provider_of(selection),
        "model_name": model_of(selection),
        "task_type": usage.pop("task_type", "image"),
        "scope": usage.pop("scope", Path(output_path).stem if output_path else "image"),
        "episode": usage.pop("episode", infer_episode_from_path(output_path)),
        **usage,
    }
    request_ids: list[str] = []

    def accepted(request_id: str, credits: float) -> None:
        request_ids.append(request_id)
        _ledger(project_dir, "record", request_id=request_id, cost_estimate=credits, **fields)

    with tempfile.TemporaryDirectory(prefix="dramaclaw-image-") as tmp_name:
        tmp = Path(tmp_name)
        ref_paths = [_ref_path(ref, tmp, i) for i, ref in enumerate(r for r in refs if r)]
        out = Path(output_path) if output_path else tmp / "image.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            if selection == DRAWTHINGS:
                accepted(uuid.uuid4().hex, 0.0)
                await drawthings.generate(
                    prompt, out, aspect_ratio=aspect_ratio or "1:1",
                    width=width, height=height, refs=ref_paths,
                )
            else:
                model_ref = model_of(selection)
                ratio, resolution, extra = await asyncio.to_thread(
                    _higgsfield_shape, model_ref, aspect_ratio, image_size, quality
                )
                _, job_id, _, notes = await higgsfield.generate_with_schema(
                    model_ref, out, prompt=prompt, aspect_ratio=ratio,
                    resolution=resolution, extra=extra, refs=ref_paths,
                    on_accepted=accepted,
                )
                if job_id not in request_ids:
                    request_ids.append(job_id)  # reattached: recorded when it was paid
                for note in notes:
                    logger.info("Higgsfield %s: %s", model_ref, note)
            data = out.read_bytes()
        except (EngineError, OSError, ValueError) as exc:
            for request_id in request_ids:
                _ledger(project_dir, "update", request_id=request_id, status="failed",
                        error_message=str(exc)[:500])
            return None, "", str(exc)
    for request_id in request_ids:
        _ledger(project_dir, "update", request_id=request_id, status="completed")
    return data, "", ""


async def quote_credits(
    selection: str,
    *,
    aspect_ratio: str | None = "1:1",
    image_size: str | None = None,
    quality: str | None = None,
) -> float:
    """What one image costs, in Higgsfield credits (Draw Things is free)."""
    if provider_of(selection) == DRAWTHINGS:
        return 0.0
    model_ref = model_of(selection)
    ratio, resolution, extra = await asyncio.to_thread(
        _higgsfield_shape, model_ref, aspect_ratio, image_size, quality
    )
    return await higgsfield.quote(
        model_ref, aspect_ratio=ratio, resolution=resolution, extra=extra
    )
