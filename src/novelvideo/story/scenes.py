"""Scene catalogue extraction from a screenplay's own text.

Scene headings name every location, so the catalogue is built from the source
text: a deterministic parser locates blocks, a model normalizes them, and a
model writes each scene's 360 environment contract. Results are cached per
scene in the project's SQLite analysis cache so an interrupted build resumes.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, List, Optional, Protocol

from pydantic import BaseModel, Field

from novelvideo.i18n_message import MessageLike, lmsg
from novelvideo.models import NovelScene
from novelvideo.story.screenplay_normalizer import (
    NormalizedSceneBlock,
    clean_scene_name_and_time,
    normalize_screenplay_scenes,
    normalize_time_of_day,
)
from novelvideo.utils.bounded_concurrency import default_llm_concurrency, map_bounded
from novelvideo.utils.source_language import (
    AssetLanguage,
    asset_language_instruction,
    detect_asset_language,
)

def _clean_aliases(primary_name: str, aliases: List[str]) -> List[str]:
    """规范化 alias：strip、去重、去掉与主名等价的项。"""
    primary = (primary_name or "").strip()
    cleaned: List[str] = []
    seen: set[str] = set()
    for alias in aliases or []:
        normalized = (alias or "").strip()
        if not normalized or normalized == primary or normalized in seen:
            continue
        cleaned.append(normalized)
        seen.add(normalized)
    return cleaned



# ============================================================
# 场景提取 Pipeline
# ============================================================


class SceneEnrichment(BaseModel):
    """LLM 补充的场景信息。"""

    name: str = Field(..., description="场景主名称")
    aliases: List[str] = Field(default_factory=list, description="别名列表")
    scene_type: str = Field(default="interior", description="interior/exterior/nature")
    environment_prompt: str = Field(
        default="",
        description="场景空间视觉描述（按方位描述空间布局、光源方向、建筑风格、材质纹理，150-200字，不含人物）",
    )
    description: str = Field(default="", description="场景叙述性描述")


class SceneEnrichmentList(BaseModel):
    """场景补充信息列表。"""

    scenes: List[SceneEnrichment]

SCENE_ENVIRONMENT_REQUIRED_HEADINGS = ("正面", "左侧", "右侧", "背面")


SCENE_ENRICHMENT_SYSTEM_PROMPT = """你是场景环境设计专家。
根据提供的场景名称和剧本原文，生成该场景的视觉环境描述。

生成：
1. name: 直接使用提供的场景名称（原样返回）
2. aliases: 空列表
3. scene_type: 根据场景判断 interior/exterior/nature
4. environment_prompt: 必须输出“完整 360 空间合同”，使用以下固定标题，不得省略、改名或合并：
   正面：
   左侧：
   右侧：
   背面：
   光源：
   材质/风格：
   禁止元素：

