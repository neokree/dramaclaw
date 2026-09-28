from __future__ import annotations


import pytest

from novelvideo.egress_context import TrustedEgressContext
from novelvideo.ports.authz import BillingPrincipal
from novelvideo.ports.egress_operations import (
    HandleKind,
    OperationClaimResult,
    OperationSnapshot,
    OperationState,
)
from novelvideo.ports.model_credentials import CredentialReference
from support.egress_ledger import assert_transition_allowed


class FakeOperations:
    """带状态机的替身。

    原先它连 `mark_accepted` 都没有，`mark_completed` 也不看前置态，于是服务路径
    「从 dispatching 直跳 completed」——真库上必抛 P0001——在这里一路绿灯。替身不建
    状态机，DB 侧的约束就是摆设，见 OI-49。
    """

    def __init__(
        self, *, won: bool = True, state: OperationState = OperationState.DISPATCHING
    ):
        self.won = won
        self.state = state
        self.claims = []
        self.accepted = []
        self.completed = []
        self.unknown = []
        self._state = state
        self._version = 1

    async def claim(self, *, spec):
        self.claims.append(spec)
        return OperationClaimResult(
            won=self.won,
            operation=OperationSnapshot(
                operation_id="op-1",
                operation_key=spec.operation_key,
                state=self.state,
                version=1,
            ),
            transition_token="transition-1" if self.won else None,
        )

    def _transition(self, kwargs, target: OperationState) -> OperationSnapshot:
        assert_transition_allowed(
            current=self._state,
            target=target,
            expected_version=kwargs["expected_version"],
            row_version=self._version,
        )
        self._state = target
        self._version = kwargs["expected_version"] + 1
        return OperationSnapshot(
            operation_id=kwargs["operation_id"],
            operation_key="operation-key",
            state=target,
            version=self._version,
        )

    async def mark_accepted(self, **kwargs):
        snapshot = self._transition(kwargs, OperationState.ACCEPTED)
        self.accepted.append(kwargs)
        return snapshot

    async def mark_completed(self, **kwargs):
        snapshot = self._transition(kwargs, OperationState.COMPLETED)
        self.completed.append(kwargs)
        return snapshot

    async def mark_unknown(self, **kwargs):
        snapshot = self._transition(kwargs, OperationState.UNKNOWN)
        self.unknown.append(kwargs)
        return snapshot


def _org_context(*, org_id: str = "org-a", project_id: str = "project-a"):
    return TrustedEgressContext(
        envelope_id="envelope-1",
        project_id=project_id,
        task_type="image.generate",
        requester_user_id="user-1",
        root_task_id="root-1",
        admission_id="admission-1",
        admitted_at="2026-08-03T04:05:00Z",
        membership_id="membership-1",
        authz_version=1,
        billing_principal=BillingPrincipal(kind="organization", id=org_id),
        credential=CredentialReference(
            source="organization",
            credential_id="org-gateway-key",
            key_version=7,
            org_id=org_id,
        ),
    )


def _platform_context():
    return TrustedEgressContext(
        envelope_id="envelope-platform",
        project_id="project-platform",
        task_type="image.generate",
        requester_user_id="user-platform",
        root_task_id="root-platform",
        admission_id="admission-platform",
        admitted_at="2026-08-03T04:05:00Z",
        membership_id=None,
        authz_version=1,
        billing_principal=BillingPrincipal(kind="platform", id="platform"),
        credential=CredentialReference(
            source="platform",
            credential_id="platform-key",
            key_version=1,
        ),
    )


@pytest.mark.asyncio
async def test_c1_eg21_release_feed_drops_untrusted_release_url(tmp_path, monkeypatch):
    from novelvideo.ports.local.release_feed import LocalReleaseFeed

    monkeypatch.setenv("RELEASE_NOTIFICATIONS_ENABLED", "true")
    notes = tmp_path / "release-notes.md"
    notes.write_text(
        "# v1.0.0\n## User-facing Highlights (en)\n- **Current**: local\n",
        encoding="utf-8",
    )

    async def fetcher():
        return {
            "tag_name": "v2.0.0",
            "html_url": "https://attacker.example/secret-object-canary",
            "body": "# v2.0.0\n## User-facing Highlights (en)\n- **New**: item\n",
        }

    feed = await LocalReleaseFeed(
        notes_path=notes,
        version_reader=lambda: "1.0.0",
        github_fetcher=fetcher,
    ).current(locale="en")

    assert feed.update_available is True
    assert feed.release_url is None


