"""Nano Banana Pro 角色参考图生成模块。

使用 Google AI Studio (Gemini) 生成角色参考图，
与网格生成使用同一模型，确保角色视觉一致性。

核心技术：Identity Locking（身份锁定）
- 参考图是"身份锚点"，不是姿势模板
- 保持: 面部结构、五官比例、表情、整体相似度
- 变化: 服装、背景、姿势、角度、光线

参考资料:
- https://github.com/aimikoda/nano-banana-pro-prompts
- https://replicate.com/blog/how-to-prompt-nano-banana-pro
"""

import os
import time
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from novelvideo.config import (
    get_grid_generation_config,
    get_style_preset,
    IMAGE_DEFAULT_STYLE,
)
from novelvideo.engines.image import generate_image
from novelvideo.shared.billing_errors import is_fatal_billing_error
from novelvideo.services.style_service import StyleService
from novelvideo.generators.nanobanana_grid import normalize_image_size


def _default_ethnicity_instruction(ethnicity: str) -> str:
    value = (ethnicity or "").strip()
    if not value:
        return ""
    return (
        f"Default ethnicity for unspecified people: {value}. "
        f"Use this only when the character description and reference images do not specify ethnicity, "
        f"nationality, or regional appearance. If the character description says Western, Persian, Japanese, "
        f"mixed-race, foreign, or any other explicit origin, follow that explicit description."
    )


def create_composite_reference(
    portrait_path: str,
    fullbody_path: str,
    output_path: str,
) -> str:
    """将 Portrait（面部特写）和全身图拼接成复合参考图。

    复合图布局：
    ┌─────────────┬─────────────┐
    │   LEFT      │   RIGHT     │
    │   PANEL     │   PANEL     │
    │             │             │
    │   FACE      │   FULL      │
    │   (Portrait)│   BODY      │
    └─────────────┴─────────────┘

    Args:
        portrait_path: Portrait 面部特写图路径
        fullbody_path: 全身图路径
        output_path: 复合图输出路径

    Returns:
        输出路径
    """
    from PIL import Image

    portrait = Image.open(portrait_path)
    fullbody = Image.open(fullbody_path)

    # 统一高度（取较大者）
    target_height = max(portrait.height, fullbody.height)

    # 调整尺寸保持比例
    p_ratio = portrait.width / portrait.height
    portrait_resized = portrait.resize(
        (int(target_height * p_ratio), target_height), Image.Resampling.LANCZOS
    )

    f_ratio = fullbody.width / fullbody.height
    fullbody_resized = fullbody.resize(
        (int(target_height * f_ratio), target_height), Image.Resampling.LANCZOS
    )

    # 拼接：Portrait 在左，Fullbody 在右
    total_width = portrait_resized.width + fullbody_resized.width
    composite = Image.new("RGB", (total_width, target_height))
    composite.paste(portrait_resized, (0, 0))
    composite.paste(fullbody_resized, (portrait_resized.width, 0))

    # 保存
    composite.save(output_path, "PNG")
    print(f"[NanoBanana Character] 复合图已生成: {output_path} ({total_width}x{target_height})")

    return output_path


class CharacterReferenceResult(BaseModel):
    """角色参考图生成结果。"""

    success: bool
    character_name: str
    reference_paths: List[str] = Field(default_factory=list)
    primary_reference: str = Field(default="")
    error: Optional[str] = None
    generation_time: float = 0.0
    # Dry Run 模式返回的 Prompt 信息
    prompt: Optional[str] = None
    prompt_file: Optional[str] = None


