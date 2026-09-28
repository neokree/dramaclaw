"""NanoBananaPro 网格生成模块。

使用 Google AI Studio (Gemini Pro Image) 生成网格图，
Sketch 模式使用 3x3 网格（每张 9 panel 位），按 beat 顺序分块，自动产出 ceil(N/9) 张网格。

生成流程:
1. 从 beats 数据构建网格 Prompt
2. 调用 NanoBananaPro API 生成网格图
3. 使用 grid_splitter 分割成独立分镜
4. 使用 Seedream 图生图做高清修复
"""

import contextvars
import hashlib
import io
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from novelvideo.config import (
    IMAGE_DEFAULT_STYLE,
    get_grid_generation_config,
    get_style_preset,
)
from novelvideo.egress_context import (
    TrustedEgressContext,
    ambient_organization_egress_context,
)
from novelvideo.engines.image import generate_image
from novelvideo.ports.authz import AdmissionContext
from novelvideo.ports.egress import EgressError
from novelvideo.ports.egress_operations import (
    OperationClaimResult,
    HandleKind,
    OperationSpec,
    canonical_request_digest,
)
from novelvideo.ports.model_credentials import ModelCredentialError, RequestCredential
from novelvideo.shared.billing_errors import is_fatal_billing_error
from novelvideo.generators.prompt_builder import (
    PromptComponents,
    PromptContext,
    PromptMode,
    UnifiedPromptBuilder,
    create_prompt_context,
)
from novelvideo.generators.render_identity_guard import render_ai_detection_error
from novelvideo.models import (
    beat_scene_id,
    collect_prop_marker_ids_from_beat,
    real_detected_identities,
)
from novelvideo.manual_shots import beat_order_value
from novelvideo.services.style_service import StyleService
from novelvideo.utils.asset_resolver import AssetResolver
from novelvideo.image_request_usage import infer_episode_from_path, infer_project_output_dir

_VALID_IMAGE_SIZES = {"1K", "2K", "4K"}
SINGLE_CELL_RENDER_MODE_KEY = "1x1_2-3"
SINGLE_CELL_RENDER_MODE_BY_ASPECT = {
    "1:1": "1x1_1-1",
    "9:16": "1x1_9-16",
    "16:9": "1x1_16-9",
}
logger = logging.getLogger(__name__)


# Organization egress claims, still used by the freezone vision gateway.
@dataclass(frozen=True, slots=True)
class _OrganizationImageEgress:
    credential: RequestCredential
    operation_port: Any
    claim: OperationClaimResult


def _validate_egress_context(
    egress_context: TrustedEgressContext | None,
) -> TrustedEgressContext | None:
    if egress_context is not None and type(egress_context) is not TrustedEgressContext:
        raise TypeError("egress_context must be a TrustedEgressContext")
    return egress_context


async def _prepare_organization_image_egress(
    *,
    egress_context: TrustedEgressContext | None,
    provider: str,
    capability: str,
    request: dict[str, object],
    business_task_id: str | None = None,
) -> _OrganizationImageEgress | None:
    context = _validate_egress_context(egress_context)
    if context is None:
        context = ambient_organization_egress_context()
    if context is None or not context.is_organization:
        return None
    if provider != "newapi":
        raise EgressError("ORG_EGRESS_DENIED")

    from novelvideo.ports import get_egress_operation_port, get_model_credentials

    admission = AdmissionContext(
        requester_user_id=context.requester_user_id,
        billing_principal=context.billing_principal,
        credential=context.credential,
        admission_id=context.admission_id,
        root_task_id=context.root_task_id,
        admitted_at=context.admitted_at,
        membership_id=context.membership_id,
        authz_version=context.authz_version,
    )
    operation_port = get_egress_operation_port()
    claim = await operation_port.claim(
        spec=OperationSpec(
            organization_id=context.billing_principal.id,
            project_id=context.project_id,
            root_task_id=context.root_task_id,
            business_task_id=business_task_id or context.envelope_id,
            capability=capability,
            credential_id=context.credential.credential_id,
            credential_version=context.credential.key_version,
            request_digest=canonical_request_digest(request),
            handle_kind=HandleKind.PROVIDER_JOB,
        )
    )
    if not claim.won:
        raise EgressError("EGRESS_OPERATION_REPLAYED")
    try:
        credential = await get_model_credentials().resolve(admission)
        if (
            type(credential) is not RequestCredential
            or credential.reference != context.credential
        ):
            raise ModelCredentialError("ORG_CREDENTIAL_VERSION_MISMATCH")
    except Exception:
        await operation_port.mark_rejected_before_submit(
            operation_id=claim.operation.operation_id,
            transition_token=claim.transition_token,
            expected_version=claim.operation.version,
        )
        raise
    return _OrganizationImageEgress(
        credential=credential,
        operation_port=operation_port,
        claim=claim,
    )


async def _complete_organization_image_egress(
    state: _OrganizationImageEgress | None,
    *,
    trace: dict[str, str],
    result_ref: str,
) -> None:
    if state is None:
        return
    provider_job_id = str(
        trace.get("request_id") or trace.get("response_id") or ""
    ).strip()
    if not provider_job_id:
        await state.operation_port.mark_unknown(
            operation_id=state.claim.operation.operation_id,
            transition_token=state.claim.transition_token,
            expected_version=state.claim.operation.version,
        )
        raise RuntimeError("gateway response is missing a request id")
    accepted = await state.operation_port.mark_accepted(
        operation_id=state.claim.operation.operation_id,
        transition_token=state.claim.transition_token,
        expected_version=state.claim.operation.version,
        provider_job_id=provider_job_id,
    )
    await state.operation_port.mark_completed(
        operation_id=state.claim.operation.operation_id,
        transition_token=state.claim.transition_token,
        expected_version=accepted.version,
        result_ref=result_ref,
    )


async def _abandon_organization_image_egress(
    state: _OrganizationImageEgress | None,
) -> None:
    """Close out a claim whose outcome we cannot observe.

    Deliberately `mark_unknown` rather than `mark_rejected_before_submit`: once the
    credential is out of our hands we cannot prove the request was never submitted,
    and only the ledger's unknown state is honest about that.
    """

    if state is None:
        return
    await state.operation_port.mark_unknown(
        operation_id=state.claim.operation.operation_id,
        transition_token=state.claim.transition_token,
        expected_version=state.claim.operation.version,
    )


_SCENE_REFERENCE_FEATURE_BILLING = contextvars.ContextVar(
    "scene_reference_feature_billing", default=False
)


class scene_reference_feature_billing:
    """Temporarily delegate scene-reference billing to the EE feature ledger."""

    def __enter__(self):
        self._token = _SCENE_REFERENCE_FEATURE_BILLING.set(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        _SCENE_REFERENCE_FEATURE_BILLING.reset(self._token)


def _scene_reference_feature_billing_active() -> bool:
    return _SCENE_REFERENCE_FEATURE_BILLING.get()


def _beat_display_sort_key(beat: dict) -> tuple[int, int]:
    return (beat_order_value(beat), int(beat.get("beat_number", 0) or 0))


class _InlineImagePart:
    """A reference image in `contents`: bytes plus mime, handed to the image engine."""

    def __init__(self, data: bytes, mime_type: str = "image/png"):
        self.inline_data = SimpleNamespace(data=data, mime_type=mime_type)


def _infer_project_dir(*paths: str | None) -> Optional[Path]:
    for path_str in paths:
        if not path_str:
            continue
        path = Path(path_str)
        parts = list(path.parts)
        if "grids" in parts:
            grids_idx = parts.index("grids")
            if grids_idx > 0:
                return Path(*parts[:grids_idx])
    return None


def _resolve_scene_prop_asset_refs(
    project_dir: Optional[Path],
    beats: List[dict],
    *,
    episode_number: int | None = None,
    sketch: bool = False,
    use_director_refs: bool = False,
    include_pano_view_refs: bool = False,
    director_ref_beat_numbers: list[int] | None = None,
    director_control_frames_dir: str | Path | None = None,
    scene_menu: list[dict] | list | None = None,
    prop_menu: list[dict] | list | None = None,
    allow_beat_background_anchor: bool | None = None,
) -> tuple[dict[int, list], dict[int, list]]:
    if not project_dir:
        return {}, {}
    resolver = AssetResolver(
        project_dir,
        episode_number=episode_number,
        scene_menu=scene_menu,
        prop_menu=prop_menu,
        scene_reference_kind="sketch" if sketch else "render",
        use_director_refs=use_director_refs,
        include_pano_view_refs=include_pano_view_refs,
        director_ref_beat_numbers=director_ref_beat_numbers,
        director_control_frames_dir=director_control_frames_dir,
        allow_beat_background_anchor=allow_beat_background_anchor,
    )
    return resolver.resolve_all_for_beats(beats)


def _global_prop_marker_colors(
    beats: List[dict],
    prop_menu: list[dict] | list | None = None,
    sketch_colors: dict[str, str] | None = None,
    *,
    assign_missing: bool = False,
) -> dict[str, str]:
    """Return episode-persisted marker colors for global [[prop]] markers.

    Generation/export paths must be WYSIWYG: prop colors come from the episode
    prop_menu marker_color field only. The color assignment UI flow may pass
    assign_missing=True to compute missing colors before persisting them.
    """
    active_prop_ids: list[str] = []
    seen: set[str] = set()
    for beat in beats:
        for prop_id in collect_prop_marker_ids_from_beat(beat):
            if prop_id and prop_id not in seen:
                seen.add(prop_id)
                active_prop_ids.append(prop_id)
    if not active_prop_ids:
        return {}

    colorable_prop_ids: set[str] = set()
    explicit_colors: dict[str, str] = {}
    for raw_item in prop_menu or []:
        if isinstance(raw_item, dict):
            prop_id = str(
                raw_item.get("prop_id") or raw_item.get("base_id") or raw_item.get("name") or ""
            ).strip()
            asset_scope = str(raw_item.get("asset_scope") or "").strip().lower()
            is_global_asset = raw_item.get("is_global_asset") is True
            marker_color = str(raw_item.get("marker_color") or "").strip()
        else:
            prop_id = str(getattr(raw_item, "prop_id", "") or getattr(raw_item, "name", "")).strip()
            asset_scope = str(getattr(raw_item, "asset_scope", "")).strip().lower()
            is_global_asset = getattr(raw_item, "is_global_asset", False) is True
            marker_color = str(getattr(raw_item, "marker_color", "") or "").strip()
        if prop_id and (asset_scope == "global" or is_global_asset or marker_color):
            colorable_prop_ids.add(prop_id)
        if prop_id and marker_color:
            explicit_colors[prop_id] = marker_color
    if not colorable_prop_ids:
        return {}
    if not assign_missing:
        return {
            prop_id: explicit_colors[prop_id]
            for prop_id in active_prop_ids
            if prop_id in explicit_colors
        }

    from novelvideo.generators.episode_optimizer import (
        PROP_MARKER_PALETTE,
        _hex_to_hue,
    )

    used_hexes = {
        str(value or "").strip().split(" ", 1)[0].lower()
        for value in (sketch_colors or {}).values()
        if str(value or "").strip()
    }
    used_hexes.update(
        str(value or "").strip().split(" ", 1)[0].lower()
        for value in explicit_colors.values()
        if str(value or "").strip()
    )
    used_hues = [_hex_to_hue(h) for h in used_hexes if h.startswith("#") and len(h) == 7]

    def _min_hue_gap(candidate_hex: str) -> float:
        if not used_hues:
            return 360.0
        h = _hex_to_hue(candidate_hex)
        gaps = []
        for used_h in used_hues:
            diff = abs(h - used_h) % 360
            gaps.append(min(diff, 360 - diff))
        return min(gaps)

    # Prop 用专用调色板 PROP_MARKER_PALETTE（深色 / 非荧光），跟角色调色板（荧光高饱和）
    # 形成 value contrast。即使色相意外接近，亮度/饱和度差异让 prop 仍能跟 character 视觉分开。
    not_used = [item for item in PROP_MARKER_PALETTE if item[0].lower() not in used_hexes]
    # 视觉上跟已用色色相差 ≥60° 才"安全"；不够则退化用未占用 hex 全集，最后兜底全 prop 调色板
    safe = [item for item in not_used if _min_hue_gap(item[0]) >= 60.0]
    available = safe or not_used or list(PROP_MARKER_PALETTE)
    # 按距离已用色相由远到近排序，让最反差的色优先被选
    available = sorted(available, key=lambda it: -_min_hue_gap(it[0]))

    colors: dict[str, str] = {}
    color_index = 0
    for prop_id in active_prop_ids:
        if prop_id not in colorable_prop_ids:
            continue
        if prop_id in explicit_colors:
            colors[prop_id] = explicit_colors[prop_id]
            continue
        hex_code, color_name = available[color_index % len(available)]
        colors[prop_id] = f"{hex_code} {color_name}"
        color_index += 1
    return colors


def normalize_image_size(size: str, provider: str = "google") -> str:
    """Normalize image_size across providers.

    Internal configs may still use 0.5K; the engines' smallest size is 1K.
    """
    return "1K" if size == "0.5K" else size


def clamp_image_size(size: str) -> str:
    """Clamp image_size to the labels the engines understand."""
    normalized = normalize_image_size(size)
    return normalized if normalized in _VALID_IMAGE_SIZES else "1K"


# =============================================================================
# Sketch 模式配置
# =============================================================================
# Sketch 默认比例（向后兼容）
DEFAULT_SKETCH_ASPECT_RATIO = "2:3"

# =============================================================================
# 再生模式配置表（Regen Mode Configs）
# =============================================================================
# mode_key 格式: "{rows}x{cols}_{aspect_ratio_normalized}"
# 唯一全局配置表，所有 pool 引用此表
# 后续需要新比例时（如 1x1_16-9 横屏），直接在此表新增即可

REGEN_MODE_CONFIGS: Dict[str, dict] = {
    "1x1_1-1": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "1x1_1:1 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_9-16": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "1x1_9:16 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_2-3": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "1x1_2:3 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_16-9": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x1_16:9 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x2_4-3": {
        "rows": 1,
        "cols": 2,
        "aspect_ratio": "4:3",
        "image_size": "1K",
        "label": "1x2_4:3 1K",
        "capacity": 2,
        "model": "nanobanana",
    },
    "1x2_16-9": {
        "rows": 1,
        "cols": 2,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x2_16:9 1K",
        "capacity": 2,
        "model": "nanobanana",
    },
    "1x3_16-9": {
        "rows": 1,
        "cols": 3,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x3_16:9 1K",
        "capacity": 3,
        "model": "nanobanana",
    },
    "1x3_21-9": {
        "rows": 1,
        "cols": 3,
        "aspect_ratio": "21:9",
        "image_size": "1K",
        "label": "1x3_21:9 1K",
        "capacity": 3,
        "model": "nanobanana",
    },
    "1x4_21-9": {
        "rows": 1,
        "cols": 4,
        "aspect_ratio": "21:9",
        "image_size": "1K",
        "label": "1x4_21:9 1K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "1x6_4-1": {
        "rows": 1,
        "cols": 6,
        "aspect_ratio": "4:1",
        "image_size": "1K",
        "label": "1x6_4:1 1K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "2x2_1-1": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "1:1",
        "image_size": "2K",
        "label": "2x2_1:1 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x2_9-16": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "label": "2x2_9:16 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x3_1-1": {
        "rows": 2,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "2K",
        "label": "2x3_1:1 2K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "2x4_4-3": {
        "rows": 2,
        "cols": 4,
        "aspect_ratio": "4:3",
        "image_size": "2K",
        "label": "2x4_4:3 2K",
        "capacity": 8,
        "model": "nanobanana",
    },
    "2x2_2-3": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "2:3",
        "image_size": "2K",
        "label": "2x2_2:3 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x2_16-9": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "16:9",
        "image_size": "2K",
        "label": "2x2_16:9 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "3x2_9-16": {
        "rows": 3,
        "cols": 2,
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "label": "3x2_9:16 2K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "3x2_2-3": {
        "rows": 3,
        "cols": 2,
        "aspect_ratio": "2:3",
        "image_size": "2K",
        "label": "3x2_2:3 2K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "3x3_1-1": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "label": "3x3_1:1 4K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_9-16": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "9:16",
        "image_size": "4K",
        "label": "3x3_9:16 4K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_2-3": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "2:3",
        "image_size": "2K",
        "label": "3x3_2:3 2K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_16-9": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "16:9",
        "image_size": "4K",
        "label": "3x3_16:9 4K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "4x3_9-16": {
        "rows": 4,
        "cols": 3,
        "aspect_ratio": "9:16",
        "image_size": "4K",
        "label": "4x3_9:16 4K",
        "capacity": 12,
        "model": "nanobanana",
    },
    "4x3_3-4": {
        "rows": 4,
        "cols": 3,
        "aspect_ratio": "3:4",
        "image_size": "4K",
        "label": "4x3_3:4 4K",
        "capacity": 12,
        "model": "nanobanana",
    },
    "4x4_1-1": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "label": "4x4_1:1 4K",
        "capacity": 16,
        "model": "nanobanana",
    },
    "4x4_16-9": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "16:9",
        "image_size": "4K",
        "label": "4x4_16:9 4K",
        "capacity": 16,
        "model": "nanobanana",
    },
    "5x4_9-16": {
        "rows": 5,
        "cols": 4,
        "aspect_ratio": "9:16",
        "image_size": "4K",
        "label": "5x4_9:16 4K",
        "capacity": 20,
        "model": "nanobanana",
    },
    "5x5_1-1": {
        "rows": 5,
        "cols": 5,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "label": "5x5_1:1 4K",
        "capacity": 25,
        "model": "nanobanana",
    },
    # Sketch 专用
    "1x1_1-1_sketch": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "1x1_1:1 Sketch",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_9-16_sketch": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "1x1_9:16 Sketch",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_2-3_sketch": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "1x1_2:3 Sketch",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_16-9_sketch": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x1_16:9 Sketch",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x2_4-3_sketch": {
        "rows": 1,
        "cols": 2,
        "aspect_ratio": "4:3",
        "image_size": "1K",
        "label": "1x2_4:3 Sketch",
        "capacity": 2,
        "model": "nanobanana",
    },
    "2x2_2-3_sketch": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "2x2_2:3 Sketch",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x2_16-9_sketch": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "2x2_16:9 Sketch",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x2_9-16_sketch": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "2x2_9:16 Sketch",
        "capacity": 4,
        "model": "nanobanana",
    },
    "3x3_1-1_sketch": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "3x3_1:1 Sketch",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_9-16_sketch": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "3x3_9:16 Sketch",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_3-4_sketch": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "3:4",
        "image_size": "1K",
        "label": "3x3_3:4 Sketch",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_2-3_sketch": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "3x3_2:3 Sketch",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_16-9_sketch": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "3x3_16:9 Sketch",
        "capacity": 9,
        "model": "nanobanana",
    },
    "2x3_1-1_sketch": {
        "rows": 2,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "2x3_1:1 Sketch",
        "capacity": 6,
        "model": "nanobanana",
    },
    "2x4_4-3_sketch": {
        "rows": 2,
        "cols": 4,
        "aspect_ratio": "4:3",
        "image_size": "1K",
        "label": "2x4_4:3 Sketch",
        "capacity": 8,
        "model": "nanobanana",
    },
    "4x3_3-4_sketch": {
        "rows": 4,
        "cols": 3,
        "aspect_ratio": "3:4",
        "image_size": "1K",
        "label": "4x3_3:4 Sketch",
        "capacity": 12,
        "model": "nanobanana",
    },
    "4x4_1-1_sketch": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "4x4_1:1 Sketch",
        "capacity": 16,
        "model": "nanobanana",
    },
    "4x4_9-16_sketch": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "4x4_9:16 Sketch",
        "capacity": 16,
        "model": "nanobanana",
    },
    "4x4_2-3_sketch": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "4x4_2:3 Sketch",
        "capacity": 16,
        "model": "nanobanana",
    },
    "4x4_16-9_sketch": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "4x4_16:9 Sketch",
        "capacity": 16,
        "model": "nanobanana",
    },
    "5x5_1-1_sketch": {
        "rows": 5,
        "cols": 5,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "5x5_1:1 Sketch",
        "capacity": 25,
        "model": "nanobanana",
    },
    "5x5_2-3_sketch": {
        "rows": 5,
        "cols": 5,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "5x5_2:3 Sketch",
        "capacity": 25,
        "model": "nanobanana",
    },
    "5x5_16-9_sketch": {
        "rows": 5,
        "cols": 5,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "5x5_16:9 Sketch",
        "capacity": 25,
        "model": "nanobanana",
    },
    "5x5_9-16_sketch": {
        "rows": 5,
        "cols": 5,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "5x5_9:16 Sketch",
        "capacity": 25,
        "model": "nanobanana",
    },
}


