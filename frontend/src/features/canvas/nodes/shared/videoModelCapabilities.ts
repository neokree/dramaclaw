// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type {
  VideoGenMode,
  VideoKeyframeSlot,
} from "@/features/canvas/domain/canvasNodes";

/**
 * Freezone 画布视频模型的**能力口径**——CTA / 模式可见性 / 自动推导默认 / 提交校验的
 * 单一事实来源。能力只读媒体目录（`/freezone/video/models`，由已安装引擎生成，见
 * src/novelvideo/media_catalog.py）下发的 `supportedModes` / `referenceAudioMax` 等字段，
 * 不按模型名猜。只拿到一个 id 字符串（目录还没到）时按通用口径：除视频编辑 / 视频延长
 * 之外的模式都放行，交给后端做最终校验。
 */
/** 前端模式与媒体模型目录能力的唯一映射。 */
export const GEN_MODE_TO_CATALOG_MODE: Record<VideoGenMode, string> = {
  textToVideo: "text_to_video",
  firstFrame: "first_frame",
  imageToVideo: "image_to_video",
  firstLastFrame: "first_last_frame",
  imageReference: "image_reference",
  allReference: "all_reference",
  videoEdit: "video_edit",
  videoExtend: "video_extend",
};

/**
 * 这些模式的输出画幅由输入关键帧/源视频决定，不能提交用户保存的固定比例。
 * 只计算本次请求的有效值，不覆盖节点中的比例，切回其它模式时可恢复用户选择。
 */
export function videoModeForcesAutomaticAspectRatio(mode: VideoGenMode): boolean {
  return (
    mode === "firstFrame" ||
    mode === "firstLastFrame" ||
    mode === "videoEdit" ||
    mode === "videoExtend"
  );
}

export interface VideoKeyframeCandidate {
  url: string;
  slot?: VideoKeyframeSlot | null;
  legacyDisplayName?: string | null;
}

/**
 * 从视频节点的上游图片中解析稳定的首帧/尾帧槽位。
 *
 * 新画布以 edge.data.keyframeSlot 为准，节点名称只负责展示，用户重命名不会改变语义。
 * 旧画布没有槽位字段时才兼容“首帧/尾帧”标题，最后按连线顺序补齐未分配图片。
 */
export function resolveVideoKeyframeUrls(
  candidates: readonly VideoKeyframeCandidate[],
): { firstFrameUrl: string | null; lastFrameUrl: string | null } {
  let firstFrameUrl: string | null = null;
  let lastFrameUrl: string | null = null;
  const unassigned: string[] = [];

  for (const candidate of candidates) {
    if (candidate.slot === "first") {
      if (!firstFrameUrl) firstFrameUrl = candidate.url;
      continue;
    }
    if (candidate.slot === "last") {
      if (!lastFrameUrl) lastFrameUrl = candidate.url;
      continue;
    }

    const displayName = String(candidate.legacyDisplayName ?? "").trim();
    // 老数据里的 displayName 是写死的中文槽位名，属于**存量数据格式**不是界面文案。
    if (displayName.includes("首帧") && !firstFrameUrl) { // i18n-exempt
      firstFrameUrl = candidate.url;
    } else if (displayName.includes("尾帧") && !lastFrameUrl) { // i18n-exempt
      lastFrameUrl = candidate.url;
    } else {
      unassigned.push(candidate.url);
    }
  }

  if (!firstFrameUrl) firstFrameUrl = unassigned.shift() ?? null;
  if (!lastFrameUrl) lastFrameUrl = unassigned.shift() ?? null;
  return { firstFrameUrl, lastFrameUrl };
}

/**
 * 模型入参的统一形态：既可以只给一个 id 字符串，也可以给媒体目录下发的模型对象
 * （`supportedModes` 存在时以它为准，那是 Admin 显式配置的能力声明）。
 */
export type VideoModelRef =
  | string
  | {
      id?: string;
      apiModel?: string;
      supportedModes?: string[];
      referenceAudioMax?: number | null;
      supportsGenerateAudio?: boolean;
    }
  | null
  | undefined;

