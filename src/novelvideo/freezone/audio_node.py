"""Freezone audio-node helpers: speech in a reference voice and music, on Higgsfield."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novelvideo.audio_request_usage import HiggsfieldLedger
from novelvideo.config import INDEXTTS2_RECORD_MODEL, OUTPUT_DIR
from novelvideo.egress_context import TrustedEgressContext
from novelvideo.engines import audio
from novelvideo.engines.higgsfield import DEFAULT_MUSIC_MODEL
from novelvideo.generators.higgsfield_tts import HiggsfieldTTSClient
from novelvideo.project_config import (
    load_effective_narration_style_for_voice_from_state_dir,
    load_narrator_reference_audio_from_state_dir,
)
from novelvideo.seedance2_i2v.voice_clone import (
    build_reference_audio_url,
    file_sha256,
    narration_style_prompt,
    resolve_character_voice,
    resolve_narrator_source,
)
from novelvideo.freezone.paths import outputs_dir

USER_VOICE_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm"}
USER_VOICE_SCOPE = "user_custom"
VOICE_FILE_UNREADABLE_MESSAGE = "声线文件无法读取，请重新选择或检查文件是否完整"


@dataclass
class FreezoneAudioSpeechResult:
    audio_path: Path
    duration_ms: int
    mime_type: str
    model: str
    voice_source: str
    voice_sha256: str


@dataclass
class FreezoneVoiceRefResolution:
    audio_path: Path
    sha256: str
    source: str


class VoicePrerequisiteError(RuntimeError):
    error_code = "voice_prereq_required"


def freezone_audio_speech_output_path(project_dir: Path, job_id: str) -> Path:
    return outputs_dir(project_dir, "freezone_audio_speech") / f"{job_id}.mp3"


def freezone_audio_eleven_music_output_path(project_dir: Path, job_id: str) -> Path:
    return outputs_dir(project_dir, "freezone_audio_eleven_music") / f"{job_id}.mp3"


def freezone_audio_music_billing_seconds(music_length_ms: int) -> int:
    try:
        value = int(music_length_ms or 0)
    except (TypeError, ValueError):
        value = 0
    return max((max(value, 0) + 999) // 1000, 1)


def user_audio_voices_dir(username: str) -> Path:
    return Path(OUTPUT_DIR) / username / "_account" / "freezone" / "audio" / "voices"


def user_audio_voices_index_path(username: str) -> Path:
    return user_audio_voices_dir(username) / "voices.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_voice_name(value: str) -> str:
    return re.sub(r"[\x00-\x1f]", "", str(value or "").strip())[:80] or "未命名音色"


def _safe_extension(filename: str | None) -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in USER_VOICE_EXTENSIONS:
        raise ValueError("unsupported voice audio format; use mp3/wav/m4a/aac/ogg/webm")
    return suffix


def _load_user_voice_records(username: str) -> list[dict]:
    path = user_audio_voices_index_path(username)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        records = data.get("voices", [])
    else:
        records = data
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def _write_user_voice_records(username: str, records: list[dict]) -> None:
    path = user_audio_voices_index_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"voices": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _user_voice_abs_path(username: str, record: dict) -> Path:
    return Path(OUTPUT_DIR) / username / str(record.get("path") or "")


def is_readable_audio_file(path: Path) -> bool:
    """Return whether ``path`` is a regular file that can actually be opened."""
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        stream.read(1)
    return True


def public_user_voice_payload(username: str, record: dict) -> dict:
    voice_id = str(record.get("voice_id") or "")
    label = str(record.get("name") or record.get("label") or voice_id or "未命名音色")
    path = str(record.get("path") or "")
    abs_path = _user_voice_abs_path(username, record)
    try:
        exists = bool(path and is_readable_audio_file(abs_path))
    except OSError:
        exists = False
    return {
        "scope": USER_VOICE_SCOPE,
        "voice_id": voice_id,
        "label": label,
        "name": label,
        "path": path,
        "url": "",
        "exists": exists,
        "sha256": str(record.get("sha256") or ""),
        "duration_ms": int(record.get("duration_ms") or 0),
        "mime_type": str(record.get("mime_type") or ""),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "source_filename": str(record.get("source_filename") or ""),
    }


def list_user_audio_voices(username: str) -> list[dict]:
    return [
        public_user_voice_payload(username, record)
        for record in _load_user_voice_records(username)
    ]


def create_user_audio_voice(
    *,
    username: str,
    name: str,
    filename: str | None,
    content: bytes,
    mime_type: str = "",
) -> dict:
    if not content:
        raise ValueError("voice audio file is empty")
    extension = _safe_extension(filename)
    voice_id = f"fv_{uuid.uuid4().hex[:16]}"
    rel_path = f"_account/freezone/audio/voices/{voice_id}/reference{extension}"
    abs_path = Path(OUTPUT_DIR) / username / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)

    now = _utc_now()
    record = {
        "voice_id": voice_id,
        "name": _safe_voice_name(name),
        "path": rel_path,
        "sha256": file_sha256(abs_path),
        "duration_ms": _duration_ms(abs_path),
        "mime_type": mime_type or "application/octet-stream",
        "source_filename": Path(str(filename or "reference")).name,
        "created_at": now,
        "updated_at": now,
    }
    records = _load_user_voice_records(username)
    records.append(record)
    _write_user_voice_records(username, records)
    return public_user_voice_payload(username, record)


def resolve_user_audio_voice(
    username: str, voice_id: str
) -> FreezoneVoiceRefResolution:
    target = str(voice_id or "").strip()
    if not target:
        raise RuntimeError("user_custom voice_id is required")
    for record in _load_user_voice_records(username):
        if str(record.get("voice_id") or "") != target:
            continue
        path = _user_voice_abs_path(username, record)
        if not is_readable_audio_file(path):
            raise RuntimeError(f"用户音色文件不存在: {target}")
        sha = str(record.get("sha256") or "") or file_sha256(path)
        return FreezoneVoiceRefResolution(path, sha, USER_VOICE_SCOPE)
    raise RuntimeError(f"用户音色不存在: {target}")


def _duration_ms(audio_path: Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return int(float(result.stdout.strip()) * 1000)
    except Exception:
        return 0


async def _project_path(project_dir: Path, stored_path: str) -> Path | None:
    value = str(stored_path or "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = project_dir / path
    return path if await asyncio.to_thread(is_readable_audio_file, path) else None


@dataclass(frozen=True)
class _PathOnlyStore:
    """Stands in for the project store where only ``project_dir`` is ever read.

    ``resolve_narrator_source`` takes a store for exactly two things: the
    project directory, and -- if no character rows are handed to it -- a lookup
    of every character in the database.  Once the rows travel with the task the
    second use is gone, and passing this instead of a real store keeps that
    function unchanged while making the difference impossible to lose: any
    attribute other than ``project_dir`` fails loudly instead of quietly
    reopening project state.
    """

    project_dir: str


def _projected_character_rows(row: Any) -> list[Any]:
    """Rebuild the character rows the projection carried as plain JSON.

    The resolution below reads characters as model objects, so they are
    validated back into the model here rather than teaching four call sites to
    accept a row in two shapes.  An absent row becomes an empty list, never
    ``None``: ``None`` is what the callee reads as "go look them up yourself".
    """
    from novelvideo.models import NovelCharacter

    if not isinstance(row, dict):
        return []
    return [NovelCharacter.model_validate(dict(row))]


async def _resolve_voice_ref(
    *,
    store,
    username: str,
    account_voice_username: str | None = None,
    project_dir: Path,
    voice_ref: dict | None,
    characters: list[Any] | None = None,
) -> FreezoneVoiceRefResolution | None:
    if not isinstance(voice_ref, dict):
        return None

    scope = str(voice_ref.get("scope") or "").strip()
    character_name = str(voice_ref.get("character_name") or "").strip()
    identity_id = str(voice_ref.get("identity_id") or "").strip()
    slot = str(voice_ref.get("slot") or "").strip()

    if scope == USER_VOICE_SCOPE:
        return await asyncio.to_thread(
            resolve_user_audio_voice,
            account_voice_username or username,
            str(voice_ref.get("voice_id") or ""),
        )

    if characters is None:
        characters = list(await store.list_characters())
    else:
        characters = list(characters)

    def _find_character():
        return next(
            (
                item
                for item in characters
                if str(getattr(item, "name", "") or "") == character_name
            ),
            None,
        )

    if scope == "character_default":
        character = _find_character()
        path = await _project_path(
            project_dir,
            getattr(character, "reference_audio_path", "") if character else "",
        )
        if path is None:
            raise RuntimeError(f"角色默认声线不可用: {character_name or '<空>'}")
        sha = str(
            getattr(character, "reference_audio_sha256", "") or ""
        ) or await asyncio.to_thread(file_sha256, path)
        return FreezoneVoiceRefResolution(path, sha, "character_default")

    if scope == "character_age_group":
        character = _find_character()
        samples = (
            getattr(character, "voice_samples_by_age_group", None) or {}
            if character
            else {}
        )
        entry = samples.get(slot) if isinstance(samples, dict) else None
        path = await _project_path(
            project_dir, entry.get("path", "") if isinstance(entry, dict) else ""
        )
        if path is None:
            raise RuntimeError(
                f"角色年龄段声线不可用: {character_name or '<空>'}/{slot or '<空>'}"
            )
        sha = str(entry.get("sha256", "") or "") if isinstance(entry, dict) else ""
        return FreezoneVoiceRefResolution(
            path, sha or await asyncio.to_thread(file_sha256, path), "character_age_group"
        )

    if scope in {"identity", "identity_resolved"}:
        character = _find_character()
        identity = None
        if character is not None:
            identity = next(
                (
                    item
                    for item in list(getattr(character, "identities", None) or [])
                    if str(getattr(item, "identity_id", "") or "") == identity_id
                ),
                None,
            )
        if character is None or identity is None:
            raise RuntimeError(
                f"身份声线不可用: {character_name or '<空>'}/{identity_id or '<空>'}"
            )
        if scope == "identity":
            path = await _project_path(
                project_dir, getattr(identity, "reference_audio_path", "")
            )
            if path is None:
                raise RuntimeError(f"身份声线未配置: {identity_id}")
            sha = str(
                getattr(identity, "reference_audio_sha256", "") or ""
            ) or await asyncio.to_thread(file_sha256, path)
            return FreezoneVoiceRefResolution(path, sha, "identity")
        resolved = await asyncio.to_thread(
            resolve_character_voice,
            project_dir=project_dir,
            character=character,
            identity=identity,
        )
        if resolved.audio_path is None:
            raise RuntimeError(f"身份实际声线不可用: {identity_id}")
        if not await asyncio.to_thread(is_readable_audio_file, resolved.audio_path):
            raise RuntimeError(f"身份实际声线不可用: {identity_id}")
        return FreezoneVoiceRefResolution(
            resolved.audio_path,
            resolved.sha256
            or await asyncio.to_thread(file_sha256, resolved.audio_path),
            f"identity_resolved:{resolved.tier or 'unknown'}",
        )

    return None


async def resolve_speech_voice(
    *,
    store,
    username: str,
    project: str,
    account_voice_username: str | None = None,
    project_dir: Path,
    voice_ref: dict | None = None,
    projection: Any = None,
) -> tuple[str, FreezoneVoiceRefResolution]:
    """Resolve narration style + reference voice for one speech job.

    Without a projection this reads project-bound state exactly as it always
    has: ``load_effective_narration_style_for_voice`` /
    ``load_narrator_reference_audio`` read the project state directory and
    ``store.list_characters()`` reads the project database, all of which only
    exist on the machine that holds the project.

    With a projection those same values arrive with the task, pinned when it was
    submitted, and nothing project-bound is read here.  A projection that is
    present but missing a field raises rather than falling back to the database:
    a silent fallback would hide the very thing the projection exists to make
    visible.
    """
    if projection is None:
        narration_style = load_effective_narration_style_for_voice_from_state_dir(
            store.state_dir
        )
        voice_characters = None
        narrator_store = store
    else:
        narration_style = str(projection.require("narration_style") or "")
        voice_characters = _projected_character_rows(projection.require("voice_character"))
        narrator_store = _PathOnlyStore(str(project_dir))

    try:
        selected_voice = await _resolve_voice_ref(
            store=store,
            username=username,
            account_voice_username=account_voice_username,
            project_dir=project_dir,
            voice_ref=voice_ref,
            characters=voice_characters,
        )
    except OSError as exc:
        raise VoicePrerequisiteError(VOICE_FILE_UNREADABLE_MESSAGE) from exc
    except RuntimeError as exc:
        raise VoicePrerequisiteError(str(exc)) from exc
    if selected_voice is None:
        if projection is None:
            descriptor = load_narrator_reference_audio_from_state_dir(store.state_dir)
            characters = (
                await store.list_characters() if narration_style == "first_person" else None
            )
        else:
            descriptor = dict(projection.require("narrator_reference_audio") or {})
            characters = (
                _projected_character_rows(projection.require("narrator_main_character"))
                if narration_style == "first_person"
                else None
            )
        narrator_descriptor_present = bool(str(descriptor.get("path") or "").strip())
        try:
            voice = await asyncio.to_thread(
                resolve_narrator_source,
                store=narrator_store,
                narration_style=narration_style,
                project_narrator_stored_path=descriptor.get("path", ""),
                characters=characters,
            )
        except OSError as exc:
            raise VoicePrerequisiteError(VOICE_FILE_UNREADABLE_MESSAGE) from exc
        if voice.audio_path is not None:
            try:
                readable = await asyncio.to_thread(
                    is_readable_audio_file,
                    voice.audio_path,
                )
            except OSError as exc:
                raise VoicePrerequisiteError(VOICE_FILE_UNREADABLE_MESSAGE) from exc
            if not readable:
                raise VoicePrerequisiteError(VOICE_FILE_UNREADABLE_MESSAGE)
        if voice.audio_path is None:
            if voice.source == "project_narrator":
                message = (
                    "解说人声线文件无法读取，请检查文件是否完整"
                    if narrator_descriptor_present
                    else "项目解说人声线未配置，请上传或录制解说人音频"
                )
            else:
                message = voice.error or "解说声线缺失"
            raise VoicePrerequisiteError(message)
        selected_voice = FreezoneVoiceRefResolution(
            voice.audio_path,
            voice.sha256,
            voice.source or "project_narrator",
        )
    return narration_style, selected_voice


async def generate_freezone_audio_speech(
    *,
    store=None,
    username: str,
    project: str,
    account_voice_username: str | None = None,
    project_dir: Path,
    job_id: str,
    text: str,
    emotion_prompt: str = "",
    voice_ref: dict | None = None,
    projection: Any = None,
    egress_context: TrustedEgressContext | None = None,
) -> FreezoneAudioSpeechResult:
    """Generate standalone Freezone speech using the project narrator reference.

    ``projection`` is the payload projection pinned when the task was submitted
    (``task_backend.projection.read_projection``). When it is supplied nothing
    project-bound is read here and ``store`` may be ``None``; when it is absent
    the voice is resolved from project state exactly as before.
    """
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("text is required")

    narration_style, selected_voice = await resolve_speech_voice(
        store=store,
        username=username,
        project=project,
        account_voice_username=account_voice_username,
        project_dir=project_dir,
        voice_ref=voice_ref,
        projection=projection,
    )

    output_path = freezone_audio_speech_output_path(project_dir, job_id)
    del egress_context  # ponytail: Higgsfield runs on the local CLI account, no org gateway
    generator = HiggsfieldTTSClient(
        ledger=HiggsfieldLedger(
            project_dir,
            task_type="freezone_audio_speech",
            scope=job_id,
            model=INDEXTTS2_RECORD_MODEL,
        )
    )
    result = await generator.generate(
        prompt=clean_text,
        audio_url=build_reference_audio_url(selected_voice.audio_path),
        output_path=output_path,
        emotion_prompt=str(emotion_prompt or "").strip()
        or narration_style_prompt(narration_style),
    )
    if not result.success:
        raise RuntimeError(result.error or "audio generation failed")

    output_path = Path(result.audio_path or output_path)  # renamed to its real container
    duration_ms = int((result.duration_seconds or 0) * 1000) or _duration_ms(
        output_path
    )
    return FreezoneAudioSpeechResult(
        audio_path=output_path,
        duration_ms=duration_ms,
        mime_type=audio.MIME_TYPES.get(output_path.suffix, "application/octet-stream"),
        model=INDEXTTS2_RECORD_MODEL,
        voice_source=selected_voice.source,
        voice_sha256=selected_voice.sha256,
    )


async def generate_freezone_audio_eleven_music(
    *,
    project_dir: Path,
    job_id: str,
    prompt: str,
    music_length_ms: int = 30_000,
    force_instrumental: bool = True,
    respect_sections_durations: bool = True,
    output_format: str = "mp3_44100_128",
    response_format: str = "mp3",
    model: str = DEFAULT_MUSIC_MODEL,
    egress_context: TrustedEgressContext | None = None,
) -> FreezoneAudioSpeechResult:
    """Generate standalone Freezone music on Higgsfield (`sonilo_music` by default)."""
    # ponytail: the music models take only prompt + duration; the ElevenLabs-era
    # knobs stay in the signature for queued payloads and are ignored.
    del force_instrumental, respect_sections_durations, output_format, response_format
    del egress_context
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("prompt is required")
    length = int(music_length_ms or 0)
    if length < 3_000 or length > 600_000:
        raise ValueError("music_length_ms must be between 3000 and 600000")
    model_name = str(model or "").strip()
    if not model_name or model_name == "LingShan-MU-11":  # legacy default in queued payloads
        model_name = DEFAULT_MUSIC_MODEL

    output_path = freezone_audio_eleven_music_output_path(project_dir, job_id)
    ledger = HiggsfieldLedger(
        project_dir, task_type="freezone_audio_music", scope=job_id, model=model_name
    )
    try:
        output_path = await audio.music(
            clean_prompt,
            output_path,
            duration=length / 1000,
            model=model_name,
            on_accepted=ledger.accepted,
        )
    except Exception as exc:
        ledger.finish(str(exc) or type(exc).__name__)
        raise
    ledger.finish()
    return FreezoneAudioSpeechResult(
        audio_path=output_path,
        duration_ms=_duration_ms(output_path) or length,
        mime_type=audio.MIME_TYPES.get(output_path.suffix, "application/octet-stream"),
        model=model_name,
        voice_source=model_name,
        voice_sha256="",
    )