@pytest.mark.asyncio
async def test_c1_eg22_pos_allows_only_newapi_admin_service_identity():
    from novelvideo.newapi_provisioner import (
        NewApiAdminServiceIdentity,
        run_newapi_admin_operation,
    )

    network_calls = []
    operations = FakeOperations()

    result = await run_newapi_admin_operation(
        identity=NewApiAdminServiceIdentity(
            credential_id="svc-newapi-admin",
            credential_version=2,
            admin_base_url="http://new-api:3000",
        ),
        admin_base_url="http://new-api:3000",
        capability="gateway.provisioning.setup",
        business_task_id="setup-default",
        request={"action": "setup", "channel": "default"},
        operations=operations,
        invoke=lambda: network_calls.append("called") or {"ok": True},
    )

    assert result == {"ok": True}
    assert network_calls == ["called"]
    assert len(operations.claims) == 1
    # 这条路径没有上游作业号、也没有结果引用（HandleKind.NONE），两列就该是 NULL。
    # 原先它断言的是占位串 `"service-operation-completed"` ——把「能骗过非空检查的
    # 字符串」钉成了期望值，DB 约束、写入点、用例三方互相背书，见 OI-49。
    assert operations.claims[0].handle_kind is HandleKind.NONE
    assert operations.accepted[0]["provider_job_id"] is None
    assert operations.completed[0]["result_ref"] is None


@pytest.mark.asyncio
async def test_c1_eg22_org_deny_rejects_org_gateway_key_before_claim_or_network():
    from novelvideo.newapi_provisioner import (
        NewApiAdminServiceIdentity,
        ServiceControlEgressDenied,
        run_newapi_admin_operation,
    )

    operations = FakeOperations()
    network_calls = []
    with pytest.raises(ServiceControlEgressDenied) as exc_info:
        await run_newapi_admin_operation(
            identity=NewApiAdminServiceIdentity(
                credential_id="svc-newapi-admin",
                credential_version=2,
                admin_base_url="http://new-api:3000",
            ),
            admin_base_url="http://new-api:3000",
            capability="gateway.provisioning.setup",
            business_task_id="setup-default",
            request={"action": "setup"},
            operations=operations,
            invoke=lambda: network_calls.append("called"),
            context=_org_context(),
        )

    assert exc_info.value.code == "ORG_SERVICE_EGRESS_DENIED"
    assert operations.claims == []
    assert network_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [_org_context(), _platform_context()])
async def test_c1_eg22_facade_only_denies_every_trusted_request_context(context):
    from novelvideo.newapi_provisioner import (
        NewApiAdminServiceIdentity,
        ServiceControlEgressDenied,
        run_newapi_admin_operation,
    )

    operations = FakeOperations()
    network_calls = []
    with pytest.raises(ServiceControlEgressDenied):
        await run_newapi_admin_operation(
            identity=NewApiAdminServiceIdentity(
                credential_id="svc-newapi-admin",
                credential_version=1,
                admin_base_url="http://new-api:3000",
            ),
            admin_base_url="http://new-api:3000",
            capability="gateway.provisioning.setup",
            business_task_id="setup-default",
            request={"action": "setup"},
            operations=operations,
            invoke=lambda: network_calls.append("called"),
            context=context,
        )

    assert operations.claims == []
    assert network_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "capability"),
    [
        (object(), "gateway.provisioning.setup"),
        (None, "model.generate"),
    ],
)
async def test_c1_eg22_facade_only_denies_wrong_identity_or_capability(
    identity, capability
):
    from novelvideo.newapi_provisioner import (
        ServiceControlEgressDenied,
        run_newapi_admin_operation,
    )

    operations = FakeOperations()
    network_calls = []
    with pytest.raises(ServiceControlEgressDenied):
        await run_newapi_admin_operation(
            identity=identity,
            admin_base_url="http://new-api:3000",
            capability=capability,
            business_task_id="setup-default",
            request={"action": "setup"},
            operations=operations,
            invoke=lambda: network_calls.append("called"),
        )

    assert operations.claims == []
    assert network_calls == []


