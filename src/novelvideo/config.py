"""NovelVideo 配置模块。

独立的配置系统，不依赖 SuperScript。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from novelvideo.official_defaults import OFFICIAL_NEWAPI_BASE_URL

# 加载环境变量（必须在任何其他导入之前）
load_dotenv()

# =============================================================================
# 文本模型引擎 (TEXT_ENGINE)
# =============================================================================
# Text and vision-with-text calls go to one OpenAI-compatible engine:
#   mtplx      (default) local `mtplx serve`, started on first request.
#   openrouter https://openrouter.ai, key from OPENROUTER_API_KEY.
# NewAPI is no longer a text transport (it still serves embeddings/TTS/media).

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_TEXT_MODEL = "google/gemma-4-26b-a4b-it"


def get_text_engine() -> str:
    engine = (os.environ.get("TEXT_ENGINE") or "mtplx").strip().lower()
    if engine not in ("mtplx", "openrouter"):
        raise ValueError(f"Unknown TEXT_ENGINE: {engine}. Available: mtplx, openrouter")
    return engine


def get_text_engine_credentials(*, start: bool = True) -> tuple[str, str]:
    """Return ``(api_key, base_url)`` of the text engine.

    ``start=True`` may spawn ``mtplx serve``; pass False where only the address
    is needed (config files, import time, status).
    """
    if get_text_engine() == "openrouter":
        return os.environ.get("OPENROUTER_API_KEY", "").strip(), OPENROUTER_BASE_URL
    from novelvideo.engines import mtplx

    # MTPLX needs no key; the OpenAI client just refuses an empty one.
    return "mtplx", mtplx.ensure_running() if start else mtplx.base_url()


def ensure_text_engine_ready() -> None:
    """Start MTPLX if it is the text engine and not serving yet (blocking)."""
    if get_text_engine() == "mtplx":
        get_text_engine_credentials(start=True)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def get_pydantic_model(
    provider_override: str | None = None,
    model_name_override: str | None = None,
):
    """Return a PydanticAI model on the text engine (see ``TEXT_ENGINE``).

    ``provider_override`` is a legacy knob and is ignored: the engine alone
    decides the transport. ``model_name_override`` only applies to OpenRouter.
    """
    del provider_override
    return get_newapi_text_pydantic_model(
        "MODEL_NAME",
        OPENROUTER_DEFAULT_TEXT_MODEL,
        model_name_override=model_name_override,
        capability="text.generate.agent",
        timeout_seconds_override=_env_float("MODEL_TIMEOUT", 300.0),
    )


def _clean_env_value(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _openrouter_model_id(value: str | None) -> str | None:
    value = (value or "").strip().removeprefix("openrouter/")
    # ponytail: OpenRouter ids are "vendor/model"; bare names (DC-* aliases,
    # gemini-3.5-flash) are NewAPI-era routing names and are skipped.
    return value if "/" in value else None


def get_newapi_text_model_name(
    model_env: str,
    default_model: str,
    model_name_override: str | None = None,
) -> str:
    """Return the model id sent to the text engine for a path-specific task.

    MTPLX serves exactly one model (``MTPLX_MODEL``). OpenRouter honours, in
    order: the explicit override, the per-task env (``model_env``),
    ``OPENROUTER_MODEL``, ``MODEL_NAME``, then the OpenRouter default.
    ``default_model`` is the NewAPI-era logical name and is no longer sent.
    """
    del default_model
    if get_text_engine() == "mtplx":
        from novelvideo.engines import mtplx

        return mtplx.model_id()
    for value in (
        model_name_override,
        _clean_env_value(model_env),
        _clean_env_value("OPENROUTER_MODEL"),
        _clean_env_value("MODEL_NAME"),
    ):
        model = _openrouter_model_id(value)
        if model:
            return model
    return OPENROUTER_DEFAULT_TEXT_MODEL


def _get_newapi_text_model_profile(model_name: str):
    """PydanticAI profile for the text engine's model."""
    if get_text_engine() == "mtplx":
        from pydantic_ai.profiles.openai import OpenAIModelProfile

        # MTPLX does structured output via response_format json_schema: the
        # model finishes <think>, reasoning comes back apart, JSON in content.
        return OpenAIModelProfile(
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        )
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    return OpenRouterProvider.model_profile(model_name)


def _newapi_text_http_client_factory(
    *,
    timeout_seconds: float,
) -> Any:
    # A local MTPLX must never be reached through a system proxy.
    trust_env = _env_bool("TEXT_TRUST_ENV", get_text_engine() != "mtplx")

    def factory():
        import httpx

        kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if not trust_env:
            kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    return factory


