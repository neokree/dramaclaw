"""NovelVideo 配置模块。

独立的配置系统，不依赖 SuperScript。
"""

import os
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


NEWAPI_IMAGE_MODEL = os.environ.get("NEWAPI_IMAGE_MODEL", "LingShan-G2")
NEWAPI_NANOBANANA2_MODEL = os.environ.get("NEWAPI_NANOBANANA2_MODEL", "LingShan-NB-2")
SCENE_MASTER_IMAGE_PROVIDER = (
    os.environ.get("SCENE_MASTER_IMAGE_PROVIDER", "").strip().lower() or "newapi"
)
SCENE_MASTER_IMAGE_MODEL = os.environ.get("SCENE_MASTER_IMAGE_MODEL", "")
SCENE_REVERSE_MASTER_IMAGE_PROVIDER = (
    os.environ.get("SCENE_REVERSE_MASTER_IMAGE_PROVIDER", "").strip().lower()
    or "newapi"
)
SCENE_REVERSE_MASTER_IMAGE_MODEL = os.environ.get(
    "SCENE_REVERSE_MASTER_IMAGE_MODEL", ""
)
SCENE_360_IMAGE_PROVIDER = (
    os.environ.get("SCENE_360_IMAGE_PROVIDER", "").strip().lower() or "newapi"
)
SCENE_360_IMAGE_MODEL = os.environ.get("SCENE_360_IMAGE_MODEL", "")
PROP_REF_IMAGE_PROVIDER = (
    os.environ.get("PROP_REF_IMAGE_PROVIDER", "").strip().lower() or "newapi"
)
PROP_REF_IMAGE_MODEL = os.environ.get("PROP_REF_IMAGE_MODEL", "")


# =============================================================================
# 火山引擎图像生成配置
# =============================================================================

VOLCENGINE_VISUAL_API_KEY = os.environ.get(
    "VOLCENGINE_VISUAL_API_KEY"
) or os.environ.get("ARK_API_KEY")
VOLCENGINE_VISUAL_ENDPOINT = os.environ.get(
    "VOLCENGINE_VISUAL_ENDPOINT", "https://ark.cn-beijing.volces.com/api/v3"
)

SEEDREAM_MODEL = os.environ.get("SEEDREAM_MODEL", "doubao-seedream-4-5-251128")
SEEDEDIT_MODEL = os.environ.get("SEEDEDIT_MODEL", "doubao-seededit-3-0-i2i-250628")

IMAGE_DEFAULT_WIDTH = int(os.environ.get("IMAGE_DEFAULT_WIDTH", "1440"))
IMAGE_DEFAULT_HEIGHT = int(os.environ.get("IMAGE_DEFAULT_HEIGHT", "2560"))
IMAGE_DEFAULT_STYLE = os.environ.get("IMAGE_DEFAULT_STYLE", "chinese_period_drama")

# 角色参考图生成模型选择
# "nanobanana" - 使用 Nano Banana Pro (Gemini)，与网格生成同一模型，一致性更好
# "seedream" - 使用 Seedream 4.5 (火山引擎)，质量高但与网格生成跨模型
CHARACTER_IMAGE_MODEL = os.environ.get("CHARACTER_IMAGE_MODEL", "nanobanana")

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


def get_image_config() -> dict:
    """获取图像生成配置。"""
    from novelvideo.services.style_service import StyleService

    all_styles = StyleService.list_all_styles()
    style_presets = {
        s["id"]: StyleService.get_legacy_style_preset(s["id"]) for s in all_styles
    }

    return {
        "api_key": VOLCENGINE_VISUAL_API_KEY,
        "endpoint": VOLCENGINE_VISUAL_ENDPOINT,
        "seedream_model": SEEDREAM_MODEL,
        "seededit_model": SEEDEDIT_MODEL,
        "default_width": IMAGE_DEFAULT_WIDTH,
        "default_height": IMAGE_DEFAULT_HEIGHT,
        "default_style": IMAGE_DEFAULT_STYLE,
        "style_presets": style_presets,
        "character_image_model": CHARACTER_IMAGE_MODEL,
        "character_image_selection": get_character_image_selection(),
    }


def get_character_image_model() -> str:
    """获取角色参考图生成模型类型。

    Returns:
        "nanobanana" 或 "seedream"
    """
    return CHARACTER_IMAGE_MODEL


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
# 图像生成配置（Google / OpenRouter / OpenAI / HuiMeng）
# =============================================================================

GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HUIMENGI_API_KEY = os.environ.get("HUIMENGI_API_KEY")
OPENROUTER_GPT_IMAGE2_MODEL = os.environ.get(
    "OPENROUTER_GPT_IMAGE2_MODEL", "openai/gpt-5.4-image-2"
)
OPENROUTER_NANOBANANA2_MODEL = os.environ.get(
    "OPENROUTER_NANOBANANA2_MODEL", "google/gemini-3.1-flash-image-preview"
)