@pytest.mark.asyncio
async def test_c1_eg22_facade_only_denies_missing_authority_before_network():
    from novelvideo.newapi_provisioner import (
        NewApiAdminServiceIdentity,
        ServiceControlEgressDenied,
        run_newapi_admin_operation,
    )

    network_calls = []
    with pytest.raises(ServiceControlEgressDenied):
        await run_newapi_admin_operation(
            identity=NewApiAdminServiceIdentity(
                credential_id="svc-newapi-admin",
                credential_version=1,
                admin_base_url="http://new-api:3000",
            ),
            admin_base_url="http://new-api:3000",
            capability="gateway.provisioning.setup",
            business_task_id="setup-default",
            request={"action": "setup"},
            operations=None,
            invoke=lambda: network_calls.append("called"),
        )

    assert network_calls == []


def test_c1_eg22_org_deny_applies_at_model_gateway_route_boundary(monkeypatch):
    from novelvideo.api.routes import model_gateway
    from novelvideo.newapi_provisioner import ServiceControlEgressDenied

    monkeypatch.setattr(
        model_gateway, "require_legacy_local_service_operation", lambda: None
    )
    monkeypatch.setattr(model_gateway, "is_ce_effective", lambda: False)
    monkeypatch.setattr(
        model_gateway,
        "require_provisioner_enabled",
        lambda: (_ for _ in ()).throw(AssertionError("provisioner must stay at zero")),
    )

    with pytest.raises(ServiceControlEgressDenied) as exc_info:
        model_gateway.require_ce_gateway_management()

    assert exc_info.value.code == "ORG_SERVICE_EGRESS_DENIED"


def test_c1_eg22_provisioner_denies_org_context_before_network(monkeypatch):
    from novelvideo import newapi_provisioner
    from novelvideo.newapi_provisioner import (
        NewApiProvisionerConfig,
        ServiceControlEgressDenied,
    )

    monkeypatch.setattr(
        newapi_provisioner,
        "get_newapi_setup_status",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("network must stay at zero")
        ),
    )
    cfg = NewApiProvisionerConfig(
        admin_base_url="http://new-api:3000",
        sql_dsn="local",
        sqlite_path="/not-used",
        admin_username="root",
        init_timeout_ms=1,
        relay_token_name="relay",
    )

    with pytest.raises(ServiceControlEgressDenied):
        newapi_provisioner.ensure_newapi_setup(cfg, context=_org_context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [OperationState.ACCEPTED, OperationState.COMPLETED, OperationState.UNKNOWN],
)
async def test_service_operations_never_replay_non_winning_claims(state):
    from novelvideo.newapi_provisioner import (
        NewApiAdminServiceIdentity,
        ServiceOperationNotReplayable,
        run_newapi_admin_operation,
    )

    operations = FakeOperations(won=False, state=state)
    calls = []
    with pytest.raises(ServiceOperationNotReplayable):
        await run_newapi_admin_operation(
            identity=NewApiAdminServiceIdentity(
                credential_id="svc-newapi-admin",
                credential_version=1,
                admin_base_url="http://new-api:3000",
            ),
            admin_base_url="http://new-api:3000",
            capability="gateway.provisioning.setup",
            business_task_id="setup-default",
            request={"action": "setup"},
            operations=operations,
            invoke=lambda: calls.append("called"),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_service_operation_failure_is_stable_and_secret_free():
    from novelvideo.newapi_provisioner import (
        NewApiAdminServiceIdentity,
        ServiceInvocationFailed,
        run_newapi_admin_operation,
    )

    operations = FakeOperations()

    def fail():
        raise RuntimeError("postgres://admin:secret@example/db object-canary")

    with pytest.raises(ServiceInvocationFailed) as exc_info:
        await run_newapi_admin_operation(
            identity=NewApiAdminServiceIdentity(
                credential_id="svc-newapi-admin",
                credential_version=1,
                admin_base_url="http://new-api:3000",
            ),
            admin_base_url="http://new-api:3000",
            capability="gateway.provisioning.setup",
            business_task_id="setup-default",
            request={"action": "setup"},
            operations=operations,
            invoke=fail,
        )

    assert str(exc_info.value) == "service operation failed"
    assert len(operations.unknown) == 1
