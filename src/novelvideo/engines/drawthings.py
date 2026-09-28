"""Draw Things HTTP API driver (local images).

Draw Things exposes an A1111-compatible API once enabled in
Settings -> API Server (HTTP, port 7860). The model is whatever the app has
loaded unless DRAWTHINGS_MODEL names a file.
"""

from __future__ import annotations

import base64
import math
import os
from pathlib import Path
from typing import Iterable

import httpx

from novelvideo.engines._proc import EngineError

TARGET_PIXELS = 1024 * 1024


def base_url() -> str:
    return os.environ.get("DRAWTHINGS_URL", "http://127.0.0.1:7860").rstrip("/")


async def status() -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resp = await client.get(f"{base_url()}/sdapi/v1/options")
        resp.raise_for_status()
    except httpx.HTTPError:
        return {
            "available": False,
            "reason": "Draw Things non risponde: attiva Settings → API Server (HTTP, porta 7860).",
        }
    return {"available": True, "reason": ""}


def size_for(aspect_ratio: str, pixels: int = TARGET_PIXELS) -> tuple[int, int]:
    """~1 MP at the ratio, sides multiples of 64."""
    try:
        a, b = (float(x) for x in aspect_ratio.split(":"))
    except ValueError:
        a, b = 1.0, 1.0
    height = math.sqrt(pixels * b / a)
    width = height * a / b
    return max(64, round(width / 64) * 64), max(64, round(height / 64) * 64)


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


async def generate(
    prompt: str,
    output_path: str | Path,
    *,
    aspect_ratio: str = "1:1",
    width: int | None = None,
    height: int | None = None,
    refs: Iterable[str] = (),
    strength: float = 0.6,
    negative_prompt: str = "",
    seed: int = -1,
) -> Path:
    """txt2img, or img2img from the first reference image.

    ponytail: A1111 img2img takes one init image; extra refs are ignored. Use
    Higgsfield (nano_banana_pro, up to 14 refs) when identity needs many refs.
    """
    if not (width and height):
        width, height = size_for(aspect_ratio)
    payload: dict[str, object] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "seed": seed,
        "batch_size": 1,
    }
    if os.environ.get("DRAWTHINGS_MODEL"):
        payload["model"] = os.environ["DRAWTHINGS_MODEL"]
    if os.environ.get("DRAWTHINGS_STEPS"):
        payload["steps"] = int(os.environ["DRAWTHINGS_STEPS"])
    refs = [r for r in refs if r]
    endpoint = "txt2img"
    if refs:
        endpoint = "img2img"
        payload["init_images"] = [_b64(refs[0])]
        payload["strength"] = strength

    try:
        async with httpx.AsyncClient(timeout=1800, trust_env=False) as client:
            resp = await client.post(f"{base_url()}/sdapi/v1/{endpoint}", json=payload)
    except httpx.HTTPError as exc:
        raise EngineError(f"Draw Things non raggiungibile su {base_url()}: {exc}") from exc
    if resp.status_code != 200:
        raise EngineError(f"Draw Things {resp.status_code}: {resp.text[:300]}")
    images = (resp.json() or {}).get("images") or []
    if not images:
        raise EngineError("Draw Things non ha restituito immagini.")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(images[0]))
    return out
