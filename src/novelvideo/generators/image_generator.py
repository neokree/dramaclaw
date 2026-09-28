"""Image generator front doors: character/identity sheets and a plain generator.

Every real image goes through `novelvideo.engines.image.generate_image`
(Draw Things or Higgsfield); `MockImageGenerator` stays for tests and dry runs.
"""

import os
from typing import Optional

from pydantic import BaseModel, Field

from novelvideo.config import (
    DEFAULT_IMAGE_SELECTION,
    get_character_image_selection,
    normalize_character_image_selection,
)
from novelvideo.shared.billing_errors import is_insufficient_credits_error


class ImageGenParams(BaseModel):
    """图像生成参数（旧版，保留兼容）。"""

    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    style: str = "chinese_period_drama"  # 默认写实古装剧风格
    project_dir: str = ""  # 项目目录，用于读取项目级自定义风格

    # 参考图（用于角色一致性）
    reference_image: Optional[str] = None
    reference_strength: float = 0.7  # 参考行业最佳实践 70-85%

    # 其他参数
    seed: Optional[int] = None
    steps: int = 30
    cfg_scale: float = 7.0


class ImageGenerationRequest(BaseModel):
    """图像生成请求（支持多参考图）。

    适配即梦 4.0 API，支持最多 10 张参考图。
    """

    prompt: str = Field(description="中文提示词")
    negative_prompt: str = Field(default="", description="负向提示词")
    width: int = Field(default=720, description="图像宽度")
    height: int = Field(default=1280, description="图像高度（竖屏）")

    # 多参考图支持（最多10张）
    reference_images: list[str] = Field(
        default_factory=list,
        description="参考图路径列表（最多10张）",
    )
    reference_prompts: list[str] = Field(
        default_factory=list,
        description="每张参考图对应的角色描述（与 reference_images 一一对应）",
    )
    reference_scale: float = Field(
        default=0.7,  # 参考行业最佳实践 70-85%
        ge=0,
        le=1,
        description="参考图权重 0-1",
    )

    # 上一帧（用于连贯性）
    previous_frame: Optional[str] = Field(
        default=None,
        description="上一帧图片路径（用于帧间连贯性）",
    )

    # 其他参数
    seed: Optional[int] = None

    # 提示词控制
    skip_prompt_enhancement: bool = Field(
        default=False,
        description="跳过提示词增强，直接使用传入的 prompt",
    )

    # 草图渲染模式标志
    is_sketch_render: bool = Field(
        default=False,
        description="是否为草图渲染模式（用于调整 negative_prompt，移除 sketch 相关词以避免语义冲突）",
    )


class ImageGenResult(BaseModel):
    """图像生成结果。"""

    success: bool
    image_path: Optional[str] = None
    image_base64: Optional[str] = None
    error: Optional[str] = None
    generation_time: float = 0.0