/** 未配置的新字段沿用旧行为：支持原生音频，且默认开启。 */
export function videoModelSupportsGenerateAudio(model: VideoModelRef): boolean {
  return typeof model === "string" || model?.supportsGenerateAudio !== false;
}

export function videoModelDefaultGenerateAudio(model: VideoModelRef): boolean {
  return videoModelSupportsGenerateAudio(model);
}

/** 目录下发了 supportedModes 的模型对象。 */
function catalogModes(model: VideoModelRef): string[] | null {
  return typeof model === "object" && model !== null && (model.supportedModes?.length ?? 0) > 0
    ? (model.supportedModes ?? null)
    : null;
}

/**
 * 指定模型是否支持某 genMode（与可见 tab / 切模型时是否重置残留模式口径一致）。
 * 目录没给 supportedModes 时按通用口径：视频编辑 / 视频延长只有目录显式声明才开放。
 */
export function isVideoModeSupportedByModel(
  mode: VideoGenMode,
  model: VideoModelRef,
): boolean {
  const modes = catalogModes(model);
  if (modes) return modes.includes(GEN_MODE_TO_CATALOG_MODE[mode]);
  return mode !== "videoEdit" && mode !== "videoExtend";
}

/**
 * 空态 CTA 只覆盖「铺素材起步」的图片 / 首尾帧模式——文生视频无需素材、视频编辑走
 * 独立入口，都不在空态 CTA 里。与 `spawnFrameUploads` 接受的模式一一对应。
 */
export type VideoEmptyStateCtaMode =
  | "allReference"
  | "imageReference"
  | "firstFrame"
  | "imageToVideo"
  | "firstLastFrame";

/** 视频节点「空态」CTA 的模式顺序——只列该模型**真正能起步**的图片 / 首尾帧模式。 */
export function videoEmptyStateCtaModes(
  model: VideoModelRef,
): VideoEmptyStateCtaMode[] {
  const order: VideoEmptyStateCtaMode[] = [
    "allReference",
    "imageToVideo",
    "firstFrame",
    "imageReference",
    "firstLastFrame",
  ];
  return order.filter((mode) => isVideoModeSupportedByModel(mode, model));
}

/** 「首次接入图片素材」后的默认模式：按全能参考 → 图生视频 → 首帧 → 图片参考取第一个支持的。 */
export function videoUpstreamImageDefaultMode(
  model: VideoModelRef,
): VideoGenMode | null {
  for (const mode of [
    "allReference",
    "imageToVideo",
    "firstFrame",
    "imageReference",
  ] as const) {
    if (isVideoModeSupportedByModel(mode, model)) return mode;
  }
  return null;
}

/**
 * 该 genMode 是否**必须带提示词**才能提交：文生 / 全能参考 后端强校验 prompt；
 * 首帧 / 图生视频 / 图片参考 / 首尾帧 / 视频编辑允许空提示词（只要素材齐备即可提交）。
 */
export function videoModeRequiresPrompt(mode: VideoGenMode): boolean {
  return mode === "textToVideo" || mode === "allReference" || mode === "videoExtend";
}

/**
 * 该 genMode 是否**必须有上游素材**才能提交：只有文生视频不需要，其余模式的提交
 * 分支都会在素材为空时直接 return（见 VideoNode 的 handleSubmit）。
 *
 * 和 `videoModeRequiresPrompt` 是**并列**关系，不是二选一 —— 全能参考两条都要：
 * 后端 omni 端点强校验 prompt，而 references 为空又根本没得可发。曾经写成「要提示词
 * 的模式就只看提示词」，于是「接过图 → 又把图撤走 → 只打字」这条路上按钮看着可点、
 * 点下去却被 handleSubmit 的 `references.length === 0` 静默拦掉，表现为「点了没反应」。
 * 正常情况下 `videoNoUpstreamResetMode` 会先把模式退回文生视频，这条是兜底：上游节点
 * 还连着、里面的图却被清空时（typeCounts>0 而 counts==0）退回不触发，仍要拦住提交。
 */
