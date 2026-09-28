"""Video generation: Higgsfield (Seedance 2.0 via CLI, cloud) and h3.c (local).

The drivers live in `novelvideo.engines`; this module adapts them to the
`generate(image_path, prompt, output_path, ...) -> VideoGenResult` contract the
task runners and freezone jobs use.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from novelvideo.engines import h3c, higgsfield
from novelvideo.engines._proc import EngineError
from novelvideo.task_backend.cancel import TaskCancelled, TaskTimedOut
from novelvideo.task_backend.subprocesses import run_project_subprocess

logger = logging.getLogger(__name__)

HIGGSFIELD_MAX_MEDIA_REFS = 3  # per kind, video and audio
# Canvas workflow modes -> Higgsfield params (Seedance 2.5 style `mode`).
GEN_MODE_PARAMS: dict[str, dict[str, str]] = {
    "video_edit": {"mode": "video_edit"},
    "video_extend": {"mode": "video_extension", "extension_mode": "forward"},
}
FIRST_FRAME_ROLES = {"首帧", "first_frame"}
LAST_FRAME_ROLES = {"尾帧", "last_frame"}


class VideoGenStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class VideoBackend(Enum):
    HIGGSFIELD = "higgsfield"  # any Higgsfield video model: "higgsfield:<job_type>"
    H3C = "h3c"  # MiniMax H3 on Metal (local)
    MOCK = "mock"  # ffmpeg Ken Burns, for tests and dry runs


HIGGSFIELD_PREFIX = "higgsfield:"
H3C_LABEL = "h3.c · MiniMax H3 (locale)"


def higgsfield_backend(job_type: str) -> str:
    return f"{HIGGSFIELD_PREFIX}{job_type}"


DEFAULT_VIDEO_BACKEND = higgsfield_backend(higgsfield.DEFAULT_VIDEO_MODEL)


@dataclass
class VideoGenResult:
    status: VideoGenStatus
    video_url: Optional[str] = None
    video_path: Optional[str] = None
    last_frame_url: Optional[str] = None
    last_frame_path: Optional[str] = None
    task_id: Optional[str] = None
    provider_task_id: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: float = 0.0


@dataclass
class ShotReference:
    """A media reference for a shot."""

    type: str  # "image" / "video" / "audio"
    path: str  # local file path
    role: str  # "首帧" / "尾帧" / "角色参考" / "场景参考" / ...


class VideoGeneratorBase(ABC):
    async def generate(
        self,
        image_path: Optional[str],
        prompt: str,
        output_path: str,
        aspect_ratio: str = "16:9",
        duration: float = 5.0,
        **kwargs: Any,
    ) -> VideoGenResult:
        raise NotImplementedError


def _split_references(
    image_path: str | None,
    last_frame_path: str | None,
    references: list[ShotReference] | None,
) -> tuple[str | None, str | None, list[str], list[str], list[str]]:
    """-> (first frame, last frame, image refs, video refs, audio refs)."""
    first, last = image_path, last_frame_path
    images: list[str] = []
    videos: list[str] = []
    audios: list[str] = []
    for ref in references or []:
        if not ref.path:
            continue
        if ref.type == "image" and ref.role in FIRST_FRAME_ROLES:
            first = first or ref.path
        elif ref.type == "image" and ref.role in LAST_FRAME_ROLES:
            last = last or ref.path
        elif ref.type == "image":
            images.append(ref.path)
        elif ref.type == "video":
            videos.append(ref.path)
        elif ref.type == "audio":
            audios.append(ref.path)
    return first, last, images, videos, audios


def _last_frame(video: Path) -> str | None:
    dest = video.with_name(f"{video.stem}_last_frame.png")
    proc = run_project_subprocess(
        ["ffmpeg", "-y", "-v", "error", "-sseof", "-0.1", "-i", str(video),
         "-frames:v", "1", "-update", "1", str(dest)],
        capture_output=True, text=True, timeout=120,
    )
    return str(dest) if proc.returncode == 0 and dest.exists() else None


def _generate_audio(kwargs: dict[str, Any]) -> bool | None:
    setting = str(kwargs.get("audio_setting") or "").strip().lower()
    if setting in {"on", "true", "audio", "with_audio"}:
        return True
    if setting in {"off", "false", "silent", "mute", "no_audio"}:
        return False
    config = kwargs.get("seedance2_config")
    if config:
        from novelvideo.seedance2_i2v.models import parse_seedance2_config

        return parse_seedance2_config(config).generate_audio
    return None


def _wants_last_frame(kwargs: dict[str, Any]) -> bool:
    config = kwargs.get("seedance2_config")
    if not config:
        return False
    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    return parse_seedance2_config(config).return_last_frame


def _config_resolution(kwargs: dict[str, Any]) -> str | None:
    config = kwargs.get("seedance2_config")
    if not config:
        return None
    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    return parse_seedance2_config(config).resolution or None


def _log(kwargs: dict[str, Any], message: str) -> None:
    on_log: Callable[[str], None] | None = kwargs.get("on_log")
    if on_log:
        on_log(message)
    logger.info(message)


def _track_usage(
    project_output_dir: str | None,
    status: str,
    job_id: str,
    *,
    model: str = "",
    credits: float | None = None,
    duration: float | None = None,
    error: str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> None:
    """Record a paid Higgsfield job in the project's video usage table.

    Credits come from `higgsfield generate cost`, taken just before the job was
    sent. A failed write is logged, never raised: the video is already paid for.
    """
    if not project_output_dir:
        return
    from novelvideo import video_request_usage as usage

    try:
        if status == "accepted":
            kwargs = kwargs or {}
            usage.record_video_request(
                project_output_dir=project_output_dir,
                request_id=job_id,
                provider="higgsfield",
                model_name=model,
                episode=kwargs.get("episode"),
                beat_num=kwargs.get("beat_num"),
                task_type=kwargs.get("task_type"),
                duration_seconds=duration,
                cost_estimate=credits,
            )
        else:
            usage.update_video_request_status(
                project_output_dir=project_output_dir,
                request_id=job_id,
                status=status,
                error_message=error,
            )
    except Exception:  # noqa: BLE001
        logger.warning("could not record Higgsfield usage for %s", job_id, exc_info=True)


class HiggsfieldVideoGenerator(VideoGeneratorBase):
    """Any Higgsfield video model; the request is shaped by the model's schema."""

    def __init__(self, model: str | None = None, resolution: str | None = None, **_: Any):
        self.model = model or higgsfield.video_model()
        self.resolution = resolution or higgsfield.video_resolution()

    async def generate(
        self,
        image_path: Optional[str],
        prompt: str,
        output_path: str,
        aspect_ratio: str = "9:16",
        duration: float = 5.0,
        **kwargs: Any,
    ) -> VideoGenResult:
        first, last, images, videos, audios = _split_references(
            image_path, kwargs.get("last_frame_path"), kwargs.get("references")
        )
        usage_dir = kwargs.get("project_output_dir")
        accepted: list[str] = []

        def on_accepted(job_id: str, credits: float) -> None:
            accepted.append(job_id)
            _track_usage(
                usage_dir, "accepted", job_id,
                model=self.model, credits=credits, duration=duration, kwargs=kwargs,
            )

        try:
            out, job_id, params, notes = await higgsfield.generate_with_schema(
                self.model,
                output_path,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                duration=duration,
                resolution=_config_resolution(kwargs) or self.resolution,
                generate_audio=_generate_audio(kwargs),
                extra=GEN_MODE_PARAMS.get(str(kwargs.get("gen_mode") or "")),
                start_image=first,
                end_image=last,
                refs=images,
                video_refs=videos[:HIGGSFIELD_MAX_MEDIA_REFS],
                audio_refs=audios[:HIGGSFIELD_MAX_MEDIA_REFS],
                on_accepted=on_accepted,
            )
        except (TaskCancelled, TaskTimedOut):
            raise
        except (EngineError, OSError, ValueError) as exc:
            for job_id in accepted:
                _track_usage(usage_dir, "failed", job_id, error=str(exc))
            return VideoGenResult(status=VideoGenStatus.FAILED, error=str(exc))
        _track_usage(usage_dir, "downloaded", job_id)
        for note in notes:
            _log(kwargs, f"Higgsfield {self.model}: {note}")
        last_frame = await asyncio.to_thread(_last_frame, out) if _wants_last_frame(kwargs) else None
        return VideoGenResult(
            status=VideoGenStatus.DONE,
            video_path=str(out),
            task_id=job_id,
            provider_task_id=job_id,
            last_frame_path=last_frame,
            duration_seconds=float(params.get("duration") or duration),
        )


