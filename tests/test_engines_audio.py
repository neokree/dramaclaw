"""Higgsfield audio facade: request shaping only, the CLI is never run."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from novelvideo.engines import audio, higgsfield
from novelvideo.engines._proc import EngineError

SCHEMAS = {
    "text2speech_v2": {"params": [
        {"name": "prompt"},
        {"name": "variant", "enum": ["elevenlabs", "minimax", "seed_speech"]},
        {"name": "voice_id"},
        {"name": "voice_type", "enum": ["preset", "element"]},
    ]},
    "qwen_audio_tts": {"params": [
        {"name": "prompt"}, {"name": "voice_id"}, {"name": "voice_type"},
        {"name": "language"}, {"name": "format"},
    ]},
    "seed_audio": {"params": [
        {"name": "prompt"}, {"name": "audio_references"}, {"name": "format"},
        {"name": "voice_id"}, {"name": "voice_type"},
    ]},
    "sonilo_music": {"params": [{"name": "prompt"}, {"name": "duration"}]},
    "mirelo_text_to_audio": {"params": [{"name": "prompt"}, {"name": "duration"}]},
}


@pytest.fixture()
def jobs(monkeypatch):
    sent: list[dict] = []

    async def fake_generate(job_type, params, out, *, audio_refs=(), on_accepted=None, **_):
        sent.append({"job_type": job_type, "params": params, "audio_refs": list(audio_refs)})
        if on_accepted:
            on_accepted("job-1", 3.0)
        Path(out).write_bytes(b"audio")
        return Path(out), "job-1"

    monkeypatch.setattr(higgsfield, "schema", lambda job_type: SCHEMAS[job_type])
    monkeypatch.setattr(higgsfield, "generate", fake_generate)
    monkeypatch.setattr(
        higgsfield, "voices",
        lambda: [{"id": "el-1", "voice_type": "element"}, {"id": "pre-1", "voice_type": "preset"}],
    )
    monkeypatch.delenv("HIGGSFIELD_TTS_MODEL", raising=False)
    monkeypatch.delenv("HIGGSFIELD_TTS_VARIANT", raising=False)
    monkeypatch.delenv("HIGGSFIELD_TTS_VOICE", raising=False)
    return sent


async def test_reference_voice_goes_to_seed_audio_as_audio_reference(jobs, tmp_path):
    await audio.tts("你好", tmp_path / "a.mp3", reference_audio=tmp_path / "voice.wav")

    assert jobs == [{
        "job_type": "seed_audio",
        "params": {"prompt": "你好", "format": "mp3"},
        "audio_refs": [str(tmp_path / "voice.wav")],
    }]


async def test_preset_voice_uses_tts_model_variant_and_first_preset(jobs, tmp_path):
    await audio.tts("hello", tmp_path / "a.mp3")
    await audio.tts("ciao", tmp_path / "b.mp3", model="qwen_audio_tts?voice_type=element",
                    voice_id="v9", language="it")

    assert jobs[0]["job_type"] == "text2speech_v2"
    assert jobs[0]["params"] == {
        "prompt": "hello", "variant": "elevenlabs", "voice_id": "pre-1", "voice_type": "preset",
    }
    assert jobs[1]["job_type"] == "qwen_audio_tts"
    assert jobs[1]["params"] == {
        "prompt": "ciao", "voice_id": "v9", "voice_type": "element", "language": "it",
        "format": "mp3",
    }


async def test_env_picks_tts_model_variant_and_voice(jobs, tmp_path, monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_TTS_VARIANT", "minimax")
    monkeypatch.setenv("HIGGSFIELD_TTS_VOICE", "custom")

    await audio.tts("hello", tmp_path / "a.mp3")

    assert jobs[0]["params"]["variant"] == "minimax"
    assert jobs[0]["params"]["voice_id"] == "custom"


async def test_music_and_sfx_send_prompt_and_whole_seconds(jobs, tmp_path):
    accepted = []
    await audio.music("rain", tmp_path / "m.mp3", duration=30.5, on_accepted=lambda *a: accepted.append(a))
    await audio.sfx("door slam", tmp_path / "s.mp3", duration=2)

    assert [(j["job_type"], j["params"]) for j in jobs] == [
        ("sonilo_music", {"prompt": "rain", "duration": 31}),
        ("mirelo_text_to_audio", {"prompt": "door slam", "duration": 2}),
    ]
    assert accepted == [("job-1", 3.0)]


async def test_quote_prices_speech_as_seed_audio_and_music_by_seconds(jobs, monkeypatch):
    priced = []

    async def fake_cost(job_type, params, media):
        priced.append((job_type, params, media))
        return 2.0

    monkeypatch.setattr(higgsfield, "_cost", fake_cost)

    assert await audio.quote("tts", 3) == 2.0
    assert await audio.quote("music", 30) == 2.0
    assert priced == [
        ("seed_audio", {"prompt": "字字字", "format": "mp3"}, []),
        ("sonilo_music", {"prompt": "music", "duration": 30}, []),
    ]
    assert jobs == []  # quoting never creates a job


async def test_tts_client_records_paid_job_in_project_ledger(jobs, tmp_path):
    from novelvideo.audio_request_usage import HiggsfieldLedger, get_audio_request_usage_db_path
    from novelvideo.generators.higgsfield_tts import HiggsfieldTTSClient

    ledger = HiggsfieldLedger(tmp_path, task_type="t", scope="s", model="seed_audio")
    result = await HiggsfieldTTSClient(ledger=ledger).generate(
        prompt="你好", audio_url=str(tmp_path / "voice.wav"), output_path=tmp_path / "a.mp3"
    )

    assert result.success is True
    with sqlite3.connect(get_audio_request_usage_db_path(tmp_path)) as conn:
        assert conn.execute(
            "SELECT request_id, provider, model_name, status, cost_credits FROM audio_request_usage"
        ).fetchall() == [("job-1", "higgsfield", "seed_audio", "completed", 3.0)]


async def test_tts_client_turns_engine_errors_into_failed_results(monkeypatch, tmp_path):
    from novelvideo.generators.higgsfield_tts import HiggsfieldTTSClient

    async def signed_out(*_a, **_k):
        raise EngineError("Higgsfield non è autenticato")

    monkeypatch.setattr(audio, "tts", signed_out)
    client = HiggsfieldTTSClient()

    failed = await client.generate(prompt="hi", audio_url="v.wav", output_path=tmp_path / "a.mp3")
    empty = await client.generate(prompt=" ", audio_url="v.wav", output_path=tmp_path / "a.mp3")

    assert (failed.success, failed.error) == (False, "Higgsfield non è autenticato")
    assert empty.success is False


def test_ledger_v1_table_gains_cost_column(tmp_path):
    from novelvideo import audio_request_usage as usage

    db = usage.get_audio_request_usage_db_path(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(usage._SCHEMA_SQL.replace(",\n    cost_credits REAL", ""))

    usage.HiggsfieldLedger(tmp_path, task_type="t", scope="s", model="m").accepted("job-9", 1.5)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT request_id, cost_credits FROM audio_request_usage"
        ).fetchall() == [("job-9", 1.5)]


@pytest.mark.parametrize(("head", "suffix"), [
    (b"\x00\x00\x00\x20ftypM4A ", ".m4a"),
    (b"RIFF\x00\x00\x00\x00WAVE", ".wav"),
    (b"ID3\x04\x00", ".mp3"),
    (b"\xff\xfb\x90\x00", ".mp3"),
    (b"audio", ".mp3"),  # unknown: left as requested
])
async def test_audio_is_renamed_to_the_container_higgsfield_sent(jobs, tmp_path, monkeypatch, head, suffix):
    async def fake_generate(job_type, params, out, **_):
        Path(out).write_bytes(head)
        return Path(out), "job-1"

    monkeypatch.setattr(higgsfield, "generate", fake_generate)
    path = await audio.music("rain", tmp_path / "m.mp3", duration=5)

    assert path == tmp_path / f"m{suffix}" and path.read_bytes() == head
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