export function videoModeRequiresMedia(mode: VideoGenMode): boolean {
  return mode !== "textToVideo";
}

/**
 * 上游素材被全部撤走后该退回哪个模式：`"textToVideo"` = 退回文生视频，null = 不动。
 *
 * 视频节点的模式推导原本是**单向**的：接入图片/视频/音频时有一堆 effect 把模式推进
 * 到能消费该素材的模式，却没有任何一条在素材撤空后把它推回来。于是「连一张图 → 又把
 * 图删掉」之后节点卡在全能参考上，界面上看不出异常，提交却必然被静默拦下。
 *
 * 素材计数要传**按节点类型**的口径（`upstreamTypeCounts`，空的图片节点也算），不能用
 * 「已解析 URL」的口径：空态 CTA（全能参考 / 图片参考 / 首尾帧）正是先铺一个还没出图
 * 的图片/上传节点、再把模式切过去，按 URL 口径这一瞬间素材数是 0，会被这条规则当场
 * 顶回文生视频，等于把三个 CTA 全废掉。
 */
export function videoNoUpstreamResetMode(
  mode: VideoGenMode,
  counts: { images: number; videos: number; audios: number },
): VideoGenMode | null {
  if (counts.images > 0 || counts.videos > 0 || counts.audios > 0) return null;
  return mode === "textToVideo" ? null : "textToVideo";
}

/** 该模型是否能一次消费多张图（目录声明了全能参考或图片参考）。 */
export function videoModelAcceptsMultipleImages(
  model: VideoModelRef,
): boolean {
  return (
    isVideoModeSupportedByModel("allReference", model) ||
    isVideoModeSupportedByModel("imageReference", model)
  );
}

/**
 * 「首帧生成视频」(imageToVideo / i2v) 接了多图时该切到哪个模式，null = 不动。
 * 优先「全能参考」(还能继续接视频 / 音频)，不支持时退「图片参考」；模型消费不了多图
 * 就不动，让提交守卫说清楚「该换模型」。
 */
export function videoMultiImageAutoSwitchMode(
  mode: VideoGenMode,
  model: VideoModelRef,
  imageCount: number,
): VideoGenMode | null {
  if (mode !== "imageToVideo" || imageCount <= 1) return null;
  if (!videoModelAcceptsMultipleImages(model)) return null;
  const candidates: VideoGenMode[] = ["allReference", "imageReference"];
  return candidates.find((candidate) => isVideoModeSupportedByModel(candidate, model)) ?? null;
}

/**
 * Seedance 2.0 音频引用的时长边界。厂商有**两条互相独立**的规则，都会以 400 打回：
 *
 * 1. 逐条：`[InvalidParameter.DurationTooShort] Duration must be between 1.8s and 15.2s`
 * 2. 总和：`the parameter audio total duration (seconds) specified in the request must
 *    be less than or equal to 15.2 for model doubao-seedance-2-0 in r2v`
 *
 * **这里曾经只卡第 1 条**，注释里还写着「厂商口径是逐条，没有一个字提到总和」「别再
 * 回到按总时长判定」。那是错的：2026-08-06 3060 环境两次任务失败
 * （freezone_video_gen/01KZ5R8ZZZY9M8T9F01H159RP7，gen_mode=allReference）实测抓到了
 * 第 2 条报文——3 条各 6s 每条都在 1.8~15.2 区间内、逐条判定必然放行，总计 18s 却被
 * 厂商直接拒。所以总时长这条**不是我们臆想的规则**，删掉它就等于把这个故障放回去。
 *
 * 总时长上限优先读媒体模型目录的 `referenceAudioTotalMaxSeconds`（后台可配），没配才用
 * 下面这个 15.2s 的厂商兜底值。兜底值刻意与单条上限取同一个数：单条 15.2s 是厂商明确
 * 放行的，兜底若取更小（比如 15s）就会把一条合法的顶格音频误拦在本地。想留安全余量
 * 请在后台把 `referenceAudioTotalMaxSeconds` 配小，而不是改这里的常量。
 *
 * 后端 freezone omni-gen 端点有同一套兜底（`validate_omni_reference_audio_durations`，
 * src/novelvideo/freezone/video_node.py），那层拿的是落地文件路径 + ffprobe，是本地
 * `<audio>` 探测失败时最后一道能在计费前拦下的闸门。
 *
 * 文案里的秒数一律从这些常量推（`/ 1000`），别在调用点另写一遍字面量，否则改阈值
 * 时提示会静默漂移。
 */
