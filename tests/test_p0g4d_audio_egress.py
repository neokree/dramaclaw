from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from novelvideo.egress_context import TrustedEgressContext
from novelvideo.ports.authz import BillingPrincipal
from novelvideo.ports.egress_operations import (
    OperationClaimResult,
    OperationSnapshot,
    OperationState,
)
from novelvideo.ports.model_credentials import (
    CredentialReference,
    ModelCredentialError,
    RequestCredential,
)


def _context(kind: str = "organization") -> TrustedEgressContext:
    principal_id = "org-1" if kind == "organization" else "local-user-1"
    return TrustedEgressContext(
        envelope_id="envelope-1",
        project_id="project-1",
        task_type="freezone_audio_speech",
        requester_user_id="user-1",
        root_task_id="root-task-1",
        admission_id="admission-1",
        admitted_at="2026-08-03T04:05:00Z",
        membership_id="membership-1" if kind == "organization" else None,
        authz_version=7,
        billing_principal=BillingPrincipal(kind=kind, id=principal_id),
        credential=CredentialReference(
            source=kind,
            credential_id=f"{kind}-credential-1",
            key_version=3,
            org_id="org-1" if kind == "organization" else None,
        ),
    )


class _CredentialPort:
    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.admissions = []

    async def resolve(self, admission):
        self.admissions.append(admission)
        if self.error:
            raise ModelCredentialError(self.error, "unsafe resolver detail")
        return RequestCredential(
            reference=admission.credential,
            api_key="org-secret-key",
            base_url="https://gateway.example/v1",
        )


class _OperationPort:
    def __init__(
        self, events: list[str], *, existing_state: OperationState | None = None
    ):
        self.events = events
        self.existing_state = existing_state
        self.specs = []

    async def claim(self, *, spec):
        self.events.append("claim")
        self.specs.append(spec)
        state = self.existing_state or OperationState.DISPATCHING
        return OperationClaimResult(
            won=self.existing_state is None,
            operation=OperationSnapshot("operation-1", spec.operation_key, state, 1),
            transition_token=(
                None if self.existing_state is not None else "transition-1"
            ),
        )

    async def mark_rejected_before_submit(self, **kwargs):
        self.events.append("rejected")
        return OperationSnapshot(
            "operation-1", "operation-key", OperationState.REJECTED_BEFORE_SUBMIT, 2
        )

    async def mark_accepted(self, **kwargs):
        self.events.append("accepted")
        return OperationSnapshot(
            "operation-1", "operation-key", OperationState.ACCEPTED, 2
        )

    async def mark_completed(self, **kwargs):
        self.events.append("completed")
        return OperationSnapshot(
            "operation-1", "operation-key", OperationState.COMPLETED, 3
        )

    async def mark_unknown(self, **kwargs):
        self.events.append("unknown")
        return OperationSnapshot(
            "operation-1", "operation-key", OperationState.UNKNOWN, 2
        )


def _install_ports(monkeypatch, credential_port, operation_port) -> None:
    import novelvideo.ports as ports

    monkeypatch.setattr(ports, "get_model_credentials", lambda: credential_port)
    monkeypatch.setattr(ports, "get_egress_operation_port", lambda: operation_port)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "forwarded"),
    [("organization", True), ("platform", False), ("local", False)],
)
async def test_audio_runner_forwards_only_organization_context_to_beat_leaf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, forwarded: bool
) -> None:
    from novelvideo.egress_context import (
        TRUSTED_EGRESS_CONTEXT_KEY,
        TrustedRunnerEnvelope,
    )
    from novelvideo.task_backend.runners import audio as audio_runner
    import novelvideo.audio.indextts2_beat_audio_task as beat_task
    import novelvideo.sqlite_store as sqlite_store

    context = _context(kind)
    captured: dict = {}

    class Store:
        def __init__(self, *_args, **_kwargs):
            pass

        async def initialize(self):
            return None

        async def close(self):
            return None

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            generated=0,
            total_targets=0,
            skipped_existing=0,
            skipped_empty=0,
            skipped_manual=0,
            skipped_silence=0,
            skipped_non_dialogue=0,
            failed=[],
            generated_beats=[],
            to_dict=lambda: {},
        )

    monkeypatch.setattr(sqlite_store, "SQLiteStore", Store)
    monkeypatch.setattr(beat_task, "run_indextts2_beat_audio_generation", fake_run)
    monkeypatch.setattr(
        audio_runner,
        "get_task_manager",
        lambda: SimpleNamespace(
            update_progress_for_project=lambda *_args, **_kwargs: None
        ),
    )
    envelope = TrustedRunnerEnvelope(
        {
            "episode": 1,
            "payload": {"episode": 1},
            TRUSTED_EGRESS_CONTEXT_KEY: context,
        }
    )
    ctx = SimpleNamespace(
        owner_project_label="owner/project",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        owner_username="owner",
        project_name="project",
    )

    await audio_runner._run_indextts2_audio(envelope, ctx)

    # Higgsfield runs on the local CLI account: no task forwards an egress identity.
    del forwarded
    assert "egress_context" not in captured