def _newapi_text_openai_provider(
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
):
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    class _LifecycleManagedOpenAIProvider(OpenAIProvider):
        def __init__(self) -> None:
            http_client_factory = _newapi_text_http_client_factory(
                timeout_seconds=timeout_seconds,
            )
            http_client = http_client_factory()
            super().__init__(
                openai_client=AsyncOpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=timeout_seconds,
                    max_retries=0,
                    http_client=http_client,
                ),
            )
            self._own_http_client = http_client
            self._http_client_factory = http_client_factory

    return _LifecycleManagedOpenAIProvider()


def _newapi_text_openai_model(
    model_name: str,
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
    profile: Any,
    ensure_ready: Any = None,
):
    """``ensure_ready`` (blocking, optional) runs before every request, so a
    local engine is started lazily at first use rather than at construction."""
    import asyncio
    from contextlib import asynccontextmanager

    from pydantic_ai.models.openai import OpenAIChatModel

    async def _ready() -> None:
        if ensure_ready is not None:
            await asyncio.to_thread(ensure_ready)

    class _AutoClosingOpenAIChatModel(OpenAIChatModel):
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            await _ready()
            async with self:
                return await super().request(*args, **kwargs)

        @asynccontextmanager
        async def request_stream(self, *args: Any, **kwargs: Any):
            await _ready()
            async with self:
                async with super().request_stream(*args, **kwargs) as response:
                    yield response

    return _AutoClosingOpenAIChatModel(
        model_name,
        provider=_newapi_text_openai_provider(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        ),
        profile=profile,
    )


def get_newapi_text_pydantic_model(
    model_env: str,
    default_model: str,
    *,
    model_name_override: str | None = None,
    timeout_seconds_override: float | None = None,
    capability: str = "text.generate",
):
    """Create a PydanticAI OpenAI-compatible model on the text engine.

    The name is historical: text no longer routes through NewAPI (nor, in EE,
    through the request-scoped organization gateway). ``capability`` is kept
    for call-site compatibility and is unused.
    """
    del capability
    engine = get_text_engine()
    model_name = get_newapi_text_model_name(
        model_env, default_model, model_name_override
    )
    timeout_seconds = (
        float(timeout_seconds_override)
        if timeout_seconds_override is not None
        else _env_float(
            f"{model_env}_TIMEOUT_SECONDS",
            _env_float("TEXT_TIMEOUT_SECONDS", 300.0),
        )
    )
    api_key, base_url = get_text_engine_credentials(start=False)
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set (TEXT_ENGINE=openrouter).")
    return _newapi_text_openai_model(
        model_name,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        profile=_get_newapi_text_model_profile(model_name),
        ensure_ready=ensure_text_engine_ready if engine == "mtplx" else None,
    )


def get_newapi_structured_output_model_settings() -> dict:
    """Model settings for PydanticAI structured output requests.

    OpenRouter: reasoning off (``reasoning_effort=none``), as before.
    MTPLX: nothing is sent. The server runs with ``--reasoning auto``; the
    model finishes its <think> block, reasoning comes back separately and the
    JSON lands in ``content`` (json_schema response_format, see the profile).
    That path is the one verified in MacGen; ``none`` is untested on MTPLX.
    """
    if get_text_engine() == "mtplx":
        return {}
    return {"openai_reasoning_effort": "none"}


def get_newapi_structured_output_litellm_kwargs() -> dict:
    """LiteLLM twin of :func:`get_newapi_structured_output_model_settings`."""
    if get_text_engine() == "mtplx":
        return {}
    return {
        "reasoning_effort": "none",
        "allowed_openai_params": ["reasoning_effort"],
    }


def get_superpower_pydantic_model(
    *,
    feature_provider_env: str | None = None,
    feature_model_env: str | None = None,
):
    """Return the multimodal model used by SuperPower prompt builders.

    Runs on the text engine. Feature-specific model env vars (for example
    GLOBAL_VIDEO_MODEL) or SUPERPOWER_MODEL only apply to TEXT_ENGINE=openrouter;
    the *_PROVIDER vars are legacy and ignored.
    """

    provider_override = (
        _clean_env_value(feature_provider_env)
        or _clean_env_value("SUPERPOWER_PROVIDER")
        or _clean_env_value("SUPERPOWER_MODEL_PROVIDER")
    )
    model_name_override = (
        _clean_env_value(feature_model_env)
        or _clean_env_value("SUPERPOWER_MODEL")
        or _clean_env_value("SUPERPOWER_MODEL_NAME")
    )
    return get_pydantic_model(
        provider_override=provider_override,
        model_name_override=model_name_override,
    )


# Redis 配置
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


# =============================================================================
# 基础配置
# =============================================================================

# 数据根目录，三类子目录 (output/state/runtime) 默认基于此目录派生
DATA_ROOT = os.path.abspath(os.environ.get("NOVELVIDEO_DATA_ROOT", "."))