export const MIN_AUDIO_REFERENCE_DURATION_MS = 1_800;
export const MAX_AUDIO_REFERENCE_DURATION_MS = 15_200;
export const MAX_AUDIO_REFERENCE_TOTAL_DURATION_MS = 15_200;

/** 媒体目录里与音频时长相关的那个字段（`ModelOption` 的子集）。 */
export interface AudioDurationLimitModel {
  referenceAudioMinSeconds?: number | null;
  referenceAudioMaxSeconds?: number | null;
  referenceAudioTotalMinSeconds?: number | null;
  referenceAudioTotalMaxSeconds?: number | null;
  referenceVideoMinSeconds?: number | null;
  referenceVideoMaxSeconds?: number | null;
  referenceVideoTotalMinSeconds?: number | null;
  referenceVideoTotalMaxSeconds?: number | null;
}

export interface ReferenceDurationLimitsMs {
  minMs?: number;
  maxMs?: number;
  totalMinMs?: number;
  totalMaxMs?: number;
}

export function referenceDurationLimitsMs(
  model: AudioDurationLimitModel | null | undefined,
  media: "audio" | "video",
): ReferenceDurationLimitsMs {
  const prefix = media === "audio" ? "referenceAudio" : "referenceVideo";
  const read = (suffix: string): number | undefined => {
    const seconds = (model as Record<string, unknown> | null | undefined)?.[
      `${prefix}${suffix}`
    ];
    return typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0
      ? Math.round(seconds * 1000)
      : undefined;
  };
  return {
    minMs: read("MinSeconds"),
    maxMs: read("MaxSeconds"),
    totalMinMs: read("TotalMinSeconds"),
    totalMaxMs: read("TotalMaxSeconds"),
  };
}

/**
 * 所选模型的**有效**音频总时长上限（毫秒）：目录配置优先，没配才用 15.2s。
 *
 * 与后端 `_catalog_audio_total_duration_max`（api/routes/freezone.py）同一口径：只认
 * 有限正数，null / 0 / 负数 / NaN / Infinity 一律当作没配。允许小数——15.2 本身就不是
 * 整数，这与那几个「非负整数」的计数字段不同，别顺手套 `Number.isInteger`。
 *
 * `vendorCapMs` = 这个模型的厂商硬顶（seedance2 传 15.2s，边界未知的模型不传）。传了
 * 就与目录值**取小**：管理员可以配得更严，但配宽了不该让厂商也跟着放行——给 seedance2
 * 配 60s 的话，3 条 6s 在本地全过、到厂商那儿照样 400，正是这套守卫要消灭的失败。
 */
export function audioReferenceTotalDurationLimitMs(
  model: AudioDurationLimitModel | null | undefined,
  { vendorCapMs }: { vendorCapMs?: number } = {},
): number {
  const seconds = model?.referenceAudioTotalMaxSeconds;
  const configured =
    typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0
      ? Math.round(seconds * 1000)
      : null;
  if (vendorCapMs == null) {
    return configured ?? MAX_AUDIO_REFERENCE_TOTAL_DURATION_MS;
  }
  return configured == null ? vendorCapMs : Math.min(configured, vendorCapMs);
}

/**
 * 三个 kind 必须各占一个联合分支：写成 `kind: "tooShort" | "tooLong"` 合并那两个的话，
 * TS 无法靠 `kind !== "tooLong"` 把整个分支从联合里剔掉，调用点的三元链就取不到
 * totalTooLong 独有的 totalMs / limitMs。
 */