class MockImageGenerator:
    """模拟图像生成器（用于测试）。

    不调用真实 API，生成占位图像。
    """

    def __init__(self):
        self.default_width = 1024
        self.default_height = 1024

    async def generate(
        self,
        prompt: str,
        output_path: Optional[str] = None,
        **kwargs,
    ) -> ImageGenResult:
        """生成模拟图像。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return ImageGenResult(
                success=False,
                error="Pillow not installed",
            )

        # 创建占位图像
        width = kwargs.get("width", self.default_width)
        height = kwargs.get("height", self.default_height)

        img = Image.new("RGB", (width, height), color=(50, 50, 80))
        draw = ImageDraw.Draw(img)

        # 添加文字
        text = f"Mock Image\n{prompt[:50]}..."
        try:
            font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 24)
        except Exception:
            font = ImageFont.load_default()

        draw.multiline_text(
            (width // 4, height // 3),
            text,
            fill=(200, 200, 200),
            font=font,
        )

        # 保存
        if output_path:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path)

        return ImageGenResult(
            success=True,
            image_path=output_path,
        )

    async def generate_with_request(
        self,
        request: ImageGenerationRequest,
        output_path: str,
    ) -> ImageGenResult:
        """使用新版请求格式生成模拟图像。"""
        return await self.generate(
            prompt=f"[{len(request.reference_images)} refs] {request.prompt}",
            output_path=output_path,
            width=request.width,
            height=request.height,
        )

    async def generate_character_reference(
        self,
        character_name: str,
        appearance_prompt: str,
        output_dir: str,
        count: int = 3,
        style: str = None,
        project_dir: str = "",
    ) -> list[str]:
        """生成角色参考图。"""
        os.makedirs(output_dir, exist_ok=True)
        paths = []

        for i in range(count):
            path = os.path.join(output_dir, f"reference_{i + 1:02d}.png")
            result = await self.generate(
                prompt=f"{character_name} - {appearance_prompt}",
                output_path=path,
            )
            if result.success:
                paths.append(path)

        return paths


class EngineImageGenerator:
    """Plain prompt → image through the configured image engine."""

    def __init__(self, selection: str = DEFAULT_IMAGE_SELECTION):
        self.selection = selection

    async def generate(
        self,
        prompt: str,
        output_path: Optional[str] = None,
        **kwargs,
    ) -> ImageGenResult:
        from novelvideo.engines.image import generate_image

        width, height = kwargs.get("width"), kwargs.get("height")
        image_bytes, _, error = await generate_image(
            self.selection,
            prompt,
            aspect_ratio=f"{width}:{height}" if width and height else "1:1",
            output_path=output_path,
        )
        if not image_bytes:
            return ImageGenResult(success=False, error=error)
        return ImageGenResult(success=True, image_path=output_path)


def create_image_generator(use_mock: bool = False):
    """Mock generator for tests, otherwise the configured image engine."""
    return MockImageGenerator() if use_mock else EngineImageGenerator()


async def generate_character_reference_unified(
    character_name: str,
    appearance_prompt: str,
    output_dir: str,
    character_tag: str = "",
    count: int = 3,
    use_mock: bool = False,
    style: str = None,
    ethnicity: str = "Chinese",
    prompt_only: bool = False,  # Dry Run 模式：只生成提示词，不调用 API
    model: str = None,  # image selection (legacy names map to Higgsfield)
    project_dir: str = "",  # 项目根目录，用于定位 prompts 目录
    state_dir: str = "",  # 项目状态目录，用于读取 scoped project config
    usage_task_type: str = "character_portrait",
    usage_scope: str = "",
    identity_name: str = "",
    raise_on_error: bool = False,
) -> list[str]:
    """统一的角色参考图生成接口：返回生成的图片路径列表。"""
    if use_mock:
        return await MockImageGenerator().generate_character_reference(
            character_name=character_name,
            appearance_prompt=appearance_prompt,
            output_dir=output_dir,
            count=count,
            project_dir=project_dir,
        )

    model = normalize_character_image_selection(model or get_character_image_selection())
    from novelvideo.generators.nanobanana_character import NanoBananaCharacterGenerator

    generator = NanoBananaCharacterGenerator(selection=model)
    result = await generator.generate_character_portrait(
        character_name=character_name,
        character_prompt=appearance_prompt,
        character_tag=character_tag,
        output_dir=output_dir,
        style=style,
        ethnicity=ethnicity,
        prompt_only=prompt_only,
        project_dir=project_dir,
        state_dir=state_dir,
        usage_task_type=usage_task_type,
        usage_scope=usage_scope,
        identity_name=identity_name,
    )
    if result.success:
        return result.reference_paths
    print(f"[Character] {model} 生成失败: {result.error}")
    if is_insufficient_credits_error(message=result.error or ""):
        raise RuntimeError("INSUFFICIENT_CREDITS")
    if raise_on_error:
        raise RuntimeError(result.error or f"{model} 生成失败")
    return []


async def generate_identity_image_unified(
    character_name: str,
    identity_prompt: str,
    reference_image_path: str,
    output_path: str,
    character_tag: str = "",
    ethnicity: str = "Chinese",
    style: str = None,
    dry_run: bool = False,
    model: str = None,  # image selection (legacy names map to Higgsfield)
    project_dir: str = "",  # 项目根目录，用于定位 prompts 目录
    state_dir: str = "",  # 项目状态目录，用于读取 scoped project config
    costume_image_path: str = "",  # 服装参考图路径
    usage_task_type: str = "identity_image",
    usage_scope: str = "",
    identity_name: str = "",
    raise_on_error: bool = False,
):
    """基于角色基准图生成身份参考图（Identity Locking）。

    Returns `{"success", "prompt", "prompt_file"}` on dry runs, else a bool.
    """
    model = normalize_character_image_selection(model or get_character_image_selection())
    from novelvideo.generators.nanobanana_character import NanoBananaCharacterGenerator

    generator = NanoBananaCharacterGenerator(selection=model)
    result = await generator.generate_identity_with_reference(
        character_name=character_name,
        identity_prompt=identity_prompt,
        reference_image_path=reference_image_path,
        output_path=output_path,
        character_tag=character_tag,
        ethnicity=ethnicity,
        style=style,
        dry_run=dry_run,
        project_dir=project_dir,
        state_dir=state_dir,
        costume_image_path=costume_image_path,
        usage_task_type=usage_task_type,
        usage_scope=usage_scope,
        identity_name=identity_name,
    )
    if dry_run:
        return {
            "success": result.success,
            "prompt": result.prompt,
            "prompt_file": result.prompt_file,
        }
    if not result.success:
        if is_insufficient_credits_error(message=result.error or ""):
            raise RuntimeError("INSUFFICIENT_CREDITS")
        if raise_on_error:
            raise RuntimeError(result.error or f"{model} 身份图生成失败")
    return result.success