class NanoBananaCharacterGenerator:
    """Nano Banana Pro 角色 portrait 生成器。

    与网格生成器使用同一模型（Gemini），确保角色视觉一致性。
    当前仅负责生成 portrait / identity anchor。
    四视图 reference sheet 走 generate_identity_with_reference()。

    示例:
        >>> generator = NanoBananaCharacterGenerator()
        >>> result = await generator.generate_character_portrait(
        ...     character_name="姜裳宁",
        ...     character_prompt="女性，20岁，黑发盘髻，柳叶眉，杏眼，红唇，古装汉服",
        ...     output_dir="output/characters/jiangsn"
        ... )
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        config: Optional[dict] = None,
        selection: Optional[str] = None,
    ):
        """初始化生成器。

        Args:
            api_key: unused, kept for callers; engines hold their own credentials.
            selection: image selection (`drawthings` / `higgsfield:<ref>`).
        """
        config = config or get_grid_generation_config(selection_override=selection)
        from novelvideo.config import infer_image_generation_selection

        self.selection = config.get("selection") or infer_image_generation_selection(
            config.get("provider"), config.get("model")
        )
        self.provider = self.selection.split(":", 1)[0]
        self.model = self.selection.split(":", 1)[-1]
        print(f"[NanoBanana Character] Image engine: {self.selection}")

    async def generate_character_portrait(
        self,
        character_name: str,
        character_prompt: str,
        character_tag: str = "",
        style: str = None,
        output_dir: str = None,
        ethnicity: str = "Chinese",
        prompt_only: bool = False,  # Dry Run 模式：只生成提示词，不调用 API
        project_dir: str = "",
        state_dir: str = "",
        usage_task_type: str = "character_portrait",
        usage_scope: str = "",
        identity_name: str = "",
    ) -> CharacterReferenceResult:
        """生成角色 portrait / identity anchor。

        Args:
            character_name: 角色名称
            character_prompt: 角色外貌描述 Prompt
            character_tag: 角色唯一短标签（用于 Prompt 中的身份标记）
            style: 风格名称，默认使用全局配置
            output_dir: 输出目录
            ethnicity: 角色种族（默认 "Chinese"），用于确保生成正确的面部特征

        Returns:
            CharacterReferenceResult
        """
        start_time = time.time()

        # 使用全局默认风格
        if style is None:
            style = IMAGE_DEFAULT_STYLE

        # 确保输出目录存在
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        project_output_dir = Path(project_dir).resolve() if project_dir else None

        try:
            # 获取风格预设
            style_preset = get_style_preset(
                style,
                project_dir=project_dir,
                state_dir=state_dir or None,
            )
            style_keywords = style_preset.get("style_instructions", "")
            negative_keywords = style_preset.get("avoid_instructions", "")

            # 生成角色标签（如果未提供）
            if not character_tag:
                # 从角色名生成简短标签
                character_tag = self._generate_character_tag(character_name)

            print(f"[NanoBanana Character] 生成 {character_name} 正面基准图...")

            front_prompt = self._build_character_prompt(
                character_name=character_name,
                character_prompt=character_prompt,
                character_tag=character_tag,
                style_name=style,
                project_dir=project_dir,
                style_keywords=style_keywords,
                negative_keywords=negative_keywords,
                state_dir=state_dir,
                ethnicity=ethnicity,
            )

            # 保存 prompt 到文件（审计用）
            if output_dir:
                if project_dir:
                    prompts_dir = Path(project_dir) / "prompts" / "characters"
                else:
                    prompts_dir = Path(output_dir).parent.parent.parent / "prompts" / "characters"
                prompts_dir.mkdir(parents=True, exist_ok=True)
                prompt_file = prompts_dir / f"{character_name}_portrait.prompt.txt"
                prompt_file.write_text(front_prompt, encoding="utf-8")
                print(f"[NanoBanana Character] Prompt 已保存: {prompt_file}")

            # Prompt-Only 模式：只生成提示词，跳过 API 调用
            if prompt_only:
                print(f"[NanoBanana Character] Prompt-Only 模式，跳过 API 调用")
                return CharacterReferenceResult(
                    success=True,
                    character_name=character_name,
                    reference_paths=[],
                    primary_reference="",
                    generation_time=time.time() - start_time,
                )

            portrait_ref_path = (
                os.path.join(output_dir, "reference_portrait.png") if output_dir else None
            )
            portrait_bytes = await self._generate_single_image(
                prompt=front_prompt,
                output_path=portrait_ref_path,
                image_size="0.5K",
                usage={
                    "project_output_dir": project_output_dir,
                    "task_type": usage_task_type,
                    "scope": usage_scope or f"character:{character_name}:portrait",
                    "character_name": character_name,
                    "identity_name": identity_name or None,
                },
            )

            if portrait_bytes and portrait_ref_path:
                print(f"[NanoBanana Character] Portrait 已生成: {portrait_ref_path}")
            else:
                return CharacterReferenceResult(
                    success=False,
                    character_name=character_name,
                    error="生成正面基准图失败",
                    generation_time=time.time() - start_time,
                )

            generation_time = time.time() - start_time
            print(
                f"[NanoBanana Character] {character_name} portrait 生成完成，耗时 {generation_time:.1f}s"
            )

            return CharacterReferenceResult(
                success=True,
                character_name=character_name,
                reference_paths=[portrait_ref_path] if portrait_ref_path else [],
                primary_reference=portrait_ref_path or "",
                generation_time=generation_time,
            )

        except Exception as e:
            if is_fatal_billing_error(e):
                raise
            return CharacterReferenceResult(
                success=False,
                character_name=character_name,
                error=str(e),
                generation_time=time.time() - start_time,
            )

    async def generate_character_reference(
        self,
        character_name: str,
        character_prompt: str,
        character_tag: str = "",
        style: str = None,
        views: List[str] = None,
        output_dir: str = None,
        face_only: bool = False,
        ethnicity: str = "Chinese",
        prompt_only: bool = False,
        project_dir: str = "",
        state_dir: str = "",
        usage_task_type: str = "character_portrait",
        usage_scope: str = "",
        identity_name: str = "",
    ) -> CharacterReferenceResult:
        """兼容旧接口，内部统一转 portrait-first 机制。"""
        _ = views, face_only
        return await self.generate_character_portrait(
            character_name=character_name,
            character_prompt=character_prompt,
            character_tag=character_tag,
            style=style,
            output_dir=output_dir,
            ethnicity=ethnicity,
            prompt_only=prompt_only,
            project_dir=project_dir,
            state_dir=state_dir,
            usage_task_type=usage_task_type,
            usage_scope=usage_scope,
            identity_name=identity_name,
        )

    async def generate_identity_with_reference(
        self,
        character_name: str,
        identity_prompt: str,
        reference_image_path: str,
        output_path: str,
        character_tag: str = "",
        style: str = None,
        ethnicity: str = "Chinese",
        dry_run: bool = False,
        project_dir: str = "",
        state_dir: str = "",
        costume_image_path: str = "",
        usage_task_type: str = "identity_image",
        usage_scope: str = "",
        identity_name: str = "",
    ) -> CharacterReferenceResult:
        """基于角色基准图生成身份参考图（Identity Locking）。

        使用角色的正面基准图作为身份锚点，保持面部一致性，
        只变换服装、背景等身份特定的外观。
        统一生成 4 面板 reference sheet（全脸特写 + 正面全身 + 45° 三分全身 + 背面全身）。

        Args:
            character_name: 角色名称
            identity_prompt: 身份外貌描述（服装、场景等）
            reference_image_path: 角色正面基准图路径
            output_path: 输出图片路径
            character_tag: 角色唯一短标签
            style: 风格名称
            ethnicity: 角色种族（默认 "Chinese"），用于确保生成正确的面部特征
            dry_run: 是否仅生成 Prompt 不生成图片（默认 False）

        Returns:
            CharacterReferenceResult
        """
        start_time = time.time()

        if style is None:
            style = IMAGE_DEFAULT_STYLE

        project_output_dir = Path(project_dir).resolve() if project_dir else None
        resolved_identity_name = identity_name or (Path(output_path).stem if output_path else "")

        try:
            # 获取风格预设
            style_preset = get_style_preset(
                style,
                project_dir=project_dir,
                state_dir=state_dir or None,
            )
            style_keywords = style_preset.get("style_instructions", "")
            negative_keywords = style_preset.get("avoid_instructions", "")

            if not character_tag:
                character_tag = self._generate_character_tag(character_name)

            print(
                f"[NanoBanana Character] 基于基准图生成 {character_name} 身份图（4面板: 正面+三分+背面）..."
            )

            # 构建 Identity Locked Prompt（4 面板: 全脸 + 正面 + 45° 三分 + 背面）
            has_costume_ref = bool(costume_image_path and os.path.exists(costume_image_path))
            prompt = self._build_identity_locked_prompt(
                character_name=character_name,
                character_prompt=identity_prompt,
                character_tag=character_tag,
                target_view="front",
                style_name=style,
                project_dir=project_dir,
                style_keywords=style_keywords,
                negative_keywords=negative_keywords,
                state_dir=state_dir,
                ethnicity=ethnicity,
                has_costume_reference=has_costume_ref,
            )

            # 保存 prompt 到文件（审计用）
            prompt_file = None
            if output_path:
                if project_dir:
                    prompts_dir = Path(project_dir) / "prompts" / "characters"
                else:
                    # output_path: output/{username}/{project}/assets/characters/{角色名}/identities/{身份名}.png
                    prompts_dir = (
                        Path(output_path).parent.parent.parent.parent.parent
                        / "prompts"
                        / "characters"
                    )
                prompts_dir.mkdir(parents=True, exist_ok=True)
                # 从 output_path 提取身份名
                prompt_file = (
                    prompts_dir / f"{character_name}_identity_{resolved_identity_name}.prompt.txt"
                )
                prompt_file.write_text(prompt, encoding="utf-8")
                print(f"[NanoBanana Character] Identity Prompt 已保存: {prompt_file}")

            # Dry Run 模式：仅生成 Prompt，不生成图片
            if dry_run:
                print(f"[NanoBanana Character] Dry Run 模式，跳过图片生成")
                return CharacterReferenceResult(
                    success=True,
                    character_name=character_name,
                    prompt=prompt,
                    prompt_file=str(prompt_file) if prompt_file else None,
                    generation_time=time.time() - start_time,
                )

            # 参考图：身份基准图在前，服装参考图在后（年龄变体等场景允许为空）
            refs = [
                path
                for path in (reference_image_path, costume_image_path)
                if path and os.path.exists(path)
            ]
            if not refs:
                print(f"[NanoBanana Character] 无参考图，从文字描述独立生成")

            # 统一流程：生成 body 到临时文件 → 拼接 portrait → 删 temp
            aspect_ratio = "16:9"  # 4面板: 全脸+正+三分+背面
            image_size = "1K"
            body_label = "4面板 reference sheet"

            temp_body_path = output_path.replace(".png", "_body_temp.png")
            print(f"[NanoBanana Character] 生成{body_label}到临时文件: {temp_body_path}")

            image_bytes = await self._generate_single_image(
                prompt=prompt,
                refs=refs,
                output_path=temp_body_path,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                usage={
                    "project_output_dir": project_output_dir,
                    "task_type": usage_task_type,
                    "scope": usage_scope
                    or f"character:{character_name}:identity:{resolved_identity_name}",
                    "character_name": character_name,
                    "identity_name": resolved_identity_name,
                },
            )

            if image_bytes:
                # 直接使用 sheet 作为身份参考图（零拼接，天然一致）
                import shutil

                shutil.move(temp_body_path, output_path)
                print(f"[NanoBanana Character] {body_label}已保存: {output_path}")

                generation_time = time.time() - start_time
                print(
                    f"[NanoBanana Character] 复合身份图已生成: {output_path}，耗时 {generation_time:.1f}s"
                )
                return CharacterReferenceResult(
                    success=True,
                    character_name=character_name,
                    reference_paths=[output_path],
                    primary_reference=output_path,
                    generation_time=generation_time,
                )
            else:
                return CharacterReferenceResult(
                    success=False,
                    character_name=character_name,
                    error=f"生成{body_label}失败",
                    generation_time=time.time() - start_time,
                )

        except Exception as e:
            if is_fatal_billing_error(e):
                raise
            return CharacterReferenceResult(
                success=False,
                character_name=character_name,
                error=str(e),
                generation_time=time.time() - start_time,
            )

    async def generate_composite_reference(
        self,
        character_name: str,
        character_prompt: str,
        character_tag: str = "",
        style: str = None,
        output_dir: str = None,
        ethnicity: str = "Chinese",
        project_dir: str = "",
        state_dir: str = "",
    ) -> CharacterReferenceResult:
        """生成 Face+Body 复合参考图（C1 优化）。

        生成一张包含面部特写和全身的并排图像，用于更强的身份锁定。
        这是 Nano Banana Pro 官方推荐的最佳实践。

        参考资料:
        - https://imaginewithrashid.com/how-to-create-consistent-characters-using-gemini-nano-banana-pro/
        - "Start by using Nano Banana Pro to generate a side-by-side image:
           a close-up face on the left and a full-body view on the right"

        Args:
            character_name: 角色名称
            character_prompt: 角色外貌描述 Prompt
            character_tag: 角色唯一短标签
            style: 风格名称，默认使用全局配置
            output_dir: 输出目录
            ethnicity: 角色种族（默认 "Chinese"）

        Returns:
            CharacterReferenceResult，primary_reference 指向复合图
        """
        start_time = time.time()

        if style is None:
            style = IMAGE_DEFAULT_STYLE

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        try:
            # 获取风格预设
            style_preset = get_style_preset(
                style,
                project_dir=project_dir,
                state_dir=state_dir or None,
            )
            style_keywords = style_preset.get("style_instructions", "")
            negative_keywords = style_preset.get("avoid_instructions", "")

            if not character_tag:
                character_tag = self._generate_character_tag(character_name)

            print(f"[NanoBanana Character] 生成 {character_name} 复合参考图 (Face+Body)...")

            # 构建复合参考图 Prompt（核心 C1 优化）
            prompt = f"""Generate a SIDE-BY-SIDE composite reference image for character identity locking.