export interface ReferenceDurationClip {
  label: string;
  durationMs: number | null;
  nodeId?: string;
  url?: string;
  index?: number;
}

type MeasuredReferenceDurationClip = ReferenceDurationClip & { durationMs: number };

export type AudioDurationRejection =
  | { kind: "tooShort"; clips: MeasuredReferenceDurationClip[] }
  | { kind: "tooLong"; clips: MeasuredReferenceDurationClip[] }
  | {
      kind: "totalTooShort";
      clips: MeasuredReferenceDurationClip[];
      totalMs: number;
      limitMs: number;
    }
  | {
      kind: "totalTooLong";
      clips: MeasuredReferenceDurationClip[];
      totalMs: number;
      limitMs: number;
    };

/**
 * 提交前音频时长守卫。
 *
 * `durationMs` 为 null = 探测不出时长（音频节点没渲染过波形，且 `<audio>` 探测撞上
 * CORS / 网络 / 超时）。这类一律**不参与判定**——宁可放过去让后端兜底，也不要凭空
 * 拦住一次正常提交。对总时长来说这意味着算出来的和是个**下界**，但判定方向仍然安全：
 * 漏算只会让和变小，所以「算出来超了」必定真超，不会因此误拦。
 *
 * 两类边界**分开授权**，与后端 `validate_omni_reference_audio_durations` 一一对应：
 *   - `perClipLimits`：逐条 1.8~15.2s，这两个数字是从 Seedance 2.0 的报文里实测出来的，
 *     只对它成立。别家模型传 `false` —— 拿 2.0 的数字去卡它，一条正常的 25s 音频会被
 *     我们凭空拦在本地。
 *   - `totalLimitMs`：总时长，优先听目录里的 `referenceAudioTotalMaxSeconds`
 *     （见 `audioReferenceTotalDurationLimitMs`），没配才落到 15.2s 兜底。
 *
 * 上报顺序 太短 → 单条太长 → 总和太长：前两类是「换掉这条」，最后一类是「整体裁一裁」，
 * 混在一起列用户不知道先动哪个。
 */
export function audioReferenceDurationRejection(
  clips: readonly ReferenceDurationClip[],
  options: {
    totalLimitMs?: number | null;
    totalMinMs?: number;
    minMs?: number | null;
    maxMs?: number | null;
    perClipLimits?: boolean;
  } = {},
): AudioDurationRejection | null {
  const {
    totalLimitMs = MAX_AUDIO_REFERENCE_TOTAL_DURATION_MS,
    totalMinMs,
    minMs = MIN_AUDIO_REFERENCE_DURATION_MS,
    maxMs = MAX_AUDIO_REFERENCE_DURATION_MS,
    perClipLimits = true,
  } = options;
  const measured = clips.filter(
    (clip): clip is MeasuredReferenceDurationClip =>
      typeof clip.durationMs === "number" && clip.durationMs > 0,
  );
  if (perClipLimits) {
    const tooShort = minMs == null
      ? []
      : measured.filter((clip) => clip.durationMs < minMs);
    if (tooShort.length > 0) {
      return { kind: "tooShort", clips: tooShort };
    }
    const tooLong = maxMs == null
      ? []
      : measured.filter((clip) => clip.durationMs > maxMs);
    if (tooLong.length > 0) {
      return { kind: "tooLong", clips: tooLong };
    }
  }
  const totalMs = measured.reduce((sum, clip) => sum + clip.durationMs, 0);
  if (totalMinMs != null && measured.length === clips.length && totalMs < totalMinMs) {
    return { kind: "totalTooShort", clips: measured, totalMs, limitMs: totalMinMs };
  }
  if (totalLimitMs != null && measured.length > 0 && totalMs > totalLimitMs) {
    return { kind: "totalTooLong", clips: measured, totalMs, limitMs: totalLimitMs };
  }
  return null;
}