environment_prompt 规则：
- 正面/左侧/右侧/背面必须分别说明该方向的固定空间、墙体/边界、门窗/入口、固定陈设或外部延展。
- 正面是 master 图要看的主方向；背面是 reverse 图要看的方向；左右侧是两者边缘需要连续拼接的空间。
- 如果剧本没有明确某一方向，必须基于场景类型和原文证据合理补全，不能留空，不能写“未提及”。
- 描述中性默认状态；不要把临时剧情动作、天气、人物情绪当成固定环境。
- 不含人物，不含临时剧情道具，不含镜头调度。
- 总长度约 220-320 字，可超过 200 字以保证四向完整。
5. description: 场景叙述性描述（使用本次指定的输出语言，简短，不超过约 50 字）"""


# Every heading the contract may carry, in the order they must appear.
SCENE_ENVIRONMENT_ALL_HEADINGS = (
    "正面", "左侧", "右侧", "背面", "光源", "材质/风格", "禁止元素",
)

# A heading may open the text or follow a newline or a sentence break. Models
# routinely emit the whole contract on one line, and rejecting that discards a
# perfectly good description in favour of boilerplate.
_SCENE_HEADING_RE = re.compile(
    r"(?:^|[\n。；;])\s*(" + "|".join(re.escape(h) for h in SCENE_ENVIRONMENT_ALL_HEADINGS) + r")\s*[:：]"
)

# The first line of the generated fallback. Distinctive enough to recognise a
# prompt the system wrote when it could not use the model's.
SCENE_FALLBACK_FINGERPRINT = "最能代表地点身份的主入口、主墙面、主装置或主要活动面作为正面"
SCENE_FALLBACK_FINGERPRINT_EN = "Use the main entrance, main wall, main fixture, or primary activity area"
SCENE_FALLBACK_FINGERPRINTS = (
    SCENE_FALLBACK_FINGERPRINT,
    SCENE_FALLBACK_FINGERPRINT_EN,
)


def parse_scene_environment_sections(prompt: str) -> list[tuple[str, str]]:
    """Split a 360 contract into (heading, body) pairs, wherever they sit.

    Returns an empty list when the text is not a contract, so callers can tell
    "no sections" from "sections with empty bodies".
    """
    text = str(prompt or "").strip()
    if not text:
        return []

    matches = list(_SCENE_HEADING_RE.finditer(text))
    if not matches:
        return []

    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip().strip("。；;")
        sections.append((match.group(1), body))
    return sections


def _has_required_scene_environment_headings(prompt: str) -> bool:
    """Whether the text is a usable 360 contract.

    Presence alone is not enough: a body that merely mentions "左侧：" in passing
    would pass a substring test. The four directions must each open a section,
    appear in order, and actually say something.
    """
    sections = parse_scene_environment_sections(prompt)
    if not sections:
        return False

    seen = [heading for heading, body in sections if body]
    position = 0
    for required in SCENE_ENVIRONMENT_REQUIRED_HEADINGS:
        try:
            position = seen.index(required, position) + 1
        except ValueError:
            return False
    return True


def normalize_scene_environment_prompt(prompt: str) -> str:
    """Rewrite a valid contract as one section per line.

    Downstream readers and human reviewers both expect the sectioned form; the
    model's single-line output carries the same content in a shape that is hard
    to read and easy to mis-parse later.
    """
    sections = parse_scene_environment_sections(prompt)
    if not sections:
        return str(prompt or "").strip()
    return "\n".join(f"{heading}：{body}" for heading, body in sections if body)


def should_repair_scene_placeholder(existing_prompt: str, new_prompt: str) -> bool:
    """Whether a stored environment prompt is boilerplate worth replacing.

    Both tracks wrote the generated fallback for every scene while the contract
    validator rejected valid single-line model output, so both need the same
    narrow repair on rebuild. All three conditions matter:

    * the stored prompt carries the fallback fingerprint, so it is something
      this code wrote, never something a user typed or edited;
    * the replacement does not carry it, so a rebuild that fell back again does
      not churn the row;
    * the replacement is itself a valid 360 contract, so a malformed or empty
      model response can never overwrite a stored prompt with something worse.
    """
    existing = str(existing_prompt or "")
    replacement = str(new_prompt or "")
    if not any(fingerprint in existing for fingerprint in SCENE_FALLBACK_FINGERPRINTS):
        return False
    if any(fingerprint in replacement for fingerprint in SCENE_FALLBACK_FINGERPRINTS):
        return False
    return _has_required_scene_environment_headings(replacement)


def _compact_scene_context(
    lines: list[str] | tuple[str, ...] | str, *, limit: int = 180
) -> str:
    if isinstance(lines, str):
        raw = lines
    else:
        raw = " ".join(str(line).strip() for line in lines if str(line).strip())
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:limit].rstrip()


def _ensure_directional_environment_prompt(
    *,
    prompt: str,
    scene_name: str,
    scene_type: str,
    time_of_day: str,
    context_lines: list[str],
    output_language: AssetLanguage = "zh",
) -> str:
    """Ensure graph-built scene prompts are usable as a 360 spatial contract."""
    text = str(prompt or "").strip()
    if _has_required_scene_environment_headings(text):
        return normalize_scene_environment_prompt(text)
    # Evidence comes from the script, never from the prompt that just failed
    # validation. Quoting a rejected description back as "原文证据" produced a
    # self-referential contract that cited itself as its own source.
    evidence = _compact_scene_context(context_lines)
    if not evidence:
        evidence = (
            f"{scene_name}, {scene_type or 'interior'} scene"
            if output_language == "en"
            else f"{scene_name}，{scene_type or 'interior'} 场景"
        )
    type_label = scene_type or "interior"
    if output_language == "en":
        return "\n".join(
            [
                f"正面：Use the main entrance, main wall, main fixture, or primary activity area that best identifies \"{scene_name}\" as the front view; use the source evidence \"{evidence}\" to define fixed structures and visual anchors.",
                f"左侧：From the front-facing view, extend left into side walls, passages, doors, windows, fixed furnishings, or exterior boundaries appropriate to \"{scene_name}\"; keep materials, scale, and depth continuous with the front and include no people.",
                "右侧：From the front-facing view, extend right into the opposing side space, wall corners, corridors, streets, adjoining rooms, or fixed facilities; maintain spatial continuity without duplicating the front subject.",
                "背面：Show the rear half of the location when facing away from the front, such as the reverse side of an entrance, a corridor end, courtyard, rear wall, windows, street continuation, or secondary functional area; close the full 360-degree space consistently.",
                "光源：Use stable, neutral environmental lighting from fixed lamps, windows, skylight, or ceiling fixtures; do not bake a temporary story time or mood into the reusable environment.",
                f"材质/风格：Keep the fixed architectural style, surfaces, door and window structure, furnishing materials, and wear level appropriate to an {type_label} scene; describe only the reusable environment and no character actions.",
                "禁止元素：No people, temporary story props, subtitles, watermarks, UI, or historical, modern, or science-fiction elements that conflict with the location name and source evidence.",
            ]
        )
    return "\n".join(
        [
            f"正面：以“{scene_name}”最能代表地点身份的主入口、主墙面、主装置或主要活动面作为正面；根据原文证据“{evidence}”确定固定结构和主要视觉锚点。",
            f"左侧：从正面视角向左延伸，布置与“{scene_name}”功能一致的侧墙、通道、门窗、固定陈设或外部边界；保持与正面材质、尺度和空间深度连续，不放人物。",
            "右侧：从正面视角向右延伸，布置与左侧相对的侧向空间、墙体转角、走廊/街道/房间延展或固定设施；不要复制正面主体，只做合理连续补全。",
            "背面：背对正面时看到该地点的后半空间，可为入口反向、走廊尽端、外院、后墙、窗面、街道延伸或次要功能区；必须和正面/左右侧构成完整 360 度闭合空间。",
            "光源：使用中性默认状态的稳定环境光；光源方向来自场景固定灯具、窗户、天光或室内顶灯，避免把剧情时间或临时情绪当成唯一照明。",
            f"材质/风格：保持{type_label}场景的固定建筑风格、墙地顶材质、门窗结构、家具/设施质感和旧化程度；只描述可复用环境，不描述人物动作。",
            "禁止元素：不出现人物、临时剧情道具、字幕、水印、UI、现代/古代/科幻等与场景名和原文证据冲突的元素。",
        ]
    )



def _create_scene_build_agent(system_prompt: str, output_type: Any, name: str):
    """Create the scene-build business LLM agent.

    Runs on the configured text engine.
    """
    from pydantic_ai import Agent
    from novelvideo.config import (
        get_newapi_structured_output_model_settings,
        get_newapi_text_pydantic_model,
    )

    return Agent(
        get_newapi_text_pydantic_model(
            "SCENE_BUILD_MODEL",
            "gemini-3-flash-preview",
            capability="text.generate",
        ),
        system_prompt=system_prompt,
        model_settings=get_newapi_structured_output_model_settings(),
        output_type=output_type,
        name=name,
    )


async def enrich_scene_environment_from_context(
    *,
    scene_name: str,
    scene_type: str = "interior",
    time_of_day: str = "",
    interior: bool = True,
    episodes: list[int] | None = None,
    characters: list[str] | None = None,
    context_lines: list[str] | None = None,
    aliases: list[str] | None = None,
    synopsis: str = "",
    enrichment_agent: Any | None = None,
    output_language: AssetLanguage | None = None,
) -> NovelScene:
    """Generate the canonical 360 environment prompt for one scene.

    Used by both project-level scene construction and episode-level scene planning
    so they do not drift into separate prompt contracts.
    """
    scene_name = str(scene_name or "").strip()
    context_lines = [
        str(line) for line in (context_lines or []) if str(line or "").strip()
    ]
    aliases = list(aliases or [])
    characters = list(characters or [])
    episodes = list(episodes or [])
    scene_type = str(
        scene_type or ("interior" if interior else "exterior") or "interior"
    )
    language = output_language or detect_asset_language(
        "\n".join([scene_name, *context_lines, synopsis])
    )

    agent = enrichment_agent or _create_scene_build_agent(
        SCENE_ENRICHMENT_SYSTEM_PROMPT,
        SceneEnrichmentList,
        "Scene Build Enricher",
    )
    context = "\n".join(context_lines[:50])
    synopsis_section = f"\n\n【故事梗概与人物设定】\n{synopsis}" if synopsis else ""
    user_text = f"""{asset_language_instruction(language)}