class H3VideoGenerator(VideoGeneratorBase):
    """MiniMax H3 through the local h3.c binary (first/last frame, no refs)."""

    def __init__(self, **_: Any) -> None:
        pass

    async def generate(
        self,
        image_path: Optional[str],
        prompt: str,
        output_path: str,
        aspect_ratio: str = "9:16",
        duration: float = 5.0,
        **kwargs: Any,
    ) -> VideoGenResult:
        first, last, images, videos, audios = _split_references(
            image_path, kwargs.get("last_frame_path"), kwargs.get("references")
        )
        if not first and images:
            first, images = images[0], images[1:]
        if images or videos or audios:
            # Ref2VA is not installed: h3 conditions only on first/last frame.
            _log(kwargs, "h3.c non usa riferimenti: solo primo e ultimo fotogramma")
        _log(kwargs, f"h3.c: {h3c.frames(duration)} frame {h3c.canvas(aspect_ratio)}")
        try:
            out = await h3c.generate(
                prompt, output_path,
                aspect_ratio=aspect_ratio, duration=duration,
                first_frame=first, last_frame=last,
            )
        except (TaskCancelled, TaskTimedOut):
            raise
        except (EngineError, OSError) as exc:
            return VideoGenResult(status=VideoGenStatus.FAILED, error=str(exc))
        last_frame = await asyncio.to_thread(_last_frame, out) if _wants_last_frame(kwargs) else None
        return VideoGenResult(
            status=VideoGenStatus.DONE,
            video_path=str(out),
            last_frame_path=last_frame,
            duration_seconds=h3c.frames(duration) / h3c.FPS,
        )