@pytest.mark.asyncio
async def test_eg16a_platform_stays_available_but_organization_denies_dashscope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.generators import tts_generator

    calls: list[str] = []
    _install_fake_dashscope(monkeypatch, calls)
    monkeypatch.setattr(
        tts_generator.CosyVoiceTTSGenerator, "_get_audio_duration", _async_value(1.0)
    )

    platform = tts_generator.CosyVoiceTTSGenerator(api_key="platform-dashscope")
    assert (await platform.generate("hello", str(tmp_path / "platform.mp3"))).success
    assert calls == ["synthesizer", "call"]

    monkeypatch.setattr(
        tts_generator,
        "get_tts_config",
        lambda: pytest.fail("organization path must not read DashScope config"),
    )
    organization = tts_generator.CosyVoiceTTSGenerator(
        api_key="org-dashscope-must-not-be-used",
        egress_context=_context(),
    )
    denied = await organization.generate("hello", str(tmp_path / "denied.mp3"))
    assert denied.success is False
    assert denied.error == "ORG_EGRESS_DENIED"
    assert calls == ["synthesizer", "call"]


@pytest.mark.asyncio
async def test_eg16b_edge_accepts_only_trusted_local_context_and_isolates_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.generators import tts_generator

    events: list[str] = []
    operation_port = _OperationPort(events)
    _install_ports(monkeypatch, _CredentialPort(), operation_port)
    captured = _install_fake_edge_tts(monkeypatch, events)
    monkeypatch.setattr(
        tts_generator.EdgeTTSGenerator, "_get_audio_duration", _async_value(1.0)
    )
    monkeypatch.setenv("MODEL_API_KEY", "platform-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "provider-secret")
    monkeypatch.setattr(
        tts_generator,
        "get_tts_config",
        lambda: pytest.fail("local Edge path must not read model provider config"),
    )

    generator = tts_generator.EdgeTTSGenerator(egress_context=_context("local"))
    result = await generator.generate("hello", str(tmp_path / "edge.mp3"))

    assert result.success is True
    assert events == ["claim", "edge_stream", "accepted", "completed"]
    assert captured == {
        "text": "hello",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
    }
    assert "secret" not in repr(captured)


@pytest.mark.asyncio
async def test_eg16b_rejects_org_context_and_provider_switch_before_edge_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.generators import tts_generator

    events: list[str] = []
    _install_fake_edge_tts(monkeypatch, events)
    denied = await tts_generator.EdgeTTSGenerator(egress_context=_context()).generate(
        "hello", str(tmp_path / "denied.mp3")
    )
    assert denied.success is False
    assert denied.error == "ORG_SERVICE_EGRESS_DENIED"
    assert events == []

    with pytest.raises(tts_generator.AudioEgressError) as exc:
        tts_generator.create_tts_generator(
            provider="cosyvoice",
            egress_context=_context("local"),
        )
    assert exc.value.code == "ORG_SERVICE_EGRESS_DENIED"
    assert events == []


def _async_value(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call


def _install_fake_dashscope(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    dashscope = ModuleType("dashscope")
    dashscope.api_key = None
    audio = ModuleType("dashscope.audio")
    tts_v2 = ModuleType("dashscope.audio.tts_v2")

    class ResultCallback:
        pass

    class SpeechSynthesizer:
        def __init__(self, *, callback, **_kwargs):
            calls.append("synthesizer")
            self.callback = callback

        def call(self, _text):
            calls.append("call")
            self.callback.on_open()
            self.callback.on_data(b"audio")
            self.callback.on_close()

    tts_v2.ResultCallback = ResultCallback
    tts_v2.SpeechSynthesizer = SpeechSynthesizer
    monkeypatch.setitem(sys.modules, "dashscope", dashscope)
    monkeypatch.setitem(sys.modules, "dashscope.audio", audio)
    monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts_v2)


def _install_fake_edge_tts(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> dict[str, str]:
    module = ModuleType("edge_tts")
    captured: dict[str, str] = {}

    class Communicate:
        def __init__(self, text, voice, *, rate, pitch):
            captured.update(text=text, voice=voice, rate=rate, pitch=pitch)

        async def stream(self):
            events.append("edge_stream")
            yield {"type": "audio", "data": b"audio"}

    class SubMaker:
        def feed(self, _chunk):
            return None

        def get_srt(self):
            return ""

    module.Communicate = Communicate
    module.SubMaker = SubMaker
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    return captured