CHARACTER: {character_tag} ({character_name})

⚠️ CRITICAL LAYOUT REQUIREMENTS ⚠️
Create a 1:2 aspect ratio image divided into TWO EQUAL halves:
LEFT HALF = Face close-up (identity source)
RIGHT HALF = Full body (proportions & costume source)

LEFT HALF (Face Close-up):
- Head and shoulders portrait
- Face fills 60-70% of this panel
- Neutral soft gradient background
- Focus on facial features: eyes, nose, lips, skin texture
- Well-lit face with clear details

RIGHT HALF (Full Body):
- Full-body shot of the SAME PERSON
- Standing pose, facing camera
- Neutral background
- Show complete figure from head to feet
- Same lighting style as left panel

DEFAULT ETHNICITY (FALLBACK ONLY):
{_default_ethnicity_instruction(ethnicity)}

CHARACTER DESCRIPTION:
{character_prompt}

IDENTITY CONSISTENCY (CRITICAL):
Both panels MUST show the EXACT SAME PERSON with IDENTICAL:
- Facial structure (bone structure, face shape, proportions)
- Facial features (eye shape/color, nose, lips)
- Skin tone and texture
- Hair color, style, length, texture
- Body proportions and build
- Clothing style (if described)

VISUAL STYLE:
{style_keywords}

