"""Speech in the voice of a reference sample, on Higgsfield (replaces IndexTTS2)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx

from novelvideo.audio_request_usage import HiggsfieldLedger
from novelvideo.engines import audio
from novelvideo.engines._proc import EngineError
from novelvideo.generators.tts_generator import TTSResult


def _duration_seconds(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


class HiggsfieldTTSClient:
    """`generate(prompt, audio_url, output_path)` -> TTSResult, never raises on a failed job.

    `audio_url` is the reference sample: a local path (the CLI uploads it) or URL.
    Set `ledger` before a call to record the paid job in the project audio ledger.
    """

    def __init__(self, *, ledger: HiggsfieldLedger | None = None) -> None:
        self.ledger = ledger

    async def generate(
        self,
        *,
        prompt: str,
        audio_url: str,
        output_path: str | Path,
        emotion_prompt: str = "",
    ) -> TTSResult:
        del emotion_prompt  # ponytail: seed_audio has no emotion/style input
        prompt = str(prompt or "").strip()
        if not prompt:
            return TTSResult(success=False, error="TTS prompt is empty")
        reference = str(audio_url or "").strip()
        if not reference:
            return TTSResult(success=False, error="reference voice is empty")
        ledger = self.ledger
        try:
            path = await audio.tts(
                prompt,
                output_path,
                reference_audio=reference,
                on_accepted=ledger.accepted if ledger else None,
            )
        except (EngineError, OSError, ValueError, httpx.HTTPError) as exc:
            if ledger:
                ledger.finish(str(exc))
            return TTSResult(success=False, error=str(exc))
        if ledger:
            ledger.finish()
        return TTSResult(
            success=True,
            audio_path=str(path),
            duration_seconds=await asyncio.to_thread(_duration_seconds, path),
        )