class MockVideoGenerator(VideoGeneratorBase):
    """ffmpeg Ken Burns over the first frame; no AI engine."""

    def __init__(self, width: int = 1080, height: int = 1920, fps: int = 24, **_: Any):
        self.width, self.height, self.fps = width, height, fps

    async def generate(
        self,
        image_path: Optional[str],
        prompt: str,
        output_path: str,
        aspect_ratio: str = "9:16",
        duration: float = 5.0,
        **kwargs: Any,
    ) -> VideoGenResult:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        frames = int(duration * self.fps)
        source = ["-loop", "1", "-i", image_path] if image_path else [
            "-f", "lavfi", "-i", f"color=c=black:s={self.width}x{self.height}"
        ]
        cmd = [
            "ffmpeg", "-y", *source,
            "-vf", (
                f"scale=8000:-1,zoompan=z='min(zoom+0.0008,1.15)':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"s={self.width}x{self.height}:fps={self.fps}"
            ),
            "-t", str(duration), "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", "-an", output_path,
        ]
        result = await asyncio.to_thread(
            run_project_subprocess, cmd, capture_output=True, text=True, timeout=30 * 60
        )
        if result.returncode != 0:
            return VideoGenResult(status=VideoGenStatus.FAILED, error=f"FFmpeg error: {result.stderr[:500]}")
        return VideoGenResult(status=VideoGenStatus.DONE, video_path=output_path, duration_seconds=duration)


def _known_backend(value: str | None) -> str | None:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in {"mock", "h3c"}:
        return lowered
    if lowered in {"h3", "h3.c"}:
        return VideoBackend.H3C.value
    job_type = text[len(HIGGSFIELD_PREFIX):].strip() if lowered.startswith(HIGGSFIELD_PREFIX) else ""
    return higgsfield_backend(job_type) if job_type else None


def normalize_video_backend(backend: VideoBackend | str | None) -> str:
    """Canonical backend id: `higgsfield:<job_type>`, `h3c` or `mock`.

    Empty, bare `higgsfield` and retired ids (newapi_*, huimeng_*, comfyui, ...)
    resolve to VIDEO_BACKEND, then to HIGGSFIELD_VIDEO_MODEL.
    """
    if isinstance(backend, VideoBackend):
        backend = backend.value
    return (
        _known_backend(backend)
        or _known_backend(os.environ.get("VIDEO_BACKEND"))
        or higgsfield_backend(higgsfield.video_model())
    )


def video_engine(backend: VideoBackend | str | None) -> tuple[str, str | None]:
    """-> ("higgsfield", job_type) | ("h3c", None) | ("mock", None)."""
    value = normalize_video_backend(backend)
    if value.startswith(HIGGSFIELD_PREFIX):
        return VideoBackend.HIGGSFIELD.value, value[len(HIGGSFIELD_PREFIX):]
    return value, None


def video_backend_catalog() -> list[dict[str, Any]]:
    """Higgsfield video models (from the CLI) plus local h3.c, with their capabilities."""
    items: list[dict[str, Any]] = []
    try:
        for model in higgsfield.catalog("video"):
            items.append({**model, "backend": higgsfield_backend(model["ref"])})
    except (EngineError, OSError, ValueError) as exc:
        logger.warning("Higgsfield catalog unavailable: %s", exc)
    default = normalize_video_backend(None)
    items.sort(key=lambda item: item["backend"] != default)
    if h3c.available()["available"]:
        items.append(
            {
                "backend": VideoBackend.H3C.value,
                "ref": None,
                "job_type": None,
                "preset": {},
                "label": H3C_LABEL,
                "max_images": 2,
                "aspect_ratios": ["9:16", "16:9", "1:1", "4:3", "3:4", "4:5"],
                "resolutions": [],
                "durations": [],
                "start_image": True,
                "end_image": True,
                "image_references": False,
                "video_references": False,
                "audio_references": False,
                "audio": True,
            }
        )
    return items


def video_backend_options() -> dict[str, str]:
    return {item["backend"]: item["label"] for item in video_backend_catalog()}


def create_video_generator(
    backend: Optional[VideoBackend | str] = None,
    use_mock: bool = False,
    **kwargs: Any,
) -> VideoGeneratorBase:
    """Create a generator for `higgsfield:<job_type>` (default), `h3c` or `mock`."""
    kwargs.pop("egress_context", None)
    kwargs.pop("workflow_type", None)
    engine, job_type = video_engine(VideoBackend.MOCK if use_mock else backend)
    if engine == VideoBackend.MOCK.value:
        return MockVideoGenerator()
    if engine == VideoBackend.H3C.value:
        return H3VideoGenerator(**kwargs)
    return HiggsfieldVideoGenerator(model=job_type, **kwargs)
