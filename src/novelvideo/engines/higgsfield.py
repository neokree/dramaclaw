"""Higgsfield CLI driver: every video, image and audio model on the platform.

Mirrors MacGen's `higgsfield_video_engine.rs`: `generate create` runs without
`--wait`, and the job id lands on disk *before* anything is waited for, so an
interrupted paid job is reattached, never paid twice. The CLI has no cancel.

Models are not hard-coded: the catalog comes from `higgsfield model list` and
each request is shaped by that model's schema (`higgsfield model get`), so a
parameter the model does not take is never sent.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode

import httpx

from novelvideo.engines._proc import EngineError, error_text, run

DONE_OK = "completed"
DONE_FAILED = {"failed", "nsfw", "canceled", "cancelled", "error", "rejected"}
CATALOG_TTL_SECONDS = 24 * 3600
DEFAULT_VIDEO_MODEL = "seedance_2_0?mode=fast"
DEFAULT_IMAGE_MODEL = "nano_banana_pro"
DEFAULT_TTS_MODEL = "text2speech_v2"
DEFAULT_MUSIC_MODEL = "sonilo_music"
DEFAULT_SFX_MODEL = "mirelo_text_to_audio"
# Models NeoKree works with, listed first with their meaningful presets
# (`mode`/`variant` mean different things per model). Every other model on the
# platform stays usable through the same schema-driven path.
FEATURED: dict[str, list[tuple[str, dict[str, Any], str]]] = {
    "video": [
        ("seedance_2_5", {}, "Seedance 2.5"),
        ("seedance_2_0", {"mode": "fast"}, "Seedance 2.0 Fast"),
        ("seedance_2_0", {"mode": "std"}, "Seedance 2.0"),
        ("seedance_2_0_mini", {}, "Seedance 2.0 Mini"),
        ("kling3_0", {"mode": "std"}, "Kling 3.0"),
        ("kling3_0", {"mode": "pro"}, "Kling 3.0 Pro"),
        ("kling3_0", {"mode": "4k"}, "Kling 3.0 4K"),
        ("kling3_0_turbo", {}, "Kling 3.0 Turbo"),
    ],
    "image": [
        ("gpt_image_2_5", {"variant": "flare"}, "GPT Image 2.5 Flare"),
        ("gpt_image_2_5", {"variant": "sunburst"}, "GPT Image 2.5 Sunburst"),
        ("gpt_image_2", {}, "GPT Image 2"),
        ("nano_banana_flash", {}, "Nano Banana 2"),
        ("nano_banana_2_lite", {}, "Nano Banana 2 Lite"),
        ("seedream_v5_pro", {}, "Seedream 5.0 Pro"),
        ("seedream_v5_lite", {}, "Seedream 5.0 Lite"),
        ("seedream_5_0_flash", {}, "Seedream 5.0 Flash"),
    ],
}

# Seedance bounds, measured: `generate cost` refuses 3 and 16.
MIN_SECONDS, MAX_SECONDS = 4, 15
_BOUND_RE = re.compile(r"duration: Input should be (greater|less) than or equal to (\d+)")


def binary() -> str:
    return (
        os.environ.get("HIGGSFIELD_BINARY")
        or shutil.which("higgsfield")
        or "/opt/homebrew/bin/higgsfield"
    )


def video_model() -> str:
    return os.environ.get("HIGGSFIELD_VIDEO_MODEL", DEFAULT_VIDEO_MODEL)


def video_mode() -> str:
    return os.environ.get("HIGGSFIELD_VIDEO_MODE", "fast")


def video_resolution() -> str:
    return os.environ.get("HIGGSFIELD_VIDEO_RESOLUTION", "480p")


def image_model() -> str:
    return os.environ.get("HIGGSFIELD_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)


def _signed_out(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in ("session expired", "not authenticated", "auth login"))


def _parse(proc: Any) -> Any:
    if proc.returncode != 0:
        text = error_text(proc)
        if _signed_out(text):
            raise EngineError("Higgsfield non è autenticato: esegui `higgsfield auth login`.")
        raise EngineError(f"Higgsfield: {text[:500]}")
    return json.loads(proc.stdout or "null")


async def _json(args: list[str], *, timeout: float | None = 120) -> Any:
    return _parse(await run([binary(), *args, "--json"], timeout=timeout))


def _json_sync(args: list[str], *, timeout: float = 60) -> Any:
    return _parse(
        subprocess.run(
            [binary(), *args, "--json"], capture_output=True, text=True, timeout=timeout
        )
    )


async def status() -> dict[str, Any]:
    """`{"available": bool, "credits": float|None, "reason": str}` — never raises."""
    if not Path(binary()).exists():
        return {"available": False, "credits": None, "reason": "higgsfield CLI non installata"}
    try:
        account = await _json(["account", "status"], timeout=15)
    except (EngineError, ValueError, OSError) as exc:
        return {"available": False, "credits": None, "reason": str(exc)}
    return {"available": True, "credits": float(account.get("credits") or 0), "reason": ""}


# --------------------------------------------------------------------------
# Catalog and schemas (disk-cached for a day; sync, callers are sync routes)
# --------------------------------------------------------------------------


def _cache_dir() -> Path:
    root = os.environ.get("HIGGSFIELD_CACHE_DIR") or str(
        Path.home() / ".cache" / "dramaclaw" / "higgsfield"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached(name: str, args: list[str]) -> Any:
    path = _cache_dir() / f"{name}.json"
    if path.exists() and time.time() - path.stat().st_mtime < CATALOG_TTL_SECONDS:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    data = _json_sync(args)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
    return data


def models(kind: str | None = None) -> list[dict[str, str]]:
    """`[{job_type, display_name, type}]`, optionally only `video|image|audio`."""
    data = _cached("models", ["model", "list"])
    items = data if isinstance(data, list) else []
    return [m for m in items if not kind or m.get("type") == kind]


def schema(job_type: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_]+", job_type or ""):
        raise EngineError(f"Modello Higgsfield non valido: {job_type!r}")
    return _cached(f"model-{job_type}", ["model", "get", job_type])


def model_ref(job_type: str, preset: dict[str, Any] | None = None) -> str:
    """`kling3_0` or `kling3_0?mode=pro`: a model plus the params that pin a preset."""
    return job_type + (f"?{urlencode(preset)}" if preset else "")


def parse_model_ref(ref: str) -> tuple[str, dict[str, Any]]:
    job_type, _, query = str(ref or "").strip().partition("?")
    preset: dict[str, Any] = {}
    for key, value in parse_qsl(query):
        preset[key] = {"true": True, "false": False}.get(value, value)
    return job_type.strip(), preset


def param_specs(model_schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in model_schema.get("params") or [] if p.get("name")}


def is_generative_video(model_schema: dict[str, Any]) -> bool:
    """Text/image-to-video models (not upscalers, editors or background removers)."""
    specs = param_specs(model_schema)
    return "prompt" in specs and ("duration" in specs or "aspect_ratio" in specs)


def is_generative_image(model_schema: dict[str, Any]) -> bool:
    specs = param_specs(model_schema)
    return "prompt" in specs and "aspect_ratio" in specs


def _describe(job_type: str, sch: dict[str, Any], label: str, preset: dict[str, Any]) -> dict[str, Any]:
    specs = param_specs(sch)
    return {
        "ref": model_ref(job_type, preset),
        "job_type": job_type,
        "preset": preset,
        "label": label,
        "aspect_ratios": [r for r in specs.get("aspect_ratio", {}).get("enum") or [] if r != "auto"],
        "resolutions": list((specs.get("resolution") or specs.get("quality") or {}).get("enum") or []),
        "durations": [int(d) for d in specs.get("duration", {}).get("enum") or []],
        "start_image": "start_image" in specs,
        "end_image": "end_image" in specs,
        "image_references": "image_references" in specs,
        "max_images": image_limit(sch),
        "video_references": "video_references" in specs,
        "audio_references": "audio_references" in specs,
        "audio": bool({"generate_audio", "sound"} & set(specs)),
        "modes": list((specs.get("mode") or {}).get("enum") or []),
    }


def catalog(kind: str) -> list[dict[str, Any]]:
    """Generative models of `kind`: featured presets first, then every other model."""
    names = {m.get("job_type"): str(m.get("display_name") or "") for m in models(kind)}
    generative = is_generative_video if kind == "video" else is_generative_image
    out: list[dict[str, Any]] = []
    featured = set()
    for job_type, preset, label in FEATURED.get(kind, []):
        if job_type not in names:
            continue
        try:
            out.append(_describe(job_type, schema(job_type), label, preset))
            featured.add(job_type)
        except (EngineError, ValueError, OSError, subprocess.SubprocessError):
            continue
    for job_type, label in names.items():
        if not job_type or job_type in featured:
            continue
        try:
            sch = schema(job_type)
        except (EngineError, ValueError, OSError, subprocess.SubprocessError):
            continue
        if generative(sch):
            out.append(_describe(job_type, sch, label or job_type, {}))
    return out


def voices() -> list[dict[str, Any]]:
    data = _cached("voices", ["voices", "list"])
    if isinstance(data, dict):
        data = data.get("items") or data.get("voices") or []
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# Shaping a request to a model's schema
# --------------------------------------------------------------------------


_IMAGE_LIMIT_RE = re.compile(r"at most (\d+) image")


def image_limit(model_schema: dict[str, Any]) -> int | None:
    """Most images (incl. start/end frame) the model's rules allow, if they say."""
    limits = [
        int(m.group(1))
        for r in model_schema.get("rules") or []
        if (m := _IMAGE_LIMIT_RE.search(str(r.get("message") or "")))
    ]
    return min(limits) if limits else None