# 使用绝对路径，确保 task worker 也能找到正确的目录
OUTPUT_DIR = os.path.abspath(
    os.environ.get("NOVELVIDEO_OUTPUT_DIR", os.path.join(DATA_ROOT, "output"))
)

# 状态文件目录 (data.db, cognee_system/, project_config.json)
STATE_DIR = os.path.abspath(
    os.environ.get("NOVELVIDEO_STATE_DIR", os.path.join(DATA_ROOT, "state"))
)

# 运行时临时目录 (日志、staging、temp panels)
RUNTIME_DIR = os.path.abspath(
    os.environ.get("NOVELVIDEO_RUNTIME_DIR", os.path.join(DATA_ROOT, "runtime"))
)

# =============================================================================
# OSS presign 配置
# =============================================================================

OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT")
OSS_PUBLIC_ENDPOINT = os.environ.get("OSS_PUBLIC_ENDPOINT")
OSS_BUCKET = os.environ.get("OSS_BUCKET")
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET")
OSS_OBJECT_PREFIX = os.environ.get("OSS_OBJECT_PREFIX", "output")
DOWNLOAD_VIA_OSS = os.environ.get("DOWNLOAD_VIA_OSS", "1") not in {
    "0",
    "false",
    "False",
    "",
}
STATIC_VIA_OSS = os.environ.get("STATIC_VIA_OSS", "1") not in {
    "0",
    "false",
    "False",
    "",
}
OSS_STATIC_REQUIRE_READY = os.environ.get("OSS_STATIC_REQUIRE_READY", "1") not in {
    "0",
    "false",
    "False",
    "",
}
OSS_STATIC_READY_PROBE_ATTEMPTS = int(
    os.environ.get("OSS_STATIC_READY_PROBE_ATTEMPTS", "3")
)
OSS_STATIC_READY_PROBE_DELAY_SECONDS = float(
    os.environ.get("OSS_STATIC_READY_PROBE_DELAY_SECONDS", "0.15")
)
OSS_PRESIGN_EXPIRES = int(os.environ.get("OSS_PRESIGN_EXPIRES", "900"))
OSS_STATIC_PRESIGN_EXPIRES = int(os.environ.get("OSS_STATIC_PRESIGN_EXPIRES", "3600"))


# =============================================================================
# 音频（声线 / 音乐 / 音效）：Higgsfield，见 engines/audio.py
# =============================================================================

# Beat speech in a reference voice runs on Higgsfield seed_audio. The names keep
# the INDEXTTS2 prefix for the voice-audio records and billing params that use them.
INDEXTTS2_RECORD_PROVIDER = "higgsfield"
INDEXTTS2_RECORD_MODEL = "seed_audio"

NEWAPI_BASE_URL = os.environ.get("NEWAPI_BASE_URL", "")
NEWAPI_API_KEY = os.environ.get("NEWAPI_API_KEY", "")


def get_effective_newapi_gateway_config():
    """Return the selected NewAPI runtime gateway credentials."""
    from novelvideo.model_gateway_settings import get_effective_newapi_config

    return get_effective_newapi_config(
        official_base_url=OFFICIAL_NEWAPI_BASE_URL,
        official_api_key=NEWAPI_API_KEY,
    )


