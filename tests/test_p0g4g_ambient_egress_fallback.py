"""OI-48：叶子闸门在调用点漏传 `egress_context=` 时必须回落到请求作用域的身份。

出网闸门通过可选参数携带身份，于是「平台任务，允许」与「调用点忘了穿参数」
被压成同一个 `None`——闸门无法区分，组织流量因此拿平台凭据直连上游、记到平台
账上。派发点（`task_backend/run_core.py`）已把身份绑在请求作用域上，每个做出
`is_organization` 判决的**叶子**闸门都必须读得到它。

透传站点（`egress_context=egress_context`）不在此列：只要叶子会回落，透传空值
仍然得到正确判决，在透传处再解析一次只是重复。
"""

from __future__ import annotations

import pytest

from novelvideo.egress_context import TrustedEgressContext
from novelvideo.model_gateway_runtime import model_gateway_request_scope
from novelvideo.ports.authz import BillingPrincipal
from novelvideo.ports.model_credentials import CredentialReference


def _platform_context() -> TrustedEgressContext:
    return TrustedEgressContext(
        envelope_id="envelope-platform",
        project_id="project-1",
        task_type="sketch_generation",
        requester_user_id="user-1",
        root_task_id="root-platform",
        admission_id="admission-platform",
        admitted_at="2026-08-11T00:00:00Z",
        membership_id=None,
        authz_version=1,
        billing_principal=BillingPrincipal(kind="platform", id="user-1"),
        credential=CredentialReference(
            source="platform",
            credential_id="platform-newapi",
            key_version=1,
        ),
    )


def _organization_context() -> TrustedEgressContext:
    return TrustedEgressContext(
        envelope_id="envelope-1",
        project_id="project-1",
        task_type="sketch_generation",
        requester_user_id="user-1",
        root_task_id="root-task-1",
        admission_id="admission-1",
        admitted_at="2026-08-11T00:00:00Z",
        membership_id="membership-1",
        authz_version=7,
        billing_principal=BillingPrincipal(kind="organization", id="org-1"),
        credential=CredentialReference(
            source="organization",
            credential_id="credential-1",
            key_version=3,
            org_id="org-1",
        ),
    )


def test_platform_scope_is_not_adopted_as_an_egress_context() -> None:
    """回落只补组织这一支——平台身份在这些闸门里与 `None` 同义。

    否则修 fail-open 会修出 fail-closed 的误伤：`IndexTTS2FalClient.generate`
    对「有身份且非组织」直接判 `ORG_EGRESS_DENIED`，把平台身份回落进去，
    平台自己的语音合成就被自己的组织闸门拒了。
    """

    from novelvideo.egress_context import (
        ambient_egress_context,
        ambient_organization_egress_context,
    )
    from novelvideo.generators.indextts2_fal import IndexTTS2FalClient

    with model_gateway_request_scope(_platform_context()):
        assert ambient_egress_context() is not None
        assert ambient_organization_egress_context() is None
        client = IndexTTS2FalClient()
    assert client.egress_context is None


def test_subprocess_model_child_denies_org_without_explicit_context() -> None:
    """漏传参数不该让组织拿到一个持有平台密钥的模型子进程。"""

    from novelvideo.task_backend.subprocesses import (
        EgressBoundaryError,
        build_model_child_env,
    )

    with model_gateway_request_scope(_organization_context()):
        with pytest.raises(EgressBoundaryError) as excinfo:
            build_model_child_env({"PATH": "/usr/bin"}, egress_context=None)
    assert excinfo.value.code == "ORG_EGRESS_DENIED"


def test_subprocess_launch_does_not_adopt_scope_identity() -> None:
    """受限启动**不**回落——它要的是调用方备好的策略，不是身份判决。

    全仓 16 个 `run_project_subprocess` 调用点只有一个传 `restricted_policy`，
    回落会把其余 15 处本地 ffmpeg/媒体命令对组织一律拒掉。那些命令不带凭据也
    不出网，拒掉是功能损坏而非安全收益。
    """

    from novelvideo.task_backend import subprocesses

    with model_gateway_request_scope(_organization_context()):
        proc = subprocesses.run_project_subprocess(
            ["/bin/echo", "hi"],
            capture_output=True,
            text=True,
        )
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_image_egress_denies_non_newapi_without_explicit_context() -> None:
    """组织只允许走 newapi；漏传参数不该让 fal 直连悄悄放行。"""

    from novelvideo.generators.nanobanana_grid import (
        _prepare_organization_image_egress,
    )
    from novelvideo.ports.egress import EgressError

    with model_gateway_request_scope(_organization_context()):
        with pytest.raises(EgressError) as excinfo:
            await _prepare_organization_image_egress(
                egress_context=None,
                provider="fal",
                capability="image.asset.character",
                request={"model": "m"},
            )
    assert excinfo.value.code == "ORG_EGRESS_DENIED"

def test_indextts2_client_blanks_platform_key_without_explicit_context() -> None:
    """漏传参数不该让组织的语音合成落在平台 FAL/newapi 密钥上。"""

    from novelvideo.generators.indextts2_fal import IndexTTS2FalClient

    with model_gateway_request_scope(_organization_context()):
        client = IndexTTS2FalClient()
    assert client.api_key == ""
    assert client.endpoint == ""

def test_scope_survives_asyncio_run_into_a_leaf_gate() -> None:
    """`verification/sketch_edit_execute.py:546` 用 `asyncio.run` 进叶子，全程无
    `egress_context` 参数可传——它只能靠作用域身份。ContextVar 会随
    `asyncio.run` 复制进新事件循环，这条依赖必须被钉住而不是假定。
    """

    import asyncio

    from novelvideo.egress_context import ambient_organization_egress_context

    async def _inside() -> object:
        return ambient_organization_egress_context()

    with model_gateway_request_scope(_organization_context()):
        seen = asyncio.run(_inside())
    assert type(seen) is TrustedEgressContext

@pytest.mark.asyncio
async def test_beat_audio_builds_org_client_without_explicit_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整批语音漏传参数时会用平台客户端——必须回落到 newapi 组织客户端。"""

    from novelvideo.audio import indextts2_beat_audio_task as task
    from novelvideo.generators import indextts2_fal

    seen: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    class _StopHere(RuntimeError):
        pass

    class _Store:
        async def get_beats_as_dicts(self, episode: int) -> list[dict[str, object]]:
            raise _StopHere()

    monkeypatch.setattr(indextts2_fal, "IndexTTS2FalClient", _FakeClient)
    with model_gateway_request_scope(_organization_context()):
        with pytest.raises(_StopHere):
            await task.run_indextts2_beat_audio_generation(
                store=_Store(),
                username="user-1",
                project="project-1",
                episode=1,
                beat_numbers=[1],
            )
    assert seen.get("provider") == "newapi"
    assert type(seen.get("egress_context")) is TrustedEgressContext


@pytest.mark.asyncio
async def test_freezone_vision_egress_prepares_org_path_without_explicit_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自由区视觉分析漏传参数时，会当成平台流量直连——必须回落到组织通道。"""

    from novelvideo.freezone import presets

    seen: dict[str, object] = {}

    async def _fake_prepare(**kwargs: object):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(
        "novelvideo.generators.nanobanana_grid._prepare_organization_image_egress",
        _fake_prepare,
    )
    with model_gateway_request_scope(_organization_context()):
        await presets.prepare_freezone_vision_egress(
            egress_context=None,
            model_name="m",
            prompt="p",
            images=[],
            timeout_seconds=1.0,
        )
    assert type(seen.get("egress_context")) is TrustedEgressContext