STRICT REQUIREMENTS:
- Single character only (no other people)
- Clean, neutral backgrounds in both panels
- High quality, detailed rendering
- No text, watermarks, or labels
- This is a CHARACTER REFERENCE SHEET for video production

MUST AVOID:
{negative_keywords}
- Do NOT show different people in the two panels
- Do NOT add busy backgrounds
- Do NOT include multiple characters
"""

            # 保存 prompt 到文件（审计用）
            if output_dir:
                if project_dir:
                    prompts_dir = Path(project_dir) / "prompts" / "characters"
                else:
                    prompts_dir = Path(output_dir).parent.parent.parent / "prompts" / "characters"
                prompts_dir.mkdir(parents=True, exist_ok=True)
                prompt_file = prompts_dir / f"{character_name}_composite.prompt.txt"
                prompt_file.write_text(prompt, encoding="utf-8")
                print(f"[NanoBanana Character] Composite Prompt 已保存: {prompt_file}")

            # 生成复合图（使用 16:9 宽幅比例）
            composite_path = (
                os.path.join(output_dir, "reference_composite.png") if output_dir else None
            )

            image_bytes = await self._generate_single_image(
                prompt=prompt,
                output_path=composite_path,
                aspect_ratio="16:9",
                image_size="1K",
            )
            if not image_bytes:
                return CharacterReferenceResult(
                    success=False,
                    character_name=character_name,
                    error="API 未返回图像数据",
                    generation_time=time.time() - start_time,
                )

            generation_time = time.time() - start_time
            print(
                f"[NanoBanana Character] 复合参考图已生成: {composite_path}，耗时 {generation_time:.1f}s"
            )

            return CharacterReferenceResult(
                success=True,
                character_name=character_name,
                reference_paths=[composite_path] if composite_path else [],
                primary_reference=composite_path or "",
                generation_time=generation_time,
            )

        except Exception as e:
            return CharacterReferenceResult(
                success=False,
                character_name=character_name,
                error=str(e),
                generation_time=time.time() - start_time,
            )

    def _generate_character_tag(self, character_name: str) -> str:
        """从角色名生成唯一标签。

        使用完整角色名 + 全名哈希确保唯一性和可读性。

        Args:
            character_name: 角色全名

        Returns:
            标签，如 '[叙述者_a1b2]'、'[叙述者母亲_0305]'
        """
        import hashlib

        # 生成全名哈希后4位作为唯一后缀
        name_hash = hashlib.md5(character_name.encode("utf-8")).hexdigest()[:4]

        # 使用完整角色名，避免长名字被截断
        return f"[{character_name}_{name_hash}]"

    @classmethod
    def _animation_medium_phrase(
        cls,
        style_name: Optional[str],
        project_dir: str = "",
        state_dir: str = "",
    ) -> str:
        _, subtype = StyleService.get_style_branch(
            style_name or IMAGE_DEFAULT_STYLE,
            project_dir=project_dir or None,
            state_dir=state_dir or None,
        )
        if subtype == "3d":
            return "stylized 3D animated character rendering"
        if subtype == "hybrid":
            return "stylized hybrid mixed-media animated character rendering"
        return "stylized 2D animated character rendering"

    def _build_character_prompt(
        self,
        character_name: str,
        character_prompt: str,
        character_tag: str,
        style_name: str,
        project_dir: str,
        style_keywords: str,
        negative_keywords: str,
        state_dir: str = "",
        ethnicity: str = "Chinese",
    ) -> str:
        """构建 portrait 生成 Prompt。

        Args:
            character_name: 角色名称
            character_prompt: 角色外貌描述
            character_tag: 角色唯一标签
            style_keywords: 风格关键词
            negative_keywords: 负面关键词
            ethnicity: 角色种族（默认 "Chinese"）

        Returns:
            完整的生成 Prompt
        """
        family, _ = StyleService.get_style_branch(
            style_name or IMAGE_DEFAULT_STYLE,
            project_dir=project_dir or None,
            state_dir=state_dir or None,
        )
        if family == "animation":
            medium = self._animation_medium_phrase(
                style_name,
                project_dir=project_dir,
                state_dir=state_dir,
            )
            prompt = f"""Generate a face-only animated character identity portrait for production reference.