def get_newapi_runtime_credentials(
    *,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
    env_api_key: str = "NEWAPI_API_KEY",
    env_base_url: str = "NEWAPI_BASE_URL",
) -> tuple[str, str]:
    """Resolve NewAPI credentials from the edition's effective gateway.

    CE reads dynamic credentials from settings.db and never falls back to the
    process environment. EE's deployment-time gateway remains environment
    backed. Explicit per-call overrides remain available for isolated tools.
    """

    gateway = get_effective_newapi_gateway_config()
    environment_backed = gateway.source == "environment"
    api_key = (
        str(api_key_override or "").strip()
        or str(gateway.api_key or "").strip()
        or (
            os.environ.get(env_api_key, "").strip()
            or NEWAPI_API_KEY
            or os.environ.get("MODEL_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            if environment_backed
            else ""
        )
    )
    base_url = (
        str(base_url_override or "").strip().rstrip("/")
        or str(gateway.base_url or "").strip()
        or (
            os.environ.get(env_base_url, "").strip().rstrip("/")
            or str(NEWAPI_BASE_URL or "").strip().rstrip("/")
            or os.environ.get("MODEL_BASE_URL", "").strip().rstrip("/")
            or OFFICIAL_NEWAPI_BASE_URL
            if environment_backed
            else ""
        )
    )
    return api_key, base_url




# Volcengine key, still read by the TTS config below.
VOLCENGINE_VISUAL_API_KEY = os.environ.get(
    "VOLCENGINE_VISUAL_API_KEY"
) or os.environ.get("ARK_API_KEY")

IMAGE_DEFAULT_STYLE = os.environ.get("IMAGE_DEFAULT_STYLE", "chinese_period_drama")


# 风格预设统一由 src/novelvideo/styles/presets/*.json 提供。


def get_style_preset(
    style: str = None,
    *,
    username: str | None = None,
    project: str | None = None,
    project_dir: str | None = None,
    state_dir: str | Path | None = None,
) -> dict:
    """获取视觉风格预设配置。

    Args:
        style: 风格名称，默认使用 IMAGE_DEFAULT_STYLE

    Returns:
        风格预设字典
    """
    style = style or IMAGE_DEFAULT_STYLE

    from novelvideo.services.style_service import StyleService

    config = StyleService.get_style(
        style,
        username=username,
        project=project,
        project_dir=project_dir,
        state_dir=state_dir,
    )
    if not config:
        raise KeyError(f"Style '{style}' not found")
    return config.to_legacy_dict()


# =============================================================================
# LLM 临时媒体中转（给 newAPI/视觉模型拉取本地参考图）
# =============================================================================

MEDIA_RELAY_PROVIDER = (
    os.environ.get("MEDIA_RELAY_PROVIDER", "aliyun_oss").strip().lower()
)
MEDIA_RELAY_TTL_SECONDS = int(os.environ.get("MEDIA_RELAY_TTL_SECONDS", "1800"))

OSS_RELAY_ENDPOINT = os.environ.get("OSS_RELAY_ENDPOINT", "oss-cn-chengdu.aliyuncs.com")
OSS_RELAY_BUCKET = os.environ.get("OSS_RELAY_BUCKET", "claymore-llm-relay")
OSS_RELAY_AK = os.environ.get("OSS_RELAY_AK", "")
OSS_RELAY_SK = os.environ.get("OSS_RELAY_SK", "")

CLOUDINARY_RELAY_CLOUD_NAME = os.environ.get("CLOUDINARY_RELAY_CLOUD_NAME", "")
CLOUDINARY_RELAY_API_KEY = os.environ.get("CLOUDINARY_RELAY_API_KEY", "")
CLOUDINARY_RELAY_API_SECRET = os.environ.get("CLOUDINARY_RELAY_API_SECRET", "")
CLOUDINARY_RELAY_FOLDER = os.environ.get("CLOUDINARY_RELAY_FOLDER", "")


def get_style_labels() -> dict[str, str]:
    """获取风格 ID -> 显示标签的映射。

    Returns:
        {style_id: label} 字典
    """
    from novelvideo.services.style_service import StyleService

    return StyleService.get_style_labels()


def list_available_styles() -> list[dict]:
    """列出所有可用风格（预设 + 自定义）。

    Returns:
        风格列表，每项包含 {id, name, label, type}
    """
    from novelvideo.services.style_service import StyleService

    return StyleService.list_all_styles()


# =============================================================================
# TTS 配置
# =============================================================================

TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "cosyvoice")  # 默认 CosyVoice
EDGE_TTS_VOICE = os.environ.get("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
VOLCENGINE_TTS_ENDPOINT = os.environ.get(
    "VOLCENGINE_TTS_ENDPOINT", "https://openspeech.bytedance.com/api/v1/tts"
)

# CosyVoice 配置（阿里云 DashScope）
COSYVOICE_MODEL = os.environ.get("COSYVOICE_MODEL", "cosyvoice-v3-flash")
COSYVOICE_VOICE = os.environ.get("COSYVOICE_VOICE", "longxiaoxia_v3")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")

# CosyVoice 语速倍率（范围 [0.5, 2.0]，1.0 为标准速度）
COSYVOICE_SPEECH_RATE = float(os.environ.get("COSYVOICE_SPEECH_RATE", "1.2"))

# TTS 语速估算（实测 1.0x 均值 4.45 字/秒 × 1.3x 加速 ≈ 5.8 字/秒）
TTS_CHARS_PER_SECOND = float(os.environ.get("TTS_CHARS_PER_SECOND", "5.8"))

# Dialogue beat TTS 配置（角色台词使用不同语速）
COSYVOICE_DIALOGUE_SPEECH_RATE = float(
    os.environ.get("COSYVOICE_DIALOGUE_SPEECH_RATE", "1.0")
)
TTS_DIALOGUE_CHARS_PER_SECOND = float(
    os.environ.get("TTS_DIALOGUE_CHARS_PER_SECOND", "4.45")
)


def get_tts_config() -> dict:
    """获取 TTS 配置。"""
    return {
        "provider": TTS_PROVIDER,
        # Edge TTS
        "default_voice": EDGE_TTS_VOICE,
        "rate": os.environ.get("TTS_RATE", "+0%"),
        "pitch": os.environ.get("TTS_PITCH", "+0Hz"),
        "volcengine_endpoint": VOLCENGINE_TTS_ENDPOINT,
        "volcengine_api_key": VOLCENGINE_VISUAL_API_KEY,
        # CosyVoice
        "cosyvoice_model": COSYVOICE_MODEL,
        "cosyvoice_voice": COSYVOICE_VOICE,
        "cosyvoice_speech_rate": COSYVOICE_SPEECH_RATE,
        "dashscope_api_key": DASHSCOPE_API_KEY,
    }


# =============================================================================
# Fish Audio S2 配置（情感语音合成）
# =============================================================================

FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
FISH_AUDIO_SPEED = float(os.environ.get("FISH_AUDIO_SPEED", "1.0"))

# Fish Audio 声音预设 (8 种: age_group × gender)
FISH_VOICE_PRESETS = {
    "child_male": os.environ.get("FISH_VOICE_CHILD_MALE", ""),
    "child_female": os.environ.get("FISH_VOICE_CHILD_FEMALE", ""),
    "youth_male": os.environ.get("FISH_VOICE_YOUTH_MALE", ""),
    "youth_female": os.environ.get("FISH_VOICE_YOUTH_FEMALE", ""),
    "middle_male": os.environ.get("FISH_VOICE_MIDDLE_MALE", ""),
    "middle_female": os.environ.get("FISH_VOICE_MIDDLE_FEMALE", ""),
    "elder_male": os.environ.get("FISH_VOICE_ELDER_MALE", ""),
    "elder_female": os.environ.get("FISH_VOICE_ELDER_FEMALE", ""),
}


def get_fish_voice_id(age_group: str, gender: str) -> str:
    """根据年龄段+性别获取预设 voice ID。

    两条抽取路写出的性别写法不同：legacy 的角色补全提示词要的是「男/女」,
    structured_v1 的抽取器要的是 ``male``/``female``。只认中文会把每一个
    structured 项目的女性角色都配成男声，所以两种写法都要认。
    """
    text = str(gender or "").strip().lower()
    gender_key = "female" if ("女" in text or "female" in text) else "male"
    return FISH_VOICE_PRESETS.get(f"{age_group}_{gender_key}", "")


# =============================================================================
# 视频合成配置
# =============================================================================

FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")
VIDEO_FPS = int(os.environ.get("VIDEO_FPS", "30"))
VIDEO_WIDTH = int(os.environ.get("VIDEO_WIDTH", "1080"))
VIDEO_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "1920"))
VIDEO_CODEC = os.environ.get("VIDEO_CODEC", "libx264")
VIDEO_AUDIO_CODEC = os.environ.get("VIDEO_AUDIO_CODEC", "aac")
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "4M")

