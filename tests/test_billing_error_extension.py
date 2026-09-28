from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo.api.routes import chat as chat_routes
from novelvideo.api.routes import freezone as freezone_routes
from novelvideo.chat.store import ChatScope
from novelvideo.shared.billing_errors import (
    BILLING_RULE_NOT_CONFIGURED_CODE,
    BILLING_RULE_NOT_CONFIGURED_MESSAGE,
    INSUFFICIENT_CREDITS_CODE,
    INSUFFICIENT_CREDITS_MESSAGE,
    BillingError,
    BillingRuleNotConfiguredError,
    InsufficientCreditsError,
    billing_error_payload,
    billing_rule_not_configured_payload,
    is_fatal_billing_error,
    is_insufficient_credits_error,
)
from novelvideo.task_backend import run_core


class _ForeignBillingError(BillingError):
    error_code = "FAKE_BILLING_ERROR"
    http_status = 402
    user_message = "模拟的 typed 计费错误"

    def details(self) -> dict:
        return {"probe": "task-c"}


def test_http_handler_surfaces_foreign_billing_error_verbatim() -> None:
    from novelvideo.api.app import create_app

    app = create_app()

    @app.get("/__foreign-billing-error")
    async def foreign_billing_error() -> None:
        raise _ForeignBillingError()

    response = TestClient(app).get("/__foreign-billing-error")

    assert response.status_code == 402
    assert response.json() == {
        "ok": False,
        "error": _ForeignBillingError.user_message,
        "data": {
            "error_code": _ForeignBillingError.error_code,
            "message": _ForeignBillingError.user_message,
            "probe": "task-c",
        },
    }


def test_http_handler_keeps_personal_insufficient_credit_response() -> None:
    from novelvideo.api.app import create_app

    app = create_app()

    @app.get("/__personal-billing-error")
    async def personal_billing_error() -> None:
        raise InsufficientCreditsError(user_id="usr_1", cost=40, balance=8)

    response = TestClient(app).get("/__personal-billing-error")

    assert response.status_code == 402
    assert response.json() == {
        "ok": False,
        "error": INSUFFICIENT_CREDITS_MESSAGE,
        "data": {
            "error_code": INSUFFICIENT_CREDITS_CODE,
            "message": INSUFFICIENT_CREDITS_MESSAGE,
            "user_id": "usr_1",
            "required": 40,
            "balance": 8,
        },
    }


def test_task_failure_mapper_surfaces_foreign_billing_error_verbatim() -> None:
    error, metadata, handled = run_core._project_task_failure_for_exception(
        _ForeignBillingError()
    )

    assert handled is True
    assert error == _ForeignBillingError.user_message
    assert metadata == {
        "error_code": _ForeignBillingError.error_code,
        "message": _ForeignBillingError.user_message,
        "probe": "task-c",
    }


def test_task_failure_mapper_handles_billing_rule_not_configured(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=run_core.logger.name):
        error, metadata, handled = run_core._project_task_failure_for_exception(
            BillingRuleNotConfiguredError(kind="model", key="doubao/seedance")
        )

    assert handled is True
    assert error == BILLING_RULE_NOT_CONFIGURED_MESSAGE
    # K56: intentional contract change — this whole-packet assertion was 2 fields
    # (Task C) and is tightened to 4 here. Never relax `==` to `in`/`.get()`.
    assert metadata == {
        "error_code": BILLING_RULE_NOT_CONFIGURED_CODE,
        "message": BILLING_RULE_NOT_CONFIGURED_MESSAGE,
        "billing_kind": "model",
        "billing_key": "doubao/seedance",
    }
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        "typed billing failure in project task: "
        "billing rule is not configured for model:doubao/seedance"
    ]


def test_billing_rule_not_configured_payload_is_identical_across_constructors() -> None:
    """K56: the generic and the dedicated constructor must produce the same packet.

    Whole-packet equality is the definition of "same shape" here; asserting
    field-by-field would let a future divergence drift in unnoticed.
    """
    exc = BillingRuleNotConfiguredError(kind="model", key="doubao/seedance")

    assert billing_error_payload(exc) == billing_rule_not_configured_payload(exc)
    assert billing_error_payload(exc) == {
        "error_code": BILLING_RULE_NOT_CONFIGURED_CODE,
        "message": BILLING_RULE_NOT_CONFIGURED_MESSAGE,
        "billing_kind": "model",
        "billing_key": "doubao/seedance",
    }