/**
 * 违规条目的秒数展示——**不能四舍五入到与阈值自相矛盾**。
 *
 * 早先用 `toFixed(1)`，1.799s 会显示成「1.8s」、15.201s 显示成「15.2s」：用户看到
 * 的正好是合法边界值，却被告知越界，只能怀疑是我们算错了。时长本身就是整毫秒
 * （`Math.round(secs * 1000)`），所以按毫秒精度展示，再去掉无意义的尾随 0：
 * 900 → `0.9`、1799 → `1.799`、15201 → `15.201`、6000 → `6`。
 */
export function formatAudioDurationSeconds(durationMs: number): string {
  return (durationMs / 1000).toFixed(3).replace(/\.?0+$/, "");
}

/**
 * 把违规条目拼成提示里的 `{{clips}}`（tooShort / tooLong / totalTooLong 共用）。
 *
 * 括号和分隔符都从 locale 取（zh 用全角括号 + 顿号，en 用半角括号 + 逗号），别在
 * 调用点写死——这里曾经硬编码 `（）` 和 `、`，en 用户会看到一串中文标点。
 */
export function formatAudioDurationClips(
  clips: readonly { label: string; durationMs: number }[],
  translate: (key: string, vars?: Record<string, string | number>) => string,
): string {
  return clips
    .map((clip) =>
      translate("node.videoNode.audio.clipDuration", {
        label: clip.label,
        seconds: formatAudioDurationSeconds(clip.durationMs),
      }),
    )
    .join(translate("node.videoNode.audio.clipSeparator"));
}

/**
 * 提交前守卫：当前 (模型, 模式) 是否会**丢弃或被后端直接拒绝**已接入的上游素材。
 * 返回非空理由则应禁用提交、并把理由显示到按钮 tooltip 上，替代「静默丢素材 / 提交 400」。
 *
 * 规则对齐后端 freezone i2v / omni-gen 端点（src/novelvideo/api/routes/freezone.py）：
 * - 视频素材：「全能参考」「视频编辑」「视频延长」消费，其余模式静默丢弃 → 拦；
 * - 音频素材：「全能参考」消费；「视频编辑」仅在媒体目录显式配置音频上限时消费；
 *   其余模式静默丢弃 → 拦；
 * - 多图(>1)：目录没声明全能参考 / 图片参考的模型一次只吃一张图 → 拦。
 *
 * 未配置相应视频模式的模型接入视频/音频后，这些规则会阻止素材被静默丢弃。
 */
/** 返回 i18n key（非 null 时由调用方 `t()` 出文案），不是可直接展示的句子。 */
export function videoSubmitMediaRejectionReason(
  mode: VideoGenMode,
  model: VideoModelRef,
  counts: { images: number; videos: number; audios: number },
): string | null {
  if (
    mode === "videoExtend" &&
    !isVideoModeSupportedByModel("videoExtend", model)
  ) {
    return "node.videoOps.modeDisabled.modelNoVideoExtend";
  }
  if (
    mode === "videoExtend" &&
    (counts.videos !== 1 || counts.images > 0 || counts.audios > 0)
  ) {
    return "node.videoModel.reason.videoExtendSourceOnly";
  }
  if (
    counts.videos > 0 &&
    mode !== "allReference" &&
    mode !== "videoEdit" &&
    mode !== "videoExtend"
  ) {
    return "node.videoModel.reason.videoUnsupported";
  }
  const videoEditAcceptsAudio =
    mode === "videoEdit" &&
    typeof model === "object" &&
    model !== null &&
    isVideoModeSupportedByModel("videoEdit", model) &&
    typeof model.referenceAudioMax === "number" &&
    model.referenceAudioMax > 0;
  if (counts.audios > 0 && mode !== "allReference" && !videoEditAcceptsAudio) {
    return "node.videoModel.reason.audioUnsupported";
  }
  if (counts.images > 1 && !videoModelAcceptsMultipleImages(model)) {
    return "node.videoModel.reason.singleImageOnly";
  }
  return null;
}

/**
 * 模型选择器里某个候选**为什么不能选**（非 null 则置灰 + 悬浮显示这句理由）。
 * 不变量：**「不置灰 ⇒ 存在一个该模型支持、且提交守卫放行的模式」**。
 * 返回 i18n key，调用方负责 `t()`。
 */