KEN_BURNS_ZOOM_RANGE = (1.0, 1.15)
KEN_BURNS_PAN_SPEED = 0.02

# =============================================================================
# AI 视频生成配置（图生视频）
# =============================================================================


def _csv_env(name: str, default: str) -> list[str]:
    values = [item.strip() for item in os.environ.get(name, default).split(",")]
    return [item for item in values if item]


# 视频生成后端: higgsfield:<job_type>[?preset] (默认 Seedance 2.0 Fast), h3c, mock.
# 模型目录来自 `higgsfield model list`，见 novelvideo.engines.higgsfield。
VIDEO_BACKEND = os.environ.get("VIDEO_BACKEND", "")


# 默认视频分辨率（竖屏）
VIDEO_RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "720x1280")

# 分辨率预设
VIDEO_RESOLUTION_PRESETS = {
    "720x1280": {"width": 720, "height": 1280, "label": "720p 竖屏"},
    "1080x1920": {"width": 1080, "height": 1920, "label": "1080p 竖屏"},
}


def get_video_generation_config() -> dict:
    """获取 AI 视频生成（图生视频）配置。"""
    resolution = VIDEO_RESOLUTION_PRESETS.get(
        VIDEO_RESOLUTION, VIDEO_RESOLUTION_PRESETS["720x1280"]
    )
    from novelvideo.generators.video_generator import normalize_video_backend

    return {
        "backend": normalize_video_backend(VIDEO_BACKEND),
        "resolution": VIDEO_RESOLUTION,
        "width": resolution["width"],
        "height": resolution["height"],
        "resolution_presets": VIDEO_RESOLUTION_PRESETS,
    }


def get_video_config() -> dict:
    """获取视频配置。"""
    return {
        "ffmpeg_path": FFMPEG_PATH,
        "fps": VIDEO_FPS,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "codec": VIDEO_CODEC,
        "audio_codec": VIDEO_AUDIO_CODEC,
        "bitrate": VIDEO_BITRATE,
        "ken_burns_zoom_range": KEN_BURNS_ZOOM_RANGE,
        "ken_burns_pan_speed": KEN_BURNS_PAN_SPEED,
    }


# =============================================================================
# 图像生成配置：Draw Things (locale) / Higgsfield (cloud)
# =============================================================================

