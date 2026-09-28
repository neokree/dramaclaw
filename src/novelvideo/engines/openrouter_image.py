"""OpenRouter image generation: chat/completions with image output modality.

Key from OPENROUTER_API_KEY. Models from OPENROUTER_IMAGE_MODELS (csv); the
selection is `"openrouter:<model>"`, e.g. `openrouter:google/gemini-3.1-flash-image-preview`.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

import httpx

from novelvideo.config import OPENROUTER_BASE_URL
from novelvideo.engines._proc import EngineError

DEFAULT_MODELS = ("google/gemini-3.1-flash-image-preview", "openai/gpt-5.4-image-2")
LABELS = {
    "google/gemini-3.1-flash-image-preview": "OpenRouter NanoBanana 2",
    "openai/gpt-5.4-image-2": "OpenRouter GPT Image 2",
}


def api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def models() -> list[str]:
    raw = os.environ.get("OPENROUTER_IMAGE_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()] or list(DEFAULT_MODELS)


def status() -> dict[str, object]:
    if api_key():
        return {"available": True, "reason": ""}
    return {"available": False, "reason": "OPENROUTER_API_KEY non impostata"}


def _data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


def _image_url(message: dict) -> str:
    """The first data-URL image in a reply: `message.images` or content parts."""
    for image in message.get("images") or []:
        url = (image.get("image_url") or {}).get("url", "")
        if url.startswith("data:image"):
            return url
    content = message.get("content")
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:image"):
                return url
    return ""


async def generate(
    model: str,
    prompt: str,
    out: Path,
    *,
    refs: tuple[str, ...] | list[str] = (),
    aspect_ratio: str | None = "1:1",
    image_size: str | None = None,
) -> float:
    """Write the image to `out`; return the USD cost OpenRouter reported (0 if none)."""
    key = api_key()
    if not key:
        raise EngineError("OPENROUTER_API_KEY non impostata")
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": _data_url(p)}} for p in refs
    ]
    size = str(image_size or "1K").upper()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "image_config": {
            "aspect_ratio": aspect_ratio or "1:1",
            # 0.5K is rejected by Gemini image routes on OpenRouter
            "image_size": "1K" if size in ("0.5K", "512") else size,
        },
        "usage": {"include": True},
    }
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}", "X-Title": "DramaClaw"},
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPStatusError as exc:
        raise EngineError(
            f"OpenRouter HTTP {exc.response.status_code}: {exc.response.text[:280]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise EngineError(f"OpenRouter non raggiungibile: {exc}") from exc
    choices = result.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    url = _image_url(message)
    if not url:
        text = message.get("content") if isinstance(message.get("content"), str) else ""
        raise EngineError(f"OpenRouter {model} non ha restituito un'immagine {text[:200]}".strip())
    out.write_bytes(base64.b64decode(url.split(",", 1)[1]))
    return float((result.get("usage") or {}).get("cost") or 0.0)