# 图像生成 Provider: "google" / "openrouter" / "openai" / "huimeng"
# OpenRouter 价格: $0.002/图 (2K) vs Google 官方 $0.134/图 (2K)
_NANOBANANA_PROVIDER_EXPLICIT = "NANOBANANA_PROVIDER" in os.environ
NANOBANANA_PROVIDER = os.environ.get("NANOBANANA_PROVIDER", "openrouter")


NANOBANANA_MODEL = os.environ.get("NANOBANANA_MODEL", "gemini-3.1-flash-image-preview")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
HUIMENG_IMAGE_MODEL = os.environ.get("HUIMENG_IMAGE_MODEL", "image-2")
HUIMENG_IMAGE_OFFICIAL_MODEL = os.environ.get(
    "HUIMENG_IMAGE_OFFICIAL_MODEL", "image-2-official"
)
HUIMENG_NANOBANANA2_MODEL = os.environ.get("HUIMENG_NANOBANANA2_MODEL", "nb-2")
SCENE_360_PROVIDER = os.environ.get("SCENE_360_PROVIDER") or NANOBANANA_PROVIDER
SCENE_360_HUIMENG_MODEL = os.environ.get("SCENE_360_HUIMENG_MODEL", HUIMENG_IMAGE_MODEL)
SCENE_ASSET_PROVIDER = os.environ.get("SCENE_ASSET_PROVIDER") or NANOBANANA_PROVIDER
SCENE_ASSET_MODEL = os.environ.get("SCENE_ASSET_MODEL", "")
OPENAI_IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "medium")
OPENAI_SKETCH_IMAGE_QUALITY = os.environ.get("OPENAI_SKETCH_IMAGE_QUALITY", "low")
_DEFAULT_SKETCH_SELECTION_EXPLICIT = "DEFAULT_SKETCH_IMAGE_SELECTION" in os.environ
_DEFAULT_RENDER_SELECTION_EXPLICIT = "DEFAULT_RENDER_IMAGE_SELECTION" in os.environ
DEFAULT_SKETCH_IMAGE_SELECTION = os.environ.get(
    "DEFAULT_SKETCH_IMAGE_SELECTION", "newapi_gpt_image2"
)
DEFAULT_RENDER_IMAGE_SELECTION = os.environ.get(
    "DEFAULT_RENDER_IMAGE_SELECTION", "newapi_gpt_image2"
)
CHARACTER_IMAGE_SELECTION = os.environ.get(
    "CHARACTER_IMAGE_SELECTION"
) or os.environ.get("DEFAULT_CHARACTER_IMAGE_SELECTION")

IMAGE_GENERATION_SELECTIONS: dict[str, dict[str, str]] = {
    "huimeng_gpt_image2": {
        "label": "HuiMeng GPT Image 2",
        "provider": "huimeng",
        "model": HUIMENG_IMAGE_MODEL,
    },
    "huimeng_image2_official": {
        "label": "HuiMeng Image 2 Official",
        "provider": "huimeng",
        "model": HUIMENG_IMAGE_OFFICIAL_MODEL,
    },
    "huimeng_nanobanana2": {
        "label": "HuiMeng NanoBanana 2",
        "provider": "huimeng",
        "model": HUIMENG_NANOBANANA2_MODEL,
    },
    "openai_gpt_image2": {
        "label": "OpenAI GPT Image 2",
        "provider": "openai",
        "model": OPENAI_IMAGE_MODEL,
    },
    "openrouter_gpt_image2": {
        "label": "OpenRouter GPT Image 2",
        "provider": "openrouter",
        "model": OPENROUTER_GPT_IMAGE2_MODEL,
    },
    "openrouter_nanobanana2": {
        "label": "OpenRouter NanoBanana 2",
        "provider": "openrouter",
        "model": OPENROUTER_NANOBANANA2_MODEL,
    },
    "newapi_gpt_image2": {
        "label": "LingShan-G2",
        "provider": "newapi",
        "model": NEWAPI_IMAGE_MODEL,
    },
    "newapi_nanobanana2": {
        "label": "LingShan-NB-2",
        "provider": "newapi",
        "model": NEWAPI_NANOBANANA2_MODEL,
    },
}

VISIBLE_IMAGE_GENERATION_SELECTION_KEYS = (
    "newapi_gpt_image2",
    "newapi_nanobanana2",
)