# Keys other callers still use for text / vision LLMs (not image generation).
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# A selection is "drawthings" or "higgsfield:<model_ref>" (see media_catalog).
# Default: Nano Banana 2 on Higgsfield — multi-reference, so characters and
# identities stay consistent across sheets and panels; Draw Things (local, one
# img2img init image) is one env var away: DEFAULT_IMAGE_SELECTION=drawthings.
DEFAULT_IMAGE_SELECTION = os.environ.get(
    "DEFAULT_IMAGE_SELECTION", "higgsfield:nano_banana_flash"
)
DEFAULT_SKETCH_IMAGE_SELECTION = os.environ.get(
    "DEFAULT_SKETCH_IMAGE_SELECTION", DEFAULT_IMAGE_SELECTION
)
DEFAULT_RENDER_IMAGE_SELECTION = os.environ.get(
    "DEFAULT_RENDER_IMAGE_SELECTION", DEFAULT_IMAGE_SELECTION
)
CHARACTER_IMAGE_SELECTION = (
    os.environ.get("CHARACTER_IMAGE_SELECTION")
    or os.environ.get("DEFAULT_CHARACTER_IMAGE_SELECTION")
    or DEFAULT_IMAGE_SELECTION
)

# Retired providers' selection keys, kept readable in saved projects/canvases.
LEGACY_IMAGE_GENERATION_SELECTION_ALIASES = {
    "newapi_gpt_image2": "higgsfield:gpt_image_2",
    "openai_gpt_image2": "higgsfield:gpt_image_2",
    "openrouter_gpt_image2": "higgsfield:gpt_image_2",
    "huimeng_gpt_image2": "higgsfield:gpt_image_2",
    "huimeng_image2_official": "higgsfield:gpt_image_2",
    "newapi_nanobanana2": "higgsfield:nano_banana_flash",
    "openrouter_nanobanana2": "higgsfield:nano_banana_flash",
    "huimeng_nanobanana2": "higgsfield:nano_banana_flash",
    "nanobanana": "higgsfield:nano_banana_flash",
    "seedream": "higgsfield:seedream_v5_pro",
}


class _ImageSelections(Mapping):
    """`{selection: {label, provider, model}}` for every installed image model.

    Lookups never touch the engines (any well-formed selection resolves);
    iterating lists the live catalog (Higgsfield's is disk-cached for a day).
    """

    def __getitem__(self, key: str) -> dict[str, str]:
        from novelvideo.engines.image import is_selection, model_of, provider_of

        if not is_selection(key):
            raise KeyError(key)
        return {
            "label": _image_selection_labels().get(key) or model_of(key),
            "provider": provider_of(key),
            "model": model_of(key),
        }

    def __contains__(self, key: object) -> bool:
        from novelvideo.engines.image import is_selection

        return isinstance(key, str) and is_selection(key)

    def __iter__(self):
        return iter(image_generation_selection_options())

    def __len__(self) -> int:
        return len(image_generation_selection_options())


def _image_selection_labels() -> dict[str, str]:
    from novelvideo.engines import higgsfield
    from novelvideo.media_catalog import DRAWTHINGS_SELECTION

    labels = {DRAWTHINGS_SELECTION: "Draw Things (locale)"}
    for job_type, preset, label in higgsfield.FEATURED["image"]:
        labels[f"higgsfield:{higgsfield.model_ref(job_type, preset)}"] = label
    return labels


IMAGE_GENERATION_SELECTIONS = _ImageSelections()

# 网格生成模式配置
# 竖屏 Panel 模式（每格竖屏，适合 I2V）：
# "1x1" - 单张生成（1K 分辨率，panel 高度 1376）
# "1x3" - 横向三格（panel 高度 877，竖屏 0.78）
# "1x4" - 横向四格（panel 高度 1097，竖屏 0.58）官方推荐 3-4 panel comic
# "3x2" - 6 panels（panel 高度 1365 ✓，竖屏 0.84）
# "4x3" - 12 panels（panel 高度 1024 ✓，竖屏 0.75）最优
# "5x4" - 20 panels（panel 高度 819，竖屏 0.70）
# 正方形 Panel 模式：
# "2x2" - 紧凑四格
# "3x3" - 分批生成（更稳定）
# "4x4" - 分批生成（中等，panel 高度 1024 ✓）
# "5x5" - 批量生成，最大 25 面板
GRID_MODE = os.environ.get("GRID_MODE", "1x1")

