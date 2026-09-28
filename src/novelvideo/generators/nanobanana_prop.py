"""道具三视图参考图生成器。

使用 Google AI Studio (Gemini) 生成道具三视图参考图：
正面 (FRONT) / 侧面 (SIDE) / 背面 (BACK)

核心概念：道具独立建模
- 产品摄影风格，白色/浅灰无缝背景
- 展示道具细节和材质纹理
- 下游分镜中出现道具时作为 reference，保持道具外观一致

参考资料:
- https://www.51cto.com/article/837277.html（纳米漫剧流水线 - 道具建模）
"""

import time
from typing import Optional

from novelvideo.config import (
    get_grid_generation_config,
    get_style_preset,
    IMAGE_DEFAULT_STYLE,
    normalize_image_generation_selection,
)
from novelvideo.engines.image import generate_image
from novelvideo.shared.billing_errors import is_fatal_billing_error
from novelvideo.generators.nanobanana_grid import normalize_image_size

PROP_REF_ASPECT_RATIO = "16:9"
PROP_REF_IMAGE_SIZE = "0.5K"


def resolve_prop_reference_image_model() -> str:
    """Return the image selection used by prop reference generation."""
    return get_grid_generation_config()["selection"]


def build_prop_reference_prompt(
    visual_prompt: str,
    style_keywords: str = "",
    style: str | None = None,
    project_dir: str = "",
    state_dir: str = "",
) -> str:
    """Build the exact prompt used for prop reference-sheet generation."""
    if style is None:
        style = IMAGE_DEFAULT_STYLE

    style_preset = get_style_preset(
        style,
        project_dir=project_dir or None,
        state_dir=state_dir or None,
    )
    preset_style = style_preset.get("style_instructions", "")
    preset_negative = style_preset.get("avoid_instructions", "")

    all_style = ", ".join(filter(None, [preset_style, style_keywords]))
    all_negative = preset_negative

    return f"""Generate a 3-PANEL product reference sheet for a story prop.

LAYOUT (1x3, 16:9 overall):
- Three equal unlabeled panels arranged left to right
- Left panel: front view
- Middle panel: side profile
- Right panel: back view
- Do not draw panel titles, angle labels, captions, numbers, arrows, or divider text

PROP DESCRIPTION:
{visual_prompt}

PRODUCT PHOTOGRAPHY STYLE:
- Clean white or light gray seamless background
- Soft studio lighting, no harsh shadows
- Object centered, filling approximately 70% of each panel
- High detail rendering of materials, textures, and surface finishes
- Professional product shot quality

FRONT VIEW: Straight-on frontal view of the prop, showing its face/main side
SIDE PROFILE: 90-degree side view showing the prop's profile and thickness
BACK VIEW: Straight-on rear view of the same prop, showing rear-side details, straps, seams, closures, ports, or worn backside surfaces

VISUAL STYLE:
{all_style}

STRICT REQUIREMENTS:
- NO people, hands, fingers, or living creatures
- Object only, isolated on clean background
- Each panel must be distinguishable by object angle only, never by written labels
- Consistent lighting, scale, silhouette, and material identity across all three panels
- Show fine details: gems, stitching, weathering, non-text surface marks, etc.
- No readable writing anywhere, even if the description mentions a cover title, sign, label, document text, engraving, or lettering
- If text-like markings are necessary for the prop design, render them as abstract unreadable strokes or blank surface texture

MUST AVOID:
{all_negative}
- Do NOT add text, labels, panel titles, captions, numbers, arrows, logos, watermarks, signatures, readable letters, Chinese characters, or English words
- Do NOT include any people, hands, or body parts
- Do NOT show the prop being held or worn
- Do NOT add busy or distracting backgrounds"""


async def generate_prop_reference(
    visual_prompt: str,
    output_path: str,
    style_keywords: str = "",
    style: str = None,
    project_dir: str = "",
    state_dir: str = "",
    model: str | None = None,
) -> Optional[str]:
    """生成道具三视图参考图。

    生成一张 1x3 三面板图像，包含道具的正面、侧面、背面，
    采用产品摄影风格，用于保持道具外观一致性。

    Args:
        visual_prompt: 道具视觉 prompt
        output_path: 输出文件路径
        style_keywords: 额外的风格关键词
        style: 风格名称，默认使用全局配置

    Returns:
        生成的图片路径，失败返回 None
    """
    start_time = time.time()

    if style is None:
        style = IMAGE_DEFAULT_STYLE

    selection = normalize_image_generation_selection(
        model, fallback=resolve_prop_reference_image_model()
    )
    prompt = build_prop_reference_prompt(
        visual_prompt=visual_prompt,
        style_keywords=style_keywords,
        style=style,
        project_dir=project_dir,
        state_dir=state_dir,
    )

    print(f"[PropRefGen] 生成道具三视图: {visual_prompt[:60]}...")
    print(f"[PropRefGen] Image engine: {selection}")

    try:
        image_bytes, _text, error_text = await generate_image(
            selection,
            prompt,
            aspect_ratio=PROP_REF_ASPECT_RATIO,
            image_size=normalize_image_size(PROP_REF_IMAGE_SIZE),
            output_path=output_path,
            usage={"task_type": "prop_reference"},
        )
        elapsed = time.time() - start_time
        if not image_bytes:
            print(f"[PropRefGen] 生成失败: {error_text}，耗时 {elapsed:.1f}s")
            return None
        with open(output_path, "wb") as f:
            f.write(image_bytes)
        print(f"[PropRefGen] 三视图已生成: {output_path}，耗时 {elapsed:.1f}s")
        return output_path

    except Exception as e:
        if is_fatal_billing_error(e):
            raise
        elapsed = time.time() - start_time
        print(f"[PropRefGen] 生成异常: {e}，耗时 {elapsed:.1f}s")
        return None