def test_billing_rule_not_configured_r2_packet_grew_from_two_to_four_fields() -> None:
    """K56: R2's triple no longer carries a thinner packet than R1/R3/R4."""
    exc = BillingRuleNotConfiguredError(kind="model", key="doubao/seedance")

    _error, metadata, _handled = run_core._project_task_failure_for_exception(exc)

    assert len(metadata) == 4
    assert set(metadata) == {
        "error_code",
        "message",
        "billing_kind",
        "billing_key",
    }


def test_insufficient_credits_payload_shape_is_untouched_by_k56() -> None:
    """K56 negative guard: the sibling class must not grow `details()` fields."""
    exc = InsufficientCreditsError(user_id="user-k56", cost=10, balance=1)

    assert billing_error_payload(exc) == {
        "error_code": INSUFFICIENT_CREDITS_CODE,
        "message": INSUFFICIENT_CREDITS_MESSAGE,
    }


@pytest.mark.asyncio
async def test_newapi_image_call_reraises_billing_rule_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.generators import nanobanana_grid

    class FakeUsageMeter:
        async def reserve_current_model_call_credit(self, **_kwargs):
            raise BillingRuleNotConfiguredError(
                kind="model", key="doubao/seedance"
            )

    monkeypatch.setattr(nanobanana_grid, "get_usage_meter", lambda: FakeUsageMeter())

    with pytest.raises(BillingRuleNotConfiguredError):
        await nanobanana_grid._call_newapi_image_api(
            api_key="newapi-token",
            model="doubao/seedance",
            prompt="portrait prompt",
            base_url="http://newapi.test/v1",
        )

    assert (
        is_insufficient_credits_error(
            BillingRuleNotConfiguredError(kind="model", key="x")
        )
        is False
    )


def test_chat_websocket_surfaces_foreign_billing_error_verbatim(monkeypatch) -> None:
    scope = ChatScope(kind="home")

    async def authenticate(_websocket):
        return {"username": "local"}

    async def send_scope_changed(_websocket, _user, _username, _scope):
        return scope

    async def reauthenticate(_websocket, *, original_user, **_kwargs):
        return original_user

    async def allow_access(**_kwargs):
        return None

    async def fail_turn(**_kwargs):
        raise _ForeignBillingError()

    monkeypatch.setattr(chat_routes, "_authenticate_ws", authenticate)
    monkeypatch.setattr(chat_routes, "_scope_for_authenticated_user", lambda _user: scope)
    monkeypatch.setattr(chat_routes, "_send_scope_changed", send_scope_changed)
    monkeypatch.setattr(chat_routes, "_reauthenticate_ws_event", reauthenticate)
    monkeypatch.setattr(chat_routes, "_require_ai_assistant_access", allow_access)
    monkeypatch.setattr(chat_routes, "_stream_home_turn", fail_turn)

    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api/v1")
    with TestClient(app).websocket_connect("/api/v1/chat/ws") as websocket:
        websocket.send_json(
            {
                "type": "chat.message",
                "scope": {"kind": "home"},
                "text": "probe",
                "turn_id": "turn-task-c",
            }
        )
        frame = websocket.receive_json()

    assert frame == {
        "type": "error",
        "turn_id": "turn-task-c",
        "message": _ForeignBillingError.user_message,
        "data": {
            "error_code": _ForeignBillingError.error_code,
            "message": _ForeignBillingError.user_message,
            "probe": "task-c",
        },
    }


def test_freezone_task_start_reraises_foreign_billing_error() -> None:
    billing_error = _ForeignBillingError()
    wrapper = RuntimeError("failed to start task")
    wrapper.__cause__ = billing_error

    with pytest.raises(_ForeignBillingError) as exc_info:
        freezone_routes._handle_task_start_runtime_error("start failed", wrapper)

    assert exc_info.value is billing_error


def test_fatal_predicate_does_not_widen_insufficient_credit_predicate() -> None:
    billing_error = _ForeignBillingError()

    assert is_fatal_billing_error(billing_error) is True
    assert is_insufficient_credits_error(billing_error) is False