# 网格尺寸配置表
# 格式: mode -> (rows, cols, batch_size)
MODE_CONFIG = {
    # 竖屏 Panel 模式（每格竖屏，适合 I2V）
    "1x1": (1, 1, 1),
    "1x2": (1, 2, 2),  # panel 0.89 竖屏
    "1x3": (1, 3, 3),  # panel 0.78 竖屏 ✓
    "1x4": (1, 4, 4),  # panel 0.58 竖屏 ✓ 官方推荐
    "3x2": (3, 2, 6),  # panel 0.84 竖屏, 高度 1365 ✓
    "4x3": (4, 3, 12),  # panel 0.75 竖屏 ✓ 最优
    "5x4": (5, 4, 20),  # panel 0.70 竖屏 ✓
    # 正方形 Panel 模式
    "2x2": (2, 2, 4),
    "3x3": (3, 3, 9),
    "4x4": (4, 4, 16),
    "5x5": (5, 5, 25),
}

# 网格尺寸配置（根据 GRID_MODE 自动设置）
if GRID_MODE in MODE_CONFIG:
    GRID_ROWS, GRID_COLS, GRID_BATCH_SIZE = MODE_CONFIG[GRID_MODE]
else:
    # 默认使用 1x1
    GRID_ROWS, GRID_COLS, GRID_BATCH_SIZE = 1, 1, 1
GRID_TOTAL_PANELS = 25  # 动态优化时的最大面板数


def image_generation_selection_options() -> dict[str, str]:
    """`{selection: label}` for the image models installed right now."""
    from novelvideo.media_catalog import media_model_catalog

    return {entry["catalogId"]: entry["label"] for entry in media_model_catalog("image")}


def character_image_selection_options() -> dict[str, str]:
    """Return UI labels for character/identity image generation."""
    return image_generation_selection_options()


def _known_image_selection(value: str | None) -> str:
    from novelvideo.engines.image import is_selection

    candidate = str(value or "").strip()
    candidate = LEGACY_IMAGE_GENERATION_SELECTION_ALIASES.get(candidate, candidate)
    return candidate if is_selection(candidate) else ""


def normalize_image_generation_selection(
    value: str | None,
    *,
    fallback: str | None = None,
) -> str:
    for candidate in (value, fallback, DEFAULT_IMAGE_SELECTION):
        selection = _known_image_selection(candidate)
        if selection:
            return selection
    return "higgsfield:nano_banana_flash"


def image_generation_selection_label(
    value: str | None, *, fallback: str | None = None
) -> str:
    selection = normalize_image_generation_selection(value, fallback=fallback)
    return IMAGE_GENERATION_SELECTIONS[selection]["label"]


def get_character_image_selection() -> str:
    """Return the configured character/identity image source selection."""
    return normalize_image_generation_selection(CHARACTER_IMAGE_SELECTION)


def normalize_character_image_selection(value: str | None) -> str:
    return _known_image_selection(value) or get_character_image_selection()


def infer_image_generation_selection(
    provider: str | None,
    model: str | None,
    *,
    fallback: str | None = None,
) -> str:
    provider_norm = str(provider or "").strip().lower()
    model_norm = str(model or "").strip()
    from novelvideo.engines.image import is_selection

    if is_selection(model_norm):
        return model_norm
    if provider_norm == "drawthings":
        return "drawthings"
    if provider_norm == "higgsfield" and model_norm:
        return f"higgsfield:{model_norm}"
    legacy = _known_image_selection(model_norm) or _known_image_selection(provider_norm)
    return legacy or normalize_image_generation_selection(
        fallback, fallback=DEFAULT_SKETCH_IMAGE_SELECTION
    )


def get_grid_generation_config(
    selection_override: str | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
    image_size_override: str | None = None,
) -> dict:
    """Image engine config for grid/sketch/render generation.

    `provider_override`/`model_override` name an engine directly
    (`drawthings`, or `higgsfield` + model ref); otherwise the selection wins.
    """
    if provider_override or model_override:
        selection = infer_image_generation_selection(
            provider_override or "higgsfield",
            model_override,
            fallback=selection_override or DEFAULT_RENDER_IMAGE_SELECTION,
        )
    else:
        selection = normalize_image_generation_selection(
            selection_override, fallback=DEFAULT_RENDER_IMAGE_SELECTION
        )
    entry = IMAGE_GENERATION_SELECTIONS[selection]
    return {
        "selection": selection,
        "provider": entry["provider"],
        "model": entry["model"],
        "image_size": image_size_override or "1K",
        "mode": GRID_MODE,
        "rows": GRID_ROWS,
        "cols": GRID_COLS,
        "batch_size": GRID_BATCH_SIZE,
        "total_panels": GRID_TOTAL_PANELS,
    }


def get_sketch_generation_config(
    selection_override: str | None = None,
    model_override: str | None = None,
) -> dict:
    """Image engine config for the sketch workbench."""
    config = get_grid_generation_config(
        selection_override=normalize_image_generation_selection(
            selection_override, fallback=DEFAULT_SKETCH_IMAGE_SELECTION
        ),
        model_override=model_override,
    )
    config["image_size"] = "1K"
    return config