export function videoModelReferenceDisabledReason(
  model: VideoModelRef,
  counts: { images: number; videos: number; audios: number },
): string | null {
  const supportsAllReference = isVideoModeSupportedByModel("allReference", model);
  const supportsVideoEdit = isVideoModeSupportedByModel("videoEdit", model);
  const supportsVideoExtend = isVideoModeSupportedByModel("videoExtend", model);
  if (
    counts.videos > 0 &&
    !supportsAllReference &&
    !supportsVideoEdit &&
    !supportsVideoExtend
  ) {
    return "node.videoModel.reason.videoUnsupported";
  }
  const supportsVideoEditAudio =
    supportsVideoEdit &&
    typeof model === "object" &&
    model !== null &&
    typeof model.referenceAudioMax === "number" &&
    model.referenceAudioMax > 0;
  if (counts.audios > 0 && !supportsAllReference && !supportsVideoEditAudio) {
    return "node.videoModel.reason.audioUnsupported";
  }
  if (counts.images > 1 && !videoModelAcceptsMultipleImages(model)) {
    return "node.videoModel.reason.singleImageOnly";
  }
  return null;
}

export interface VideoReferenceAutoSwitch {
  /** 目标模型的 `id`（存进 `VideoNodeData.model` 的那个值，不是 apiModel）。 */
  modelId: string;
  genMode: VideoGenMode;
}

export type VideoReferenceAutoSwitchModel = {
  id: string;
  apiModel?: string;
  supportedModes?: string[];
};

/**
 * 上游接入视频 / 音频时的**自动救场**：当前模型（按目录能力）不支持全能参考、也没有
 * 视频编辑 / 延长能消费这些素材时，换成列表里第一个支持全能参考的模型。当前模型不在
 * 列表里（能力未知）时不动。
 */
function pickVideoReferenceAutoSwitch(
  currentModelId: string | null | undefined,
  counts: { videos: number; audios: number },
  models: readonly VideoReferenceAutoSwitchModel[],
): VideoReferenceAutoSwitch | null {
  if (counts.videos === 0 && counts.audios === 0) {
    return null;
  }
  const current = models.find(
    (model) => model.id === currentModelId || model.apiModel === currentModelId,
  );
  if (!current) return null;
  if (videoModelReferenceDisabledReason(current, { images: 0, ...counts }) == null) {
    return null;
  }
  const target = models.find((model) => isVideoModeSupportedByModel("allReference", model));
  return target ? { modelId: target.id, genMode: "allReference" } : null;
}

export type VideoReferenceAutoSwitchAction =
  /** 什么都别做（还在加载 / 已经救过一次 / 本来就不需要换）。 */
  | { kind: "none" }
  /** 视频音频都撤走了 —— 松开一次性闩锁，为下一次接入做准备。 */
  | { kind: "release" }
  /** 写这一个 patch（模型 + 模式一次写完），并落闩。 */
  | { kind: "switch"; modelId: string; genMode: VideoGenMode };

/**
 * 自动救场的**完整闸门**：素材撤走优先（松闩）；`modelsLoading` 期间不动（兜底列表
 * 未必是项目真列表）；`alreadySwitched` 落闩后不再纠正，避免把 undo 堵死。
 */
export function videoReferenceAutoSwitchAction(input: {
  counts: { videos: number; audios: number };
  currentModelId: string | null | undefined;
  models: readonly VideoReferenceAutoSwitchModel[];
  modelsLoading: boolean;
  alreadySwitched: boolean;
}): VideoReferenceAutoSwitchAction {
  const { counts, currentModelId, models, modelsLoading, alreadySwitched } = input;
  if (counts.videos === 0 && counts.audios === 0) {
    return { kind: "release" };
  }
  if (modelsLoading || alreadySwitched) {
    return { kind: "none" };
  }
  const target = pickVideoReferenceAutoSwitch(currentModelId, counts, models);
  return target
    ? { kind: "switch", modelId: target.modelId, genMode: target.genMode }
    : { kind: "none" };
}