CHARACTER: {character_tag} ({character_name})

DEFAULT ETHNICITY (FALLBACK ONLY):
{_default_ethnicity_instruction(ethnicity)}

FRAMING & PRESENTATION (CRITICAL):
- PERFECT FRONT-FACING head-and-shoulders portrait, symmetrical composition
- Face fills 60-70% of frame
- Neutral expression, mouth closed
- Plain solid neutral background only
- Minimal visible clothing; keep attention on face, hair silhouette, and head shape
- This is a clean animation identity anchor, not a photographic actor portrait

FACIAL FEATURES TO CAPTURE (from character description):
{character_prompt}

VISUAL STYLE:
{style_keywords}

STRICT REQUIREMENTS:
- Final medium must be {medium}
- Preserve exact face shape, eye shape, nose, mouth, hairline, and silhouette
- Keep shape language and graphic readability consistent
- High-quality production reference image

MUST AVOID:
{negative_keywords}
- Do not turn the face away from camera
- Do not include elaborate costume staging or environment
- Do not add text, labels, watermarks, or signatures
- Do not include multiple characters
- Do not convert this into realistic photography or real-human actor rendering
"""
            return prompt.strip()

        prompt = f"""Generate a face-only character identity reference portrait for identity locking.

CHARACTER: {character_tag} ({character_name})