def get_render_generation_config(
    selection_override: str | None = None,
    model_override: str | None = None,
) -> dict:
    """Image engine config for first-frame renders."""
    return get_grid_generation_config(
        selection_override=selection_override,
        model_override=model_override,
    )


# =============================================================================
# 草图（Sketch）路径管理
# =============================================================================


def get_sketch_dir(project_name: str, episode: int) -> str:
    """获取整集草图存放目录。

    Args:
        project_name: 项目名称（如 admin/test1）
        episode: 集数

    Returns:
        草图目录路径，如 output/admin/test1/grids/ep001/sketch
    """
    base_dir = os.path.abspath(os.path.join(OUTPUT_DIR, project_name))
    return os.path.join(base_dir, "grids", f"ep{episode:03d}", "sketch")


def get_sketch_path(project_name: str, episode: int, sketch_index: int = 1) -> str:
    """获取整集草图路径（已弃用，保留向后兼容）。

    新模式下草图文件名为 sketch_b{start}-{end}_{rows}x{cols}.jpg，
    建议使用 list_sketch_files() 遍历草图目录。

    Args:
        project_name: 项目名称（如 admin/test1）
        episode: 集数
        sketch_index: 草图索引（1-based），默认为 1

    Returns:
        草图目录路径（新模式下返回目录而非具体文件）
    """
    return get_sketch_dir(project_name, episode)


def list_sketch_files(project_name: str, episode: int) -> list[str]:
    """列出指定集的所有草图文件。

    支持新命名约定: sketch_b{start}-{end}_{rows}x{cols}.jpg

    Args:
        project_name: 项目名称
        episode: 集数

    Returns:
        草图文件路径列表（按文件名排序）
    """
    sketch_dir = get_sketch_dir(project_name, episode)
    if not os.path.exists(sketch_dir):
        return []

    import glob

    pattern = os.path.join(sketch_dir, "sketch_b*_*x*.jpg")
    files = glob.glob(pattern)
    return sorted(files)


# =============================================================================
# 项目管理
# =============================================================================


def get_project_dir(project_name: str) -> str:
    """获取项目输出目录。"""
    return os.path.join(OUTPUT_DIR, project_name)


def ensure_project_dirs(project_name: str) -> dict[str, str]:
    """确保项目目录结构存在，返回资源目录路径。

    `project_name` 可为 `username/project` 或历史单目录格式 `project`。
    当包含用户名时，会同时确保 output/state/runtime 三类目录存在。
    """
    base_dir = os.path.abspath(get_project_dir(project_name))

    parts = project_name.split("/", 1)
    if len(parts) == 2:
        from novelvideo.utils.project_paths import ProjectPaths

        paths = ProjectPaths(parts[0], parts[1])
        paths.ensure_dirs()
        paths.bootstrap_from_legacy_output()

    dirs = {
        "base": base_dir,
        "graph": os.path.join(base_dir, "graph"),
        "assets": os.path.join(base_dir, "assets"),
        "characters": os.path.join(base_dir, "assets", "characters"),
        "scripts": os.path.join(base_dir, "scripts"),
        "images": os.path.join(base_dir, "images"),
        "frames": os.path.join(base_dir, "frames"),  # 首帧图片
        "audio": os.path.join(base_dir, "audio"),
        "videos": os.path.join(base_dir, "videos"),
    }

    for path in dirs.values():
        os.makedirs(path, exist_ok=True)

    return dirs


def ensure_project_dirs_at_paths(
    *,
    output_dir: str | os.PathLike[str],
    state_dir: str | os.PathLike[str],
    runtime_dir: str | os.PathLike[str],
) -> dict[str, str]:
    """Ensure project directories from registry paths without legacy bootstrap."""
    base_dir = os.path.abspath(os.fspath(output_dir))
    dirs = {
        "base": base_dir,
        "graph": os.path.join(base_dir, "graph"),
        "assets": os.path.join(base_dir, "assets"),
        "characters": os.path.join(base_dir, "assets", "characters"),
        "scripts": os.path.join(base_dir, "scripts"),
        "images": os.path.join(base_dir, "images"),
        "frames": os.path.join(base_dir, "frames"),
        "audio": os.path.join(base_dir, "audio"),
        "videos": os.path.join(base_dir, "videos"),
        "state": os.path.abspath(os.fspath(state_dir)),
        "runtime": os.path.abspath(os.fspath(runtime_dir)),
        "logs": os.path.join(os.path.abspath(os.fspath(runtime_dir)), "logs"),
        "staging": os.path.join(os.path.abspath(os.fspath(runtime_dir)), "staging"),
        "temp_sketch_panels": os.path.join(
            os.path.abspath(os.fspath(runtime_dir)),
            "temp_sketch_panels",
        ),
    }

    for path in dirs.values():
        os.makedirs(path, exist_ok=True)

    return dirs