def get_sketch_default_mode_key(aspect_ratio: str = DEFAULT_SKETCH_ASPECT_RATIO) -> str:
    target = aspect_ratio.replace(":", "-")
    candidate = f"5x5_{target}_sketch"
    if candidate in REGEN_MODE_CONFIGS:
        return candidate
    return "5x5_2-3_sketch"


def get_sketch_nxn_modes(
    aspect_ratio: str = DEFAULT_SKETCH_ASPECT_RATIO,
) -> list[tuple[int, str, int, int]]:
    result: list[tuple[int, str, int, int]] = []
    for cap, rows, cols in [(1, 1, 1), (4, 2, 2), (9, 3, 3), (16, 4, 4), (25, 5, 5)]:
        matches = [
            mode_key
            for mode_key, cfg in REGEN_MODE_CONFIGS.items()
            if mode_key.endswith("_sketch")
            and cfg.get("rows") == rows
            and cfg.get("cols") == cols
            and cfg.get("aspect_ratio") == aspect_ratio
        ]
        if matches:
            result.append((cap, matches[0], rows, cols))
            continue
        same_size_fallbacks = [
            mode_key
            for mode_key, cfg in REGEN_MODE_CONFIGS.items()
            if mode_key.endswith("_sketch") and cfg.get("rows") == rows and cfg.get("cols") == cols
        ]
        if same_size_fallbacks:
            preferred_same_size = next(
                (
                    mode_key
                    for mode_key in same_size_fallbacks
                    if REGEN_MODE_CONFIGS[mode_key].get("aspect_ratio")
                    == DEFAULT_SKETCH_ASPECT_RATIO
                ),
                same_size_fallbacks[0],
            )
            result.append((cap, preferred_same_size, rows, cols))
            continue
        raise ValueError(f"No sketch mode available for grid size {rows}x{cols}")
    return result


# Sketch 默认 mode_key，其余信息从 REGEN_MODE_CONFIGS 查表
SKETCH_DEFAULT_MODE_KEY = get_sketch_default_mode_key()
PLANNER_VERSION = "2026-04-18-v1"


