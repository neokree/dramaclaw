"""Voices, music and sound effects on Higgsfield: the app's only remote audio path.

Speech in the voice of a reference sample (every character/narrator voice in the
app is an uploaded sample) goes to `seed_audio` with the sample as an audio
reference. Speech in a Higgsfield voice (`higgsfield voices list`, preset or a
cloned "element") goes to `HIGGSFIELD_TTS_MODEL` (default `text2speech_v2`).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from novelvideo.engines import higgsfield
from novelvideo.engines._proc import EngineError

VOICE_CLONE_MODEL = "seed_audio"
OnAccepted = Callable[[str, float], None] | None


def tts_model() -> str:
    return os.environ.get("HIGGSFIELD_TTS_MODEL", higgsfield.DEFAULT_TTS_MODEL)


def tts_variant() -> str:
    return os.environ.get("HIGGSFIELD_TTS_VARIANT", "elevenlabs")


def default_voice_id() -> str:
    configured = os.environ.get("HIGGSFIELD_TTS_VOICE", "").strip()
    if configured:
        return configured
    preset = next((v for v in higgsfield.voices() if v.get("voice_type") == "preset"), None)
    if not preset:
        raise EngineError("Higgsfield non ha voci disponibili (`higgsfield voices list`).")
    return str(preset["id"])


def _tts_request(
    *,
    voice_id: str | None,
    voice_type: str,
    model: str | None,
    language: str | None,
    reference_audio: str | Path | None,
) -> tuple[str, dict[str, Any], list[str]]:
    """-> (model ref, params beyond the prompt, audio references) for one speech job."""
    if reference_audio:
        # ponytail: seed_audio has no style/emotion input, the sample's delivery carries it.
        return VOICE_CLONE_MODEL, {"format": "mp3"}, [str(reference_audio)]
    voice = voice_id or default_voice_id()
    extra = {
        "variant": tts_variant(),
        "voice_id": voice,
        "voice_type": voice_type,
        "voice": voice,  # inworld_text_to_speech names its voice param `voice`
        "language": language,
        "format": "mp3",
    }
    return model or tts_model(), extra, []


async def _params(model: str, prompt: str, duration: float | None, extra: dict[str, Any]):
    job_type, preset = higgsfield.parse_model_ref(model)
    sch = await asyncio.to_thread(higgsfield.schema, job_type)
    params = higgsfield.shape_params(
        sch, prompt=prompt, duration=duration, extra={**extra, **preset}
    )
    return job_type, params


async def _run(
    model: str,
    out: str | Path,
    *,
    prompt: str,
    duration: float | None = None,
    extra: dict[str, Any] | None = None,
    audio_refs: Iterable[str] = (),
    on_accepted: OnAccepted = None,
) -> Path:
    # Not generate_with_schema: its media shaping drops audio refs without images
    # (a Seedance rule), and seed_audio takes a voice sample on its own.
    job_type, params = await _params(model, prompt, duration, extra or {})
    path, _job_id = await higgsfield.generate(
        job_type, params, out, audio_refs=list(audio_refs), on_accepted=on_accepted
    )
    return path


async def tts(
    text: str,
    out: str | Path,
    *,
    voice_id: str | None = None,
    voice_type: str = "preset",
    model: str | None = None,
    language: str | None = None,
    reference_audio: str | Path | None = None,
    on_accepted: OnAccepted = None,
) -> Path:
    ref, extra, audio_refs = _tts_request(
        voice_id=voice_id, voice_type=voice_type, model=model,
        language=language, reference_audio=reference_audio,
    )
    return await _run(
        ref, out, prompt=text, extra=extra, audio_refs=audio_refs, on_accepted=on_accepted
    )


async def music(
    prompt: str, out: str | Path, *, duration: float, model: str | None = None,
    on_accepted: OnAccepted = None,
) -> Path:
    return await _run(
        model or higgsfield.DEFAULT_MUSIC_MODEL, out, prompt=prompt, duration=duration,
        on_accepted=on_accepted,
    )


async def sfx(
    prompt: str, out: str | Path, *, duration: float, on_accepted: OnAccepted = None
) -> Path:
    return await _run(
        higgsfield.DEFAULT_SFX_MODEL, out, prompt=prompt, duration=duration,
        on_accepted=on_accepted,
    )


async def quote(kind: str, amount: int) -> float:
    """Higgsfield credits for `tts` of `amount` characters or `music` of `amount` seconds."""
    if kind == "music":
        job_type, params = await _params(higgsfield.DEFAULT_MUSIC_MODEL, "music", amount, {})
    else:
        # Every app voice is a reference sample, so speech is priced as seed_audio.
        job_type, params = await _params(
            VOICE_CLONE_MODEL, "字" * min(max(int(amount), 1), 5000), None, {"format": "mp3"}
        )
    return await higgsfield._cost(job_type, params, [])
