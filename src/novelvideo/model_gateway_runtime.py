"""Request/runner scoping of the trusted egress context for model calls."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, TypeVar

from novelvideo.egress_context import (
    TRUSTED_EGRESS_CONTEXT_KEY,
    TrustedEgressContext,
    TrustedRunnerEnvelope,
)
from novelvideo.ports import get_egress_operation_port, get_model_credentials
from novelvideo.ports.authz import AdmissionContext
from novelvideo.ports.egress_operations import (
    HandleKind,
    OperationSpec,
)
from novelvideo.ports.model_credentials import RequestCredential

_T = TypeVar("_T")
_MODEL_GATEWAY_CONTEXT: ContextVar[TrustedEgressContext | None] = ContextVar(
    "novelvideo_model_gateway_context",
    default=None,
)


@dataclass
class _SubmitLedger:
    """Occurrence counters for one request scope.

    Held in a ContextVar as a mutable object rather than as a plain int:
    asyncio snapshots the context for every task it creates, so an int written
    back with .set() lands in that task's private copy and is invisible to its
    siblings and to the parent. cognee fans out with create_task on this exact
    path, so a counter kept that way never advances and every concurrent
    submit claims the same operation key. cognee/concurrency.py:47-70 works
    because it shares an object by reference; this does the same.

    The lock guards no path that exists today — the read-modify-write below
    contains no await, and the threads that do reach this module
    (loop.run_in_executor) carry no context and are denied before they get
    here. It is here because, unlike _PipelineLimits which counts for logs, a
    torn read costs a duplicate paid egress or a spurious conflict that kills
    a user's import; and asyncio.to_thread *does* copy the context, so the
    invariant would die silently the day someone introduces it.
    """

    _occurrences: dict[tuple[str, str], int] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def next_occurrence(self, capability: str, request_digest: str) -> int:
        with self._guard:
            key = (capability, request_digest)
            occurrence = self._occurrences.get(key, 0) + 1
            self._occurrences[key] = occurrence
            return occurrence


_MODEL_GATEWAY_SUBMIT_LEDGER: ContextVar[_SubmitLedger | None] = ContextVar(
    "novelvideo_model_gateway_submit_ledger",
    default=None,
)

_EGRESS_ERROR_MESSAGES = {
    "EGRESS_OPERATION_REPLAYED": "egress operation cannot be replayed",
    "ORG_EGRESS_DENIED": "organization direct egress is denied",
}


class ModelGatewayEgressError(RuntimeError):
    """Stable, secret-free model egress failure."""

    def __init__(self, code: str) -> None:
        super().__init__(_EGRESS_ERROR_MESSAGES.get(code, "model egress failed"))
        self.code = code


def current_model_gateway_context() -> TrustedEgressContext | None:
    """Return only the trusted identity bound to the current request."""

    return _MODEL_GATEWAY_CONTEXT.get()


def model_gateway_output_retries(platform_retries: int) -> int:
    """Disable PydanticAI retry submits for organization operations."""

    context = current_model_gateway_context()
    return 0 if context is not None and context.is_organization else platform_retries


@contextmanager
def model_gateway_request_scope(
    context: TrustedEgressContext | None,
) -> Iterator[None]:
    """Bind trusted identity without resolving or retaining a plaintext key."""

    if context is not None and type(context) is not TrustedEgressContext:
        raise TypeError("context must be a TrustedEgressContext or None")
    token = _MODEL_GATEWAY_CONTEXT.set(context)
    ledger_token = _MODEL_GATEWAY_SUBMIT_LEDGER.set(_SubmitLedger())
    try:
        yield
    finally:
        _MODEL_GATEWAY_SUBMIT_LEDGER.reset(ledger_token)
        _MODEL_GATEWAY_CONTEXT.reset(token)


@contextmanager
def model_gateway_scope_for_runner(envelope: dict[str, Any]) -> Iterator[None]:
    """Bind identity only when it came from the verified runner envelope."""

    if type(envelope) is TrustedRunnerEnvelope:
        context = envelope.get(TRUSTED_EGRESS_CONTEXT_KEY)
        if type(context) is not TrustedEgressContext:
            raise TypeError("trusted runner envelope requires trusted egress context")
    elif type(envelope) is dict and TRUSTED_EGRESS_CONTEXT_KEY not in envelope:
        context = None
    else:
        raise TypeError("untrusted runner envelope cannot provide egress context")
    with model_gateway_request_scope(context):
        yield


def next_model_gateway_business_task_id(
    capability: str,
    *,
    request_digest: str,
) -> str:
    """Name one submit so the durable ledger can tell submits apart.

    The identity is content-derived because the call sites that use this
    helper have no business coordinates to name a request by — cognee's unit
    of work is an arbitrary text batch, and TrustedEgressContext carries no
    episode/beat/scope. Where a call site *does* have a real identity it
    should pass that instead; see generators/video_generator.py.

    Two byte-identical payloads in one envelope are still two side effects
    that each need a result, so an occurrence ordinal separates them. It is
    counted per (capability, digest), which makes the identity depend on the
    multiset of requests the envelope issues rather than on the order in
    which cognee's tasks happen to arrive here — so a redelivered envelope
    reproduces the same keys and is refused, per
    docs/b2b-org-tenant/p0-gray-freeze.md:108.

    request_digest is keyword-only so that a stale positional call fails
    loudly instead of quietly digesting the capability string.
    """

    context = current_model_gateway_context()
    ledger = _MODEL_GATEWAY_SUBMIT_LEDGER.get()
    if context is None or ledger is None:
        raise ModelGatewayEgressError("ORG_EGRESS_DENIED")
    occurrence = ledger.next_occurrence(capability, request_digest)
    return f"{context.envelope_id}:{capability}:{request_digest}:{occurrence:06d}"


def _organization_admission(context: TrustedEgressContext) -> AdmissionContext:
    return AdmissionContext(
        requester_user_id=context.requester_user_id,
        billing_principal=context.billing_principal,
        credential=context.credential,
        admission_id=context.admission_id,
        root_task_id=context.root_task_id,
        admitted_at=context.admitted_at,
        membership_id=context.membership_id,
        authz_version=context.authz_version,
    )


async def execute_organization_gateway_request(
    *,
    capability: str,
    business_task_id: str,
    request_digest: str,
    submit: Callable[[RequestCredential], Awaitable[_T]],
) -> _T:
    """Claim, resolve and perform exactly one organization transport submit."""

    context = current_model_gateway_context()
    if context is None or not context.is_organization:
        raise ModelGatewayEgressError("ORG_EGRESS_DENIED")
    for value, field_name in (
        (capability, "capability"),
        (business_task_id, "business_task_id"),
        (request_digest, "request_digest"),
    ):
        if type(value) is not str or not value.strip():
            raise ValueError(f"{field_name} is required")

    operation_port = get_egress_operation_port()
    claim = await operation_port.claim(
        spec=OperationSpec(
            organization_id=context.billing_principal.id,
            project_id=context.project_id,
            root_task_id=context.root_task_id,
            business_task_id=business_task_id,
            capability=capability,
            credential_id=context.credential.credential_id,
            credential_version=context.credential.key_version,
            request_digest=request_digest,
            handle_kind=HandleKind.NONE,
        )
    )
    if not claim.won:
        raise ModelGatewayEgressError("EGRESS_OPERATION_REPLAYED")

    operation = claim.operation
    transition_token = claim.transition_token
    assert transition_token is not None
    try:
        credential = await get_model_credentials().resolve(
            _organization_admission(context)
        )
    except BaseException:
        await operation_port.mark_rejected_before_submit(
            operation_id=operation.operation_id,
            transition_token=transition_token,
            expected_version=operation.version,
        )
        raise

    try:
        result = await submit(credential)
    except BaseException:
        await operation_port.mark_unknown(
            operation_id=operation.operation_id,
            transition_token=transition_token,
            expected_version=operation.version,
        )
        raise

    # 这里没有上游作业号可留：网关同步提交，返回体不带 provider 侧的作业标识。
    # 原先拿本行自己的 `operation_id` 填两列，只是为了骗过旧 CHECK 的「非空」判据，
    # 读上去像「留了上游句柄」，实则是自引用。HandleKind.NONE 把这件事写在行上。
    accepted = await operation_port.mark_accepted(
        operation_id=operation.operation_id,
        transition_token=transition_token,
        expected_version=operation.version,
        provider_job_id=None,
    )
    await operation_port.mark_completed(
        operation_id=operation.operation_id,
        transition_token=transition_token,
        expected_version=accepted.version,
        result_ref=None,
    )
    return result


@asynccontextmanager
async def execute_organization_gateway_stream(
    *,
    capability: str,
    business_task_id: str,
    request_digest: str,
    stream_factory: Callable[[RequestCredential], Any],
):
    """Run one claimed organization stream and transition it after consumption."""

    context = current_model_gateway_context()
    if context is None or not context.is_organization:
        raise ModelGatewayEgressError("ORG_EGRESS_DENIED")
    for value, field_name in (
        (capability, "capability"),
        (business_task_id, "business_task_id"),
        (request_digest, "request_digest"),
    ):
        if type(value) is not str or not value.strip():
            raise ValueError(f"{field_name} is required")

    operation_port = get_egress_operation_port()
    claim = await operation_port.claim(
        spec=OperationSpec(
            organization_id=context.billing_principal.id,
            project_id=context.project_id,
            root_task_id=context.root_task_id,
            business_task_id=business_task_id,
            capability=capability,
            credential_id=context.credential.credential_id,
            credential_version=context.credential.key_version,
            request_digest=request_digest,
            handle_kind=HandleKind.NONE,
        )
    )
    if not claim.won:
        raise ModelGatewayEgressError("EGRESS_OPERATION_REPLAYED")

    operation = claim.operation
    transition_token = claim.transition_token
    assert transition_token is not None
    try:
        credential = await get_model_credentials().resolve(
            _organization_admission(context)
        )
    except BaseException:
        await operation_port.mark_rejected_before_submit(
            operation_id=operation.operation_id,
            transition_token=transition_token,
            expected_version=operation.version,
        )
        raise

    accepted = None
    try:
        async with stream_factory(credential) as response:
            # 流式同样没有上游作业号，见上面同函数族的说明。
            accepted = await operation_port.mark_accepted(
                operation_id=operation.operation_id,
                transition_token=transition_token,
                expected_version=operation.version,
                provider_job_id=None,
            )
            try:
                yield response
            except BaseException:
                await operation_port.mark_unknown(
                    operation_id=operation.operation_id,
                    transition_token=transition_token,
                    expected_version=accepted.version,
                )
                raise
    except BaseException:
        if accepted is None:
            await operation_port.mark_unknown(
                operation_id=operation.operation_id,
                transition_token=transition_token,
                expected_version=operation.version,
            )
        raise

    await operation_port.mark_completed(
        operation_id=operation.operation_id,
        transition_token=transition_token,
        expected_version=accepted.version,
        result_ref=None,
    )