@dataclass(frozen=True)
class PlanEntry:
    """Single grid entry in a server-authoritative render plan."""

    mode_key: str
    rows: int
    cols: int
    beat_numbers: tuple[int, ...]
    location: str = ""
    padding_count: int = 0
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def hash_plan(plan: list[PlanEntry]) -> str:
    """Canonical SHA1 of plan shape, truncated for task identity."""
    payload = json.dumps(
        [(entry.mode_key, list(entry.beat_numbers)) for entry in plan],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def compute_input_fingerprint(
    beats: list[dict],
    character_map: dict,
    sketch_colors: dict,
    strategy: str,
    aspect_mode: str,
    force_one_by_one: bool,
    ref_image_hasher: Callable[[str], str],
) -> str:
    """Fingerprint planning inputs that must stay stable between plan and execute."""
    beats_by_num = sorted(beats, key=_beat_display_sort_key)
    beats_payload = [
        {
            "beat_number": beat.get("beat_number"),
            "location": beat_scene_id(beat) or beat.get("location") or "",
            "detected_identities": sorted(
                real_detected_identities(beat.get("detected_identities") or [])
            ),
            "visual_description": beat.get("visual_description") or "",
        }
        for beat in beats_by_num
    ]

    referenced_ids: set[str] = set()
    for beat in beats_by_num:
        for identity_id in real_detected_identities(beat.get("detected_identities") or []):
            referenced_ids.add(str(identity_id))

    character_payload = []
    for identity_id in sorted(referenced_ids):
        info = character_map.get(identity_id) or {}
        ref_path = info.get("ref_path") or info.get("primary_reference") or ""
        ref_mode = info.get("ref_mode") or info.get("mode") or ""
        content_hash = ref_image_hasher(ref_path) if ref_path else ""
        character_payload.append(
            {
                "id": identity_id,
                "ref_path": ref_path,
                "ref_mode": ref_mode,
                "content_hash": content_hash,
            }
        )

    sketch_color_payload = {
        identity_id: sketch_colors.get(identity_id, "") for identity_id in sorted(referenced_ids)
    }
    envelope = {
        "beats": beats_payload,
        "character_map": character_payload,
        "sketch_colors": sketch_color_payload,
        "strategy": strategy,
        "aspect_mode": aspect_mode,
        "force_one_by_one": bool(force_one_by_one),
        "planner_version": PLANNER_VERSION,
    }
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def cell_aspect_ratio(mode_key: str) -> Optional[str]:
    """计算 mode_key 中单个 cell 的实际宽高比字符串。

    公式: cell 比例 = (W × rows) : (H × cols)，其中 W:H 是 grid 整体比例。
    """
    from math import gcd

    cfg = REGEN_MODE_CONFIGS.get(mode_key)
    if not cfg or not cfg.get("aspect_ratio"):
        return None
    w, h = map(int, cfg["aspect_ratio"].split(":"))
    rows, cols = cfg["rows"], cfg["cols"]
    cell_w, cell_h = w * rows, h * cols
    g = gcd(cell_w, cell_h)
    return f"{cell_w // g}:{cell_h // g}"


def sketch_pass1_mode_key(target_mode_key: str) -> Optional[str]:
    """给定目标 sketch mode_key，返回同尺寸的 1:1 sketch mode_key（用于 two-pass）。

    已禁用 two-pass：所有比例（含 5x5 2:3）均直接 one-pass 生成。
    """
    return None


# 从 SKETCH_DEFAULT_MODE_KEY 派生（向后兼容）
_sketch_cfg = REGEN_MODE_CONFIGS[SKETCH_DEFAULT_MODE_KEY]
SKETCH_GRID_CONFIG = {
    "rows": _sketch_cfg["rows"],
    "cols": _sketch_cfg["cols"],
    "aspect_ratio": _sketch_cfg["aspect_ratio"],
    "image_size": _sketch_cfg["image_size"],
}
# 5x5 每张 25 beats，1 张即可覆盖整集
SKETCH_GRID_PLAN = [SKETCH_GRID_CONFIG] * 1

# 草图再生只允许 NxN + 1:1 模式
SKETCH_REGEN_MODE_KEYS = [
    "1x1_1-1",
    "1x1_1-1_sketch",
    "1x1_9-16_sketch",
    "1x1_2-3_sketch",
    "1x1_16-9_sketch",
    "1x2_4-3_sketch",
    "2x2_1-1",
    "2x2_2-3_sketch",
    "2x2_16-9_sketch",
    "2x2_9-16_sketch",
    "2x4_4-3_sketch",
    "3x2_2-3",
    "3x3_1-1_sketch",
    "3x3_9-16_sketch",
    "3x3_3-4_sketch",
    "3x3_2-3_sketch",
    "3x3_16-9_sketch",
    "4x3_3-4_sketch",
    "4x4_1-1_sketch",
    "4x4_16-9_sketch",
    "5x5_1-1_sketch",
    "5x5_2-3_sketch",
    "5x5_16-9_sketch",
    "5x5_9-16_sketch",
    "5x5_1-1",
]

# =============================================================================
# 统一积木池：pool 即资源清单，用完即止
# =============================================================================
# 预置池子模板（* N 生成实际资源清单）
LOCATION_POOL_TEMPLATE = ["2x4_4-3", "2x3_1-1", "2x2_2-3", "1x2_4-3", "1x1_2-3"]
LANDSCAPE_RENDER_POOL_TEMPLATE = [
    "4x4_16-9",
    "3x3_16-9",
    "2x2_16-9",
    "1x1_16-9",
]

# 默认模式 pool（与场景分组相同）
DEFAULT_POOL_TEMPLATE = ["2x4_4-3", "2x3_1-1", "2x2_2-3", "1x2_4-3", "1x1_2-3"]
# 角色分组 pool
CHARACTER_POOL_2 = ["2x4_4-3", "2x3_1-1", "2x2_2-3", "1x2_4-3", "1x1_2-3"]
CHARACTER_POOL_3 = ["2x4_4-3", "2x3_1-1", "2x2_2-3", "1x2_4-3", "1x1_2-3"]

# 铁律：>=3 个有参考图的角色引用时，只能用 1x1（不能用网格）
MANY_CHARS_REF_THRESHOLD = 3
MANY_CHARS_MAX_CAPACITY = 1


def _cap_pool_for_many_chars(pool: list, composite_count: int) -> list:
    """当 composite 角色数 >= 阈值时，过滤池子到 capacity <= 上限。"""
    if composite_count >= MANY_CHARS_REF_THRESHOLD:
        capped = [
            mk for mk in pool if REGEN_MODE_CONFIGS[mk]["capacity"] <= MANY_CHARS_MAX_CAPACITY
        ]
        return capped if capped else pool
    return pool


def _count_batch_composite_chars(beats: list, character_map: dict) -> int:
    """统计一批 beats 中出现的唯一 composite 角色数量。"""
    all_chars = set()
    for beat in beats:
        all_chars |= _get_beat_visual_composite_chars(beat, character_map)
    return len(all_chars)


def _smart_repack_beats(
    beats: List[dict],
    character_map: dict,
    pool_template: list,
    overrides: dict = None,
    loc: str = "",
) -> list:
    """智能拆分：将 beats 按 composite 角色数分成连续子组，每组 n_comp < 阈值。

    当一批 beats 合计 n_comp >= 3 时，不是一刀切全部 cap 到 1x2，
    而是贪心扫描：连续 beats 合并 composite 集合，只在真正达到阈值时断开。
    每个子组用完整 pool pack（因为子组内 n_comp < 3），
    只有单个 beat 本身 >= 3 时才用 capped pool。
    """
    if not beats:
        return []

    groups: List[List[dict]] = []
    current_group: List[dict] = []
    current_chars: set = set()

    for beat in beats:
        beat_chars = _get_beat_visual_composite_chars(beat, character_map)
        merged = current_chars | beat_chars

        if len(merged) >= MANY_CHARS_REF_THRESHOLD and current_group:
            # 当前 beat 会让 n_comp 超标，先 flush 已有组
            groups.append(current_group)
            current_group = [beat]
            current_chars = set(beat_chars)
        else:
            current_group.append(beat)
            current_chars = merged

    if current_group:
        groups.append(current_group)

    # 合并角色集相同的子组（如 B1-6{唐若瑜,陆洲} 和 B17-18{唐若瑜,陆洲}）
    merged_groups: List[List[dict]] = []
    merged_charsets: List[frozenset] = []
    for group in groups:
        chars = frozenset()
        for beat in group:
            chars = chars | _get_beat_visual_composite_chars(beat, character_map)
        # 找已有的同角色集组合并
        found = False
        for i, existing_chars in enumerate(merged_charsets):
            if (
                chars == existing_chars
                or (chars | existing_chars) == existing_chars
                or (chars | existing_chars) == chars
            ):
                # 角色集相同或是子集，检查合并后是否仍 < 阈值
                union = chars | existing_chars
                if len(union) < MANY_CHARS_REF_THRESHOLD:
                    merged_groups[i].extend(group)
                    merged_charsets[i] = union
                    found = True
                    break
        if not found:
            merged_groups.append(list(group))
            merged_charsets.append(chars)

    # 合并后按首 beat_number 排序每组内的 beats
    for group in merged_groups:
        group.sort(key=lambda b: b.get("beat_number", 0))

    # 对每个子组用合适的 pool pack
    result = []
    for group in merged_groups:
        n_comp = _count_batch_composite_chars(group, character_map)
        if n_comp >= MANY_CHARS_REF_THRESHOLD:
            # 单个 beat 本身就 >= 3 composite，用 capped pool
            capped = _cap_pool_for_many_chars(list(pool_template), n_comp)
            if loc:
                result.extend(_pack_location_beats(group, loc, capped, overrides or {}))
            else:
                pool = list(capped) * 100
                mode_keys = pack_beats(len(group), pool)
                result.extend(mode_keys)
        else:
            # n_comp < 3，用完整 pool
            if loc:
                result.extend(
                    _pack_location_beats(group, loc, list(pool_template), overrides or {})
                )
            else:
                pool = list(pool_template) * 100
                mode_keys = pack_beats(len(group), pool)
                result.extend(mode_keys)

    return result


def pad_to_aspect_ratio(panel, target_aspect: str, fill_color=(220, 220, 220)):
    """panel 上下/左右补白到目标比例。不裁剪、不拉伸。
    如果 panel 已经比目标更宽/更高，原样返回（不裁剪）。
    白色填充，并向内覆盖几像素盖住 AI 生成的边框线。"""
    from PIL import Image, ImageDraw

    w, h = panel.size
    aw, ah = map(int, target_aspect.replace(":", "x").split("x"))
    target_ratio = aw / ah
    current_ratio = w / h

    if abs(current_ratio - target_ratio) < 0.08:
        return panel  # 已匹配（0.5K 小 panel trim 后比例会有几 % 偏移）

    white = (255, 255, 255)
    # 向内覆盖像素数：盖住原图边缘的边框线
    overlap = max(6, min(w, h) // 60)

    if current_ratio > target_ratio:
        # panel 更宽 → 需要加高（上下补白）
        new_h = int(w / target_ratio)
        result = Image.new("RGB", (w, new_h), white)
        py = (new_h - h) // 2
        result.paste(panel, (0, py))
        # 白色覆盖原图顶部 / 底部 overlap 行
        draw = ImageDraw.Draw(result)
        draw.rectangle([0, py, w - 1, py + overlap - 1], fill=white)
        draw.rectangle([0, py + h - overlap, w - 1, py + h - 1], fill=white)
        return result
    else:
        # panel 更高 → 需要加宽（左右补白）
        new_w = int(h * target_ratio)
        result = Image.new("RGB", (new_w, h), white)
        px = (new_w - w) // 2
        result.paste(panel, (px, 0))
        # 白色覆盖原图左侧 / 右侧 overlap 列
        draw = ImageDraw.Draw(result)
        draw.rectangle([px, 0, px + overlap - 1, h - 1], fill=white)
        draw.rectangle([px + w - overlap, 0, px + w - 1, h - 1], fill=white)
        return result


def pack_beats(total_beats: int, pool: list[str]) -> list[str]:
    """贪心 bin-pack：优先减少网格数量。

    策略：先尝试找一个能装下所有 beats 的最小网格；
    找不到时取最大网格装满，剩余递归。
    """
    if total_beats <= 0:
        return [pool[0]] if pool else []

    # 去重并按容量从小到大排序
    unique_modes = list(dict.fromkeys(pool))  # 保序去重
    by_cap_asc = sorted(unique_modes, key=lambda mk: REGEN_MODE_CONFIGS[mk]["capacity"])

    # 1. 尝试用单个网格装下所有 beats（选最小能装下的）
    for mk in by_cap_asc:
        if REGEN_MODE_CONFIGS[mk]["capacity"] >= total_beats:
            return [mk]

    # 2. 装不下 → 取最大网格，剩余递归
    largest = by_cap_asc[-1]
    largest_cap = REGEN_MODE_CONFIGS[largest]["capacity"]
    remaining = total_beats - largest_cap
    return [largest] + pack_beats(remaining, pool)


# =============================================================================
# 场景分组比例覆盖表
# =============================================================================
# aspect_mode → {(rows, cols): (aspect_ratio, image_size)}
# 只列出需要覆盖默认查找的 grid size
LOCATION_ASPECT_CONFIGS = {
    "9:16": {
        # 2x2 不覆盖，走 SQUARE 默认 1:1
    },
    "1:1": {
        (2, 2): ("1:1", "2K"),
        (1, 1): ("1:1", "1K"),
    },
}


def parse_regen_mode(mode_key: str) -> tuple:
    """解析再生模式 key，返回 (rows, cols, aspect_ratio, image_size)。

    Args:
        mode_key: 如 '1x1_9-16', '2x2_1-1'

    Returns:
        (rows, cols, aspect_ratio, image_size)
    """
    cfg = REGEN_MODE_CONFIGS[mode_key]
    return cfg["rows"], cfg["cols"], cfg["aspect_ratio"], cfg["image_size"]


def get_default_mode_for_grid(grid_size: str) -> str:
    """Return the default mode key for a grid size like 1x1 / 2x2 / 3x3."""
    for key in REGEN_MODE_CONFIGS:
        if key.startswith(grid_size + "_"):
            return key
    raise ValueError(f"No regen mode for grid size: {grid_size}")


def get_regen_modes_for_grid(grid_size: str) -> list:
    """获取指定 grid_size 的所有模式 key 列表。

    Args:
        grid_size: 如 '1x1', '2x2'

    Returns:
        [mode_key, ...]
    """
    return [k for k in REGEN_MODE_CONFIGS if k.startswith(grid_size + "_")]


def grid_mode_to_mode_key(grid_mode: str) -> str:
    """将简单 grid_mode (如 '3x3') 转换为带比例的 mode_key (如 '3x3_1-1')。

    从 REGEN_MODE_CONFIGS 查找匹配的 mode_key。
    已经是 mode_key 格式的（含 '_'）直接返回。
    """
    # 已经包含比例信息（如 '3x3_1-1'），直接返回
    if "_" in grid_mode:
        return grid_mode
    parts = grid_mode.split("x")
    if len(parts) != 2:
        return grid_mode  # loc 等特殊模式
    try:
        rows, cols = int(parts[0]), int(parts[1])
    except ValueError:
        return grid_mode
    # 从 REGEN_MODE_CONFIGS 查找第一个匹配 (rows, cols) 的 mode_key
    for mk, cfg in REGEN_MODE_CONFIGS.items():
        if cfg["rows"] == rows and cfg["cols"] == cols:
            return mk
    return grid_mode  # fallback


def _single_cell_render_mode_key(aspect_mode: str) -> str:
    return SINGLE_CELL_RENDER_MODE_BY_ASPECT.get(
        str(aspect_mode or "").strip(),
        SINGLE_CELL_RENDER_MODE_KEY,
    )


def _render_pool_template_for_aspect(aspect_mode: str) -> list[str]:
    if str(aspect_mode or "").strip() == "16:9":
        # Square 16:9 grids keep every split cell at 16:9 while still combining beats.
        return list(LANDSCAPE_RENDER_POOL_TEMPLATE)
    return list(LOCATION_POOL_TEMPLATE)


# =============================================================================
# Shot-Level Grid 配置（v2.0 Shot-Centric）
# =============================================================================
# Shot 内 N 个 beats → 1 个 Grid，作为 Seedance 2.0 的 @图片 分镜参考
# 仅使用已验证的格式（Seedance 2.0 理解的 Grid 布局）

SHOT_GRID_CONFIGS: Dict[int, dict] = {
    1: {
        "rows": 1, "cols": 1, "aspect_ratio": "9:16", "image_size": "1K", "order_hint": "",
    },
    2: {
        "rows": 1, "cols": 2, "aspect_ratio": "16:9", "image_size": "2K", "order_hint": "从左到右",
    },
    3: {
        "rows": 1, "cols": 3, "aspect_ratio": "21:9", "image_size": "2K", "order_hint": "从左到右",
    },
    4: {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "1:1",
        "image_size": "2K",
        "order_hint": "从左到右从上到下",
    },
    5: {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "order_hint": "从左到右从上到下，前5格",
    },
}


def get_shot_grid_config(beat_count: int) -> dict:
    """获取 Shot 级 Grid 配置。

    Args:
        beat_count: Shot 内 beat 数量（1-5）

    Returns:
        {"rows", "cols", "aspect_ratio", "image_size", "order_hint"}
    """
    beat_count = max(1, min(5, beat_count))
    return SHOT_GRID_CONFIGS[beat_count]


class GridGenerationRequest(BaseModel):
    """网格生成请求。

    参考模式由上游 build_character_map_for_grid() 决定：
    - composite: 复合参考图（Portrait + Fullbody 拼接），锁脸 + 锁服装
    - portrait_only: 仅面部特写，锁脸，服装由 appearance_details 文字控制
    - prompt_only: 无参考图，完全由提示词控制
    """

    beats: List[dict] = Field(description="Beats 数据列表（每张网格最多25个，不足留空）")
    character_map: Dict[str, dict] = Field(
        default_factory=dict,
        description="""角色映射 {角色名: {
            'character_tag': ...,
            'base_prompt': ...,
            'appearance_details': ...,
            'portrait_path': ...,  # 面部特写图（用于锁脸）
            'ref_path': ...,  # 参考图路径
            'reference_mode': ...,  # composite / portrait_only / prompt_only
        }}""",
    )
    style: str = Field(
        default=None,
        description="全局风格 (chinese_period_drama, anime, realistic)，默认使用 IMAGE_DEFAULT_STYLE",
    )
    episode: int = Field(description="集数")
    ethnicity: str = Field(
        default="Chinese",
        description="角色默认种族 (Chinese, Korean, Japanese, Western, etc.)",
    )


class GridGenerationResult(BaseModel):
    """网格生成结果。"""

    success: bool
    grid_image_path: Optional[str] = None
    grid_image_bytes: Optional[bytes] = None
    error: Optional[str] = None
    generation_time: float = 0.0
    # 用于单网格重新生成时的元数据
    beat_start_index: Optional[int] = None  # 该网格对应的起始 beat 索引 (0-based)
    beat_count: Optional[int] = None  # 该网格实际的 beat 数量（不含填充）
    grid_rows: Optional[int] = None  # 网格行数
    grid_cols: Optional[int] = None  # 网格列数


def filter_character_map_for_beats(
    character_map: dict,
    beats: list,
    scene_refs: dict[int, list[Any]] | None = None,
) -> dict:
    """过滤角色映射为当前 beats 中实际出场角色。

    只保留在 beats visual_description 中 {{角色名}} 出现的角色。

    Returns:
        过滤后的 character_map（新 dict，不修改原始数据）
    """
    panel_chars = PromptComponents.extract_panel_characters(beats, character_map)

    # 只保留出场角色
    filtered = {k: dict(v) for k, v in character_map.items() if k in panel_chars}

    non_panel = [c for c in character_map if c not in filtered]
    if non_panel:
        print(f"[filter_character_map] 过滤非出场角色: {non_panel}")

    return filtered


def _has_director_image_ref(scene_refs: dict[int, list[Any]], panel_idx: int = 1) -> bool:
    for ref in scene_refs.get(panel_idx, []) or []:
        if str(getattr(ref, "source_level", "") or "").strip() != "director_image":
            continue
        image_paths = list(getattr(ref, "image_paths", []) or [])
        if image_paths and all(os.path.exists(path) for path in image_paths):
            return True
    return False


def build_color_map_from_character_map(
    character_map: dict,
    sketch_colors: dict[str, str] | None = None,
) -> dict[str, str]:
    """从 character_map + sketch_colors 构建 {key: "#HEX COLOR_NAME"} 颜色映射。

    Returns:
        {identity_key: "#HEX COLOR_NAME"}。
        角色 key 格式为 "char_name_suffix" 或 "char_name"。
    """
    color_map = {}
    for char_name, info in character_map.items():
        identity_colors = info.get("identity_sketch_colors", {})
        if identity_colors:
            for suffix, color_str in identity_colors.items():
                color_map[f"{char_name}_{suffix}"] = color_str
        elif info.get("sketch_color"):
            color_map[char_name] = info["sketch_color"]
    return color_map


def _extract_hex_color(color_str: str, fallback: str) -> str:
    text = str(color_str or "").strip()
    match = re.search(r"#[0-9A-Fa-f]{6}", text)
    return match.group(0) if match else fallback


def _color_for_identity(identity_id: str, character_map: dict, fallback: str) -> str:
    color_map = build_color_map_from_character_map(character_map)
    if identity_id in color_map:
        return _extract_hex_color(color_map[identity_id], fallback)
    for char_name, info in character_map.items():
        if identity_id == char_name or identity_id.startswith(f"{char_name}_"):
            identity_colors = info.get("identity_sketch_colors") or {}
            suffix = (
                identity_id[len(char_name) + 1 :] if identity_id.startswith(f"{char_name}_") else ""
            )
            if suffix and suffix in identity_colors:
                return _extract_hex_color(identity_colors[suffix], fallback)
            return _extract_hex_color(info.get("sketch_color", ""), fallback)
    return fallback


def _draw_blocking_stick_figure(
    draw,
    *,
    x: float,
    y: float,
    scale: float,
    color: str,
    foreground: bool = False,
    seated: bool = False,
) -> None:
    """Draw simple color-coded stick blocking onto a director environment ref."""

    width = max(5, int(scale * 0.08))
    head_r = scale * (0.22 if foreground else 0.16)
    body_len = scale * (0.62 if foreground else (0.34 if seated else 0.48))
    shoulder = scale * (0.22 if foreground else 0.16)
    hip = scale * (0.16 if foreground else 0.12)
    outline = color

    # Head.
    draw.ellipse(
        (x - head_r, y - head_r, x + head_r, y + head_r),
        outline=outline,
        width=width,
    )

    neck_y = y + head_r
    torso_y = neck_y + body_len
    draw.line((x, neck_y, x, torso_y), fill=outline, width=width)
    draw.line(
        (x - shoulder, neck_y + scale * 0.1, x + shoulder, neck_y + scale * 0.1),
        fill=outline,
        width=width,
    )
    draw.line((x - hip, torso_y, x + hip, torso_y), fill=outline, width=width)

    # Arms and rough lower-body cues. Keep it rough: these are placement markers, not anatomy.
    if foreground:
        draw.line(
            (x + shoulder, neck_y + scale * 0.12, x + scale * 0.38, torso_y),
            fill=outline,
            width=width,
        )
        draw.line(
            (x - shoulder, neck_y + scale * 0.12, x - scale * 0.34, torso_y),
            fill=outline,
            width=width,
        )
    elif seated:
        draw.line(
            (x - shoulder, neck_y + scale * 0.1, x - scale * 0.34, torso_y + scale * 0.04),
            fill=outline,
            width=width,
        )
        draw.line(
            (x + shoulder, neck_y + scale * 0.1, x + scale * 0.3, torso_y + scale * 0.02),
            fill=outline,
            width=width,
        )
    else:
        draw.line(
            (x - shoulder, neck_y + scale * 0.12, x - scale * 0.28, torso_y - scale * 0.08),
            fill=outline,
            width=width,
        )
        draw.line(
            (x + shoulder, neck_y + scale * 0.12, x + scale * 0.26, torso_y - scale * 0.1),
            fill=outline,
            width=width,
        )
        draw.line(
            (x - hip, torso_y, x - scale * 0.22, torso_y + scale * 0.24), fill=outline, width=width
        )
        draw.line(
            (x + hip, torso_y, x + scale * 0.24, torso_y + scale * 0.22), fill=outline, width=width
        )


def _prepare_director_blocking_refs(
    *,
    scene_refs: dict[int, list],
    beats: list[dict],
    character_map: dict,
) -> None:
    """Legacy no-op.

    Current 3GS scene sketch submits the PlayCanvas combined.png directly.
    The image already contains the visible actor placeholder and prop/staging
    marker colors; the model edits that single image in place.
    """
    return


def detect_panel_characters(
    character_map: dict,
    sketch_image_path: str,
    rows: int,
    cols: int,
    sketch_colors: dict[str, str] | None = None,
) -> dict[int, set[str]]:
    """Per-panel 颜色检测，返回每个 panel 检测到的角色 key。

    Returns:
        {panel_index(0-based): set of detected color_map keys}
    """
    from novelvideo.generators.sketch_color_detector import detect_sketch_colors_per_panel

    color_map = build_color_map_from_character_map(character_map, sketch_colors)
    if not color_map:
        return {}

    return detect_sketch_colors_per_panel(sketch_image_path, color_map, rows=rows, cols=cols)


def filter_character_map_by_sketch(
    character_map: dict,
    sketch_image_path: str,
    sketch_colors: dict[str, str] | None = None,
) -> dict:
    """检测草图颜色，过滤不存在的角色。

    保留 prompt_only 和无 sketch_color 的角色（无法通过颜色判断）。
    仅过滤有 sketch_color 但颜色在草图中不存在的角色。

    Returns:
        过滤后的 character_map（新 dict）
    """
    from novelvideo.generators.sketch_color_detector import detect_sketch_colors

    color_map = build_color_map_from_character_map(character_map, sketch_colors)
    if not color_map:
        return dict(character_map)

    # 构建 char_to_keys 映射
    char_to_keys = {}
    for char_name, info in character_map.items():
        keys = []
        identity_colors = info.get("identity_sketch_colors", {})
        if identity_colors:
            keys = [f"{char_name}_{suffix}" for suffix in identity_colors]
        elif info.get("sketch_color"):
            keys = [char_name]
        char_to_keys[char_name] = keys

    detected = detect_sketch_colors(sketch_image_path, color_map, verbose=True)

    filtered = {}
    removed = []
    for char_name, info in character_map.items():
        keys = char_to_keys.get(char_name, [])
        if not keys:
            filtered[char_name] = dict(info)
        elif any(k in detected for k in keys):
            filtered[char_name] = dict(info)
        else:
            removed.append(char_name)

    if removed:
        print(f"[filter_by_sketch] 草图中未检测到颜色，移除角色: {removed}")

    return filtered


def load_precomputed_panel_detected(
    beat_numbers: list[int],
    beats_data: list[dict],
) -> dict[int, set[str] | None]:
    """从 beat 数据中读取 detected_identities，转换为 panel_detected 格式。

    Returns:
        {panel_index(0-based): set of identity keys}，未检测的 panel 值为 None
    """
    beat_map = {b.get("beat_number"): b for b in (beats_data or [])}
    result = {}
    for panel_idx, bn in enumerate(beat_numbers):
        beat = beat_map.get(bn, {})
        ids = real_detected_identities(beat.get("detected_identities", []))
        result[panel_idx] = set(ids) if ids else None
    return result


def filter_character_map_by_precomputed(
    character_map: dict,
    panel_detected: dict[int, set[str] | None],
) -> dict:
    """根据预计算结果过滤 character_map：只保留在任一 panel 中出现的角色。"""
    if not panel_detected or all(v is None for v in panel_detected.values()):
        return {}

    all_detected = set()
    for ids in panel_detected.values():
        if ids is not None:
            all_detected.update(ids)

    if not all_detected:
        return {}

    filtered = {}
    removed = []
    for char_name, info in character_map.items():
        identity_colors = info.get("identity_sketch_colors", {})
        keys = []
        if identity_colors:
            keys = [f"{char_name}_{suffix}" for suffix in identity_colors]
        elif info.get("sketch_color"):
            keys = [char_name]

        if not keys or any(k in all_detected for k in keys):
            filtered[char_name] = dict(info)
        else:
            removed.append(char_name)

    if removed:
        print(f"[filter_by_precomputed] 预计算未检测到，移除角色: {removed}")

    return filtered


def resolve_render_reference_order(
    ctx,
    beats: list[dict],
    grid_capacity: int,
    valid_character_map: dict,
) -> list[str]:
    """统一 Render 模式参考图顺序。

    Single source of truth:
    1. 只复用 prompt_builder 在 build() 阶段解析出的顺序
    2. 缺失即报错，禁止运行时重新推导导致所见非所得
    """
    if not valid_character_map:
        return []

    ordered_chars = list(getattr(ctx, "resolved_render_chars", []) or [])
    if ordered_chars:
        return ordered_chars

    raise RuntimeError(
        "Render reference order missing: ctx.resolved_render_chars was not populated "
        "by prompt_builder. Refusing to recompute attachment order at runtime."
    )


def crop_sketch_panels(
    sketch_path: str,
    beat_numbers: List[int],
    target_rows: int,
    target_cols: int,
    output_path: str,
    label_beats: bool = False,
    beat_sketch_paths: Dict[int, str] = None,
    target_aspect: str = None,
) -> str:
    """从草图中按 beat 编号提取 panel，拼接成目标布局。

    导出和生成共用此函数，确保所见即所得（WYSIWYG）。

    Args:
        sketch_path: 草图文件路径或草图目录路径
        beat_numbers: 实际 beat 编号列表（1-based），如 [2, 5, 8]
        target_rows: 目标网格行数
        target_cols: 目标网格列数
        output_path: 输出文件路径
        label_beats: 是否在每个 panel 左上角标注 beat 编号
        beat_sketch_paths: {beat_num: full_path} 从图片池取的 per-beat 草图路径
        target_aspect: 目标比例（如 "9:16"），非空时补白到该比例

    Returns:
        保存后的图片路径
    """
    from PIL import Image
    from novelvideo.generators.grid_splitter import _trim_outer_border
    import numpy as np

    def _trim_panel(panel_img):
        """裁掉单个 panel 的白边。"""
        gray_arr = np.array(panel_img.convert("L"))
        trimmed, _ = _trim_outer_border(panel_img, gray_arr)
        return trimmed

    panels = []
    panel_width = None
    panel_height = None
    pool_hit = 0

    for beat_num in beat_numbers:
        # 优先从图片池取单个 beat 草图
        if beat_sketch_paths and beat_num in beat_sketch_paths:
            pool_img = Image.open(beat_sketch_paths[beat_num])
            pool_img = _trim_panel(pool_img)
            if target_aspect:
                pool_img = pad_to_aspect_ratio(pool_img, target_aspect)
            if panel_width is None:
                panel_width = pool_img.width
                panel_height = pool_img.height
            else:
                if pool_img.size != (panel_width, panel_height):
                    pool_img = pool_img.resize((panel_width, panel_height), Image.LANCZOS)
            panels.append(pool_img)
            pool_hit += 1
        else:
            # 无草图 → 灰色占位
            if panel_width:
                panels.append(Image.new("RGB", (panel_width, panel_height), (128, 128, 128)))

    if pool_hit > 0:
        now = time.time()
        if now - getattr(crop_sketch_panels, "_last_log_t", 0.0) >= 5.0:
            print(f"[crop_sketch_panels] 从图片池取 {pool_hit}/{len(beat_numbers)} 个 beat 草图")
            crop_sketch_panels._last_log_t = now

    if not panels or panel_width is None:
        raise ValueError(f"无法从草图中提取 beat {beat_numbers}")

    target_width = target_cols * panel_width
    target_height = target_rows * panel_height
    result_img = Image.new("RGB", (target_width, target_height), (255, 255, 255))

    for i, panel in enumerate(panels):
        if i >= target_rows * target_cols:
            break
        r = i // target_cols
        c = i % target_cols
        x = c * panel_width
        y = r * panel_height
        result_img.paste(panel, (x, y))

    # 画面板间分割线
    if target_rows > 1 or target_cols > 1:
        from PIL import ImageDraw as _ImageDraw

        _draw = _ImageDraw.Draw(result_img)
        _line_w = max(1, min(panel_width, panel_height) // 200)
        _line_color = (180, 180, 180)
        for c in range(1, target_cols):
            _x = c * panel_width
            _draw.line([(_x, 0), (_x, target_height)], fill=_line_color, width=_line_w)
        for r in range(1, target_rows):
            _y = r * panel_height
            _draw.line([(0, _y), (target_width, _y)], fill=_line_color, width=_line_w)

    # 在每个 panel 左上角标注 beat 编号
    if label_beats and beat_numbers:
        from PIL import ImageDraw, ImageFont

        draw = ImageDraw.Draw(result_img)
        font_size = max(16, min(panel_width, panel_height) // 6)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()
        for i, beat_num in enumerate(beat_numbers):
            if i >= target_rows * target_cols:
                break
            r = i // target_cols
            c = i % target_cols
            x = c * panel_width + 4
            y = r * panel_height + 2
            label = f"B{beat_num}"
            # 黑色描边 + 白色文字，确保可读
            for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1), (-2, 0), (2, 0), (0, -2), (0, 2)]:
                draw.text((x + dx, y + dy), label, fill="black", font=font)
            draw.text((x, y), label, fill="white", font=font)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    result_img.save(output_path, quality=85)
    return output_path


def get_optimal_grid_size(beat_count: int, max_grid: int = 16) -> tuple[int, int]:
    """根据 beat 数量选择最优网格大小，最小化黑色填充。

    注意：此函数已被 perfect_grid_split() 替代用于批量生成。
    保留此函数用于向后兼容和单网格重新生成。

    Args:
        beat_count: 需要填充的 beat 数量
        max_grid: 最大网格容量

    Returns:
        (rows, cols) 元组
    """
    if beat_count <= 0:
        return (1, 1)  # 最小网格：单张

    # 可用网格（按容量从小到大排列）
    # 包含竖屏 Panel 模式: 1x4=4, 3x2=6, 4x3=12, 5x4=20
    grid_options = [
        (1, 1),  # 1
        (1, 3),  # 3
        (1, 4),  # 4  竖屏 panel (21:9 宽屏)
        (2, 2),  # 4
        (3, 2),  # 6  竖屏 panel
        (3, 3),  # 9
        (4, 3),  # 12 竖屏 panel
        (5, 3),  # 15 竖屏 panel
        (4, 4),  # 16
        (5, 4),  # 20 竖屏 panel
        (5, 5),  # 25
    ]

    for rows, cols in grid_options:
        capacity = rows * cols
        # 跳过超过最大限制的网格
        if capacity > max_grid:
            continue
        if beat_count <= capacity:
            return (rows, cols)

    # 超过所有可用网格，返回最大允许的网格
    for rows, cols in reversed(grid_options):
        if rows * cols <= max_grid:
            return (rows, cols)

    # 默认返回 1x1
    return (1, 1)


def perfect_grid_split(total_beats: int, max_grid: int = 12) -> list[str]:
    """完美分割 beats，使每个网格都正好填满，不需要任何填充 panel。

    内部代理到 pack_beats，使用无限池。

    Args:
        total_beats: 总 beat 数量
        max_grid: 最大网格容量（如 12 表示最大 4x3）

    Returns:
        mode_key 列表，如 ["3x2_9-16", "1x1_9-16"]
    """
    template = DEFAULT_POOL_TEMPLATE
    # 过滤超过 max_grid 的，然后 * 100 生成无限池
    pool = [mk for mk in template if REGEN_MODE_CONFIGS[mk]["capacity"] <= max_grid] * 100
    if not pool:
        smallest = min(template, key=lambda mk: REGEN_MODE_CONFIGS[mk]["capacity"])
        pool = [smallest] * 100
    return pack_beats(total_beats, pool)


def scene_grid_split(
    all_beats: List[dict],
    aspect_mode: str = "9:16",
    pool_template: List[str] | None = None,
    character_map: Dict[str, dict] | None = None,
) -> List[dict]:
    """按 scene_id 分组 beats，每组用最少网格数覆盖。

    将同一 scene_id 的 beat 聚合到同一网格，减少跨网格场景漂移。
    每个 scene_id 组根据 beat 数量自动选择最优网格尺寸。

    积木块序列: 2x2(4), 1x3(3), 1x2(2), 1x1(1)
    2x2 是最大网格，与 2K 1:1 配合效果最佳。

    Args:
        all_beats: 所有 beat 数据（含 scene_id / beat_number 字段）
        aspect_mode: 比例模式（"9:16", "1:1" 等），用于覆盖特定网格尺寸的比例
        pool_template: 自定义积木池模板，优先于 aspect_mode 选择

    Returns:
        网格计划列表，每项：
        {
            "scene_id": "家·餐厅",
            "rows": 2, "cols": 2,
            "mode_key": "2x2_1-1",
            "beats": [beat_dict, ...],
            "beat_numbers": [4, 5, 6, ...],
            "padding_count": 0,
        }
    """
    from collections import OrderedDict

    if pool_template:
        template = pool_template
    else:
        template = LOCATION_POOL_TEMPLATE
    overrides = LOCATION_ASPECT_CONFIGS.get(aspect_mode, {})

    # 1. 按 scene_id 聚合，保持首次出现顺序
    location_groups: OrderedDict[str, List[dict]] = OrderedDict()
    for beat in all_beats:
        loc = beat_scene_id(beat) or "未知场景"
        if loc not in location_groups:
            location_groups[loc] = []
        location_groups[loc].append(beat)

    # 1.5 粗粒度合并（家·客厅 + 家·餐厅 → 家）
    location_groups = _coalesce_locations(location_groups)

    # 2. 对每个 scene_id 组，按 per-beat composite 数细分后分别 pack
    result = []
    for loc, beats in location_groups.items():
        if character_map:
            # 按 per-beat composite 角色数分成轻量组（<= 2）和重量组（>= 3）
            light_beats = []  # <= 2 composite → 先用完整 pool，再 post-process
            heavy_beats = []  # >= 3 composite → cap 到 2x2
            for beat in beats:
                n = len(_get_beat_visual_composite_chars(beat, character_map))
                if n >= MANY_CHARS_REF_THRESHOLD:
                    heavy_beats.append(beat)
                else:
                    light_beats.append(beat)

            # 轻量组：智能 repack（按 composite 连续分组，尽量用大网格）
            if light_beats:
                light_entries = _pack_location_beats(
                    light_beats,
                    loc,
                    list(template),
                    overrides,
                )
                for entry in light_entries:
                    grid_cc = _count_batch_composite_chars(entry["beats"], character_map)
                    if (
                        grid_cc >= MANY_CHARS_REF_THRESHOLD
                        and len(entry["beats"]) > MANY_CHARS_MAX_CAPACITY
                    ):
                        # 智能拆分：按 composite 连续分组，≤2 的子组用完整 pool
                        result.extend(
                            _smart_repack_beats(
                                entry["beats"],
                                character_map,
                                list(template),
                                overrides,
                                loc,
                            )
                        )
                    else:
                        result.append(entry)
            # 重量组：智能 repack
            if heavy_beats:
                result.extend(
                    _smart_repack_beats(
                        heavy_beats,
                        character_map,
                        list(template),
                        overrides,
                        loc,
                    )
                )
        else:
            result.extend(
                _pack_location_beats(
                    beats,
                    loc,
                    list(template),
                    overrides,
                )
            )

    # 3. 合并小网格：连续的 1-beat 网格合并成更大的网格
    result = _merge_small_grids(result, template, character_map)

    return result


def _merge_small_grids(
    entries: List[dict],
    pool_template: List[str],
    character_map: Dict[str, dict] | None = None,
) -> List[dict]:
    """合并连续的小网格（1-2 beat）为更大的网格，减少 1x1 碎片。

    策略：扫描连续的小网格（capacity <= 2），累积 beats 后用 pack_beats
    重新分配到更大的网格（优先 2x3、2x2、1x2）。
    合并后仍遵守铁律：>= 3 composite 角色时 capacity <= 2。
    """
    merged = []
    small_buffer = []  # 暂存同 scene_id 的连续小网格

    def flush_buffer():
        """将累积的小网格 beats 重新 pack 成更大的网格。"""
        if not small_buffer:
            return
        all_beats = []
        for entry in small_buffer:
            all_beats.extend(entry["beats"])

        combined_loc = small_buffer[0].get("scene_id", "")

        # 铁律检查：合并后的 beats 如果 composite 角色 >= 阈值，用 smart_repack
        if character_map:
            n_comp = _count_batch_composite_chars(all_beats, character_map)
            if n_comp >= MANY_CHARS_REF_THRESHOLD:
                merged.extend(
                    _smart_repack_beats(
                        all_beats,
                        character_map,
                        list(pool_template),
                        overrides={},
                        loc=combined_loc,
                    )
                )
                return

        # 正常合并：用 pack_beats 重新分配
        pool = pool_template * 100
        mode_keys = pack_beats(len(all_beats), pool)

        offset = 0
        for mk in mode_keys:
            cfg = REGEN_MODE_CONFIGS[mk]
            rows, cols = cfg["rows"], cfg["cols"]
            capacity = cfg["capacity"]
            batch = all_beats[offset : offset + capacity]
            beat_numbers = [b.get("beat_number", i + 1) for i, b in enumerate(batch)]
            merged.append(
                {
                    "scene_id": combined_loc,
                    "rows": rows,
                    "cols": cols,
                    "mode_key": mk,
                    "beats": batch,
                    "beat_numbers": beat_numbers,
                    "padding_count": capacity - len(batch),
                }
            )
            offset += capacity

    for entry in entries:
        n_beats = len(entry["beats"])
        entry_scene = entry.get("scene_id", "")
        buffer_scene = small_buffer[0].get("scene_id", "") if small_buffer else None
        if n_beats <= 2:
            if small_buffer and entry_scene != buffer_scene:
                flush_buffer()
                small_buffer = []
            small_buffer.append(entry)
        else:
            # 遇到大网格，先 flush 之前的小网格
            flush_buffer()
            small_buffer = []
            merged.append(entry)

    # flush 尾部
    flush_buffer()

    return merged


def _pack_location_beats(
    beats: List[dict],
    loc: str,
    pool_template: List[str],
    overrides: dict,
) -> List[dict]:
    """将一个场景子组的 beats pack 成网格 entries。"""
    pool = pool_template * 100
    mode_keys = pack_beats(len(beats), pool)
    entries = []
    offset = 0
    for mk in mode_keys:
        cfg = REGEN_MODE_CONFIGS[mk]
        rows, cols = cfg["rows"], cfg["cols"]
        capacity = cfg["capacity"]
        batch = beats[offset : offset + capacity]
        beat_numbers = [b.get("beat_number", i + 1) for i, b in enumerate(batch)]
        entry = {
            "scene_id": loc,
            "rows": rows,
            "cols": cols,
            "mode_key": mk,
            "beats": batch,
            "beat_numbers": beat_numbers,
            "padding_count": capacity - len(batch),
        }
        grid_key = (rows, cols)
        if grid_key in overrides:
            ar, isz = overrides[grid_key]
            entry["mode_key"] = f"{rows}x{cols}_{ar.replace(':', '-')}"
        entries.append(entry)
        offset += capacity
    return entries


def build_regen_plan(
    selected_beats: list[dict],
    strategy: str,
    aspect_mode: str,
    character_map: dict | None = None,
    force_one_by_one: bool = False,
    image_generation_selection: str | None = None,
) -> list[PlanEntry]:
    """Build the canonical render plan for a selected beat set."""
    single_cell_reason = ""
    if force_one_by_one:
        single_cell_reason = "force-1x1"

    if single_cell_reason:
        mode_key = _single_cell_render_mode_key(aspect_mode)
        return [
            PlanEntry(
                mode_key=mode_key,
                rows=1,
                cols=1,
                beat_numbers=(int(beat["beat_number"]),),
                location=str(beat_scene_id(beat) or beat.get("location") or ""),
                reasons=(single_cell_reason,),
            )
            for beat in selected_beats
        ]

    pool_template = _render_pool_template_for_aspect(aspect_mode)

    if strategy == "location":
        raw_plan = scene_grid_split(
            list(selected_beats),
            aspect_mode=aspect_mode,
            pool_template=pool_template,
            character_map=character_map if character_map else None,
        )
        return [
            PlanEntry(
                mode_key=entry["mode_key"],
                rows=int(entry["rows"]),
                cols=int(entry["cols"]),
                beat_numbers=tuple(int(n) for n in entry["beat_numbers"]),
                location=str(entry.get("scene_id") or entry.get("location") or ""),
                padding_count=int(entry.get("padding_count") or 0),
                reasons=tuple(entry.get("reasons") or ()),
                warnings=tuple(entry.get("warnings") or ()),
            )
            for entry in raw_plan
        ]

    if strategy == "naive":
        mode_keys = pack_beats(len(selected_beats), list(pool_template))
        entries: list[PlanEntry] = []
        offset = 0
        for mode_key in mode_keys:
            cfg = REGEN_MODE_CONFIGS[mode_key]
            capacity = cfg["capacity"]
            batch = selected_beats[offset : offset + capacity]
            if not batch:
                break
            entries.append(
                PlanEntry(
                    mode_key=mode_key,
                    rows=int(cfg["rows"]),
                    cols=int(cfg["cols"]),
                    beat_numbers=tuple(int(beat["beat_number"]) for beat in batch),
                    padding_count=capacity - len(batch),
                )
            )
            offset += capacity
        return entries

    raise ValueError(f"unknown strategy: {strategy!r}")


def _get_beat_visual_composite_chars(beat: dict, character_map: dict) -> frozenset:
    """从 visual_description 的 {{}} 标记提取实际绘制的有参考图角色。

    Render 场景优先使用 detected_identities，确保铁律拆分与上色/导出使用同一套角色来源。
    若无 detected_identities，再回退到 visual_description 的 {{}} 标记。
    包括 composite 和 portrait_only 模式（都有参考图占 image slot）。
    """
    ref_names = sorted(
        [
            name
            for name, info in character_map.items()
            if info.get("reference_mode") != "prompt_only"
            and (info.get("reference_path") or info.get("ref_path") or info.get("portrait_path"))
        ],
        key=len,
        reverse=True,
    )
    detected = real_detected_identities(beat.get("detected_identities") or [])
    if detected:
        markers = list(detected)
    else:
        visual = beat.get("visual_description", "")
        markers = re.findall(r"\{\{([^}]+)\}\}", visual)
    result = set()
    for marker in markers:
        for char_name in ref_names:
            if marker == char_name or marker.startswith(char_name + "_"):
                result.add(char_name)
                break
    return frozenset(result)


def _flush_to_grids(
    beats: List[dict],
    composite_count: int,
) -> List[dict]:
    """将一组 beats 打包为一个或多个网格 entry。"""
    if not beats:
        return []
    if composite_count <= 2:
        template = CHARACTER_POOL_2  # 最大 2x2
    else:
        template = CHARACTER_POOL_3  # 最大 2x2
    # 铁律
    template = _cap_pool_for_many_chars(template, composite_count)
    pool = list(template) * 100
    mode_keys = pack_beats(len(beats), pool)

    entries = []
    offset = 0
    for mk in mode_keys:
        cfg = REGEN_MODE_CONFIGS[mk]
        capacity = cfg["capacity"]
        batch = beats[offset : offset + capacity]
        beat_numbers = [b.get("beat_number", i + 1) for i, b in enumerate(batch)]
        entries.append(
            {
                "rows": cfg["rows"],
                "cols": cfg["cols"],
                "mode_key": mk,
                "beats": batch,
                "beat_numbers": beat_numbers,
                "padding_count": capacity - len(batch),
                "composite_count": composite_count,
            }
        )
        offset += capacity
    return entries


def character_grid_split(
    all_beats: List[dict],
    character_map: Dict[str, dict],
) -> List[dict]:
    """按 composite 角色全局分组 beats，自动选择网格尺寸。

    三阶段算法：
    1. 分类：按 visual_description 的 {{}} 标记提取 composite 角色数
       - >2 chars → overflow（≤2x2 pool）
       - 0 chars  → empty（就近吸收）
       - 1-2 chars → normal（配对分组）
    2. 贪心 set-cover 配对：全局聚合同角色对的所有 beats
    3. pack 到网格：≤2 chars → CHARACTER_POOL_2（最大2x2），>2 chars → CHARACTER_POOL_3（最大2x2）

    Args:
        all_beats: 所有 beat 数据（含 visual_description / beat_number 字段）
        character_map: 角色映射 {角色名: {reference_mode, ...}}

    Returns:
        网格计划列表，格式与 scene_grid_split 对齐
    """
    if not all_beats:
        return []

    # ── 阶段 1：分类 ──
    overflow_beats: List[dict] = []  # >2 composite chars
    empty_beats: List[dict] = []  # 0 composite chars
    normal_beats: List[Tuple[int, dict, frozenset]] = []  # (index, beat, chars)

    for idx, beat in enumerate(all_beats):
        chars = _get_beat_visual_composite_chars(beat, character_map)
        if len(chars) > 2:
            overflow_beats.append(beat)
        elif len(chars) == 0:
            empty_beats.append(beat)
        else:
            normal_beats.append((idx, beat, chars))

    # ── 阶段 2：贪心 set-cover 配对 ──
    # 收集所有出现过的 1-char 和 2-char 候选对
    all_char_sets: set = set()
    for _, _, chars in normal_beats:
        all_char_sets.add(chars)

    # 枚举所有可能的 pair（1 或 2 个角色的组合）
    all_single_chars = set()
    for cs in all_char_sets:
        all_single_chars |= cs
    candidate_pairs: List[frozenset] = []
    single_list = sorted(all_single_chars)
    for i, c1 in enumerate(single_list):
        candidate_pairs.append(frozenset({c1}))
        for c2 in single_list[i + 1 :]:
            candidate_pairs.append(frozenset({c1, c2}))

    assigned = [False] * len(normal_beats)
    pair_groups: List[Tuple[frozenset, List[dict]]] = []  # (pair, beats)

    while True:
        # 找覆盖最多未分配 beats 的 pair
        best_pair = None
        best_indices = []
        for pair in candidate_pairs:
            indices = [
                i
                for i, (_, _, chars) in enumerate(normal_beats)
                if not assigned[i] and chars <= pair
            ]
            if len(indices) > len(best_indices):
                best_pair = pair
                best_indices = indices
        if not best_indices:
            break
        for i in best_indices:
            assigned[i] = True
        group_beats = [normal_beats[i][1] for i in best_indices]
        pair_groups.append((best_pair, group_beats))

    # ── empty beats 就近吸收到最近的 pair group ──
    if empty_beats and pair_groups:
        for eb in empty_beats:
            eb_num = eb.get("beat_number", 0)
            best_group_idx = 0
            best_dist = float("inf")
            for gi, (_, group_beats) in enumerate(pair_groups):
                for gb in group_beats:
                    dist = abs(gb.get("beat_number", 0) - eb_num)
                    if dist < best_dist:
                        best_dist = dist
                        best_group_idx = gi
            pair_groups[best_group_idx][1].append(eb)
    elif empty_beats:
        # 没有 pair group，empty beats 自成一组
        pair_groups.append((frozenset(), empty_beats))

    # ── 阶段 3：pack 到网格 ──
    result: List[dict] = []

    # pair groups → 4x4
    for pair, beats in pair_groups:
        # 按 beat_number 排序保持顺序
        beats.sort(key=lambda b: b.get("beat_number", 0))
        result.extend(_flush_to_grids(beats, len(pair)))

    # overflow → 批量 flush，让 pack_beats 自动打包
    if overflow_beats:
        overflow_beats.sort(key=lambda b: b.get("beat_number", 0))
        max_cc = max(
            len(_get_beat_visual_composite_chars(ob, character_map)) for ob in overflow_beats
        )
        result.extend(_flush_to_grids(overflow_beats, max_cc))

    # 按首个 beat_number 排序
    result.sort(key=lambda g: g["beat_numbers"][0] if g["beat_numbers"] else 0)

    return result


def sketch_grid_split(total_beats: int) -> list[tuple[int, int]]:
    """Sketch 模式专用分割：返回 1x 5x5 单张网格（共 25 panel 位）。

    按 SKETCH_GRID_PLAN 顺序分配，beats 不足时只使用需要的网格数量。
    """
    plan = []
    remaining = total_beats
    for grid_cfg in SKETCH_GRID_PLAN:
        if remaining <= 0:
            break
        r, c = grid_cfg["rows"], grid_cfg["cols"]
        plan.append((r, c))
        remaining -= r * c
    return plan if plan else [(1, 1)]


def _coalesce_locations(location_groups):
    """按 · 前缀合并细粒度场景组为粗粒度。

    "家·客厅", "家·餐厅" → "家"
    无 · 的场景保持原样。保持首次出现顺序。
    """
    from collections import OrderedDict

    coarse: OrderedDict[str, list] = OrderedDict()
    for fine_loc, beats in location_groups.items():
        prefix = fine_loc.split("·")[0].split("・")[0].strip()
        if not prefix:
            prefix = fine_loc
        if prefix not in coarse:
            coarse[prefix] = []
        coarse[prefix].extend(beats)
    return coarse


# (capacity, mode_key, rows, cols) — 按 capacity 升序
SKETCH_NXN_MODES = get_sketch_nxn_modes()


async def generate_text_to_image(
    prompt: str,
    output_path: str,
    *,
    aspect_ratio: str = "1:1",
    image_size: str = "2K",
    quality: str | None = None,
    api_key: Optional[str] = None,
    config: Optional[dict] = None,
    egress_capability: str = "image.generate",
) -> Path:
    """Generate one image from a prompt only — no reference images.

    `config` overrides the image selection picked from env defaults.
    """
    return await _generate_image(
        prompt=prompt,
        reference_image_paths=[],
        output_path=output_path,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        quality=quality,
        api_key=api_key,
        config=config,
        egress_capability=egress_capability,
    )


async def generate_reference_edit_image(
    prompt: str,
    reference_images: list[str],
    output_path: str,
    *,
    aspect_ratio: str = "2:3",
    image_size: str = "2K",
    quality: str | None = None,
    api_key: Optional[str] = None,
    config: Optional[dict] = None,
    egress_capability: str = "image.edit",
) -> Path:
    """Generate one edited image from reference images plus a free-form edit prompt.

    `config` lets callers override the provider / model selection (passed through
    to `NanoBananaGridGenerator`); when None, falls back to env-var defaults via
    `get_grid_generation_config()`.
    """
    ref_paths = [path for path in reference_images if path and os.path.exists(path)]
    if not ref_paths:
        raise FileNotFoundError("No valid reference images provided for edit generation")
    return await _generate_image(
        prompt=prompt,
        reference_image_paths=ref_paths,
        output_path=output_path,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        quality=quality,
        api_key=api_key,
        config=config,
        egress_capability=egress_capability,
    )


async def _generate_image(
    *,
    prompt: str,
    reference_image_paths: list[str],
    output_path: str,
    aspect_ratio: str,
    image_size: str,
    quality: str | None,
    api_key: Optional[str],
    config: Optional[dict],
    egress_capability: str,
) -> Path:
    """Shared body for text-only and image-edit single-image generation."""
    generator = NanoBananaGridGenerator(api_key=api_key, config=config)
    image_bytes, _, error_detail = await generate_image(
        generator.selection,
        prompt,
        refs=list(reference_image_paths or []),
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        quality=quality or generator.quality,
        output_path=output_path,
        usage={"task_type": egress_capability},
    )
    if not image_bytes:
        raise ValueError(f"{generator.selection} image generation failed: {error_detail}")
    return Path(output_path)


def _pick_nxn_mode(n: int, aspect_ratio: str = DEFAULT_SKETCH_ASPECT_RATIO):
    """选最小能装下 n beats 的 NxN 模式。"""
    modes = get_sketch_nxn_modes(aspect_ratio)
    for cap, mk, r, c in modes:
        if n <= cap:
            return mk, r, c, cap
    # fallback 到最大模式
    cap, mk, r, c = modes[-1]
    return mk, r, c, cap


def _group_beats_by_location(beats: List[dict]):
    """按 scene_id 字段分组 beats，保持出现顺序。返回 (scene_id, beats_list) 的列表。"""
    from collections import OrderedDict

    groups: OrderedDict = OrderedDict()
    for b in beats:
        loc = beat_scene_id(b) or "未知"
        groups.setdefault(loc, []).append(b)
    return list(groups.items())


def _is_space_map_beat(beat: dict) -> bool:
    visual = str((beat or {}).get("visual_description") or "").strip().lower()
    return (
        visual.startswith("[space_map")
        or visual.startswith("[space_anchor_map]")
        or visual.startswith("[absolute_layout_map]")
    )


def sketch_scene_grid_split(
    all_beats: List[dict],
    aspect_ratio: str = DEFAULT_SKETCH_ASPECT_RATIO,
) -> List[dict]:
    """草图：严格按 scene 切分，再为每个 scene 选择最小可容纳网格。"""
    modes = get_sketch_nxn_modes(aspect_ratio)
    max_cap = modes[-1][0]
    all_beats = [beat for beat in (all_beats or []) if not _is_space_map_beat(beat)]
    if not all_beats:
        cap, mk, r, c = modes[-1]
        return [
            {
                "scene_id": "",
                "rows": r,
                "cols": c,
                "mode_key": mk,
                "beats": [],
                "beat_numbers": [],
                "padding_count": cap,
            }
        ]

    result = []
    for scene_id, scene_beats in _group_beats_by_location(all_beats):
        offset = 0
        total = len(scene_beats)
        while offset < total:
            remaining = min(total - offset, max_cap)
            mk, r, c, cap = _pick_nxn_mode(remaining, aspect_ratio=aspect_ratio)
            chunk = scene_beats[offset : offset + cap]
            beat_numbers = [b.get("beat_number", 0) for b in chunk]
            result.append(
                {
                    "scene_id": scene_id,
                    "rows": r,
                    "cols": c,
                    "mode_key": mk,
                    "beats": list(chunk),
                    "beat_numbers": beat_numbers,
                    "padding_count": cap - len(chunk),
                }
            )
            offset += cap
    return result


def find_sketch_for_beat_range(
    sketch_dir: str, beat_start: int, beat_end: int
) -> Optional[Tuple[str, int, int]]:
    """在草图目录中查找覆盖指定 beat 范围的草图文件。

    Args:
        sketch_dir: 草图目录路径
        beat_start: 起始 beat 编号（1-based）
        beat_end: 结束 beat 编号（1-based，含）

    Returns:
        (文件路径, rows, cols) 或 None。
        文件命名约定: sketch_b{start}-{end}_{rows}x{cols}.jpg
    """
    candidates = []
    for f in Path(sketch_dir).glob("sketch_b*_*x*.jpg"):
        name = f.stem  # e.g., "sketch_b1-25_5x5"
        try:
            parts = name.split("_b")[1].split("_")
            s, e = parts[0].split("-")
            r, c = parts[1].split("x")
            file_start, file_end = int(s), int(e)
            if file_start <= beat_start and beat_end <= file_end:
                span = file_end - file_start
                candidates.append((span, str(f), int(r), int(c)))
        except (IndexError, ValueError):
            continue
    if not candidates:
        return None
    # 选择覆盖范围最小的（最精确匹配）
    candidates.sort(key=lambda x: x[0])
    _, path, rows, cols = candidates[0]
    return path, rows, cols


class NanoBananaGridGenerator:
    """NanoBananaPro 网格生成器。

    支持多种模式（统一使用批量生成，动态优化）:
    - "1x1": 单张生成（1K 分辨率）
    - "1x3": 横向三格
    - "2x2": 紧凑四格
    - "3x3": 分批生成多个 3x3 网格
    - "4x4": 中等平衡
    - "5x5": 批量生成，最大 25 个分镜（动态优化）

    使用 Google AI Studio 的 Gemini Pro Image 模型生成分镜网格。

    示例:
        >>> generator = NanoBananaGridGenerator()
        >>> # 3x3 模式批量生成
        >>> results = await generator.generate_grid_batch(
        ...     all_beats=beats_data,
        ...     character_map=char_map,
        ...     output_dir="output/grids"
        ... )
    """

    def __init__(self, api_key: Optional[str] = None, config: Optional[dict] = None):
        """初始化生成器。

        Args:
            api_key: unused, kept for callers; engines hold their own credentials.
            config: `get_grid_generation_config()` shape; `selection` (or
                `provider` + `model`) picks the image engine.
        """
        from novelvideo.config import infer_image_generation_selection

        config = config or get_grid_generation_config()
        self.selection = config.get("selection") or infer_image_generation_selection(
            config.get("provider"), config.get("model")
        )
        self.provider = self.selection.split(":", 1)[0]
        self.model = self.selection.split(":", 1)[-1]
        self.default_image_size = config.get("image_size", "1K")
        self.quality = config.get("quality")
        self.mode = config.get("mode", "3x3")
        self.rows = config["rows"]
        self.cols = config["cols"]
        self.batch_size = config.get("batch_size", self.rows * self.cols)
        self.total_panels = config["total_panels"]
        print(f"[NanoBanana Grid] Image engine: {self.selection}")

    async def generate_grid(
        self,
        beats: List[dict],
        character_map: Dict[str, dict] = None,
        scene_menu: list[dict] | list | None = None,
        prop_menu: list[dict] | list | None = None,
        sketch_colors: Dict[str, str] = None,
        style: str = None,  # 默认使用全局风格配置
        output_path: Optional[str] = None,
        ethnicity: str = "Chinese",
        rows: int = None,  # 可配置行数，默认使用配置值
        cols: int = None,  # 可配置列数，默认使用配置值
        sketch: bool = False,  # 是否生成草图模式
        prompt_only: bool = False,  # Dry Run 模式：只生成提示词，不调用 API
        beat_start_index: int = 0,  # Render 模式：当前 grid 的 beat 起始索引（用于从 sketch 切片）
        total_episode_beats: int = 0,  # Render 模式：整集 beat 总数（用于计算 sketch 尺寸）
        location_beat_numbers: List[int] = None,  # 场景分组的原始 beat 编号（1-based）
        explicit_episode_number: Optional[
            int
        ] = None,  # 调用方已知的集数，避免从路径反推
        scene_refs_override: dict[int, list[Any]] | None = None,
        prop_refs_override: dict[int, list[Any]] | None = None,
        sketch_dir: str = "",  # 草图目录路径（由调用方通过 PathResolver 计算）
        aspect_ratio_override: Optional[
            str
        ] = None,  # 覆盖 aspect_ratio（再生模式使用）
        image_size_override: Optional[str] = None,  # 覆盖 image_size（再生模式使用）
        mode_key: Optional[str] = None,  # mode_key 查表取 aspect_ratio/image_size
        prompt_aspect_ratio: Optional[
            str
        ] = None,  # 覆盖 prompt 中的比例（two-pass: 图用 1:1，prompt 用 9:16）
        beat_sketch_paths: dict = None,  # {beat_num: full_path} 从图片池取的 per-beat 草图路径
        sketch_aspect_padding: bool = False,  # 草图补白到目标比例
        force_image_size: Optional[str] = None,  # 强制覆盖 image_size（如 "0.5K"）
        use_director_refs: bool = False,  # 是否优先使用 beat 级导演参考图
        director_sheet_path: Optional[str] = None,  # 当前 grid 的 DirectorWorld sheet
        director_ref_beat_numbers: Optional[
            List[int]
        ] = None,  # 仅这些 beat 使用导演参考
        director_control_frames_dir: str | Path | None = None,
    ) -> GridGenerationResult:
        """生成网格图。

        参考模式由上游 build_character_map_for_grid() 决定：
        - composite: 复合参考图（Portrait + Fullbody 拼接），锁脸 + 锁服装
        - portrait_only: 仅面部特写，锁脸，服装由 appearance_details 文字控制
        - prompt_only: 无参考图，完全由提示词控制

        Args:
            beats: Beats 数据列表（不足网格容量用黑色填充）
            character_map: 角色映射 {角色名: {
                'character_tag': ...,
                'base_prompt': ...,
                'appearance_details': ...,
                'portrait_path': ...,  # 面部特写图（用于锁脸）
                'ref_path': ...,  # 参考图路径
                'reference_mode': ...,  # composite / portrait_only / prompt_only
            }}
            style: 全局风格名称 (chinese_period_drama, anime, realistic)，
                   默认使用 IMAGE_DEFAULT_STYLE
            output_path: 输出路径
            ethnicity: 角色默认种族（默认 "Chinese"），用于确保生成正确的面部特征
            rows: 网格行数（默认使用配置值）
            cols: 网格列数（默认使用配置值）

        Returns:
            GridGenerationResult
        """
        # 使用传入的 rows/cols 或默认配置值
        rows = rows or self.rows
        cols = cols or self.cols
        grid_capacity = rows * cols
        start_time = time.time()
        character_map = character_map or {}
        previous_grid_path = None  # Render 模式会在内部设置草图路径

        # 使用全局默认风格
        if style is None:
            style = IMAGE_DEFAULT_STYLE
        print(f"[NanoBananaPro] 使用风格: {style}, 网格: {rows}x{cols}")

        if len(beats) < 1:
            return GridGenerationResult(
                success=False,
                error="需要至少 1 个 beat，当前没有 beats",
                generation_time=time.time() - start_time,
            )

        # 如果不足网格容量，后面会用黑色填充
        actual_beat_count = min(len(beats), grid_capacity)
        print(
            f"[NanoBananaPro] 有效 beats: {actual_beat_count}/{grid_capacity}，不足部分用黑色填充"
        )
        if not sketch and sketch_dir:
            detection_error = render_ai_detection_error(beats[:grid_capacity])
            if detection_error:
                print(f"[NanoBananaPro] ❌ {detection_error}")
                return GridGenerationResult(
                    success=False,
                    error=detection_error,
                    generation_time=time.time() - start_time,
                )

        try:
            # 验证参考图存在 - 信任上游 reference_mode，只做文件存在性确认
            valid_character_map = {}
            for char_name, info in character_map.items():
                char_info = dict(info)
                ref_path = info.get("ref_path") or info.get("portrait_path")
                upstream_mode = info.get("reference_mode", "prompt_only")

                if (
                    upstream_mode == "composite" and ref_path and os.path.exists(ref_path)
                ):
                    char_info["reference_path"] = ref_path
                    char_info["reference_mode"] = "composite"
                    print(f"[NanoBananaPro] {char_name}: 复合图模式 -> {ref_path}")
                    valid_character_map[char_name] = char_info
                    continue

                if ref_path and os.path.exists(ref_path):
                    char_info["reference_path"] = ref_path
                    char_info["reference_mode"] = "portrait_only"
                    print(
                        f"[NanoBananaPro] {char_name}: Portrait 模式（仅锁脸）-> {ref_path}"
                    )
                    valid_character_map[char_name] = char_info
                    continue

                char_info["reference_path"] = None
                char_info["reference_mode"] = "prompt_only"
                print(f"[NanoBananaPro] {char_name}: 提示词模式（无参考图）")
                valid_character_map[char_name] = char_info

            # 1. 构建网格 Prompt
            # 分流：Sketch 模式 vs Render 模式
            # 统一使用 UnifiedPromptBuilder 以确保导出和生成使用相同的提示词
            is_render_mode = False  # 标记是否为 Render 模式
            project_dir = _infer_project_dir(output_path, sketch_dir)
            episode_number = (
                int(explicit_episode_number)
                if explicit_episode_number is not None
                else infer_episode_from_path(output_path) or infer_episode_from_path(sketch_dir)
            )
            scene_refs: dict[int, list[Any]] = {}
            prop_asset_refs: dict[int, list[Any]] = {}
            scene_refs, prop_asset_refs = _resolve_scene_prop_asset_refs(
                project_dir,
                beats[:grid_capacity],
                episode_number=episode_number,
                sketch=sketch,
                use_director_refs=use_director_refs,
                include_pano_view_refs=False,
                director_ref_beat_numbers=director_ref_beat_numbers,
                director_control_frames_dir=director_control_frames_dir,
                scene_menu=scene_menu,
                prop_menu=prop_menu,
                allow_beat_background_anchor=(
                    actual_beat_count == 1 and int(rows or 0) == 1 and int(cols or 0) == 1
                ),
            )
            if scene_refs_override is not None:
                scene_refs = {
                    int(panel_idx): list(refs or [])
                    for panel_idx, refs in scene_refs_override.items()
                }
            if prop_refs_override is not None:
                prop_asset_refs = {
                    int(panel_idx): list(refs or [])
                    for panel_idx, refs in prop_refs_override.items()
                }
            if (
                sketch
                and use_director_refs
                and director_sheet_path
                and os.path.exists(director_sheet_path)
            ):
                from novelvideo.utils.asset_resolver import ResolvedAssetRef

                director_sheet_ref = ResolvedAssetRef(
                    asset_type="scene",
                    base_id=beats[0].get("scene_id") or beats[0].get("scene") or "DirectorWorld",
                    variant_id=None,
                    image_paths=[director_sheet_path],
                    text_description="DirectorWorld blocking reference sheet",
                    source_level="director_sheet",
                )
                selected_director_beats = {
                    int(bn) for bn in (director_ref_beat_numbers or []) if bn is not None
                }
                for panel_idx in range(1, actual_beat_count + 1):
                    if selected_director_beats:
                        beat_num = beats[panel_idx - 1].get("beat_number", panel_idx)
                        if int(beat_num or 0) not in selected_director_beats:
                            continue
                    refs = scene_refs.setdefault(panel_idx, [])
                    refs.insert(0, director_sheet_ref)
            # 过滤为当前网格出场角色。scene_refs 先解析，后续可按参考图上下文收窄角色集。
            if sketch:
                valid_character_map = filter_character_map_for_beats(
                    valid_character_map,
                    beats[:grid_capacity],
                    scene_refs=scene_refs if use_director_refs else None,
                )
            if sketch and use_director_refs:
                has_director_sheet = bool(
                    director_sheet_path and os.path.exists(director_sheet_path)
                )
                if has_director_sheet:
                    print(
                        f"[DirectorSheet] 使用 DirectorWorld sheet: {director_sheet_path}"
                    )
                elif actual_beat_count != 1 or rows != 1 or cols != 1:
                    return GridGenerationResult(
                        success=False,
                        error=(
                            "导演参考图模式只支持单 beat 1x1；"
                            "批量草图请先导出对应 DirectorWorld 控制图。"
                        ),
                        generation_time=time.time() - start_time,
                    )
                if not has_director_sheet and not _has_director_image_ref(
                    scene_refs, panel_idx=1
                ):
                    return GridGenerationResult(
                        success=False,
                        error=(
                            "导演单镜缺少 beat 级 3GS control frame；"
                            "草图主线不再回退到旧场景参考图。"
                        ),
                        generation_time=time.time() - start_time,
                    )
                if not has_director_sheet:
                    _prepare_director_blocking_refs(
                        scene_refs=scene_refs,
                        beats=beats[:grid_capacity],
                        character_map=valid_character_map,
                    )
            style_family, animation_subtype = StyleService.get_style_branch(
                style or IMAGE_DEFAULT_STYLE,
                project_dir=project_dir,
            )

            if sketch:
                # Sketch 模式使用 UnifiedPromptBuilder（与导出逻辑一致）
                print("[NanoBananaPro] 进入 Sketch 模式")

                # 当前网格的全局 beat 范围 (1-based)
                grid_beat_start = beat_start_index + 1
                grid_beat_end = beat_start_index + grid_capacity

                # prompt_aspect_ratio 优先（two-pass 时图用 1:1 但 prompt 写 2:3）
                _prompt_ar = prompt_aspect_ratio or (
                    REGEN_MODE_CONFIGS[mode_key]["aspect_ratio"] if mode_key else None
                )
                # image_aspect_ratio = 实际输出比例（two-pass Pass1 时为 1:1，否则与 prompt_ar 相同）
                _image_ar = (
                    REGEN_MODE_CONFIGS[mode_key]["aspect_ratio"] if mode_key else ""
                )
                ctx = create_prompt_context(
                    mode=PromptMode.SKETCH,
                    beats=beats[:grid_capacity],
                    rows=rows,
                    cols=cols,
                    character_map=valid_character_map,
                    style=style,
                    ethnicity=ethnicity,
                    aspect_ratio=_prompt_ar,
                    image_aspect_ratio=_image_ar,
                    scene_refs=scene_refs,
                    prop_asset_refs=prop_asset_refs,
                    sketch_colors=sketch_colors or {},
                    prop_marker_colors=_global_prop_marker_colors(
                        beats[:grid_capacity],
                        prop_menu,
                        sketch_colors=sketch_colors or {},
                    ),
                    style_family=style_family,
                    animation_subtype=animation_subtype,
                    project_dir=str(project_dir) if project_dir else "",
                    image_provider=self.provider,
                    image_model=self.model,
                )
                from novelvideo.verification.failure_registry import (
                    load_negative_clause_for_project,
                )

                ctx.registry_negative_clause = await load_negative_clause_for_project(
                    str(project_dir) if project_dir else None, "generator"
                )
                builder = UnifiedPromptBuilder(ctx)
                prompt = builder.build()

            elif sketch_dir:
                # Render 模式：通过 find_sketch_for_beat_range 在草图目录中定位对应的草图
                sketch_dir_path = sketch_dir

                # 确定 beat 编号列表 — 始终从 beats 自身提取，避免与外部参数不同步
                actual_beats = beats[:grid_capacity]
                actual_beat_numbers = [
                    _generation_beat_number(b, i) for i, b in enumerate(actual_beats)
                ]

                beat_range_start = min(actual_beat_numbers)
                beat_range_end = max(actual_beat_numbers)

                sketch_result = find_sketch_for_beat_range(
                    sketch_dir_path, beat_range_start, beat_range_end
                )
                has_all_pool_sketches = beat_sketch_paths and all(
                    bn in beat_sketch_paths for bn in actual_beat_numbers
                )
                if sketch_result is None and not has_all_pool_sketches:
                    print(
                        f"[Render] 警告：未找到覆盖 beat {beat_range_start}-{beat_range_end} 的草图"
                    )

                if sketch_result or has_all_pool_sketches:
                    print("[NanoBananaPro] 进入 Render 模式 (基于草图渲染)")
                    if has_all_pool_sketches:
                        print(
                            f"[Render] 使用图片池草图: {len(beat_sketch_paths)} 个 beat"
                        )
                    elif sketch_result:
                        sketch_file, s_rows, s_cols = sketch_result
                        print(f"[Render] 使用草图: {sketch_file} ({s_rows}x{s_cols})")
                    is_render_mode = True

                    # Render 模式：先切片草图，再用颜色检测过滤角色
                    if output_path:
                        temp_dir = Path(output_path).parent
                    else:
                        temp_dir = Path("output")
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    sub_sketch_path = str(temp_dir / "temp_sub_sketch.jpg")

                    target_aspect = None
                    if sketch_aspect_padding and mode_key:
                        target_aspect = cell_aspect_ratio(mode_key)

                    sub_sketch_path = crop_sketch_panels(
                        sketch_path=sketch_dir_path,
                        beat_numbers=actual_beat_numbers,
                        target_rows=rows,
                        target_cols=cols,
                        output_path=sub_sketch_path,
                        beat_sketch_paths=beat_sketch_paths,
                        target_aspect=target_aspect,
                    )
                    print(
                        f"[Render] 草图切片: beat_numbers={actual_beat_numbers} -> {rows}x{cols}"
                    )
                    print(f"[Render] 子草图已保存: {sub_sketch_path}")

                    # 用切片后的草图作为参考
                    previous_grid_path = sub_sketch_path

                    # 读取预计算的 per-beat 身份检测结果（草图工作台已完成检测）
                    _panel_det = load_precomputed_panel_detected(
                        actual_beat_numbers, beats
                    )
                    valid_character_map = filter_character_map_by_precomputed(
                        valid_character_map, _panel_det
                    )

                    # 使用 UnifiedPromptBuilder（与导出逻辑一致）
                    ctx = create_prompt_context(
                        mode=PromptMode.RENDER,
                        beats=beats[:grid_capacity],
                        rows=rows,
                        cols=cols,
                        character_map=valid_character_map,
                        style=style,
                        ethnicity=ethnicity,
                        aspect_ratio=(
                            REGEN_MODE_CONFIGS[mode_key]["aspect_ratio"] if mode_key else None
                        ),
                        panel_detected_keys=_panel_det,
                        scene_refs=scene_refs,
                        prop_asset_refs=prop_asset_refs,
                        sketch_colors=sketch_colors or {},
                        style_family=style_family,
                        animation_subtype=animation_subtype,
                        project_dir=str(project_dir) if project_dir else "",
                    )
                    builder = UnifiedPromptBuilder(ctx)
                    prompt = builder.build()
                else:
                    # 草图未找到，明确报错终止（不 fallback）
                    msg = f"Render 模式需要草图但未找到覆盖 beat {beat_range_start}-{beat_range_end} 的草图"
                    print(f"[NanoBananaPro] ❌ {msg}")
                    return GridGenerationResult(
                        success=False,
                        error=msg,
                    )
            else:
                # 需要草图或草图目录
                msg = "generate_grid() 需要 sketch 或 sketch_dir 参数"
                print(f"[NanoBananaPro] ❌ {msg}")
                return GridGenerationResult(
                    success=False,
                    error=msg,
                )

            print(
                f"[NanoBananaPro] 构建 Prompt 完成，共 {len(beats[:grid_capacity])} 个分镜"
            )

            # 保存 prompt 到文件（审计用）
            # 目录结构: grids/ep001/2x2/prompts/grid_01.prompt.txt
            if output_path:
                grid_dir = Path(output_path).parent  # grids/ep001/2x2
                prompts_dir = grid_dir / "prompts"  # grids/ep001/2x2/prompts
                prompts_dir.mkdir(parents=True, exist_ok=True)
                grid_basename = Path(output_path).stem  # "grid_01"
                prompt_file = prompts_dir / f"{grid_basename}.prompt.txt"
                prompt_file.write_text(prompt, encoding="utf-8")
                print(f"[NanoBananaPro] Grid Prompt 已保存: {prompt_file}")

            # Prompt-Only 模式：只生成提示词，跳过 API 调用
            if prompt_only:
                print("[NanoBananaPro] Prompt-Only 模式，跳过 API 调用")
                # 在 Render 模式下，显示 sketch 切片信息（用于验证）
                if is_render_mode:
                    sketch_capacity = (
                        SKETCH_GRID_CONFIG["rows"] * SKETCH_GRID_CONFIG["cols"]
                    )
                    local_offset = beat_start_index % sketch_capacity
                    end_index = local_offset + len(beats[:grid_capacity])
                    print(
                        f"[NanoBananaPro] [Render 预览] 草图 {SKETCH_GRID_CONFIG['rows']}x{SKETCH_GRID_CONFIG['cols']}"
                    )
                    print(
                        f"[NanoBananaPro] [Render 预览] 本地切片: [{local_offset}:{end_index}] (共 {end_index - local_offset} panels)"
                    )
                return GridGenerationResult(
                    success=True,
                    grid_image_path=None,
                    error=None,
                    generation_time=time.time() - start_time,
                )

            project_output_dir = infer_project_output_dir(output_path or sketch_dir)
            scope_beat_numbers = [
                int(b) for b in (location_beat_numbers or []) if b is not None
            ]
            if not scope_beat_numbers:
                scope_beat_numbers = [
                    _generation_beat_number(beat, beat_start_index + idx)
                    for idx, beat in enumerate(beats[:grid_capacity])
                ]
            first_beat_num = scope_beat_numbers[0] if scope_beat_numbers else None
            task_type = "sketch_grid" if sketch else "render_grid"
            scope = f"{task_type}:{mode_key or f'{rows}x{cols}'}:{'-'.join(str(b) for b in scope_beat_numbers)}"

            def _usage_fail(error_message: str) -> GridGenerationResult:
                return GridGenerationResult(
                    success=False,
                    error=error_message,
                    generation_time=time.time() - start_time,
                )

            def _usage_success(
                final_output_path: str | None, final_bytes: bytes | None
            ) -> GridGenerationResult:
                return GridGenerationResult(
                    success=True,
                    grid_image_path=final_output_path,
                    grid_image_bytes=final_bytes,
                    generation_time=time.time() - start_time,
                )

            # =================================================================
            # Render 模式 / Sketch 模式（统一使用单次 API 调用）
            # =================================================================

            # 2. 准备参考图
            # 按照 Google 官方文档：prompt 在前，图像连续排列在后
            # https://ai.google.dev/gemini-api/docs/image-generation
            contents = [prompt]  # prompt 放在最前面
            submitted_refs: list[dict] = []

            # Render 模式：草图必须是 Image 1。它是唯一构图底图；角色/场景/道具只提供身份和材质。
            # Render 模式传角色/身份/场景/道具；Sketch 模式只传场景参考图。
            # 道具在草图阶段只保留名称和 marker 颜色，不传道具参考图，避免最终
            # 材质/三视图干扰 blocking。
            if not sketch and previous_grid_path and os.path.exists(previous_grid_path):
                previous_grid_image = self._load_image_as_part(previous_grid_path)
                if previous_grid_image:
                    contents.append(previous_grid_image)
                    submitted_refs.append(
                        {
                            "kind": "previous_grid",
                            "base_id": "sketch",
                            "path": previous_grid_path,
                            "bytes": (
                                os.path.getsize(previous_grid_path)
                                if os.path.exists(previous_grid_path)
                                else None
                            ),
                        }
                    )
                    print(
                        "[NanoBananaPro] 添加草图底图 (Image 1 composition lock): "
                        f"{previous_grid_path}"
                    )

            if sketch:
                if use_director_refs:
                    self._append_reference_parts_from_plan(
                        contents,
                        ctx,
                        [],
                        valid_character_map,
                        allowed_kinds={"scene"},
                        verbose=True,
                        audit_refs=submitted_refs,
                    )
                elif scene_refs or prop_asset_refs:
                    self._append_reference_parts_from_plan(
                        contents,
                        ctx,
                        [],
                        valid_character_map,
                        allowed_kinds={"scene"},
                        verbose=True,
                        audit_refs=submitted_refs,
                    )
            else:
                ordered_chars = resolve_render_reference_order(
                    ctx, beats, grid_capacity, valid_character_map
                )

                print(f"[NanoBananaPro] 角色参考图顺序: {ordered_chars}")
                self._append_reference_parts_from_plan(
                    contents,
                    ctx,
                    ordered_chars,
                    valid_character_map,
                    allowed_kinds=None,
                    verbose=True,
                    audit_refs=submitted_refs,
                )

            if output_path:
                grid_dir = Path(output_path).parent
                prompts_dir = grid_dir / "prompts"
                prompts_dir.mkdir(parents=True, exist_ok=True)
                grid_basename = Path(output_path).stem
                submitted_file = prompts_dir / f"{grid_basename}.submitted.json"
                submitted_payload = {
                    "provider": self.provider,
                    "model": self.model,
                    "prompt": prompt,
                    "reference_images": submitted_refs,
                }
                submitted_file.write_text(
                    json.dumps(submitted_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"[NanoBananaPro] Submitted Prompt/Refs 已保存: {submitted_file}")

            # contents 结构:
            # - Render 模式: [prompt, char/scene/prop refs..., sketch(最后)]
            # - 普通 Sketch 模式: [prompt]
            # - Director Sketch 模式: [prompt, director scene/prop refs...]

            # 3. 调用 API
            # 从 mode_key 或 REGEN_MODE_CONFIGS 查找宽高比和分辨率
            if aspect_ratio_override:
                # 再生模式：使用显式指定的 aspect_ratio 和 image_size
                aspect_ratio = aspect_ratio_override
                image_size = image_size_override or "2K"
            elif mode_key:
                _cfg = REGEN_MODE_CONFIGS[mode_key]
                aspect_ratio = _cfg["aspect_ratio"]
                image_size = _cfg["image_size"]
            elif sketch:
                # Sketch 模式：使用独立配置
                aspect_ratio = SKETCH_GRID_CONFIG["aspect_ratio"]
                image_size = SKETCH_GRID_CONFIG["image_size"]
            else:
                # 从 (rows, cols) 在 REGEN_MODE_CONFIGS 中查找
                _found = False
                for _mk, _cfg in REGEN_MODE_CONFIGS.items():
                    if _cfg["rows"] == rows and _cfg["cols"] == cols:
                        aspect_ratio = _cfg["aspect_ratio"]
                        image_size = _cfg["image_size"]
                        _found = True
                        break
                if not _found:
                    if rows == cols:
                        aspect_ratio = "1:1"
                        image_size = "4K" if rows >= 4 else "2K"
                    elif rows > cols:
                        aspect_ratio = "9:16"
                        image_size = "4K"
                    else:
                        aspect_ratio = "21:9"
                        image_size = "2K"

            if force_image_size:
                image_size = force_image_size

            prompt_text, ref_bytes = self._extract_ref_bytes_from_contents(
                contents, include_mime=True
            )
            print(
                f"[NanoBananaPro] 调用 {self.selection} 生成网格图 "
                f"(分辨率: {image_size}, 比例: {aspect_ratio})..."
            )
            image_bytes, _text, engine_error = await generate_image(
                self.selection,
                prompt_text,
                refs=ref_bytes,
                aspect_ratio=aspect_ratio,
                image_size=normalize_image_size(image_size),
                quality=self.quality,
                output_path=output_path,
                usage={
                    "project_output_dir": project_output_dir,
                    "task_type": task_type,
                    "scope": scope,
                    "episode": infer_episode_from_path(output_path),
                    "beat_num": first_beat_num,
                },
            )
            if not image_bytes:
                message = "图像引擎未返回图像数据"
                if engine_error:
                    message = f"{message}: {engine_error}"
                return _usage_fail(message)

            # 5. 保存文件
            if output_path:
                output_dir = os.path.dirname(output_path)
                if output_dir:
                    os.makedirs(output_dir, exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(image_bytes)
                print(f"[NanoBananaPro] 网格图已保存: {output_path}")

                # 5.1 后处理：移除面板间缝隙并覆盖
                try:
                    from novelvideo.generators.grid_splitter import remove_grid_gaps
                    from PIL import Image as PILImage

                    grid_img = PILImage.open(output_path)
                    processed_grid = remove_grid_gaps(grid_img, rows, cols)
                    if processed_grid is not grid_img:
                        processed_grid.save(output_path)
                    # 更新 image_bytes 以保持返回值一致
                    with open(output_path, "rb") as f:
                        image_bytes = f.read()
                    print(f"[NanoBananaPro] Gap removal 后处理完成: {output_path}")
                except Exception as e:
                    print(f"[NanoBananaPro] Gap removal 失败，保留原图: {e}")

            generation_time = time.time() - start_time
            print(f"[NanoBananaPro] 生成完成，耗时 {generation_time:.1f}s")

            return _usage_success(output_path, image_bytes)

        except Exception as e:
            if is_fatal_billing_error(e):
                raise
            return GridGenerationResult(
                success=False,
                error=str(e),
                generation_time=time.time() - start_time,
            )

    async def reformat_sketch(
        self,
        source_path: str,
        output_path: str,
        target_aspect: str = "9:16",
        target_size: str = "1K",
        rows: int = 0,
        cols: int = 0,
    ) -> GridGenerationResult:
        """Second pass: 保持分镜构图，转换宽高比（1:1 → 9:16）。

        读取 Pass 1 保存的完整提示词，连同草图一起发给 Gemini，
        确保模型理解每个 panel 的内容，只改比例不丢信息。

        Args:
            source_path: Pass 1 生成的 1:1 草图路径
            output_path: 输出 9:16 草图路径
            target_aspect: 目标宽高比
            target_size: 目标分辨率
            rows: 网格行数（0 = 从文件名推断）
            cols: 网格列数（0 = 从文件名推断）

        Returns:
            GridGenerationResult
        """
        start_time = time.time()
        try:
            # 使用传入的 rows/cols，否则从路径推断（格式: sketch_g0_5x5_pass1.jpg）
            if rows and cols:
                grid_rows, grid_cols = rows, cols
            else:
                _m = re.search(r"(\d+)x(\d+)", os.path.basename(source_path))
                grid_rows = int(_m.group(1)) if _m else 5
                grid_cols = int(_m.group(2)) if _m else 5

            # 读取 Pass 1 保存的完整提示词（generate_grid 自动存到 prompts/ 目录）
            source_dir = Path(source_path).parent
            source_stem = Path(source_path).stem  # e.g. "sketch_g0_5x5_pass1"
            prompt_file = source_dir / "prompts" / f"{source_stem}.prompt.txt"

            original_prompt = ""
            if prompt_file.exists():
                original_prompt = prompt_file.read_text(encoding="utf-8")
                print(
                    f"[Reformat] 读取 Pass 1 提示词: {prompt_file} ({len(original_prompt)} chars)"
                )

            # reformat_sketch 现在只处理 outpaint（1:1 → 2:3）
            # 9:16 已改为 one-pass 直接生成，不再走 two-pass
            reformat_instruction = (
                f"This is a {grid_rows}x{grid_cols} storyboard grid where each panel is 1:1 (square).\n"
                f"OUTPAINT every panel from 1:1 to {target_aspect} — extend each scene vertically "
                f"(add space above and below) while keeping the original content centered.\n"
                f"Do NOT crop, stretch, or rearrange. Just extend each panel's background/environment vertically."
            )
            if original_prompt:
                import re as _re

                structural_prompt = original_prompt
                cut_tail = _re.search(
                    r"\n(?:DIRECTING GUIDELINES|SCENE DESCRIPTIONS)",
                    structural_prompt,
                )
                if cut_tail:
                    structural_prompt = structural_prompt[: cut_tail.start()]
                structural_prompt = _re.sub(
                    r"\nROLE:.*?(?=\nSTYLE:|\nLAYOUT:)",
                    "",
                    structural_prompt,
                    flags=_re.DOTALL,
                )
                prompt = f"{reformat_instruction}\n\n{structural_prompt}"
                print(
                    f"[Reformat] Outpaint 模式，提示词精简: {len(original_prompt)} → {len(prompt)} chars"
                )
            else:
                prompt = reformat_instruction

            # 保存 Pass 2 prompt 到文件（审计用）
            output_stem = Path(output_path).stem  # e.g. "sketch_g0_5x5"
            pass2_prompt_file = source_dir / "prompts" / f"{output_stem}.prompt.txt"
            pass2_prompt_file.parent.mkdir(parents=True, exist_ok=True)
            pass2_prompt_file.write_text(prompt, encoding="utf-8")
            print(f"[Reformat] Pass 2 Prompt 已保存: {pass2_prompt_file} ({len(prompt)} chars)")

            ref_image = self._load_image_as_part(source_path)
            contents = [prompt, ref_image]

            print(f"[Reformat] 调用 {self.selection} 转换 → {target_aspect} ...")
            prompt_text, ref_bytes = self._extract_ref_bytes_from_contents(
                contents, include_mime=True
            )
            image_bytes, _text, engine_error = await generate_image(
                self.selection,
                prompt_text,
                refs=ref_bytes,
                aspect_ratio=target_aspect,
                image_size=normalize_image_size(target_size),
                quality=self.quality,
                output_path=output_path,
                usage={"task_type": "sketch_reformat"},
            )
            if not image_bytes:
                return GridGenerationResult(
                    success=False,
                    error=f"[Reformat] 图像引擎未返回图像数据: {engine_error}",
                    generation_time=time.time() - start_time,
                )

            # 保存
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(image_bytes)

            generation_time = time.time() - start_time
            print(f"[Reformat] 完成 → {output_path}，耗时 {generation_time:.1f}s")

            return GridGenerationResult(
                success=True,
                grid_image_path=output_path,
                grid_image_bytes=image_bytes,
                generation_time=generation_time,
            )

        except Exception as e:
            return GridGenerationResult(
                success=False,
                error=f"[Reformat] {e}",
                generation_time=time.time() - start_time,
            )

    async def prepare_batch_request(
        self,
        beats: List[dict],
        character_map: Dict[str, dict] = None,
        scene_menu: list[dict] | list | None = None,
        prop_menu: list[dict] | list | None = None,
        sketch_colors: Dict[str, str] = None,
        style: str = None,
        output_path: str = "",
        ethnicity: str = "Chinese",
        rows: int = None,
        cols: int = None,
        sketch: bool = False,
        beat_start_index: int = 0,
        total_episode_beats: int = 0,
        location_beat_numbers: List[int] = None,
        sketch_dir: str = "",
        mode_key: Optional[str] = None,
        beat_sketch_paths: dict = None,
        sketch_aspect_padding: bool = False,
        force_image_size: Optional[str] = None,
        use_director_refs: bool = False,
        director_control_frames_dir: str | Path | None = None,
    ) -> dict:
        """准备单个 Batch API 请求（构建 prompt + contents，不调用 API）。

        复用 generate_grid() 的 prompt 构建和参考图逻辑，返回 dict 格式
        供 generate_batch_api() 批量提交。

        Returns:
            {
                "contents": [...],       # prompt + 参考图 Part 对象
                "rows": int,
                "cols": int,
                "aspect_ratio": str,
                "image_size": str,
                "output_path": str,
                "beat_start_index": int,
                "actual_beat_count": int,
            }
        """
        # 调用 generate_grid 的 prompt_only 模式来获取 prompt
        # 但我们需要完整的 contents，所以直接复用其逻辑
        rows = rows or self.rows
        cols = cols or self.cols
        grid_capacity = rows * cols
        character_map = character_map or {}

        if style is None:
            style = IMAGE_DEFAULT_STYLE

        actual_beat_count = min(len(beats), grid_capacity)
        if not sketch and sketch_dir:
            detection_error = render_ai_detection_error(beats[:grid_capacity])
            if detection_error:
                raise RuntimeError(detection_error)

        # 验证参考图
        valid_character_map = {}
        for char_name, info in character_map.items():
            char_info = dict(info)
            ref_path = info.get("ref_path") or info.get("portrait_path")
            upstream_mode = info.get("reference_mode", "prompt_only")

            if upstream_mode == "composite" and ref_path and os.path.exists(ref_path):
                char_info["reference_path"] = ref_path
                char_info["reference_mode"] = "composite"
                valid_character_map[char_name] = char_info
            elif ref_path and os.path.exists(ref_path):
                char_info["reference_path"] = ref_path
                char_info["reference_mode"] = "portrait_only"
                valid_character_map[char_name] = char_info
            else:
                char_info["reference_path"] = None
                char_info["reference_mode"] = "prompt_only"
                valid_character_map[char_name] = char_info

        # 构建 Prompt（复用 UnifiedPromptBuilder）
        previous_grid_path = None
        project_dir = _infer_project_dir(output_path, sketch_dir)
        episode_number = infer_episode_from_path(output_path) or infer_episode_from_path(sketch_dir)
        scene_refs: dict[int, list[Any]] = {}
        prop_asset_refs: dict[int, list[Any]] = {}
        scene_refs, prop_asset_refs = _resolve_scene_prop_asset_refs(
            project_dir,
            beats[:grid_capacity],
            episode_number=episode_number,
            sketch=sketch,
            use_director_refs=use_director_refs,
            include_pano_view_refs=False,
            director_control_frames_dir=director_control_frames_dir,
            scene_menu=scene_menu,
            prop_menu=prop_menu,
            allow_beat_background_anchor=(
                actual_beat_count == 1 and int(rows or 0) == 1 and int(cols or 0) == 1
            ),
        )
        if sketch and use_director_refs:
            if actual_beat_count != 1 or rows != 1 or cols != 1:
                raise RuntimeError(
                    "导演参考图模式只支持单 beat 1x1；批量草图请先导出对应 DirectorWorld 控制图。"
                )
            if not _has_director_image_ref(scene_refs, panel_idx=1):
                raise RuntimeError(
                    "导演单镜缺少 beat 级 3GS control frame；" "草图主线不再回退到旧场景参考图。"
                )
            _prepare_director_blocking_refs(
                scene_refs=scene_refs,
                beats=beats[:grid_capacity],
                character_map=valid_character_map,
            )
        style_family, animation_subtype = StyleService.get_style_branch(
            style or IMAGE_DEFAULT_STYLE,
            project_dir=project_dir,
        )

        if sketch:
            ctx = create_prompt_context(
                mode=PromptMode.SKETCH,
                beats=beats[:grid_capacity],
                rows=rows,
                cols=cols,
                character_map=valid_character_map,
                style=style,
                ethnicity=ethnicity,
                scene_refs=scene_refs,
                prop_asset_refs=prop_asset_refs,
                sketch_colors=sketch_colors or {},
                prop_marker_colors=_global_prop_marker_colors(
                    beats[:grid_capacity],
                    prop_menu,
                    sketch_colors=sketch_colors or {},
                ),
                style_family=style_family,
                animation_subtype=animation_subtype,
                project_dir=str(project_dir) if project_dir else "",
                image_provider=self.provider,
                image_model=self.model,
            )
            from novelvideo.verification.failure_registry import (
                load_negative_clause_for_project,
            )

            ctx.registry_negative_clause = await load_negative_clause_for_project(
                str(project_dir) if project_dir else None, "generator"
            )
            builder = UnifiedPromptBuilder(ctx)
            prompt = builder.build()
        elif sketch_dir:
            # 始终从 beats 自身提取 beat 编号，避免与外部参数不同步
            actual_beats = beats[:grid_capacity]
            actual_beat_numbers = [
                _generation_beat_number(b, i) for i, b in enumerate(actual_beats)
            ]
            beat_range_start = min(actual_beat_numbers)
            beat_range_end = max(actual_beat_numbers)

            sketch_result = find_sketch_for_beat_range(sketch_dir, beat_range_start, beat_range_end)
            has_all_pool_sketches = beat_sketch_paths and all(
                bn in beat_sketch_paths for bn in actual_beat_numbers
            )

            if sketch_result or has_all_pool_sketches:
                # 先切割草图
                if output_path:
                    temp_dir = Path(output_path).parent
                else:
                    temp_dir = Path("output")
                temp_dir.mkdir(parents=True, exist_ok=True)
                sub_sketch_path = str(temp_dir / "temp_sub_sketch_batch.jpg")
                target_aspect_batch = None
                if sketch_aspect_padding and mode_key:
                    target_aspect_batch = cell_aspect_ratio(mode_key)

                sub_sketch_path = crop_sketch_panels(
                    sketch_path=sketch_dir,
                    beat_numbers=actual_beat_numbers,
                    target_rows=rows,
                    target_cols=cols,
                    output_path=sub_sketch_path,
                    beat_sketch_paths=beat_sketch_paths,
                    target_aspect=target_aspect_batch,
                )
                previous_grid_path = sub_sketch_path

                # 读取预计算的 per-beat 身份检测结果（草图工作台已完成检测）
                _panel_det = load_precomputed_panel_detected(actual_beat_numbers, beats)
                valid_character_map = filter_character_map_by_precomputed(
                    valid_character_map, _panel_det
                )

                ctx = create_prompt_context(
                    mode=PromptMode.RENDER,
                    beats=beats[:grid_capacity],
                    rows=rows,
                    cols=cols,
                    character_map=valid_character_map,
                    style=style,
                    ethnicity=ethnicity,
                    panel_detected_keys=_panel_det,
                    scene_refs=scene_refs,
                    prop_asset_refs=prop_asset_refs,
                    sketch_colors=sketch_colors or {},
                    style_family=style_family,
                    animation_subtype=animation_subtype,
                    project_dir=str(project_dir) if project_dir else "",
                )
                builder = UnifiedPromptBuilder(ctx)
                prompt = builder.build()
            else:
                raise RuntimeError(
                    f"Render 模式需要草图但未找到覆盖 beat {beat_range_start}-{beat_range_end} 的草图"
                )
        else:
            raise RuntimeError("prepare_batch_request() 需要 sketch 或 sketch_dir 参数")

        # 构建 contents
        contents = [prompt]

        if not sketch and previous_grid_path and os.path.exists(previous_grid_path):
            previous_grid_image = self._load_image_as_part(previous_grid_path)
            if previous_grid_image:
                contents.append(previous_grid_image)

        if sketch:
            if use_director_refs:
                self._append_reference_parts_from_plan(
                    contents,
                    ctx,
                    [],
                    valid_character_map,
                    allowed_kinds={"scene"},
                    verbose=True,
                )
            elif scene_refs or prop_asset_refs:
                self._append_reference_parts_from_plan(
                    contents,
                    ctx,
                    [],
                    valid_character_map,
                    allowed_kinds={"scene"},
                    verbose=True,
                )
        else:
            ordered_chars = resolve_render_reference_order(
                ctx, beats, grid_capacity, valid_character_map
            )
            self._append_reference_parts_from_plan(
                contents,
                ctx,
                ordered_chars,
                valid_character_map,
                allowed_kinds=None,
                verbose=True,
            )

        # 确定 aspect_ratio 和 image_size
        if mode_key:
            _cfg = REGEN_MODE_CONFIGS[mode_key]
            aspect_ratio = _cfg["aspect_ratio"]
            image_size = _cfg["image_size"]
        elif sketch:
            aspect_ratio = SKETCH_GRID_CONFIG["aspect_ratio"]
            image_size = SKETCH_GRID_CONFIG["image_size"]
        else:
            _found = False
            for _mk, _cfg in REGEN_MODE_CONFIGS.items():
                if _cfg["rows"] == rows and _cfg["cols"] == cols:
                    aspect_ratio = _cfg["aspect_ratio"]
                    image_size = _cfg["image_size"]
                    _found = True
                    break
            if not _found:
                if rows == cols:
                    aspect_ratio = "1:1"
                    image_size = "4K" if rows >= 4 else "2K"
                elif rows > cols:
                    aspect_ratio = "9:16"
                    image_size = "4K"
                else:
                    aspect_ratio = "21:9"
                    image_size = "2K"

        if force_image_size:
            image_size = force_image_size

        return {
            "contents": contents,
            "rows": rows,
            "cols": cols,
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
            "output_path": output_path,
            "beat_start_index": beat_start_index,
            "actual_beat_count": actual_beat_count,
        }

    async def generate_batch_api(
        self,
        requests: List[dict],
        poll_interval: int = 15,
        timeout: int = 3600,
        on_status_change: callable = None,
    ) -> List[GridGenerationResult]:
        """Generate every prepared grid request, one after the other.

        ponytail: the Google Batch API (50% price, async) is gone with Google;
        requests now run sequentially through the image engine. Parallelise
        with a bounded gather if render batches get long.
        """
        results = []
        for i, req in enumerate(requests):
            output_path = req["output_path"]
            prompt_text, ref_bytes = self._extract_ref_bytes_from_contents(
                req["contents"], include_mime=True
            )
            image_bytes, _text, engine_error = await generate_image(
                self.selection,
                prompt_text,
                refs=ref_bytes,
                aspect_ratio=req["aspect_ratio"],
                image_size=normalize_image_size(req["image_size"]),
                quality=self.quality,
                output_path=output_path or None,
                usage={"task_type": "render_grid"},
            )
            if on_status_change:
                on_status_change(f"{i + 1}/{len(requests)}")
            if not image_bytes:
                results.append(
                    GridGenerationResult(
                        success=False,
                        error=f"网格 {i} 未返回图像数据: {engine_error}",
                    )
                )
                continue

            # 保存文件
            if output_path:
                output_dir = os.path.dirname(output_path)
                if output_dir:
                    os.makedirs(output_dir, exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(image_bytes)

                # 后处理：gap removal
                try:
                    from novelvideo.generators.grid_splitter import remove_grid_gaps
                    from PIL import Image as PILImage

                    grid_img = PILImage.open(output_path)
                    grid_img = remove_grid_gaps(grid_img, req["rows"], req["cols"])
                    grid_img.save(output_path)
                except Exception as e:
                    print(f"[BatchAPI] Grid {i} gap removal 失败: {e}")

            results.append(
                GridGenerationResult(
                    success=True,
                    grid_image_path=output_path,
                    grid_image_bytes=image_bytes,
                )
            )

        print(f"[Batch] 完成: {sum(1 for r in results if r.success)}/{len(results)} 成功")
        return results

    async def generate_grid_batch(
        self,
        all_beats: List[dict],
        character_map: Dict[str, dict] = None,
        scene_menu: list[dict] | list | None = None,
        prop_menu: list[dict] | list | None = None,
        sketch_colors: Dict[str, str] = None,
        style: str = None,
        output_dir: str = None,
        ethnicity: str = "Chinese",
        grid_size: int = 9,  # 3x3
        on_grid_complete: callable = None,  # 每个网格完成时的回调
        prompt_only: bool = False,  # Dry Run 模式：只生成提示词，不调用 API
        scene_grid_plan: List[dict] = None,  # 场景分组模式：scene_grid_split() 的输出
        sketch_dir: str = "",  # 草图目录路径（由调用方通过 PathResolver 计算）
        force_image_size: Optional[str] = None,  # 强制覆盖 image_size（如 "0.5K"）
    ) -> List[GridGenerationResult]:
        """批量生成多个网格图。

        将所有 beats 分成多个批次，每批次生成一个网格。
        每个网格生成后立即切割，更稳定且支持失败重试。

        Render 模式：
        - 如果调用方传入 sketch_dir 且目录中有草图文件，自动进入 Render 模式
        - 将草图作为参考，仅添加颜色和纹理

        Args:
            all_beats: 所有 beats 数据
            character_map: 角色映射
            style: 风格名称
            output_dir: 输出目录（保存网格图）
            ethnicity: 角色种族
            grid_size: 每个网格的面板数（默认 9 = 3x3）
            on_grid_complete: 每个网格完成时的回调函数 (batch_idx, result) -> None
        Returns:
            List[GridGenerationResult] - 每个网格的生成结果
        """
        import math
        from pathlib import Path

        results = []
        character_map = character_map or {}

        # 计算最大网格尺寸（用于动态选择的上限）
        max_grid_rows = int(math.sqrt(grid_size))
        max_grid_cols = max_grid_rows

        total_beats = len(all_beats)
        print(
            f"[NanoBananaPro Batch] 共 {total_beats} 个 beats，最大网格: {max_grid_rows}x{max_grid_cols}"
        )
        print("[NanoBananaPro Batch] 动态网格优化已启用（最小化黑色填充）")

        # 确保输出目录存在
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        # ===== 场景分组模式：使用预计算的 plan =====
        if scene_grid_plan:
            grid_plan_tuples = [
                e.get("mode_key", f"{e['rows']}x{e['cols']}_1-1") for e in scene_grid_plan
            ]
            plan_beats_list = [e["beats"] for e in scene_grid_plan]
            loc_labels = [e.get("scene_id", "") for e in scene_grid_plan]
            scene_grid_labels = [
                f"{REGEN_MODE_CONFIGS[mk]['rows']}x{REGEN_MODE_CONFIGS[mk]['cols']}({loc})"
                for mk, loc in zip(grid_plan_tuples, loc_labels)
            ]
            print(
                f"[NanoBananaPro Batch] 场景分组模式: "
                f"{' + '.join(scene_grid_labels)} "
                f"(共 {len(grid_plan_tuples)} 个网格)"
            )
        else:
            # ===== 默认分割：完美分割 =====
            grid_plan_tuples = perfect_grid_split(total_beats, max_grid=grid_size)
            plan_beats_list = None  # 标记：从 all_beats 顺序取

            # 铁律 post-process（仅有参考图 & 非 prompt_only 时）
            all_beats_list = list(all_beats)
            if character_map and not prompt_only:
                final_plan = []
                offset = 0
                for mk in grid_plan_tuples:
                    cap = REGEN_MODE_CONFIGS[mk]["capacity"]
                    batch = all_beats_list[offset : offset + cap]
                    n_comp = _count_batch_composite_chars(batch, character_map)
                    if n_comp >= MANY_CHARS_REF_THRESHOLD and cap > MANY_CHARS_MAX_CAPACITY:
                        # 智能拆分：按 composite 连续分组，≤2 的子组用完整 pool
                        final_plan.extend(
                            _smart_repack_beats(
                                batch,
                                character_map,
                                DEFAULT_POOL_TEMPLATE,
                            )
                        )
                    else:
                        final_plan.append(mk)
                    offset += cap
                grid_plan_tuples = final_plan

            grid_capacities = [REGEN_MODE_CONFIGS[mk]["capacity"] for mk in grid_plan_tuples]
            grid_labels = [
                f"{REGEN_MODE_CONFIGS[mk]['rows']}x{REGEN_MODE_CONFIGS[mk]['cols']}"
                for mk in grid_plan_tuples
            ]
            print(
                f"[NanoBananaPro Batch] 完美分割方案: {' + '.join(grid_labels)} "
                f"= {sum(grid_capacities)} (共 {len(grid_plan_tuples)} 个网格，0 填充)"
            )

        # 按照分割方案逐个生成网格
        all_beats_list = list(all_beats)  # 复制列表
        processed_beats = 0

        for batch_idx, mk in enumerate(grid_plan_tuples):
            cfg = REGEN_MODE_CONFIGS[mk]
            batch_rows, batch_cols = cfg["rows"], cfg["cols"]
            batch_capacity = cfg["capacity"]

            if plan_beats_list is not None:
                # 场景分组模式：使用 plan 中的 beats（可能非连续）
                batch_beats = plan_beats_list[batch_idx]
            else:
                # 默认：顺序取 beats
                batch_beats = all_beats_list[processed_beats : processed_beats + batch_capacity]

            # 记录实际 beat 数量（用于日志）
            actual_beat_count = len(batch_beats)
            start_idx = processed_beats + 1
            end_idx = processed_beats + actual_beat_count

            # 角色过滤交给 generate_grid 内部完成，避免批量阶段重复解析引用。
            batch_character_map = character_map
            print(
                f"[NanoBananaPro Batch] 网格 {batch_idx + 1} 候选角色: "
                f"{list(batch_character_map.keys())}"
            )

            # 完美分割不需要填充，直接使用 batch_beats
            # （如果因为某种原因 beat 数量不匹配，这里会有问题，但理论上不会发生）
            if len(batch_beats) != batch_capacity:
                print(
                    f"[NanoBananaPro Batch] 警告: 网格 {batch_idx + 1} beat 数量不匹配 ({len(batch_beats)} vs {batch_capacity})"
                )

            # 生成网格图路径
            output_path = None
            if output_dir:
                output_path = str(Path(output_dir) / f"grid_{batch_idx + 1:02d}.png")

            print(
                f"[NanoBananaPro Batch] 生成网格 {batch_idx + 1} (beats {start_idx}-{end_idx}, 网格: {batch_rows}x{batch_cols})"
            )

            # 调用单网格生成（使用过滤后的角色映射和动态网格尺寸）
            result = await self.generate_grid(
                beats=batch_beats,
                character_map=batch_character_map,
                scene_menu=scene_menu,
                prop_menu=prop_menu,
                sketch_colors=sketch_colors,
                style=style,
                output_path=output_path,
                ethnicity=ethnicity,
                rows=batch_rows,  # 使用动态计算的行数
                cols=batch_cols,  # 使用动态计算的列数
                prompt_only=prompt_only,
                beat_start_index=processed_beats,  # Render 模式：传递起始索引用于 sketch 切片
                total_episode_beats=total_beats,  # Render 模式：传递整集 beat 总数
                location_beat_numbers=(
                    scene_grid_plan[batch_idx]["beat_numbers"] if scene_grid_plan else None
                ),
                sketch_dir=sketch_dir,
                mode_key=scene_grid_plan[batch_idx].get("mode_key") if scene_grid_plan else mk,
                force_image_size=force_image_size,
            )

            # 更新计数器（batch_idx 由 enumerate 自动管理）
            processed_beats += actual_beat_count

            results.append(result)

            # 回调通知
            if on_grid_complete:
                try:
                    on_grid_complete(batch_idx, result)
                except Exception as e:
                    print(f"[NanoBananaPro Batch] 回调错误: {e}")

            if not result.success:
                print(f"[NanoBananaPro Batch] 网格 {batch_idx + 1} 生成失败: {result.error}")
                # 继续生成下一个网格，不中断整个流程

        successful = sum(1 for r in results if r.success)
        total_grids = len(results)
        print(f"[NanoBananaPro Batch] 批量生成完成: {successful}/{total_grids} 成功")

        return results

    async def regenerate_single_grid(
        self,
        all_beats: List[dict],
        grid_index: int,  # 0-based grid index
        character_map: Dict[str, dict] = None,
        scene_menu: list[dict] | list | None = None,
        prop_menu: list[dict] | list | None = None,
        sketch_colors: Dict[str, str] = None,
        style: str = None,
        output_dir: str = None,
        ethnicity: str = "Chinese",
        grid_size: int = 9,  # 3x3
        prompt_only: bool = False,
        scene_grid_plan: List[dict] = None,  # 场景分组模式：scene_grid_split() 的输出
        sketch_dir: str = "",  # 草图目录路径（由调用方通过 PathResolver 计算）
        beat_sketch_paths: dict = None,  # {beat_num: full_path} 从图片池取的 per-beat 草图路径
        sketch_aspect_padding: bool = False,  # 草图补白到目标比例
        force_image_size: Optional[str] = None,  # 强制覆盖 image_size（如 "0.5K"）
    ) -> GridGenerationResult:
        """重新生成指定索引的单个网格。

        使用与 generate_grid_batch 相同的动态分割逻辑计算 beat 范围，
        然后只重新生成指定索引的网格。

        Args:
            all_beats: 所有 beats 数据
            grid_index: 要重新生成的网格索引 (0-based)
            character_map: 角色映射
            sketch_colors: 草图角色颜色映射
            style: 风格名称
            output_dir: 输出目录
            ethnicity: 角色种族
            grid_size: 每个网格的面板数（默认 9 = 3x3）
            prompt_only: 只生成提示词，不调用 API
            scene_grid_plan: 场景分组模式预计算的 plan

        Returns:
            GridGenerationResult - 重新生成的网格结果
        """
        from pathlib import Path

        character_map = character_map or {}

        # ===== 场景分组模式 =====
        if scene_grid_plan:
            if grid_index >= len(scene_grid_plan):
                return GridGenerationResult(
                    success=False,
                    error=f"网格索引 {grid_index} 超出范围（最大 {len(scene_grid_plan) - 1}）",
                    generation_time=0,
                )
            entry = scene_grid_plan[grid_index]
            target_batch_rows = entry["rows"]
            target_batch_cols = entry["cols"]
            target_batch_beats = entry["beats"]
            batch_capacity = target_batch_rows * target_batch_cols
            total_beats = len(all_beats)
            target_start_idx = 0  # 场景分组无连续起始索引
            loc_name = entry.get("scene_id", "")
            print(
                f"[NanoBananaPro Regen] 场景分组: 网格 {grid_index + 1} ({loc_name}, "
                f"{len(target_batch_beats)} beats, "
                f"网格: {target_batch_rows}x{target_batch_cols})"
            )
        else:
            # ===== 默认分割 =====
            total_beats = len(all_beats)
            grid_plan = perfect_grid_split(total_beats, max_grid=grid_size)

            if grid_index >= len(grid_plan):
                return GridGenerationResult(
                    success=False,
                    error=f"网格索引 {grid_index} 超出范围（最大 {len(grid_plan) - 1}）",
                    generation_time=0,
                )

            _mk = grid_plan[grid_index]
            _cfg = REGEN_MODE_CONFIGS[_mk]
            target_batch_rows, target_batch_cols = _cfg["rows"], _cfg["cols"]
            batch_capacity = _cfg["capacity"]
            target_start_idx = sum(
                REGEN_MODE_CONFIGS[m]["capacity"] for m in grid_plan[:grid_index]
            )
            target_batch_beats = list(all_beats)[
                target_start_idx : target_start_idx + batch_capacity
            ]

        actual_beat_count = len(target_batch_beats)
        if not scene_grid_plan:
            print(
                f"[NanoBananaPro Regen] 重新生成网格 {grid_index + 1} "
                f"(beats {target_start_idx + 1}-{target_start_idx + actual_beat_count}, "
                f"网格: {target_batch_rows}x{target_batch_cols})"
            )

        # 角色过滤交给 generate_grid 内部完成，避免重生阶段重复解析引用。
        batch_character_map = character_map
        print(
            f"[NanoBananaPro Regen] 网格 {grid_index + 1} 候选角色: "
            f"{list(batch_character_map.keys())}"
        )

        # 完美分割不需要填充，但检查以防万一
        if len(target_batch_beats) != batch_capacity:
            print(
                f"[NanoBananaPro Regen] 警告: 网格 {grid_index + 1} beat 数量不匹配 "
                f"({len(target_batch_beats)} vs {batch_capacity})"
            )

        # 生成网格图路径
        output_path = None
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            output_path = str(Path(output_dir) / f"grid_{grid_index + 1:02d}.png")

        # 调用单网格生成
        result = await self.generate_grid(
            beats=target_batch_beats,
            character_map=batch_character_map,
            scene_menu=scene_menu,
            prop_menu=prop_menu,
            sketch_colors=sketch_colors,
            style=style,
            output_path=output_path,
            ethnicity=ethnicity,
            rows=target_batch_rows,
            cols=target_batch_cols,
            prompt_only=prompt_only,
            beat_start_index=target_start_idx,  # Render 模式：传递起始索引用于 sketch 切片
            total_episode_beats=total_beats,  # Render 模式：传递整集 beat 总数
            location_beat_numbers=entry["beat_numbers"] if scene_grid_plan else None,
            sketch_dir=sketch_dir,
            beat_sketch_paths=beat_sketch_paths,
            mode_key=entry.get("mode_key") if scene_grid_plan else grid_plan[grid_index],
            sketch_aspect_padding=sketch_aspect_padding,
            force_image_size=force_image_size,
        )

        if result.success:
            print(f"[NanoBananaPro Regen] 网格 {grid_index + 1} 重新生成成功")
        else:
            print(f"[NanoBananaPro Regen] 网格 {grid_index + 1} 重新生成失败: {result.error}")

        # 添加额外的元数据到结果
        result.beat_start_index = target_start_idx
        result.beat_count = actual_beat_count
        result.grid_rows = target_batch_rows
        result.grid_cols = target_batch_cols

        return result

    def _load_image_as_part(
        self,
        image_path: str,
        compress_quality: int = 60,
        min_short_side: int = 0,
    ):
        """加载参考图为 `_InlineImagePart`（带 JPEG 压缩）。

        Args:
            image_path: 图像路径
            compress_quality: JPEG 压缩质量 (1-100)，设为 0 或 None 禁用压缩
            min_short_side: 提交前放大参考图，避免小尺寸空间图被模型读丢

        Returns:
            _InlineImagePart
        """
        try:
            from PIL import Image
            import io

            # 加载图片
            img = Image.open(image_path)
            original_size = os.path.getsize(image_path)
            original_dimensions = img.size

            if min_short_side > 0:
                short_side = min(img.size)
                if 0 < short_side < min_short_side:
                    scale = min_short_side / short_side
                    new_size = (
                        max(1, int(round(img.size[0] * scale))),
                        max(1, int(round(img.size[1] * scale))),
                    )
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                    print(
                        f"[参考图放大] {os.path.basename(image_path)}: "
                        f"{original_dimensions[0]}x{original_dimensions[1]} → "
                        f"{new_size[0]}x{new_size[1]}"
                    )

            # 压缩为 JPEG（如果启用）
            if compress_quality and compress_quality > 0:
                # 转为 RGB（JPEG 不支持 alpha）
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                # 压缩到内存
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=compress_quality, optimize=True)
                image_data = buffer.getvalue()
                mime_type = "image/jpeg"

                compressed_size = len(image_data)
                ratio = (1 - compressed_size / original_size) * 100
                print(
                    f"[压缩] {os.path.basename(image_path)}: "
                    f"{original_size/1024:.0f}KB → {compressed_size/1024:.0f}KB "
                    f"({ratio:.0f}% 压缩)"
                )
            else:
                if img.size != original_dimensions:
                    buffer = io.BytesIO()
                    img.save(buffer, format="PNG")
                    image_data = buffer.getvalue()
                    mime_type = "image/png"
                else:
                    # 不压缩，直接读取原文件
                    with open(image_path, "rb") as f:
                        image_data = f.read()

                    if image_path.lower().endswith(".png"):
                        mime_type = "image/png"
                    elif image_path.lower().endswith(".webp"):
                        mime_type = "image/webp"
                    else:
                        mime_type = "image/jpeg"

            return _InlineImagePart(image_data, mime_type)

        except Exception as e:
            print(f"[NanoBananaPro] 加载参考图失败: {image_path}, {e}")
            return None

    def _append_reference_parts_from_plan(
        self,
        contents: list,
        ctx: PromptContext,
        ordered_chars: list[str],
        valid_character_map: dict,
        *,
        allowed_kinds: set[str] | None = None,
        verbose: bool = False,
        audit_refs: list[dict] | None = None,
    ) -> None:
        """按 prompt 中的统一图片计划追加实际附件。"""
        plan = PromptComponents.build_reference_image_plan(ctx, ordered_chars)

        for entry in plan:
            kind = entry.get("kind")
            if allowed_kinds is not None and kind not in allowed_kinds:
                continue
            if kind == "combined_composite":
                sheets = []
                names = []
                try:
                    from PIL import Image

                    for item in entry.get("items", []):
                        char_name = item.get("char_name", "")
                        info = valid_character_map.get(char_name) or {}
                        ref_path = item.get("path") or info.get("reference_path")
                        if not ref_path or not os.path.exists(ref_path):
                            continue
                        sheet = Image.open(ref_path)
                        if sheet.mode in ("RGBA", "P"):
                            sheet = sheet.convert("RGB")
                        sheets.append(sheet)
                        names.append(char_name)
                        if verbose:
                            print(
                                f"[NanoBananaPro] 添加完整多视图参考 sheet: {char_name} -> "
                                f"{sheet.size[0]}x{sheet.size[1]}px"
                            )
                    if sheets:
                        merged_part = self._merge_character_panels(sheets)
                        if merged_part:
                            contents.append(merged_part)
                            if verbose:
                                print(f"[NanoBananaPro] 多人完整 sheet 合并参考图: {names}")
                finally:
                    for sheet in sheets:
                        try:
                            sheet.close()
                        except Exception:
                            pass
            elif kind in {"composite", "portrait_only", "identity_portrait"}:
                path = entry.get("path")
                if not path or not os.path.exists(path):
                    continue
                ref_image = self._load_image_as_part(path)
                if not ref_image:
                    continue
                contents.append(ref_image)
                if audit_refs is not None:
                    audit_refs.append(
                        {
                            "kind": kind,
                            "base_id": entry.get("char_name", ""),
                            "path": path,
                            "bytes": os.path.getsize(path) if os.path.exists(path) else None,
                        }
                    )
                if not verbose:
                    continue
                if kind == "composite":
                    print(
                        "[NanoBananaPro] 添加参考图 (复合图): "
                        f"{entry.get('char_name', '')} -> {path}"
                    )
                elif kind == "portrait_only":
                    print(
                        "[NanoBananaPro] 添加参考图 (Portrait): "
                        f"{entry.get('char_name', '')} -> {path}"
                    )
                else:
                    print(
                        "[NanoBananaPro] 添加身份级 Portrait (年龄变体): "
                        f"{entry.get('char_name', '')}/{entry.get('tag', '')}"
                    )
            elif kind in {"scene", "prop"}:
                ref = entry.get("ref")
                path = (getattr(ref, "image_paths", []) or [""])[0]
                if not path or not os.path.exists(path):
                    continue
                source_level = str(getattr(ref, "source_level", "") or "").strip()
                compress_quality = 0 if source_level == "scene_spatial_layout" else 60
                min_short_side = 720 if source_level == "scene_spatial_layout" else 0
                ref_image = self._load_image_as_part(
                    path,
                    compress_quality=compress_quality,
                    min_short_side=min_short_side,
                )
                if not ref_image:
                    continue
                contents.append(ref_image)
                if audit_refs is not None:
                    audit_refs.append(
                        {
                            "kind": kind,
                            "base_id": getattr(ref, "base_id", ""),
                            "variant_id": getattr(ref, "variant_id", ""),
                            "source_level": getattr(ref, "source_level", ""),
                            "path": path,
                            "bytes": os.path.getsize(path) if os.path.exists(path) else None,
                            "submitted_min_short_side": min_short_side or None,
                        }
                    )
                if verbose:
                    label = "场景" if kind == "scene" else "道具"
                    try:
                        size_bytes = os.path.getsize(path)
                    except OSError:
                        size_bytes = -1
                    print(
                        f"[NanoBananaPro][RefPlan] kind={kind} "
                        f"base_id={getattr(ref, 'base_id', '')} "
                        f"path={path} bytes={size_bytes}"
                    )
                    print(f"[NanoBananaPro] 添加{label}参考图: " f"{getattr(ref, 'base_id', '')}")

    def _extract_ref_bytes_from_contents(
        self, contents: list, *, include_mime: bool = False
    ) -> tuple:
        """从 contents 列表提取 prompt 文本和参考图 bytes（交给图像引擎）。"""
        prompt_text = ""
        ref_bytes = []
        for item in contents:
            if isinstance(item, str):
                prompt_text = item
            elif hasattr(item, "inline_data") and item.inline_data:
                if include_mime:
                    ref_bytes.append(
                        (
                            item.inline_data.data,
                            getattr(item.inline_data, "mime_type", "image/png") or "image/png",
                        )
                    )
                else:
                    ref_bytes.append(item.inline_data.data)
        return prompt_text, ref_bytes

    def _crop_center_panel(self, image_path: str):
        """从 3 面板 sheet 裁出中间面板（正面全身）。"""
        from PIL import Image

        img = Image.open(image_path)
        w, h = img.size
        panel_w = w // 3
        return img.crop((panel_w, 0, panel_w * 2, h))

    def _merge_character_panels(self, panels: list, compress_quality: int = 60):
        """将多个角色参考图水平拼接，压缩后返回 Gemini Part。"""
        from PIL import Image

        if not panels:
            return None

        # 统一高度（取最大），等比缩放。
        max_h = max(p.size[1] for p in panels)
        resized = []
        for p in panels:
            if p.size[1] != max_h:
                ratio = max_h / p.size[1]
                new_w = int(p.size[0] * ratio)
                p = p.resize((new_w, max_h), Image.LANCZOS)
            resized.append(p)

        # 水平拼接。
        total_w = sum(p.size[0] for p in resized)
        merged = Image.new("RGB", (total_w, max_h))
        x_offset = 0
        for p in resized:
            if p.mode in ("RGBA", "P"):
                p = p.convert("RGB")
            merged.paste(p, (x_offset, 0))
            x_offset += p.size[0]

        # JPEG 压缩。
        buffer = io.BytesIO()
        merged.save(buffer, format="JPEG", quality=compress_quality, optimize=True)
        image_data = buffer.getvalue()
        print(
            f"[NanoBananaPro] 合并参考图: {len(panels)} 角色, "
            f"{total_w}x{max_h}px, {len(image_data)/1024:.0f}KB"
        )

        return _InlineImagePart(image_data, "image/jpeg")

    async def generate_shot_grid(
        self,
        shot_beats: List[dict],
        character_map: Dict[str, dict] = None,
        scene_menu: list[dict] | list | None = None,
        prop_menu: list[dict] | list | None = None,
        style: str = None,
        output_path: Optional[str] = None,
        ethnicity: str = "Chinese",
        sketch_dir: str = "",
        beat_sketch_paths: dict = None,
    ) -> GridGenerationResult:
        """生成 Shot 级 Grid（v2.0 Shot-Centric）。

        一个 Shot 内的 N 个 beats → 1 个 Grid 图，用作 Seedance 2.0 的 @图片 分镜参考。
        Grid 格式根据 beat 数自动选择：1→1x1, 2→1x2, 3→1x3, 4→2x2, 5→3x3(前5格填充)。

        该 Grid 直接作为整张图喂给 Seedance 2.0，不裁切。

        Args:
            shot_beats: Shot 内的 beats 列表（1-5 个）
            character_map: 角色映射
            style: 风格名称
            output_path: 输出路径
            ethnicity: 角色种族
            sketch_dir: 草图目录
            beat_sketch_paths: per-beat 草图路径

        Returns:
            GridGenerationResult
        """
        beat_count = len(shot_beats)
        cfg = get_shot_grid_config(beat_count)
        rows, cols = cfg["rows"], cfg["cols"]
        aspect_ratio = cfg["aspect_ratio"]
        image_size = cfg["image_size"]

        print(f"[ShotGrid] 生成 Shot Grid: {beat_count} beats → {rows}x{cols} ({aspect_ratio})")

        return await self.generate_grid(
            beats=shot_beats,
            character_map=character_map,
            scene_menu=scene_menu,
            prop_menu=prop_menu,
            style=style,
            output_path=output_path,
            ethnicity=ethnicity,
            rows=rows,
            cols=cols,
            sketch_dir=sketch_dir,
            aspect_ratio_override=aspect_ratio,
            image_size_override=image_size,
            beat_sketch_paths=beat_sketch_paths,
        )


def _generation_beat_number(beat: dict, fallback_index: int) -> int:
    raw_beat_number = beat.get("beat_number")
    if raw_beat_number is not None:
        try:
            return int(raw_beat_number)
        except (TypeError, ValueError):
            pass
    raw_panel_index = beat.get("panel_index")
    if raw_panel_index is not None:
        try:
            return int(raw_panel_index)
        except (TypeError, ValueError):
            pass
    return fallback_index + 1


async def regenerate_selected_beats(
    selected_beats: List[dict],
    mode_key: str,
    character_map: Dict[str, dict],
    style: str,
    output_dir: str,
    scene_menu: list[dict] | list | None = None,
    prop_menu: list[dict] | list | None = None,
    sketch_colors: dict[str, str] | None = None,
    ethnicity: str = "Chinese",
    is_sketch: bool = False,
    sketch_dir: str = "",
    api_key: Optional[str] = None,
    episode_grids_dir: str = "",
    beat_sketch_paths_override: dict[int, str] | None = None,
    scene_refs_override: dict[int, list[Any]] | None = None,
    prop_refs_override: dict[int, list[Any]] | None = None,
    sketch_aspect_padding: bool = False,
    force_image_size: Optional[str] = None,
    generator_config: Optional[dict] = None,
) -> List[GridGenerationResult]:
    """再生选中的 beats（支持 render 和 sketch 模式）。

    从 REGEN_MODE_CONFIGS[mode_key] 读取 rows, cols, aspect_ratio, image_size，
    使用 perfect_grid_split 分割后逐 grid 调用 generate_grid。

    Args:
        selected_beats: 选中的 beat 数据列表
        mode_key: 再生模式 key，如 "1x1_9-16", "2x2_1-1"
        character_map: 角色映射
        style: 风格
        output_dir: 输出目录
        ethnicity: 种族
        is_sketch: 是否为草图模式
        sketch_dir: 草图目录
        api_key: API key
        sketch_aspect_padding: 草图补白到目标比例

    Returns:
        GridGenerationResult 列表
    """
    rows, cols, aspect_ratio, image_size = parse_regen_mode(mode_key)
    capacity = rows * cols

    # 分割 beats
    num_grids = math.ceil(len(selected_beats) / capacity)
    grid_splits = [mode_key] * num_grids
    print(
        f"[RegenBeats] mode={mode_key}, beats={len(selected_beats)}, "
        f"splits={grid_splits}, aspect_ratio={aspect_ratio}"
    )

    generator = create_grid_generator(api_key, config=generator_config)
    results = []
    beat_offset = 0
    regen_id = uuid.uuid4().hex

    for grid_idx, split_mk in enumerate(grid_splits, start=1):
        split_cfg = REGEN_MODE_CONFIGS[split_mk]
        g_rows, g_cols = split_cfg["rows"], split_cfg["cols"]
        grid_beat_count = split_cfg["capacity"]
        grid_beats = selected_beats[beat_offset : beat_offset + grid_beat_count]
        beat_offset += grid_beat_count

        # 输出路径
        output_path = str(
            Path(output_dir) / f"regen_{mode_key}_g{grid_idx:02d}_{regen_id}.png"
        )

        # 提取 beat 编号用于 location_beat_numbers
        beat_numbers = [_generation_beat_number(b, i) for i, b in enumerate(grid_beats)]

        # 从图片池构建 per-beat 草图路径
        grid_beat_sketch_paths = None
        if episode_grids_dir and not is_sketch:
            from novelvideo.generators.pool_indexer import build_beat_sketch_paths

            grid_beat_sketch_paths = build_beat_sketch_paths(episode_grids_dir, beat_numbers)
        if beat_sketch_paths_override and not is_sketch:
            grid_beat_sketch_paths = {
                int(beat_num): str(path)
                for beat_num, path in beat_sketch_paths_override.items()
                if int(beat_num) in {int(value) for value in beat_numbers}
            }

        result = await generator.generate_grid(
            beats=grid_beats,
            character_map=character_map,
            scene_menu=scene_menu,
            prop_menu=prop_menu,
            sketch_colors=sketch_colors,
            style=style,
            output_path=output_path,
            ethnicity=ethnicity,
            rows=g_rows,
            cols=g_cols,
            sketch=is_sketch,
            sketch_dir=sketch_dir if not is_sketch else "",
            location_beat_numbers=beat_numbers,
            mode_key=split_mk,
            beat_sketch_paths=grid_beat_sketch_paths,
            scene_refs_override=scene_refs_override,
            prop_refs_override=prop_refs_override,
            sketch_aspect_padding=sketch_aspect_padding,
            force_image_size=force_image_size,
        )
        result.beat_start_index = beat_offset - grid_beat_count
        result.beat_count = len(grid_beats)
        result.grid_rows = g_rows
        result.grid_cols = g_cols
        results.append(result)

        if result.success:
            print(f"[RegenBeats] Grid {grid_idx} 成功: {result.grid_image_path}")
        else:
            print(f"[RegenBeats] Grid {grid_idx} 失败: {result.error}")

    return results


def create_grid_generator(
    api_key: Optional[str] = None,
    config: Optional[dict] = None,
) -> NanoBananaGridGenerator:
    """创建网格生成器。

    Args:
        api_key: Google AI API Key

    Returns:
        NanoBananaGridGenerator 实例
    """
    return NanoBananaGridGenerator(api_key=api_key, config=config)