场景名称：{scene_name}
出现时间线索：{time_of_day or "无"}（只用于理解剧情出现时段，不要把白天、夜晚、黄昏、月光等时段光照烘焙进基础场景）
室内外：{"内" if interior else "外"}
出现集数：{episodes}
出场人物：{", ".join(characters) if characters else "无"}

以下是该场景在剧本中的原文段落：
{context}{synopsis_section}"""

    try:
        result = (await agent.run(user_text)).output
        if result.scenes:
            enriched = result.scenes[0]
            resolved_type = enriched.scene_type or scene_type
            return NovelScene(
                name=scene_name,
                aliases=_clean_aliases(scene_name, aliases),
                scene_type=resolved_type,
                environment_prompt=_ensure_directional_environment_prompt(
                    prompt=enriched.environment_prompt,
                    scene_name=scene_name,
                    scene_type=resolved_type,
                    time_of_day="",
                    context_lines=context_lines,
                    output_language=language,
                ),
                description=enriched.description,
            )
    except Exception as exc:
        import logging

        logging.error(f"LLM 场景描述生成失败 ({scene_name}): {exc}")

    return NovelScene(
        name=scene_name,
        aliases=_clean_aliases(scene_name, aliases),
        scene_type=scene_type,
        environment_prompt=_ensure_directional_environment_prompt(
            prompt="",
            scene_name=scene_name,
            scene_type=scene_type,
            time_of_day="",
            context_lines=context_lines,
            output_language=language,
        ),
    )


# Several scenes fit in one enrichment call. Each description runs 150-200
# characters, so a batch stays well inside a useful response length.
class SceneBuildCache(Protocol):
    """What a scene build needs from a store to be resumable.

    A protocol rather than the store itself: this code sits below the store
    layer, and tests drive it with a dict.  ``artifact_type`` is per call
    because a scene build caches several independent stages and none of them
    may read another's rows.
    """

    async def get(
        self, artifact_type: str, cache_keys: list[str]
    ) -> dict[str, str]: ...

    async def save(self, artifact_type: str, results: dict[str, str]) -> None: ...


class StoreAnalysisItemCache:
    """Adapter binding the protocol to a project's SQLite store.

    Nothing about it is scene-specific: ``artifact_type`` is per call, so the
    character build stores its appearance answers in the same table under its
    own type without either stage seeing the other's rows.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    async def get(self, artifact_type: str, cache_keys: list[str]) -> dict[str, str]:
        return await self._store.get_analysis_item_cache(artifact_type, cache_keys)

    async def save(self, artifact_type: str, results: dict[str, str]) -> None:
        await self._store.save_analysis_item_cache(artifact_type, results)


