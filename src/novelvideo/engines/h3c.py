"""h3.c driver: MiniMax H3 on Metal, local (port of MacGen's `h3_video_engine.rs`).

Runbook: /Users/fabiobiola/Developer/AI-Tools/h3.c/AGENTS.md. Canvas sides are
multiples of 32 with area <= 768x1344, frames are 5 + 17n (n >= 1) at 24 fps, and
`--ssd-streaming` is always on (without it the model swaps on a 48 GB Mac).
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path

from novelvideo.engines import mtplx
from novelvideo.engines._proc import EngineError, run

FPS = 24
MAX_AREA = 768 * 1344
STEPS, LAYERS, REUSE = 20, 45, 2
_HOME = Path.home() / "Developer/AI-Tools/h3.c"


def binary() -> str:
    return os.environ.get("H3C_BINARY", str(_HOME / "h3"))


def weights() -> str:
    return os.environ.get("H3C_WEIGHTS", str(_HOME / "MiniMax-H3"))


def available() -> dict[str, object]:
    if not Path(binary()).exists():
        return {"available": False, "reason": f"h3.c non trovato in {binary()}"}
    if not Path(weights()).exists():
        return {"available": False, "reason": f"pesi MiniMax-H3 non trovati in {weights()}"}
    return {"available": True, "reason": ""}


def canvas(aspect_ratio: str) -> tuple[int, int]:
    """Largest h3 canvas with exactly this ratio (9:16 -> 576x1024, 4:5 -> 896x1120)."""
    try:
        a, b = (int(x) for x in aspect_ratio.split(":"))
    except ValueError:
        raise EngineError(f"h3.c: rapporto non valido {aspect_ratio!r}") from None
    g = math.gcd(a, b)
    a, b = a // g, b // g
    step = math.lcm(32 // math.gcd(32, a), 32 // math.gcd(32, b))
    k = math.isqrt(MAX_AREA // (a * b * step * step))
    if k == 0:
        raise EngineError(f"h3.c non ha una tela in {aspect_ratio}.")
    return a * step * k, b * step * k


def frames(duration: float) -> int:
    wanted = max(22, round(duration * FPS))  # h3 refuses less than one 17-frame chunk
    return 5 + math.ceil((wanted - 5) / 17) * 17


def seed_of(name: str) -> int:
    """FNV-1a 32-bit, so the same output name renders the same video."""
    h = 0x811C9DC5
    for byte in name.encode():
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return h


async def _fit(image: str, width: int, height: int, dest: Path) -> str:
    """h3 stretches `--first-frame`; crop it to the canvas first."""
    proc = await run(
        ["ffmpeg", "-y", "-v", "error", "-i", image, "-vf",
         f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
         "-frames:v", "1", str(dest)],
        timeout=120,
    )
    if proc.returncode != 0:
        raise EngineError(f"h3.c: primo fotogramma non adattabile: {proc.stderr[:300]}")
    return str(dest)


async def generate(
    prompt: str,
    output_path: str | Path,
    *,
    aspect_ratio: str = "9:16",
    duration: float = 5.0,
    first_frame: str | None = None,
    last_frame: str | None = None,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    width, height = canvas(aspect_ratio)
    cmd = [binary(), "-d", weights(), "-p", prompt[:7000],
           "--width", str(width), "--height", str(height),
           "--frames", str(frames(duration)),
           "--steps", str(STEPS), "--layers", str(LAYERS), "--reuse", str(REUSE),
           "--seed", str(seed_of(out.name)), "--ssd-streaming"]
    if first_frame:
        cmd += ["--first-frame", await _fit(first_frame, width, height, out.with_suffix(".first.png"))]
    if last_frame:
        cmd += ["--last-frame", await _fit(last_frame, width, height, out.with_suffix(".last.png"))]
    cmd += ["-o", str(out)]

    await asyncio.to_thread(mtplx.stop)  # ~30 GB MTPLX + ~20 GB h3 swap a 48 GB Mac
    # h3 loads h3_shaders.metal from its cwd.
    proc = await run(cmd, timeout=None, cwd=Path(binary()).parent)
    for tmp in (out.with_suffix(".first.png"), out.with_suffix(".last.png")):
        tmp.unlink(missing_ok=True)
    if proc.returncode != 0:
        out.unlink(missing_ok=True)  # h3 writes straight to the output and has no SIGTERM handler
        errors = [
            line for line in f"{proc.stderr}\n{proc.stdout}".replace("\r", "\n").splitlines()
            if line.startswith("h3: ") and not line.startswith("h3: wrote")
        ]
        kind = "rifiutato" if proc.returncode == 2 else f"morto (exit {proc.returncode})"
        raise EngineError(f"h3.c {kind}: {' | '.join(errors)[-500:]}")
    return out