def _auto_mode(enum: list[str], has_start: bool, has_refs: bool) -> str | None:
    """Pick a workflow `mode` from the inputs for models whose mode means that."""
    if "omni_reference" in enum:  # Seedance 2.5: t2v takes no media at all
        return "omni_reference" if has_start or has_refs else "t2v"
    if "reference-to-video" in enum:  # Gemini Omni
        if has_refs:
            return "reference-to-video"
        return "image-to-video" if has_start else "text-to-video"
    return None


def _pick_duration(spec: dict[str, Any], seconds: float) -> int:
    wanted = max(1, math.ceil(seconds))
    enum = sorted(int(v) for v in spec.get("enum") or [])
    if enum:
        return next((v for v in enum if v >= wanted), enum[-1])
    return wanted


def shape_params(
    model_schema: dict[str, Any],
    *,
    prompt: str,
    aspect_ratio: str | None = None,
    duration: float | None = None,
    resolution: str | None = None,
    generate_audio: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Only the params this model takes, with values its enums accept."""
    specs = param_specs(model_schema)
    params: dict[str, Any] = {}
    if "prompt" in specs:
        params["prompt"] = prompt
    if aspect_ratio and "aspect_ratio" in specs:
        allowed = specs["aspect_ratio"].get("enum") or []
        if allowed and aspect_ratio not in allowed:
            usable = [r for r in allowed if r != "auto"]
            raise EngineError(
                f"{model_schema.get('display_name') or 'Questo modello'} non genera in "
                f"{aspect_ratio}: accetta solo {', '.join(usable)}."
            )
        params["aspect_ratio"] = aspect_ratio
    if duration is not None and "duration" in specs:
        params["duration"] = _pick_duration(specs["duration"], duration)
    for key in ("resolution", "quality"):
        allowed = (specs.get(key) or {}).get("enum") or []
        if resolution and resolution in allowed:
            params[key] = resolution
            break
    if generate_audio is not None:
        if "generate_audio" in specs:
            params["generate_audio"] = generate_audio
        elif "sound" in specs:
            enum = specs["sound"].get("enum")
            params["sound"] = ("on" if generate_audio else "off") if enum else generate_audio
    if "mode" in specs and "fast" in (specs["mode"].get("enum") or []):
        # Seedance 2.0: fast only renders 480p/720p
        fast_ok = params.get("resolution") in (None, "480p", "720p")
        params["mode"] = video_mode() if fast_ok else "std"
    for key, value in (extra or {}).items():
        if key in specs and value is not None:
            params[key] = value
    return params


def shape_media(
    model_schema: dict[str, Any],
    *,
    start_image: str | None = None,
    end_image: str | None = None,
    refs: Iterable[str] = (),
    video_refs: Iterable[str] = (),
    audio_refs: Iterable[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    """-> (media kwargs this model takes, notes on what was dropped)."""
    specs = param_specs(model_schema)
    rules = " ".join(str(r.get("message") or "") for r in model_schema.get("rules") or [])
    exclusive = "cannot be combined" in rules or "cannot be mixed" in rules
    refs, video_refs, audio_refs = list(refs), list(video_refs), list(audio_refs)
    notes: list[str] = []

    if start_image and "start_image" not in specs:
        if "image_references" in specs:
            refs.insert(0, start_image)
        else:
            notes.append("primo fotogramma ignorato")
        start_image = None
    if end_image and ("end_image" not in specs or not start_image):
        notes.append("ultimo fotogramma ignorato")
        end_image = None
    if (start_image or end_image) and exclusive and (refs or video_refs or audio_refs):
        notes.append("riferimenti ignorati: il modello non li combina col primo fotogramma")
        refs, video_refs, audio_refs = [], [], []
    for key, values in (
        ("image_references", refs),
        ("video_references", video_refs),
        ("audio_references", audio_refs),
    ):
        if values and key not in specs:
            notes.append(f"{key} ignorati")
            values.clear()
    if audio_refs and not (refs or video_refs or start_image or end_image):
        notes.append("audio_references ignorati: servono immagini o video")
        audio_refs = []
    limit = image_limit(model_schema)
    if limit is not None:
        room = max(0, limit - bool(start_image) - bool(end_image))
        if len(refs) > room:
            notes.append(f"{len(refs) - room} immagini di riferimento oltre il limite di {limit}")
            refs = refs[:room]
    return (
        {
            "start_image": start_image,
            "end_image": end_image,
            "refs": refs,
            "video_refs": video_refs,
            "audio_refs": audio_refs,
        },
        notes,
    )


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def job_id_of(stdout: str) -> str | None:
    """CLI 1.1.25 prints a JSON list of ids; older shapes are objects."""
    try:
        value = json.loads(stdout.strip())
    except ValueError:
        return None
    job = value[0] if isinstance(value, list) and value else value
    candidates: list[Any] = [job]
    if isinstance(job, dict):
        candidates += [job.get("id"), job.get("job_id")]
        if isinstance(job.get("job_ids"), list) and job["job_ids"]:
            candidates.append(job["job_ids"][0])
        if isinstance(job.get("jobs"), list) and job["jobs"] and isinstance(job["jobs"][0], dict):
            candidates.append(job["jobs"][0].get("id"))
    return next((c.strip() for c in candidates if isinstance(c, str) and c.strip()), None)


def _outcome(job: Any) -> tuple[str, str | None]:
    """-> ("done", url) | ("failed", None) | ("running", None)."""
    job = job[0] if isinstance(job, list) and job else job
    if not isinstance(job, dict):
        return "running", None
    state = str(job.get("status") or "").lower()
    url = job.get("result_url")
    if state == DONE_OK:
        return ("done", url) if url else ("failed", None)
    if state in DONE_FAILED:
        return "failed", None
    return "running", None


def _params(params: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name, value in params.items():
        if value is None or value == "":
            continue
        out += [f"--{name}", str(value).lower() if isinstance(value, bool) else str(value)]
    return out


def _media(
    start_image: str | None,
    end_image: str | None,
    refs: Iterable[str],
    video_refs: Iterable[str] = (),
    audio_refs: Iterable[str] = (),
) -> list[str]:
    """Media flags; local paths are auto-uploaded by the CLI."""
    out: list[str] = []
    if start_image:
        out += ["--start-image", start_image]
    if end_image:
        out += ["--end-image", end_image]
    for flag, values in (
        ("--image-references", refs),
        ("--video-references", video_refs),
        ("--audio-references", audio_refs),
    ):
        for value in values:
            out += [flag, value]
    return out


async def _cost(job_type: str, params: dict[str, Any], media: list[str]) -> float:
    """Price the job; a duration outside the model's bounds is moved inside once."""
    try:
        data = await _json(["generate", "cost", job_type, *_params(params), *media])
    except EngineError as exc:
        match = _BOUND_RE.search(str(exc))
        if not match or "duration" not in params:
            raise
        params["duration"] = int(match.group(2))
        data = await _json(["generate", "cost", job_type, *_params(params), *media])
    return float(data.get("credits") or 0)


async def generate(
    job_type: str,
    params: dict[str, Any],
    output_path: str | Path,
    *,
    start_image: str | None = None,
    end_image: str | None = None,
    refs: Iterable[str] = (),
    video_refs: Iterable[str] = (),
    audio_refs: Iterable[str] = (),
    wait_timeout: str = "60m",
    on_accepted: Callable[[str, float], None] | None = None,
) -> tuple[Path, str]:
    """Create (or reattach to) one job and download its result to `output_path`.

    `params` may be adjusted in place (duration moved inside the model's bounds).
    `on_accepted(job_id, credits)` runs once, right after a new job is paid for;
    a reattached job was already reported when it was created.
    Returns `(output_path, job_id)`.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pending = out.with_name(out.name + ".higgsfield-job")

    media = _media(start_image, end_image, list(refs), list(video_refs), list(audio_refs))
    job_id = pending.read_text().strip() if pending.exists() else ""
    if not job_id:
        account = await status()
        if not account["available"]:
            raise EngineError(str(account["reason"]))
        price = await _cost(job_type, params, media)
        credits = account["credits"]
        if credits is not None and price > credits:
            raise EngineError(
                f"Su Higgsfield restano {credits:g} crediti e questo job ne costa {price:g}: "
                "non è stato inviato."
            )
        # Point of no return: from here the job is paid for.
        # ponytail: a task cancel mid-`create` can still kill the CLI and lose the
        # id (MacGen detaches create from cancel); `higgsfield generate list` finds it.
        proc = await run(
            [binary(), "generate", "create", job_type, *_params(params), *media, "--json"],
            timeout=None,
        )
        job_id = job_id_of(proc.stdout or "") if proc.returncode == 0 else None
        if not job_id:
            raise EngineError(
                f"Higgsfield non ha restituito l'id del job ({error_text(proc)[:300]}); "
                "controlla con `higgsfield generate list`."
            )
        tmp = pending.with_name(pending.name + ".tmp")
        tmp.write_text(job_id)
        os.replace(tmp, pending)
        if on_accepted:
            on_accepted(job_id, price)

    state, url = _outcome(await _json(["generate", "get", job_id]))
    if state == "running":
        state, url = _outcome(
            await _json(["generate", "wait", job_id, "--timeout", wait_timeout], timeout=None)
        )
    if state == "running":
        raise EngineError(
            f"Il job Higgsfield {job_id} è ancora in corso: verrà ripreso, non ripagato."
        )
    if state == "failed" or not url:
        pending.unlink(missing_ok=True)
        raise EngineError(f"Il job Higgsfield {job_id} è finito senza risultato.")

    part = out.with_name(out.name + ".part")
    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
    os.replace(part, out)
    pending.unlink(missing_ok=True)
    return out, job_id


async def generate_with_schema(
    model: str,
    output_path: str | Path,
    *,
    prompt: str,
    aspect_ratio: str | None = None,
    duration: float | None = None,
    resolution: str | None = None,
    generate_audio: bool | None = None,
    extra: dict[str, Any] | None = None,
    start_image: str | None = None,
    end_image: str | None = None,
    refs: Iterable[str] = (),
    video_refs: Iterable[str] = (),
    audio_refs: Iterable[str] = (),
    on_accepted: Callable[[str, float], None] | None = None,
) -> tuple[Path, str, dict[str, Any], list[str]]:
    """Shape a request to a model's schema (plus its preset) and run it.

    `model` is a model ref: `job_type` or `job_type?param=value`.
    Returns `(output_path, job_id, params sent, notes on dropped inputs)`.
    """
    job_type, preset = parse_model_ref(model)
    sch = await asyncio.to_thread(schema, job_type)
    params = shape_params(
        sch, prompt=prompt, aspect_ratio=aspect_ratio, duration=duration,
        resolution=resolution, generate_audio=generate_audio,
        extra={**(extra or {}), **preset},
    )
    media, notes = shape_media(
        sch, start_image=start_image, end_image=end_image,
        refs=refs, video_refs=video_refs, audio_refs=audio_refs,
    )
    mode_spec = param_specs(sch).get("mode") or {}
    if "mode" not in preset and "mode" not in (extra or {}):
        auto = _auto_mode(
            mode_spec.get("enum") or [],
            bool(media["start_image"] or media["end_image"]),
            bool(media["refs"] or media["video_refs"] or media["audio_refs"]),
        )
        if auto:
            params["mode"] = auto
    if params.get("mode") == "fast" and params.get("resolution") in ("1080p", "4k"):
        params["resolution"] = "720p"  # Seedance 2.0 fast renders 480p/720p only
        notes.append("risoluzione portata a 720p: la modalità fast non va oltre")
    out, job_id = await generate(
        job_type, params, output_path, on_accepted=on_accepted, **media
    )
    return out, job_id, params, notes