DEFAULT ETHNICITY (FALLBACK ONLY):
{_default_ethnicity_instruction(ethnicity)}

FRAMING & CAMERA (CRITICAL):
- FRONT-FACING: subject facing camera DIRECTLY, looking STRAIGHT at camera, symmetrical composition
- Head-and-shoulders close-up, face fills 60-70% of the frame
- Neutral expression, mouth closed
- Solid matte gray seamless studio background — NO environment, NO scenery, NO props
- Plain simple dark top (like a basic t-shirt), MINIMAL clothing visible
- Do NOT show elaborate costumes, accessories, or period clothing
- This should follow the project visual style while remaining clear enough for identity locking.

FACIAL FEATURES TO CAPTURE (from character description):
{character_prompt}

VISUAL STYLE:
{style_keywords}

STRICT REQUIREMENTS:
- This is a CHARACTER IDENTITY ANCHOR — face must be perfectly front-facing and clearly visible
- High-quality, 4K resolution
- Maintain strict facial fidelity to the character description
- Keep facial structure, hairstyle, skin tone, and expression readable for downstream identity matching

MUST AVOID:
{negative_keywords}
- Do NOT turn the face to any side — must be perfectly FRONTAL
- Do not include elaborate clothing or costumes
- Do not show full body or lower body
- Do NOT add ANY background: no rooms, no scenery, no furniture, no props — plain solid color only
- Do not add text, watermarks, or signatures
- Do not include multiple characters
- Do NOT create beauty-filter skin, glamor retouching, fashion-magazine polish, doll-like skin, or cosmetic-ad aesthetics
"""
        return prompt.strip()

    @staticmethod
    def _costume_reference_block(has_costume_reference: bool) -> str:
        """Return the costume reference instruction block if applicable."""
        if not has_costume_reference:
            return ""
        return """