LEGACY_IMAGE_GENERATION_SELECTION_ALIASES = {
    "huimeng_gpt_image2": "newapi_gpt_image2",
    "huimeng_image2_official": "newapi_gpt_image2",
    "openai_gpt_image2": "newapi_gpt_image2",
    "openrouter_gpt_image2": "newapi_gpt_image2",
    "huimeng_nanobanana2": "newapi_nanobanana2",
    "openrouter_nanobanana2": "newapi_nanobanana2",
    "nanobanana": "newapi_nanobanana2",
    "seedream": "newapi_gpt_image2",
}

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
    """Return UI labels for configured image-generation selections."""
    return {
        key: IMAGE_GENERATION_SELECTIONS[key]["label"]
        for key in VISIBLE_IMAGE_GENERATION_SELECTION_KEYS
        if key in IMAGE_GENERATION_SELECTIONS
    }


def character_image_selection_options() -> dict[str, str]:
    """Return UI labels for character/identity image generation."""
    return image_generation_selection_options()


def _visible_image_generation_selection(value: str | None) -> str:
    candidate = str(value or "").strip()
    if (
        candidate in VISIBLE_IMAGE_GENERATION_SELECTION_KEYS
        and candidate in IMAGE_GENERATION_SELECTIONS
    ):
        return candidate
    alias = LEGACY_IMAGE_GENERATION_SELECTION_ALIASES.get(candidate)
    if (
        alias in VISIBLE_IMAGE_GENERATION_SELECTION_KEYS
        and alias in IMAGE_GENERATION_SELECTIONS
    ):
        return alias
    return ""


def _default_image_generation_selection(fallback: str | None = None) -> str:
    for candidate in (
        fallback,
        DEFAULT_SKETCH_IMAGE_SELECTION,
        DEFAULT_RENDER_IMAGE_SELECTION,
        "newapi_gpt_image2",
        *VISIBLE_IMAGE_GENERATION_SELECTION_KEYS,
    ):
        selection = _visible_image_generation_selection(candidate)
        if selection:
            return selection
    raise ValueError("No visible image generation selection configured.")


def normalize_image_generation_selection(
    value: str | None,
    *,
    fallback: str | None = None,
) -> str:
    selection = _visible_image_generation_selection(value)
    if selection:
        return selection
    return _default_image_generation_selection(fallback)


def image_generation_selection_label(
    value: str | None, *, fallback: str | None = None
) -> str:
    selection = normalize_image_generation_selection(value, fallback=fallback)
    return IMAGE_GENERATION_SELECTIONS[selection]["label"]


def get_character_image_selection() -> str:
    """Return the configured character/identity image source selection."""
    candidate = _visible_image_generation_selection(CHARACTER_IMAGE_SELECTION)
    if candidate:
        return candidate

    legacy_model = str(CHARACTER_IMAGE_MODEL or "").strip()
    legacy_selection = _visible_image_generation_selection(legacy_model)
    if legacy_selection:
        return legacy_selection

    return normalize_image_generation_selection(
        DEFAULT_RENDER_IMAGE_SELECTION,
        fallback=DEFAULT_SKETCH_IMAGE_SELECTION,
    )


def normalize_character_image_selection(value: str | None) -> str:
    candidate = _visible_image_generation_selection(value)
    if candidate:
        return candidate
    return get_character_image_selection()


def infer_image_generation_selection(
    provider: str | None,
    model: str | None,
    *,
    fallback: str | None = None,
) -> str:
    provider_norm = str(provider or "").strip().lower()
    model_norm = str(model or "").strip()
    for key, entry in IMAGE_GENERATION_SELECTIONS.items():
        if entry["provider"] == provider_norm and entry["model"] == model_norm:
            return key
    if provider_norm == "openrouter" and model_norm in {
        NANOBANANA_MODEL,
        f"google/{NANOBANANA_MODEL}",
    }:
        return "openrouter_nanobanana2"
    if provider_norm == "huimeng" and model_norm == "image-2":
        return "huimeng_gpt_image2"
    if provider_norm == "huimeng" and model_norm == "image-2-official":
        return "huimeng_image2_official"
    return normalize_image_generation_selection(
        fallback, fallback=DEFAULT_SKETCH_IMAGE_SELECTION
    )