# The scene build named this adapter first and imports it by that name.
StoreSceneBuildCache = StoreAnalysisItemCache


_ENRICHMENT_BATCH_SIZE = 5

# Bump whenever the enrichment prompt, the contract validator, or the way a
# model answer is turned into a NovelScene changes.  It is part of every cache
# key, so a bump retires every stored result rather than mixing contracts.
SCENE_ENRICHMENT_CACHE_VERSION = 2

SCENE_ENRICHMENT_CACHE_TYPE = "scene_environment"

# Bump alongside the screenplay normalizer or the way its blocks are folded
# into candidates.
SCENE_BLOCKS_CACHE_VERSION = 2

SCENE_BLOCKS_CACHE_TYPE = "scene_blocks"


def scene_enrichment_cache_key(candidate: dict[str, Any], synopsis: str = "") -> str:
    """Hash the exact input one scene's enrichment call is made from.

    Every field the model sees, plus every field used to build the NovelScene
    from its answer, plus the contract version.  Anything left out would let a
    changed input silently reuse a result produced from the old one.

    A scene can be answered by either of two calls — the batch, or the
    per-scene retry it falls through to — and they do not read the same fields.
    The batch sends 24 context lines and no time or episode list; the per-scene
    call sends 50 lines plus both. The key is the *union*, because the result
    is stored under one key whichever call produced it, so a field either call
    reads has to be able to invalidate it. Keying on the batch's inputs alone
    meant a changed time of day, episode list, or context line 25 onwards left
    the key identical and replayed a result built from the old input.

    The cost of the union is an occasional needless rebuild. The cost of the
    intersection is serving a wrong answer, which does not announce itself.
    """
    payload = {
        "v": SCENE_ENRICHMENT_CACHE_VERSION,
        "name": str(candidate.get("name") or ""),
        "aliases": list(candidate.get("aliases") or []),
        "scene_type": str(candidate.get("scene_type") or ""),
        "interior": bool(candidate.get("interior", True)),
        "characters": list(candidate.get("characters") or []),
        "time_of_day": str(candidate.get("time_of_day") or ""),
        "episodes": list(candidate.get("episodes") or []),
        # 50, the larger of the two truncations, for the same reason.
        "context": [
            str(line) for line in (candidate.get("context_lines") or [])[:50]
        ],
        "synopsis": str(synopsis or ""),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scene_to_cache_payload(scene: NovelScene) -> str:
    return json.dumps(
        {
            "name": scene.name,
            "aliases": list(scene.aliases or []),
            "scene_type": scene.scene_type,
            "environment_prompt": scene.environment_prompt,
            "description": scene.description,
        },
        ensure_ascii=False,
    )


def is_cacheable_scene_prompt(prompt: str) -> bool:
    """Whether a produced prompt is a real answer worth keeping.

    Two conditions, and the second is the one that is easy to miss: the
    generated fallback satisfies the 360 contract by construction, so a
    validity check alone would freeze boilerplate in as the permanent answer
    and every later rebuild would replay it instead of retrying the model.
    """
    text = str(prompt or "")
    if any(fingerprint in text for fingerprint in SCENE_FALLBACK_FINGERPRINTS):
        return False
    return _has_required_scene_environment_headings(text)


def scene_from_cache_payload(payload: str) -> NovelScene | None:
    """Rebuild a scene from a stored payload, or None if it is unusable.

    A stored row that no longer parses, or that carries a prompt the current
    contract rejects, is treated as a miss rather than trusted: a cache must
    never be able to publish something the live path would have rejected.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    prompt = str(data.get("environment_prompt") or "")
    if not is_cacheable_scene_prompt(prompt):
        return None
    return NovelScene(
        name=str(data.get("name") or ""),
        aliases=list(data.get("aliases") or []),
        scene_type=str(data.get("scene_type") or ""),
        environment_prompt=prompt,
        description=str(data.get("description") or ""),
    )


async def enrich_scene_environments_batched(
    candidates: list[dict[str, Any]],
    *,
    synopsis: str = "",
    enrichment_agent: Any | None = None,
    on_scene: Optional[Any] = None,
    cache: Optional[SceneBuildCache] = None,
    output_language: AssetLanguage | None = None,
) -> list[NovelScene]:
    """Generate environment prompts for several scenes per request.

    The enrichment agent already returns a list, but was being asked for one
    scene at a time. A screenplay yields dozens of base scenes, and each call
    costs the same few seconds of round trip regardless of how many it carries.

    A batch that fails or comes back short falls through to per-scene calls for
    exactly the scenes it missed, so batching can only cost time, never scenes.

    With a ``cache``, scenes whose input already produced a real answer are
    served from it and never sent to the model.  That is what makes an
    interrupted build resumable: each scene is an independent call, and the ones
    that finished stay finished.
    """
    if not candidates:
        return []
    language = output_language or detect_asset_language(
        "\n".join(
            [
                synopsis,
                *(
                    str(value)
                    for candidate in candidates
                    for value in [
                        candidate.get("name") or "",
                        *(candidate.get("context_lines") or []),
                    ]
                ),
            ]
        )
    )

    keys = {
        id(candidate): scene_enrichment_cache_key(candidate, synopsis)
        for candidate in candidates
    }
    hits: dict[int, NovelScene] = {}
    if cache is not None:
        stored = await cache.get(SCENE_ENRICHMENT_CACHE_TYPE, list(keys.values()))
        for candidate in candidates:
            payload = stored.get(keys[id(candidate)])
            scene = scene_from_cache_payload(payload) if payload else None
            if scene is not None:
                hits[id(candidate)] = scene

    pending = [candidate for candidate in candidates if id(candidate) not in hits]
    if hits and on_scene:
        # Reported before any model call, so a resumed build shows the work it
        # is skipping instead of appearing to stall at zero.
        for candidate in candidates:
            scene = hits.get(id(candidate))
            if scene is not None:
                on_scene(candidate, scene)

    agent = enrichment_agent or _create_scene_build_agent(
        SCENE_ENRICHMENT_SYSTEM_PROMPT,
        SceneEnrichmentList,
        "Scene Build Enricher",
    )
    synopsis_section = f"\n\n【故事梗概与人物设定】\n{synopsis}" if synopsis else ""

    def describe(candidate: dict[str, Any]) -> str:
        context = "\n".join(
            str(line) for line in (candidate.get("context_lines") or [])[:24]
        )
        interior = bool(candidate.get("interior", True))
        return (
            f"### 场景：{candidate['name']}\n"
            f"室内外：{'内' if interior else '外'}\n"
            f"出场人物：{', '.join(candidate.get('characters') or []) or '无'}\n"
            f"原文段落：\n{context}"
        )

    async def run_batch(batch: list[dict[str, Any]]) -> list[NovelScene]:
        prompt = (
            asset_language_instruction(language)
            + "\n\n请为下面每一个场景分别生成 environment_prompt，"
            "name 必须与给出的场景名完全一致，不要合并或遗漏：\n\n"
            + "\n\n".join(describe(candidate) for candidate in batch)
            + synopsis_section
        )
        produced: dict[str, NovelScene] = {}
        try:
            result = (await agent.run(prompt)).output
            by_name = {
                str(item.name or "").strip(): item for item in (result.scenes or [])
            }
            for candidate in batch:
                item = by_name.get(candidate["name"])
                if item is None:
                    continue
                scene_type = item.scene_type or candidate.get("scene_type") or (
                    "interior" if candidate.get("interior", True) else "exterior"
                )
                produced[candidate["name"]] = NovelScene(
                    name=candidate["name"],
                    aliases=_clean_aliases(
                        candidate["name"], candidate.get("aliases") or []
                    ),
                    scene_type=scene_type,
                    environment_prompt=_ensure_directional_environment_prompt(
                        prompt=item.environment_prompt,
                        scene_name=candidate["name"],
                        scene_type=scene_type,
                        time_of_day="",
                        context_lines=list(candidate.get("context_lines") or []),
                        output_language=language,
                    ),
                    description=item.description,
                )
        except Exception as exc:  # noqa: BLE001 - falls back per scene below
            import logging

            logging.warning("批量场景描述生成失败，逐个重试: %s", exc)

        answered: list[tuple[dict[str, Any], NovelScene]] = []
        for candidate in batch:
            scene = produced.get(candidate["name"])
            if scene is None:
                scene = await enrich_scene_environment_from_context(
                    scene_name=candidate["name"],
                    aliases=candidate.get("aliases") or [],
                    scene_type=candidate.get("scene_type") or "",
                    time_of_day=candidate.get("time_of_day") or "",
                    interior=bool(candidate.get("interior", True)),
                    episodes=candidate.get("episodes") or [],
                    characters=candidate.get("characters") or [],
                    context_lines=candidate.get("context_lines") or [],
                    synopsis=synopsis,
                    enrichment_agent=agent,
                    output_language=language,
                )
            if not str(scene.environment_prompt or "").strip():
                continue
            if on_scene:
                on_scene(candidate, scene)
            answered.append((candidate, scene))
        if cache is not None and answered:
            # Written per batch, not once at the end: a build killed halfway
            # must keep everything it already paid for.  Only real answers are
            # stored — a generated fallback would otherwise become permanent.
            await cache.save(
                SCENE_ENRICHMENT_CACHE_TYPE,
                {
                    keys[id(candidate)]: scene_to_cache_payload(scene)
                    for candidate, scene in answered
                    if is_cacheable_scene_prompt(scene.environment_prompt)
                },
            )
        return [scene for _, scene in answered]

    batches = [
        pending[start : start + _ENRICHMENT_BATCH_SIZE]
        for start in range(0, len(pending), _ENRICHMENT_BATCH_SIZE)
    ]
    results = await map_bounded(batches, run_batch, limit=default_llm_concurrency())
    fresh = {scene.name: scene for batch in results if batch for scene in batch}

    # Rebuilt in the caller's original order, so a resumed build publishes the
    # same catalogue in the same order as a fresh one.
    ordered: list[NovelScene] = []
    for candidate in candidates:
        scene = hits.get(id(candidate)) or fresh.get(candidate["name"])
        if scene is not None:
            ordered.append(scene)
    return ordered


_DEFAULT_ENRICH_SCENE_ENVIRONMENT_FROM_CONTEXT = enrich_scene_environment_from_context


def _scene_candidates_from_normalized_blocks(
    blocks: list[NormalizedSceneBlock],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for block in blocks:
        scene_name = str(block.location or "").strip()
        if not scene_name:
            continue
        existing = merged.get(scene_name)
        episode = _episode_number_from_normalized_block(block)
        normalized_time = normalize_time_of_day(block.time_of_day)
        if not existing:
            merged[scene_name] = {
                "name": scene_name,
                "aliases": _clean_aliases(scene_name, block.aliases),
                "scene_type": block.scene_type or "interior",
                "time_of_day": normalized_time,
                "time_counts": {normalized_time: 1} if normalized_time else {},
                "interior": block.interior_exterior != "外",
                "episodes": [episode],
                "characters": list(dict.fromkeys(block.characters)),
                "context_lines": list(block.content_lines or block.evidence_lines),
            }
            continue
        if episode not in existing["episodes"]:
            existing["episodes"].append(episode)
        existing["episodes"] = sorted(existing["episodes"])
        existing["aliases"] = _clean_aliases(
            scene_name,
            list(existing["aliases"]) + list(block.aliases),
        )
        existing["characters"] = list(
            dict.fromkeys(list(existing["characters"]) + list(block.characters))
        )
        existing["context_lines"].extend(block.content_lines or block.evidence_lines)
        if normalized_time:
            existing["time_counts"][normalized_time] = (
                existing["time_counts"].get(
                    normalized_time,
                    0,
                )
                + 1
            )
        if not existing["time_of_day"] and normalized_time:
            existing["time_of_day"] = normalized_time
    return list(merged.values())


def _format_observed_times_note(time_counts: dict[str, int] | None) -> str:
    counts = {
        str(key or "").strip(): int(value or 0)
        for key, value in (time_counts or {}).items()
        if str(key or "").strip() and int(value or 0) > 0
    }
    if not counts:
        return ""
    parts = [f"{time}×{counts[time]}" for time in sorted(counts)]
    return "observed_times: " + " / ".join(parts)


def _append_scene_note(existing: str, note: str) -> str:
    existing = str(existing or "").strip()
    note = str(note or "").strip()
    if not note:
        return existing
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}\n{note}"


def _episode_number_from_normalized_block(block: NormalizedSceneBlock) -> int:
    try:
        episode = int(block.episode_number or 0)
    except (TypeError, ValueError):
        episode = 0
    if episode > 0:
        return episode

    raw_header = str(block.raw_header or "").strip()
    header_match = re.match(r"^\s*(?P<episode>\d+)\s*[-－—]", raw_header)
    if header_match:
        return int(header_match.group("episode"))
    return 1


async def _normalized_scene_blocks_cached(
    novel_text: str, cache: Optional[SceneBuildCache]
) -> list[dict[str, Any]]:
    """Run the screenplay normalizer once per source text, then reuse it.

    The normalizer is a model call and its answer varies between runs. Left
    uncached it does not just cost its own few seconds again — it reshuffles the
    candidates, which changes every downstream cache key, so a rebuild would
    re-pay for every scene description too. Keyed on the source text, so an
    unchanged script always yields the same candidates.
    """
    key = hashlib.sha256(
        json.dumps(
            {"v": SCENE_BLOCKS_CACHE_VERSION, "source": novel_text},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    if cache is not None:
        stored = (await cache.get(SCENE_BLOCKS_CACHE_TYPE, [key])).get(key)
        if stored:
            try:
                restored = json.loads(stored)
            except (TypeError, ValueError):
                restored = None
            if isinstance(restored, list) and restored:
                return restored

    blocks = await normalize_screenplay_scenes(novel_text)
    candidates = _scene_candidates_from_normalized_blocks(blocks)
    if cache is not None and candidates:
        await cache.save(
            SCENE_BLOCKS_CACHE_TYPE,
            {key: json.dumps(candidates, ensure_ascii=False)},
        )
    return candidates


def strip_scene_heading_suffix(name: str) -> str:
    """Drop the heading markers the parser left on a location name.

    "郑玉琴办公室内" is the same room as "郑玉琴办公室": the 内 is the heading's
    interior marker, which the parser also returns as its own field. Removing it
    is the whole of what a per-block model call used to buy here.

    The rules already exist — ``clean_scene_name_and_time`` handles the attached,
    separated and spaced forms and refuses to strip when the result would not be
    a name any more — so this only discards the time it also returns.
    """
    cleaned, _time_of_day = clean_scene_name_and_time(str(name or "").strip(), "")
    return cleaned or str(name or "").strip()


def _candidate_from_parsed_block(candidate: Any) -> dict[str, Any]:
    """Turn one parser-located block into a candidate, without a model call.

    A standard scene heading already states the location, the time and whether
    it is interior — the parser reads all three. Asking a model to restate them
    was one round trip per block, run one at a time, and on a feature-length
    screenplay the single largest cost in the whole build. Measured against the
    model path on a real 33k-char script it produces the same catalogue: 25
    scenes either way, 24 of them identical, and the one difference is which
    spelling of a merged pair wins.

    Deciding that two spellings are one room is the part worth a model, and
    ``adjudicate_scenes`` already does it downstream — with every candidate in
    view at once and occurrence counts to choose the canonical spelling, which
    a per-block call looking at one heading in isolation cannot have.
    """
    name = strip_scene_heading_suffix(candidate.name)
    normalized_time = normalize_time_of_day(candidate.time_of_day)
    return {
        "name": name,
        "aliases": _clean_aliases(name, [candidate.name]),
        "scene_type": "interior" if candidate.interior else "exterior",
        "time_of_day": normalized_time,
        "time_counts": {normalized_time: 1} if normalized_time else {},
        "interior": candidate.interior,
        "episodes": list(candidate.episodes),
        "characters": list(candidate.characters),
        "context_lines": list(candidate.context_lines),
    }


def _scene_recall_is_covered(
    normalized: list[dict[str, Any]], parsed: list[Any]
) -> tuple[bool, str]:
    """Whether the normalizer accounted for every location the parser found.

    The old test compared distinct-location *counts* and rejected the result
    whenever the normalizer returned fewer. But folding two spellings of one
    room into a single scene is the normalizer working correctly, and it always
    lowers the count — so a correct merge was punished as lost recall, and the
    whole result was thrown away for a per-block model loop that cost minutes.

    What actually matters is that nothing was dropped: every location the parser
    located must still be reachable, as a name, an alias, or the same name with
    its heading markers stripped.

    The test stays strict — an attached "内" is genuinely ambiguous ("商务车内"
    is a place, "郑玉琴办公室内" is a marker) so a merge across one is not
    recognised here and does send the build down the fallback. That is now
    affordable: the fallback is deterministic and costs no model call, and
    adjudication performs the same merge downstream with occurrence counts to
    decide it on.
    """
    known: set[str] = set()
    for candidate in normalized:
        for value in [candidate.get("name"), *(candidate.get("aliases") or [])]:
            text = str(value or "").strip()
            if text:
                known.add(text)
                known.add(strip_scene_heading_suffix(text))

    missing = [
        candidate.name
        for candidate in parsed
        if candidate.name not in known
        and strip_scene_heading_suffix(candidate.name) not in known
    ]
    if missing:
        return False, f"AI normalizer 漏掉了 {len(missing)} 个地点: {missing[:5]}"
    return True, ""


async def extract_scenes_from_script(
    novel_text: str,
    on_progress: Optional[Any] = None,
    on_log: Optional[Any] = None,
    cache: Optional[SceneBuildCache] = None,
) -> List[NovelScene]:
    """从格式化剧本提取场景（AI normalizer first + parser fallback + LLM enrichment）。

    流程：
    1. 程序 parser 高召回定位场景块，作为 AI normalizer 的召回 sanity check
    2. 优先使用 AI normalizer 输出的 NormalizedSceneBlock
    3. AI 抛错、返回空或基础场景数少于 parser 结果时，回退到 parser + LLM 规范化
    4. LLM 逐场景生成 environment_prompt
    """
    from .script_parser import parse_scenes, extract_synopsis

    def report(progress: float, task: MessageLike):
        if on_progress:
            on_progress(progress, task)

    def log(message: MessageLike):
        print(f"[extract_scenes] {message}")

    synopsis = extract_synopsis(novel_text)
    output_language = detect_asset_language(novel_text)
    if synopsis:
        log(f"提取梗概+人物设定: {len(synopsis)} 字符")

    legacy_candidates = parse_scenes(novel_text)
    log(
        lmsg(
            "tasks.log.pipeline.legacyRecall",
            "程序召回 sanity check 得到 "
            f"{len(legacy_candidates)} 个场景块: {[c.name for c in legacy_candidates]}",
            sceneCount=len(legacy_candidates),
            sceneNames=str([c.name for c in legacy_candidates]),
        )
    )

    normalized_scene_candidates: list[dict[str, Any]] = []
    fallback_reason = ""

    report(
        0.1,
        lmsg("tasks.progress.pipeline.normalizingScenes", "AI 规范化剧本场景块..."),
    )
    try:
        normalized_scene_candidates = await _normalized_scene_blocks_cached(
            novel_text, cache
        )
        covered, gap = _scene_recall_is_covered(
            normalized_scene_candidates, legacy_candidates
        )
        if not covered:
            fallback_reason = gap
            normalized_scene_candidates = []
        if normalized_scene_candidates:
            log(
                "AI 规范化得到 "
                f"{len(normalized_scene_candidates)} 个基础场景: "
                f"{[c['name'] for c in normalized_scene_candidates]}"
            )
        elif not fallback_reason:
            fallback_reason = "AI normalizer 返回空"
    except Exception as e:
        import logging

        logging.error(f"AI 场景规范化失败: {e}")
        fallback_reason = f"AI normalizer 失败 ({e})"

    if not normalized_scene_candidates:
        if fallback_reason:
            log(f"⚠️ {fallback_reason}，回退到程序定位 + 本地规范化")

        report(0.15, "定位剧本场景块...")
        candidates = legacy_candidates
        log(f"程序定位得到 {len(candidates)} 个场景块: {[c.name for c in candidates]}")

        if not candidates:
            log("⚠️ 未从剧本中解析出任何场景")
            return []

        # Deterministic, and no model call: a standard heading already states
        # everything a candidate needs, and the name cleanup a model used to do
        # here is a suffix strip. What is left — deciding that two spellings are
        # one room — belongs to adjudication, which sees all of them at once.
        normalized_candidates = [
            _candidate_from_parsed_block(candidate) for candidate in candidates
        ]

        merged_candidates: dict[str, dict[str, Any]] = {}
        for cand in normalized_candidates:
            scene_name = (cand["name"] or "").strip()
            if not scene_name:
                continue
            existing = merged_candidates.get(scene_name)
            if not existing:
                merged_candidates[scene_name] = {
                    "name": scene_name,
                    "aliases": list(dict.fromkeys(cand["aliases"])),
                    "scene_type": cand["scene_type"],
                    "time_of_day": cand["time_of_day"],
                    "time_counts": dict(cand.get("time_counts") or {}),
                    "interior": cand["interior"],
                    "episodes": list(cand["episodes"]),
                    "characters": list(dict.fromkeys(cand["characters"])),
                    "context_lines": list(cand["context_lines"]),
                }
                continue
            existing["aliases"] = list(
                dict.fromkeys(existing["aliases"] + cand["aliases"])
            )
            existing["episodes"] = sorted(set(existing["episodes"] + cand["episodes"]))
            existing["characters"] = list(
                dict.fromkeys(existing["characters"] + cand["characters"])
            )
            existing["context_lines"].extend(cand["context_lines"])
            for time_key, count in (cand.get("time_counts") or {}).items():
                time_key = str(time_key or "").strip()
                if not time_key:
                    continue
                existing["time_counts"][time_key] = existing["time_counts"].get(
                    time_key,
                    0,
                ) + int(count or 0)
            if not existing["time_of_day"] and cand["time_of_day"]:
                existing["time_of_day"] = cand["time_of_day"]

        normalized_scene_candidates = list(merged_candidates.values())
        log(
            "规范化后得到 "
            f"{len(normalized_scene_candidates)} 个基础场景: "
            f"{[c['name'] for c in normalized_scene_candidates]}"
        )

    # Step 3: LLM 逐场景生成 environment_prompt
    report(0.5, "LLM 生成场景环境描述...")
    scenes: List[NovelScene] = []
    total = len(normalized_scene_candidates)
    if not normalized_scene_candidates:
        log("⚠️ 场景规范化后为空")
        return []

    enrichment_agent = None
    if (
        enrich_scene_environment_from_context
        is _DEFAULT_ENRICH_SCENE_ENVIRONMENT_FROM_CONTEXT
    ):
        enrichment_agent = _create_scene_build_agent(
            SCENE_ENRICHMENT_SYSTEM_PROMPT,
            SceneEnrichmentList,
            "Scene Build Enricher",
        )

    # Each candidate's enrichment is an independent round trip. A feature-length
    # screenplay carries well over a hundred scenes, so running them one at a
    # time makes the build minutes of pure latency.
    completed = 0

    def note_progress(cand: dict, scene) -> None:
        nonlocal completed
        scene.time_of_day = ""
        scene.notes = _append_scene_note(
            scene.notes,
            _format_observed_times_note(cand.get("time_counts")),
        )
        completed += 1
        report(
            0.5 + 0.4 * (completed / max(total, 1)),
            f"生成场景描述 ({completed}/{total}): {cand['name']}",
        )
        log(f"  ✓ {cand['name']}: environment_prompt={len(scene.environment_prompt)}字")

    scenes.extend(
        await enrich_scene_environments_batched(
            normalized_scene_candidates,
            synopsis=synopsis,
            enrichment_agent=enrichment_agent,
            cache=cache,
            on_scene=note_progress,
            output_language=output_language,
        )
    )

    log(f"场景提取完成: {len(scenes)} 个")
    report(1.0, "完成")
    return scenes