COSTUME REFERENCE IMAGE (CRITICAL):
A second reference image is provided showing the target costume/clothing.
- MATCH the clothing, fabric, accessories, colors, and styling from the costume reference image EXACTLY
- The costume reference takes PRIORITY over the text description for visual details
- Combine the FACE from the identity anchor (first reference) with the CLOTHING from the costume reference (second reference)
"""

    def _build_identity_locked_prompt(
        self,
        character_name: str,
        character_prompt: str,
        character_tag: str,
        target_view: str,
        style_name: str,
        project_dir: str,
        style_keywords: str,
        negative_keywords: str,
        state_dir: str = "",
        ethnicity: str = "Chinese",
        has_costume_reference: bool = False,
    ) -> str:
        """构建 4 面板 reference sheet Prompt（全脸特写 + 正面全身 + 45° 三分全身 + 背面全身）。

        使用参考图作为身份锚点，一次性生成包含面部特写、正面全身、三分视角全身和背面全身的 sheet，
        零拼接、天然一致。

        Args:
            character_name: 角色名称
            character_prompt: 角色外貌描述
            character_tag: 角色唯一标签
            target_view: 目标视角
            style_keywords: 风格关键词
            negative_keywords: 负面关键词
            ethnicity: 角色种族（默认 "Chinese"）
            full_body: 是否生成全身像（默认 True）

        Returns:
            4-panel reference sheet Prompt
        """
        family, _ = StyleService.get_style_branch(
            style_name or IMAGE_DEFAULT_STYLE,
            project_dir=project_dir or None,
            state_dir=state_dir or None,
        )
        if family == "animation":
            medium = self._animation_medium_phrase(
                style_name,
                project_dir=project_dir,
                state_dir=state_dir,
            )
            prompt = f"""Animated character turnaround / identity sheet. Neutral presentation setup.
PLAIN SOLID WHITE or LIGHT GRAY background ONLY — no environment, no scenery, no props. {style_keywords}

Using the reference image as IDENTITY ANCHOR for {character_tag} ({character_name}),
create a 4-panel animated character reference sheet arranged LEFT to RIGHT:

- Panel 1 (LEFT): FACE CLOSEUP — head and shoulders, filling the panel
- Panel 2 (CENTER-LEFT): FRONT full body — head to feet, standing pose, facing camera
- Panel 3 (CENTER-RIGHT): THREE-QUARTER VIEW full body — head to feet, body rotated about 45 degrees
- Panel 4 (RIGHT): BACK VIEW full body — head to feet, facing away from camera

IDENTITY LOCKING (CRITICAL):
Preserve the same character identity EXACTLY from the reference image:
- face shape and proportions
- eye shape and spacing
- nose and mouth shape
- hairline, hairstyle, and silhouette
- skin tone and age impression
- Preserve the reference identity exactly; do not change face structure, skin tone, hair identity, or silhouette.

CHARACTER DETAILS (CRITICAL - use this for clothing and appearance):
{character_prompt}
{self._costume_reference_block(has_costume_reference)}

PRESENTATION RULES:
- Final medium must be {medium}
- All 4 panels must keep the same character, same outfit, same hair, same proportions
- Panel 1 must visually match Panel 2's head area
- Panels 2-4 must show a complete figure from head to feet
- Plain neutral production-reference background only

STRICT REQUIREMENTS (MUST AVOID):
{negative_keywords}
- Do not allow facial feature drift from reference
- Do not mix rendering families or switch back to realistic actor rendering
- Do not include multiple characters
- No text, labels, or panel numbers on the image
- Do not add environment scenery, props, or poster composition
"""
            return prompt.strip()

        prompt = f"""Character identity reference sheet. Neutral studio setup.
PLAIN SOLID WHITE or LIGHT GRAY background ONLY — no environment, no scenery, no props. {style_keywords}

Using the reference image as IDENTITY ANCHOR for {character_tag} ({character_name}),
create a 4-panel character reference sheet arranged LEFT to RIGHT:

- Panel 1 (LEFT): FACE CLOSEUP — head and shoulders, filling the panel. This is a zoomed-in crop of Panel 2's head: SAME hairstyle, SAME visible clothing (neckline, collar, shoulders)
- Panel 2 (CENTER-LEFT): FRONT full body — head to feet, standing pose, facing camera
- Panel 3 (CENTER-RIGHT): THREE-QUARTER VIEW full body — head to feet, body rotated approximately 45 degrees from the left, both eyes still visible, standing pose
- Panel 4 (RIGHT): BACK VIEW full body — head to feet, facing away from camera, showing back of head and body

IDENTITY LOCKING (CRITICAL):
Preserve the facial structure, facial proportions, and overall likeness
of {character_tag} EXACTLY as in the reference image, allowing NO alteration,
stylization, or reinterpretation of the face under any circumstance.

MUST PRESERVE (from reference):
- Facial structure and bone structure
- Eye shape, size, spacing, color
- Nose shape and size
- Lip shape and fullness
- Skin tone
- Hair color, style, texture
- Preserve the reference identity exactly; do not change face structure, skin tone, hair identity, or silhouette.

DO NOT PRESERVE FROM REFERENCE:
- Beauty-filter smoothing or retouching
- Plastic / waxy / overly perfect skin treatment
- Any rendering finish that conflicts with the selected project style preset
- The final rendering medium should follow the project style preset, not the reference image

CHARACTER DETAILS (CRITICAL - use this for clothing and appearance):
{character_prompt}
{self._costume_reference_block(has_costume_reference)}
BACKGROUND (CRITICAL — STRICTLY ENFORCED):
- ALL 4 panels MUST have a PLAIN SOLID-COLOR background (white, light gray, or soft neutral gradient)
- Do NOT render ANY environment: no rooms, no furniture, no walls, no floors, no scenery
- This is a production character identity reference sheet, not a fashion catalog, not a glossy poster

FULL BODY FRAMING (Panels 2-4):
- MUST show COMPLETE figure from top of head to bottom of feet including shoes
- Standing in neutral pose on a visible ground line
- Ample space above head and below feet
- Do NOT crop any body part

CONSISTENCY:
- ALL 4 panels = SAME person, SAME outfit, SAME hair
- Panel 1 is a ZOOMED-IN CROP of Panel 2's head area — hairstyle, neckline, collar, and shoulder clothing MUST be identical
- Panel 1 face MUST match Panels 2-3 face exactly
- Panel 4 shows the SAME person from behind — SAME hair, SAME outfit, SAME body proportions
- Only viewing angle changes between Panel 2 (front), Panel 3 (three-quarter), and Panel 4 (back)

STRICT REQUIREMENTS (MUST AVOID):
{negative_keywords}
- Do not allow ANY facial feature drift from reference.
- Do not mix styles or reinterpret the character.
- Do not include multiple characters.
- No text, labels, or panel numbers on the image
- Do NOT create beauty-retouched, glamorized, cosmetic-ad, or fashion-editorial output
- Keep the project style consistent across all 4 panels
"""
        return prompt.strip()

    async def _generate_single_image(
        self,
        prompt: str,
        output_path: Optional[str] = None,
        aspect_ratio: str = "3:4",
        image_size: str = "1K",
        refs: Optional[List[str]] = None,
        usage: Optional[dict] = None,
    ) -> Optional[bytes]:
        """生成单张图像（`refs` 为身份/服装参考图路径）。

        Raises RuntimeError with the engine's reason when no image comes back.
        """
        print(f"[NanoBanana Character] 调用 {self.selection} 生成图像 (refs={len(refs or [])})...")
        image_bytes, _text, error_detail = await generate_image(
            self.selection,
            prompt,
            refs=refs or [],
            aspect_ratio=aspect_ratio,
            image_size=normalize_image_size(image_size),
            output_path=output_path,
            usage=usage,
        )
        if not image_bytes:
            print(f"[NanoBanana Character] 生成失败: {error_detail}")
            raise RuntimeError(error_detail or "图像引擎未返回图像数据")
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(image_bytes)
        return image_bytes


def create_character_generator(api_key: Optional[str] = None) -> NanoBananaCharacterGenerator:
    """创建角色生成器。

    Args:
        api_key: Google AI API Key

    Returns:
        NanoBananaCharacterGenerator 实例
    """
    return NanoBananaCharacterGenerator(api_key=api_key)