def _image_provider_config(
    provider: str,
    *,
    model_override: str | None = None,
    selection_override: str | None = None,
) -> dict:
    if selection_override:
        selection = normalize_image_generation_selection(selection_override)
        entry = IMAGE_GENERATION_SELECTIONS[selection]
        provider = entry["provider"]
        model = model_override or entry["model"]
    else:
        provider = (provider or "openrouter").lower()
        model = model_override or ""

    if provider == "openrouter":
        resolved_model = model or (
            f"google/{NANOBANANA_MODEL}"
            if not NANOBANANA_MODEL.startswith("google/")
            else NANOBANANA_MODEL
        )
        return {
            "provider": provider,
            "api_key": OPENROUTER_API_KEY,
            "model": resolved_model,
        }
    if provider in {"huimeng", "huimengi"}:
        return {
            "provider": "huimeng",
            "api_key": HUIMENGI_API_KEY,
            "model": model or HUIMENG_IMAGE_MODEL,
        }
    if provider == "openai":
        return {
            "provider": provider,
            "api_key": OPENAI_API_KEY,
            "model": model or OPENAI_IMAGE_MODEL,
        }
    if provider == "newapi":
        gateway = get_effective_newapi_gateway_config()
        return {
            "provider": provider,
            "api_key": gateway.api_key,
            "model": model or NEWAPI_IMAGE_MODEL,
            "base_url": gateway.base_url,
        }

    return {
        "provider": "google",
        "api_key": GOOGLE_AI_API_KEY,
        "model": model or NANOBANANA_MODEL,
    }


def get_grid_generation_config(
    selection_override: str | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
    image_size_override: str | None = None,
) -> dict:
    """获取网格生成配置。

    支持四种 Provider:
    - google: 直连 Google AI Studio (GOOGLE_AI_API_KEY)
    - openrouter: 通过 OpenRouter 代理 (OPENROUTER_API_KEY)，成本降低 60 倍
    - openai: 通过 OpenAI Image API (OPENAI_API_KEY)，默认 gpt-image-2
    - huimeng: 通过 HuiMeng Tasks API (HUIMENGI_API_KEY)

    环境变量:
    - NANOBANANA_PROVIDER: "google" / "openrouter" / "openai" / "huimeng"
    - GOOGLE_AI_API_KEY: Google AI Studio API Key
    - OPENROUTER_API_KEY: OpenRouter API Key
    - OPENAI_API_KEY: OpenAI API Key
    - HUIMENGI_API_KEY: HuiMeng API Key
    - OPENAI_IMAGE_MODEL: OpenAI Image API 模型，默认 gpt-image-2
    - HUIMENG_IMAGE_MODEL: HuiMeng 图片模型，默认 image-2
    - DEFAULT_SKETCH_IMAGE_SELECTION / DEFAULT_RENDER_IMAGE_SELECTION: UI 默认图片源
    """
    if (
        selection_override is None
        and provider_override is None
        and _DEFAULT_RENDER_SELECTION_EXPLICIT
    ):
        selection_override = DEFAULT_RENDER_IMAGE_SELECTION

    provider_config = _image_provider_config(
        provider_override or NANOBANANA_PROVIDER,
        model_override=model_override,
        selection_override=selection_override,
    )

    return {
        "provider": provider_config["provider"],
        "api_key": provider_config["api_key"],
        "model": provider_config["model"],
        "base_url": provider_config.get("base_url", ""),
        "openai_image_quality": OPENAI_IMAGE_QUALITY,
        "openai_sketch_image_quality": OPENAI_SKETCH_IMAGE_QUALITY,
        "huimeng_image_quality": os.environ.get("HUIMENG_IMAGE_QUALITY", "medium"),
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
    """获取草图工作台网格生成配置。

    优先级:
    1. 显式 DEFAULT_SKETCH_IMAGE_SELECTION（新选择表）
    2. 显式 NANOBANANA_PROVIDER / NANOBANANA_MODEL（旧环境变量兼容）
    3. 通用 get_grid_generation_config()
    """
    selection = selection_override
    if selection is None:
        selection = (
            DEFAULT_SKETCH_IMAGE_SELECTION
            if (_DEFAULT_SKETCH_SELECTION_EXPLICIT or not _NANOBANANA_PROVIDER_EXPLICIT)
            else None
        )
    provider_override = None if selection else NANOBANANA_PROVIDER
    config = get_grid_generation_config(
        selection_override=selection,
        provider_override=provider_override,
        model_override=model_override,
    )
    config["openai_image_quality"] = OPENAI_SKETCH_IMAGE_QUALITY
    config["huimeng_image_quality"] = "low"
    config["image_size"] = "1K"
    return config


def get_render_generation_config(
    selection_override: str | None = None,
    model_override: str | None = None,
) -> dict:
    """获取首帧渲染图像配置。"""
    selection = selection_override
    if selection is None:
        selection = (
            DEFAULT_RENDER_IMAGE_SELECTION
            if (_DEFAULT_RENDER_SELECTION_EXPLICIT or not _NANOBANANA_PROVIDER_EXPLICIT)
            else None
        )
    provider_override = None if selection else NANOBANANA_PROVIDER
    return get_grid_generation_config(
        selection_override=selection,
        provider_override=provider_override,
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
