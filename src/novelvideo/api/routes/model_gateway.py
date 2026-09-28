"""Model gateway configuration endpoints for CE."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from novelvideo import config as app_config
from novelvideo.model_gateway_settings import (
    MODE_CUSTOM,
    MODE_OFFICIAL,
    MODE_HYBRID,
    build_model_gateway_status,
    normalize_relay_base_url,
    normalize_api_key,
    save_official_newapi_key,
    save_custom_newapi_gateway,
    save_newapi_database_config,
    save_newapi_embedding_model_config,
    get_newapi_embedding_model_config,
    save_newapi_provider_channels,
    get_newapi_provider_channel,
    set_model_gateway_mode,
)
from novelvideo.model_gateway_runtime import refresh_model_gateway_runtime
from novelvideo.service_operation_gate import (
    ServiceOperationExcluded,
    require_legacy_local_service_operation,
)
from novelvideo.shared.runtime_env import is_ce_effective
from novelvideo.newapi_provisioner import (
    build_channel_payload,
    build_provisioner_status,
    create_or_reuse_relay_token,
    ensure_newapi_setup,
    ensure_admin_access_token,
    get_provisioner_config,
    list_channel_types,
    mask_token,
    NewApiSetupCredentials,
    ServiceControlEgressDenied,
    require_provisioner_enabled,
    upsert_channel,
    update_provider_channel_credentials,
)

router = APIRouter(prefix="/model-gateway")


def require_ce_gateway_management() -> None:
    """Reject CE-local gateway mutations from an EE-composed process."""
    try:
        require_legacy_local_service_operation()
    except ServiceOperationExcluded as exc:
        raise PermissionError(exc.code) from None
    if not is_ce_effective():
        raise ServiceControlEgressDenied()
    require_provisioner_enabled()


class OfficialGatewayBody(BaseModel):
    new_api_api_key: str = Field(alias="newApiApiKey")


class NewApiDatabaseBody(BaseModel):
    sql_dsn: str | None = Field(default=None, alias="sqlDsn")
    sqlite_path: str | None = Field(default=None, alias="sqlitePath")
    admin_username: str | None = Field(default=None, alias="adminUsername")


class NewApiInitBody(BaseModel):
    new_api_base_url: str | None = Field(default=None, alias="newApiBaseUrl")
    database: NewApiDatabaseBody | None = None
    setup_username: str | None = Field(default=None, alias="setupUsername")
    setup_password: str | None = Field(default=None, alias="setupPassword")
    setup_confirm_password: str | None = Field(
        default=None, alias="setupConfirmPassword"
    )
    token_name: str | None = Field(default=None, alias="tokenName")
    group: str = "default"
    unlimited_quota: bool = Field(default=True, alias="unlimitedQuota")
    remain_quota: int = Field(default=0, alias="remainQuota")
    expired_time: int = Field(default=-1, alias="expiredTime")
    reuse_existing: bool = Field(default=True, alias="reuseExisting")


class CreateChannelBody(BaseModel):
    new_api_base_url: str | None = Field(default=None, alias="newApiBaseUrl")
    database: NewApiDatabaseBody | None = None
    provider: str = "ali"
    type: int | None = None
    name: str | None = None
    upstream_key: str | None = Field(default=None, alias="upstreamKey")
    model_mapping: dict[str, str] = Field(alias="modelMapping")
    group: str = "default"
    priority: int | None = None
    weight: int = 0
    base_url: str | None = Field(default=None, alias="baseUrl")
    test_model: str | None = Field(default=None, alias="testModel")
    settings: dict[str, Any] = Field(default_factory=dict)


class ChannelSpec(BaseModel):
    provider: str = "ali"
    type: int | None = None
    name: str | None = None
    upstream_key: str | None = Field(default=None, alias="upstreamKey")
    model_mapping: dict[str, str] = Field(alias="modelMapping")
    group: str = "default"
    priority: int | None = None
    weight: int = 0
    base_url: str | None = Field(default=None, alias="baseUrl")
    test_model: str | None = Field(default=None, alias="testModel")
    settings: dict[str, Any] = Field(default_factory=dict)


class CreateChannelsBatchBody(BaseModel):
    new_api_base_url: str | None = Field(default=None, alias="newApiBaseUrl")
    database: NewApiDatabaseBody | None = None
    channels: list[ChannelSpec] = Field(min_length=1)


class ProviderChannelConfigBody(BaseModel):
    provider: str
    type: int | None = None
    upstream_key: str | None = Field(default=None, alias="upstreamKey")
    base_url: str | None = Field(default=None, alias="baseUrl")
    priority: int | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class SaveProviderChannelsBody(BaseModel):
    channels: list[ProviderChannelConfigBody] = Field(default_factory=list)
    preserve_unmentioned: bool = Field(default=False, alias="preserveUnmentioned")


class SyncProviderChannelBody(BaseModel):
    new_api_base_url: str | None = Field(default=None, alias="newApiBaseUrl")
    database: NewApiDatabaseBody | None = None
    provider: str
    upstream_key: str | None = Field(default=None, alias="upstreamKey")
    base_url: str | None = Field(default=None, alias="baseUrl")


class SaveEmbeddingModelBody(BaseModel):
    new_api_base_url: str | None = Field(default=None, alias="newApiBaseUrl")
    database: NewApiDatabaseBody | None = None
    provider: str
    upstream_model: str = Field(alias="upstreamModel")
    dimension: int
    batch_size: int | None = Field(default=None, alias="batchSize")
    send_dimensions: bool = Field(default=True, alias="sendDimensions")


def _permission_error(exc: PermissionError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


def _get_provisioner_config_from_request(
    new_api_base_url: str | None,
    database: NewApiDatabaseBody | None,
):
    return get_provisioner_config(
        new_api_base_url,
        sql_dsn=database.sql_dsn if database else None,
        sqlite_path=database.sqlite_path if database else None,
        admin_username=database.admin_username if database else None,
    )


def _save_request_database_config(
    cfg,
    database: NewApiDatabaseBody | None,
) -> None:
    if database is None:
        return
    save_newapi_database_config(
        sql_dsn=cfg.sql_dsn,
        sqlite_path=cfg.sqlite_path,
        admin_username=cfg.admin_username,
    )


def _setup_credentials_from_request(body: NewApiInitBody) -> NewApiSetupCredentials:
    username = (body.setup_username or "").strip()
    if not username and body.database and body.database.admin_username:
        username = body.database.admin_username.strip()
    return NewApiSetupCredentials(
        username=username,
        password=body.setup_password or "",
        confirm_password=body.setup_confirm_password or "",
        self_use_mode_enabled=True,
        demo_site_enabled=False,
    )


def _build_channel_payload_from_spec(
    spec: ChannelSpec | CreateChannelBody,
) -> dict[str, Any]:
    saved_channel = get_newapi_provider_channel(spec.provider) or {}
    return build_channel_payload(
        provider=spec.provider,
        channel_type=spec.type or int(saved_channel.get("type") or 0) or None,
        name=spec.name,
        upstream_key=spec.upstream_key or saved_channel.get("upstreamKey", ""),
        model_mapping=spec.model_mapping,
        group=spec.group,
        priority=(
            int(saved_channel.get("priority") or 0)
            if spec.priority is None
            else spec.priority
        ),
        weight=spec.weight,
        base_url=spec.base_url or saved_channel.get("baseUrl", ""),
        test_model=spec.test_model,
        other_settings=spec.settings or saved_channel.get("settings", {}),
    )


def _build_embedding_model_channel_spec(
    body: SaveEmbeddingModelBody,
) -> tuple[ChannelSpec, dict[str, Any]]:
    provider = str(body.provider or "").strip().lower()
    upstream_model = str(body.upstream_model or "").strip()
    dimension = int(body.dimension)
    batch_size = int(body.batch_size or 0)
    if not provider:
        raise ValueError("provider is required for embedding model")
    if not upstream_model:
        raise ValueError("upstreamModel is required for embedding model")
    if dimension <= 0:
        raise ValueError("dimension must be positive")
    if body.batch_size is not None and batch_size <= 0:
        raise ValueError("batchSize must be positive")
    normalized = {
        "provider": provider,
        "upstreamModel": upstream_model,
        "dimension": dimension,
        # Kept in the response/config schema for compatibility with older
        # clients. Runtime request behavior is controlled by the internal
        # EmbeddingModelSpec, not by this user-supplied field.
        "sendDimensions": True,
        "internalModel": "DC-cognee-embedding",
    }
    if batch_size > 0:
        normalized["batchSize"] = batch_size
    return (
        ChannelSpec(
            provider=provider,
            modelMapping={"DC-cognee-embedding": upstream_model},
        ),
        normalized,
    )


def _mask_sent_channel_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "channel": {
            **payload["channel"],
            "key": mask_token(payload["channel"]["key"]),
        },
    }


def _provisioner_status() -> dict[str, Any]:
    if is_ce_effective():
        return build_provisioner_status()
    return {
        "enabled": False,
        "adminBaseUrl": "",
        "dbConfigured": False,
        "database": {
            "configured": False,
            "available": False,
            "source": "unavailable",
        },
        "adminUsername": "",
        "relayTokenName": "",
        "providers": {},
        "providerChannels": [],
        "embeddingModel": {},
        "relayBaseUrl": "",
    }


@router.get("/config")
async def get_model_gateway_config() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            **build_model_gateway_status(
                official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
                official_api_key=app_config.NEWAPI_API_KEY,
            ),
            "provisioner": _provisioner_status(),
        },
    }


@router.get("/engines")
async def get_engines_status() -> dict[str, Any]:
    """Read-only reachability of the local/remote engines, for the settings UI."""
    from novelvideo.media_catalog import engines_status

    return {
        "ok": True,
        "data": {
            "textEngine": app_config.get_text_engine(),
            "engines": await engines_status(),
        },
    }


@router.post("/official/enable")
async def enable_official_gateway() -> dict[str, Any]:
    try:
        require_ce_gateway_management()
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    status = build_model_gateway_status(
        official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
        official_api_key=app_config.NEWAPI_API_KEY,
    )
    if not status["official"]["configured"]:
        raise HTTPException(
            status_code=400,
            detail="official NewAPI gateway is not configured",
        )
    set_model_gateway_mode(MODE_OFFICIAL)
    runtime = refresh_model_gateway_runtime()
    return {
        "ok": True,
        "data": build_model_gateway_status(
            official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
            official_api_key=app_config.NEWAPI_API_KEY,
        ),
        "runtime": runtime,
    }


@router.post("/custom/enable")
async def enable_custom_gateway() -> dict[str, Any]:
    try:
        require_ce_gateway_management()
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    status = build_model_gateway_status(
        official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
        official_api_key=app_config.NEWAPI_API_KEY,
    )
    if not status["custom"]["configured"]:
        raise HTTPException(
            status_code=400, detail="local NewAPI gateway is not configured"
        )
    set_model_gateway_mode(MODE_CUSTOM)
    runtime = refresh_model_gateway_runtime()
    return {
        "ok": True,
        "data": build_model_gateway_status(
            official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
            official_api_key=app_config.NEWAPI_API_KEY,
        ),
        "runtime": runtime,
    }


@router.post("/hybrid/enable")
async def enable_hybrid_gateway() -> dict[str, Any]:
    try:
        require_ce_gateway_management()
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    status = build_model_gateway_status(
        official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
        official_api_key=app_config.NEWAPI_API_KEY,
    )
    if not status["official"]["configured"] or not status["custom"]["configured"]:
        raise HTTPException(
            status_code=400,
            detail="local and official NewAPI gateways must both be configured",
        )
    set_model_gateway_mode(MODE_HYBRID)
    runtime = refresh_model_gateway_runtime()
    return {
        "ok": True,
        "data": build_model_gateway_status(
            official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
            official_api_key=app_config.NEWAPI_API_KEY,
        ),
        "runtime": runtime,
    }


@router.post("/official/config")
async def save_official_gateway_config(body: OfficialGatewayBody) -> dict[str, Any]:
    try:
        require_ce_gateway_management()
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    api_key = normalize_api_key(body.new_api_api_key)
    if not api_key:
        raise HTTPException(status_code=400, detail="newApiApiKey is required")
    save_official_newapi_key(api_key=api_key, activate=True)
    runtime = refresh_model_gateway_runtime()
    return {
        "ok": True,
        "data": build_model_gateway_status(
            official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
            official_api_key=app_config.NEWAPI_API_KEY,
        ),
        "runtime": runtime,
    }


@router.post("/custom/newapi/init")
async def init_custom_newapi(body: NewApiInitBody = NewApiInitBody()) -> dict[str, Any]:
    try:
        require_ce_gateway_management()
        cfg = _get_provisioner_config_from_request(body.new_api_base_url, body.database)
        setup_status = ensure_newapi_setup(cfg, _setup_credentials_from_request(body))
        admin = ensure_admin_access_token(cfg)
        token_name = (
            body.token_name or cfg.relay_token_name
        ).strip() or cfg.relay_token_name
        token = create_or_reuse_relay_token(
            cfg,
            admin,
            name=token_name,
            group=body.group,
            unlimited_quota=body.unlimited_quota,
            remain_quota=body.remain_quota,
            expired_time=body.expired_time,
            reuse_existing=body.reuse_existing,
        )
        relay_base_url = normalize_relay_base_url(cfg.admin_base_url)
        save_custom_newapi_gateway(
            base_url=relay_base_url,
            api_key=token["key"],
            admin_base_url=cfg.admin_base_url,
            token_name=str(token["name"]),
            token_id=token["tokenId"],
            activate=True,
        )
        _save_request_database_config(cfg, body.database)
        runtime = refresh_model_gateway_runtime()
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": True,
        "data": {
            "mode": "custom",
            "newApiAdminBaseUrl": cfg.admin_base_url,
            "newApiBaseUrl": relay_base_url,
            "adminUserId": admin.admin_user_id,
            "adminUsername": admin.admin_username,
            "adminTokenCreated": admin.token_created,
            "adminTokenPreview": mask_token(admin.access_token),
            "newApiSetup": {
                "initialized": setup_status.initialized,
                "rootInitialized": setup_status.root_initialized,
                "databaseType": setup_status.database_type,
                "setupPerformed": setup_status.setup_performed,
                "alreadyInitialized": setup_status.already_initialized,
            },
            "relayToken": {
                "created": bool(token["created"]),
                "tokenId": token["tokenId"],
                "name": token["name"],
                "keyPreview": token["keyPreview"],
            },
            "database": build_provisioner_status()["database"],
            "effective": build_model_gateway_status(
                official_base_url=app_config.OFFICIAL_NEWAPI_BASE_URL,
                official_api_key=app_config.NEWAPI_API_KEY,
            )["effective"],
            "runtime": runtime,
        },
    }


@router.post("/custom/newapi/provider-channels")
async def save_custom_newapi_provider_channels(
    body: SaveProviderChannelsBody,
) -> dict[str, Any]:
    try:
        require_ce_gateway_management()
        saved = save_newapi_provider_channels(
            [
                {
                    "provider": channel.provider,
                    "type": channel.type or 0,
                    "upstreamKey": channel.upstream_key or "",
                    "baseUrl": channel.base_url or "",
                    "priority": channel.priority,
                    "settings": channel.settings,
                }
                for channel in body.channels
            ],
            preserve_unmentioned=body.preserve_unmentioned,
        )
        embedding_model = get_newapi_embedding_model_config()
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": True,
        "data": {
            "channels": [
                {
                    "provider": channel["provider"],
                    "type": channel.get("type", 0),
                    "configured": bool(channel["upstreamKey"]),
                    "upstreamKeyPreview": mask_token(channel["upstreamKey"]),
                    "baseUrl": channel["baseUrl"],
                    "priority": channel.get("priority", 0),
                    "settings": channel.get("settings", {}),
                }
                for channel in saved
            ],
            "embeddingModel": embedding_model or None,
        },
    }


@router.get("/custom/newapi/channel-types")
async def get_custom_newapi_channel_types() -> dict[str, Any]:
    try:
        require_ce_gateway_management()
        cfg = get_provisioner_config()
        # Token discovery may wait for an uninitialized local NewAPI. Keep all
        # blocking HTTP/SQLite work off the ASGI event loop so official gateway
        # saves and application startup remain responsive.
        items = await asyncio.to_thread(
            lambda: list_channel_types(cfg, ensure_admin_access_token(cfg))
        )
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "data": {"items": items}}


@router.post("/custom/newapi/provider-channel/sync")
async def sync_custom_newapi_provider_channel(
    body: SyncProviderChannelBody,
) -> dict[str, Any]:
    provider = str(body.provider or "").strip().lower()
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    try:
        require_ce_gateway_management()
        saved_channel = get_newapi_provider_channel(provider) or {}
        upstream_key = (body.upstream_key or "").strip() or saved_channel.get(
            "upstreamKey", ""
        )
        if not upstream_key:
            raise ValueError(f"upstreamKey is required for provider {provider}")
        base_url = (
            body.base_url
            if body.base_url is not None
            else saved_channel.get("baseUrl", "")
        )
        cfg = _get_provisioner_config_from_request(body.new_api_base_url, body.database)
        admin = ensure_admin_access_token(cfg)
        update_kwargs: dict[str, Any] = {
            "provider": provider,
            "upstream_key": upstream_key,
            "base_url": base_url,
        }
        saved_channel_type = int(saved_channel.get("type") or 0)
        if saved_channel_type > 0:
            update_kwargs["channel_type"] = saved_channel_type
        result = update_provider_channel_credentials(cfg, admin, **update_kwargs)
        saved = []
        if result.get("ok"):
            saved = save_newapi_provider_channels(
                [
                    {
                        "provider": provider,
                        "upstreamKey": upstream_key,
                        "baseUrl": base_url or "",
                    }
                ],
                preserve_unmentioned=True,
            )
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    sent_payload = result.get("sentPayload")
    return {
        "ok": result["ok"],
        "data": {
            "provider": provider,
            "channelId": result.get("channelId"),
            "httpStatus": result.get("httpStatus"),
            "newApiResponse": result.get("newApiResponse"),
            "sentPayload": (
                _mask_sent_channel_payload(sent_payload)
                if isinstance(sent_payload, dict) and "channel" in sent_payload
                else sent_payload
            ),
            "savedChannel": next(
                (
                    {
                        "provider": channel["provider"],
                        "configured": bool(channel["upstreamKey"]),
                        "upstreamKeyPreview": mask_token(channel["upstreamKey"]),
                        "baseUrl": channel["baseUrl"],
                    }
                    for channel in saved
                    if channel["provider"] == provider
                ),
                None,
            ),
        },
    }


@router.post("/custom/newapi/channels")
async def create_custom_newapi_channel(body: CreateChannelBody) -> dict[str, Any]:
    try:
        require_ce_gateway_management()
        cfg = _get_provisioner_config_from_request(body.new_api_base_url, body.database)
        admin = ensure_admin_access_token(cfg)
        payload = _build_channel_payload_from_spec(body)
        result = upsert_channel(cfg, admin, payload)
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": result["ok"],
        "data": {
            "newApiAdminBaseUrl": cfg.admin_base_url,
            "httpStatus": result["httpStatus"],
            "newApiResponse": result["newApiResponse"],
            "action": result.get("action"),
            "channelId": result.get("channelId"),
            "sentPayload": _mask_sent_channel_payload(
                result.get("sentPayload") or payload
            ),
        },
    }


@router.post("/custom/newapi/channels/batch")
async def create_custom_newapi_channels_batch(
    body: CreateChannelsBatchBody,
) -> dict[str, Any]:
    try:
        require_ce_gateway_management()
        cfg = _get_provisioner_config_from_request(body.new_api_base_url, body.database)
        admin = ensure_admin_access_token(cfg)
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    results: list[dict[str, Any]] = []
    for index, channel in enumerate(body.channels):
        try:
            payload = _build_channel_payload_from_spec(channel)
            result = upsert_channel(cfg, admin, payload)
            item: dict[str, Any] = {
                "index": index,
                "ok": result["ok"],
                "httpStatus": result["httpStatus"],
                "newApiResponse": result["newApiResponse"],
                "action": result.get("action"),
                "channelId": result.get("channelId"),
                "sentPayload": _mask_sent_channel_payload(
                    result.get("sentPayload") or payload
                ),
            }
            if not result["ok"]:
                item["error"] = "NewAPI rejected channel creation"
            results.append(item)
        except Exception as exc:
            results.append(
                {
                    "index": index,
                    "ok": False,
                    "error": str(exc),
                }
            )

    succeeded = sum(1 for item in results if item["ok"])
    failed = len(results) - succeeded
    return {
        "ok": failed == 0,
        "data": {
            "newApiAdminBaseUrl": cfg.admin_base_url,
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        },
    }


@router.post("/custom/newapi/embedding-model")
async def save_custom_newapi_embedding_model(
    body: SaveEmbeddingModelBody,
) -> dict[str, Any]:
    try:
        require_ce_gateway_management()
        spec, normalized_model = _build_embedding_model_channel_spec(body)
        cfg = _get_provisioner_config_from_request(body.new_api_base_url, body.database)
        admin = ensure_admin_access_token(cfg)
        payload = _build_channel_payload_from_spec(spec)
        result = upsert_channel(cfg, admin, payload)
    except PermissionError as exc:
        raise _permission_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    item: dict[str, Any] = {
        "provider": spec.provider,
        "ok": result["ok"],
        "httpStatus": result["httpStatus"],
        "newApiResponse": result["newApiResponse"],
        "action": result.get("action"),
        "channelId": result.get("channelId"),
        "sentPayload": _mask_sent_channel_payload(result.get("sentPayload") or payload),
    }
    if not result["ok"]:
        response = result.get("newApiResponse")
        message = ""
        if isinstance(response, dict):
            message = str(
                response.get("message") or response.get("error") or ""
            ).strip()
        item["error"] = message or "NewAPI rejected embedding model channel update"
        return {
            "ok": False,
            "data": {
                "newApiAdminBaseUrl": cfg.admin_base_url,
                "embeddingModel": {},
                "result": item,
            },
        }

    saved = save_newapi_embedding_model_config(
        provider=normalized_model["provider"],
        upstream_model=normalized_model["upstreamModel"],
        dimension=normalized_model["dimension"],
        batch_size=normalized_model.get("batchSize"),
        send_dimensions=normalized_model["sendDimensions"],
    )
    return {
        "ok": True,
        "data": {
            "newApiAdminBaseUrl": cfg.admin_base_url,
            "embeddingModel": saved,
            "result": item,
        },
    }

