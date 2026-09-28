// SPDX-License-Identifier: Elastic-全能参考模型
// Copyright (c) 2026 ClaymoreLab
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import type { VideoGenMode } from "@/features/canvas/domain/canvasNodes";
import {
  audioReferenceDurationRejection,
  audioReferenceTotalDurationLimitMs,
  formatAudioDurationClips,
  MAX_AUDIO_REFERENCE_TOTAL_DURATION_MS,
  GEN_MODE_TO_CATALOG_MODE,
  isVideoModeSupportedByModel,
  referenceDurationLimitsMs,
  resolveVideoKeyframeUrls,
  videoEmptyStateCtaModes,
  videoModeForcesAutomaticAspectRatio,
  videoModeRequiresMedia,
  videoModeRequiresPrompt,
  videoModelDefaultGenerateAudio,
  videoModelSupportsGenerateAudio,
  videoModelReferenceDisabledReason,
  videoMultiImageAutoSwitchMode,
  videoNoUpstreamResetMode,
  videoReferenceAutoSwitchAction,
  type VideoReferenceAutoSwitchAction,
  videoSubmitMediaRejectionReason,
  videoUpstreamImageDefaultMode,
} from "@/features/canvas/nodes/shared/videoModelCapabilities";

describe("视频模式有效比例", () => {
  it.each([
    ["firstFrame", true],
    ["firstLastFrame", true],
    ["videoEdit", true],
    ["videoExtend", true],
    ["textToVideo", false],
    ["imageToVideo", false],
    ["imageReference", false],
    ["allReference", false],
  ] as const)("%s 是否强制跟随输入素材", (mode, expected) => {
    expect(videoModeForcesAutomaticAspectRatio(mode)).toBe(expected);
  });
});

// 目录条目形态（src/novelvideo/media_catalog.py）：能力只看 supportedModes。
const HF_REFERENCE_MODES = [
  "text_to_video",
  "first_frame",
  "image_to_video",
  "first_last_frame",
  "image_reference",
  "all_reference",
];
const catalogModel = (id: string, supportedModes: string[], extra: Record<string, unknown> = {}) => ({
  id,
  apiModel: id,
  supportedModes,
  ...extra,
});
const HF_SEEDANCE_FAST = catalogModel("higgsfield:seedance_2_0?mode=fast", HF_REFERENCE_MODES, {
  referenceAudioMax: 3,
});
const HF_SEEDANCE_25 = catalogModel("higgsfield:seedance_2_5", HF_REFERENCE_MODES, {
  referenceAudioMax: 3,
});
const HF_SEEDANCE = catalogModel("higgsfield:seedance_2_0", HF_REFERENCE_MODES, {
  referenceAudioMax: 3,
});
// 只收单张首帧的模型（目录只声明文生 + 首帧）。
const SINGLE_FRAME_A = catalogModel("single-frame-a", ["text_to_video", "first_frame"]);
const SINGLE_FRAME_B = catalogModel("single-frame-b", ["text_to_video", "first_frame"]);
// 声明了视频编辑、没有全能参考 / 首尾帧的模型。
const VIDEO_EDIT_MODEL = catalogModel("higgsfield:video_edit_model", [
  "text_to_video",
  "first_frame",
  "image_to_video",
  "image_reference",
  "video_edit",
]);

describe("video native audio capability", () => {
  it("preserves legacy support and default when catalog fields are absent", () => {
    expect(videoModelSupportsGenerateAudio(HF_SEEDANCE_FAST)).toBe(true);
    expect(videoModelDefaultGenerateAudio({ apiModel: HF_SEEDANCE_FAST.id })).toBe(true);
  });

  it("defaults native audio on whenever the model supports it", () => {
    expect(videoModelSupportsGenerateAudio({ supportsGenerateAudio: true })).toBe(true);
    expect(
      videoModelDefaultGenerateAudio({
        supportsGenerateAudio: true,
      }),
    ).toBe(true);
    expect(
      videoModelDefaultGenerateAudio({
        supportsGenerateAudio: false,
      }),
    ).toBe(false);
  });
});

describe("videoEmptyStateCtaModes — CTA by model capability", () => {
  it("全能参考模型 → 全能参考 / 图生视频 / 首帧 / 图片参考 / 首尾帧", () => {
    expect(videoEmptyStateCtaModes(HF_SEEDANCE_FAST)).toEqual([
      "allReference",
      "imageToVideo",
      "firstFrame",
      "imageReference",
      "firstLastFrame",
    ]);
  });

  it("单帧模型 → 只给「首帧」(全能参考会 400、首尾帧尾帧被静默丢弃、多图不支持)", () => {
    for (const id of [SINGLE_FRAME_A, SINGLE_FRAME_B]) {
      const cta = videoEmptyStateCtaModes(id);
      expect(cta).toEqual(["firstFrame"]);
      // 回归护栏：单帧模型 空态绝不出现只有 全能参考模型 支持的入口。
      expect(cta).not.toContain("allReference");
      expect(cta).not.toContain("firstLastFrame");
    }
  });

  it("视频编辑模型 → 图生视频 / 首帧 / 图片参考 (无全能参考 / 首尾帧)", () => {
    const cta = videoEmptyStateCtaModes(VIDEO_EDIT_MODEL);
    expect(cta).toEqual(["imageToVideo", "firstFrame", "imageReference"]);
    expect(cta).not.toContain("allReference");
    expect(cta).not.toContain("firstLastFrame");
  });

  it("每个 CTA 模式都能被同一模型支持 (CTA ⊆ supported)", () => {
    for (const id of [HF_SEEDANCE_FAST, SINGLE_FRAME_A, VIDEO_EDIT_MODEL]) {
      for (const mode of videoEmptyStateCtaModes(id)) {
        expect(isVideoModeSupportedByModel(mode, id)).toBe(true);
      }
    }
  });
});

describe("目录 supportedModes 是模式入口的单一事实来源", () => {
  const firstFrameOnly = {
    apiModel: SINGLE_FRAME_B.id,
    supportedModes: ["text_to_video", "first_frame"],
  };
  const imageReferenceOnly = {
    apiModel: "custom-reference-model",
    supportedModes: ["text_to_video", "image_reference"],
  };
  const imageToVideoOnly = {
    apiModel: "custom-image-to-video-model",
    supportedModes: ["text_to_video", "image_to_video"],
  };
  const genericAllReference = {
    apiModel: "custom-all-reference-model",
    supportedModes: ["text_to_video", "image_reference", "all_reference"],
  };

  it("只声明 first_frame 的模型仍有真正首帧入口，不会误进图生视频", () => {
    expect(isVideoModeSupportedByModel("firstFrame", firstFrameOnly)).toBe(true);
    expect(isVideoModeSupportedByModel("imageToVideo", firstFrameOnly)).toBe(false);
    expect(videoEmptyStateCtaModes(firstFrameOnly)).toEqual(["firstFrame"]);
    expect(videoUpstreamImageDefaultMode(firstFrameOnly)).toBe("firstFrame");
  });

  it("图生视频和图片参考由两个独立目录能力控制", () => {
    expect(isVideoModeSupportedByModel("firstFrame", imageReferenceOnly)).toBe(false);
    expect(isVideoModeSupportedByModel("imageToVideo", imageReferenceOnly)).toBe(false);
    expect(isVideoModeSupportedByModel("imageReference", imageReferenceOnly)).toBe(true);
    expect(videoEmptyStateCtaModes(imageReferenceOnly)).toEqual(["imageReference"]);
    expect(videoUpstreamImageDefaultMode(imageReferenceOnly)).toBe("imageReference");

    expect(isVideoModeSupportedByModel("imageToVideo", imageToVideoOnly)).toBe(true);
    expect(isVideoModeSupportedByModel("imageReference", imageToVideoOnly)).toBe(false);
    expect(videoEmptyStateCtaModes(imageToVideoOnly)).toEqual(["imageToVideo"]);
    expect(videoUpstreamImageDefaultMode(imageToVideoOnly)).toBe("imageToVideo");
  });

  it("前端模式到目录能力的映射区分首帧和图片参考", () => {
    expect(GEN_MODE_TO_CATALOG_MODE.firstFrame).toBe("first_frame");
    expect(GEN_MODE_TO_CATALOG_MODE.imageToVideo).toBe("image_to_video");
    expect(GEN_MODE_TO_CATALOG_MODE.imageReference).toBe("image_reference");
  });

  it("视频延长只在目录显式声明后开放", () => {
    const seedance25 = {
      apiModel: "seedance-2.5",
      supportedModes: ["text_to_video", "all_reference", "video_edit"],
    };
    expect(isVideoModeSupportedByModel("videoExtend", "seedance-2.5")).toBe(false);
    expect(isVideoModeSupportedByModel("videoExtend", seedance25)).toBe(false);
    expect(
      isVideoModeSupportedByModel("videoExtend", {
        ...seedance25,
        supportedModes: [...seedance25.supportedModes, "video_extend"],
      }),
    ).toBe(true);
  });

  it("非 Seedance 模型声明 all_reference 后可使用多图、视频和音频素材", () => {
    expect(isVideoModeSupportedByModel("allReference", genericAllReference)).toBe(true);
    expect(videoUpstreamImageDefaultMode(genericAllReference)).toBe("allReference");
    expect(videoMultiImageAutoSwitchMode("imageToVideo", genericAllReference, 2)).toBe(
      "allReference",
    );
    expect(
      videoModelReferenceDisabledReason(genericAllReference, {
        images: 9,
        videos: 3,
        audios: 3,
      }),
    ).toBeNull();
    expect(
      videoSubmitMediaRejectionReason("allReference", genericAllReference, {
        images: 9,
        videos: 3,
        audios: 3,
      }),
    ).toBeNull();
  });
});

describe("isVideoModeSupportedByModel — mode gating by model", () => {
  const commonModes: VideoGenMode[] = ["textToVideo", "firstFrame"];

  it("全能参考 / 首尾帧仅 全能参考模型", () => {
    for (const mode of ["allReference", "firstLastFrame"] as VideoGenMode[]) {
      expect(isVideoModeSupportedByModel(mode, HF_SEEDANCE_FAST)).toBe(true);
      expect(isVideoModeSupportedByModel(mode, SINGLE_FRAME_A)).toBe(false);
      expect(isVideoModeSupportedByModel(mode, SINGLE_FRAME_B)).toBe(false);
      expect(isVideoModeSupportedByModel(mode, VIDEO_EDIT_MODEL)).toBe(false);
    }
  });

  it("文生 / 首帧所有视频模型都支持", () => {
    for (const id of [HF_SEEDANCE_FAST, SINGLE_FRAME_A, VIDEO_EDIT_MODEL]) {
      for (const mode of commonModes) {
        expect(isVideoModeSupportedByModel(mode, id)).toBe(true);
      }
    }
  });

  it("单帧模型 不把图片参考伪装成首帧", () => {
    for (const mode of ["imageToVideo", "imageReference"] as VideoGenMode[]) {
      expect(isVideoModeSupportedByModel(mode, SINGLE_FRAME_A)).toBe(false);
      expect(isVideoModeSupportedByModel(mode, SINGLE_FRAME_B)).toBe(false);
    }
  });

  it("视频编辑仅 视频编辑模型", () => {
    expect(isVideoModeSupportedByModel("videoEdit", VIDEO_EDIT_MODEL)).toBe(true);
    expect(isVideoModeSupportedByModel("videoEdit", HF_SEEDANCE_FAST)).toBe(false);
    expect(isVideoModeSupportedByModel("videoEdit", SINGLE_FRAME_A)).toBe(false);
  });
});

describe("videoUpstreamImageDefaultMode — auto-derived default on first image", () => {
  it("全能参考模型 接图默认「全能参考」", () => {
    expect(videoUpstreamImageDefaultMode(HF_SEEDANCE_FAST)).toBe("allReference");
  });

  it("单帧模型 接图默认「首帧」而非全能参考 (否则提交必 400)", () => {
    expect(videoUpstreamImageDefaultMode(SINGLE_FRAME_A)).toBe("firstFrame");
    expect(videoUpstreamImageDefaultMode(SINGLE_FRAME_B)).toBe("firstFrame");
  });
});

describe("resolveVideoKeyframeUrls — stable edge slots", () => {
  it("稳定槽位优先于可编辑标题，重命名尾帧不会被提升成首帧", () => {
    expect(
      resolveVideoKeyframeUrls([
        { url: "last.png", slot: "last", legacyDisplayName: "结尾画面" },
      ]),
    ).toEqual({ firstFrameUrl: null, lastFrameUrl: "last.png" });
  });

  it("旧画布没有槽位时兼容历史标题，再按连线顺序补位", () => {
    expect(
      resolveVideoKeyframeUrls([
        { url: "legacy-last.png", legacyDisplayName: "尾帧" },
        { url: "unassigned.png", legacyDisplayName: "上传图片" },
      ]),
    ).toEqual({ firstFrameUrl: "unassigned.png", lastFrameUrl: "legacy-last.png" });
  });
});

describe("videoSubmitMediaRejectionReason — 提交前素材守卫 (P1/P2)", () => {
  const none = { images: 0, videos: 0, audios: 0 };

  it("存量视频延长在模型能力被撤销后禁止提交", () => {
    const videoEditOnly = {
      apiModel: "seedance-2.5",
      supportedModes: ["video_edit"],
      referenceVideoMax: 1,
    };
    const counts = { ...none, videos: 1 };

    expect(
      videoSubmitMediaRejectionReason("videoExtend", videoEditOnly, counts),
    ).toBe("node.videoOps.modeDisabled.modelNoVideoExtend");
    expect(
      videoSubmitMediaRejectionReason(
        "videoExtend",
        {
          ...videoEditOnly,
          supportedModes: ["video_edit", "video_extend"],
        },
        counts,
      ),
    ).toBeNull();
  });

  it("单帧模型：接入视频 → 拦 (P1 静默丢视频)", () => {
    expect(
      videoSubmitMediaRejectionReason("imageToVideo", SINGLE_FRAME_A, { ...none, videos: 1 }),
    ).toBeTruthy();
    expect(
      videoSubmitMediaRejectionReason("textToVideo", SINGLE_FRAME_A, { ...none, videos: 1 }),
    ).toBeTruthy();
  });

  it("单帧模型：接入音频 → 拦 (P1 静默丢音频)", () => {
    expect(
      videoSubmitMediaRejectionReason("imageToVideo", SINGLE_FRAME_A, { ...none, audios: 1 }),
    ).toBeTruthy();
  });

  it("单帧模型：>1 图 → 拦 (P2 多图 400)，无论 imageReference 还是 imageToVideo", () => {
    for (const mode of ["imageReference", "imageToVideo"] as VideoGenMode[]) {
      expect(
        videoSubmitMediaRejectionReason(mode, SINGLE_FRAME_A, { images: 2, videos: 0, audios: 0 }),
      ).toBeTruthy();
      expect(
        videoSubmitMediaRejectionReason(mode, SINGLE_FRAME_B, { images: 9, videos: 0, audios: 0 }),
      ).toBeTruthy();
    }
  });

  it("单帧模型：单图 / 纯文本 → 放行", () => {
    expect(
      videoSubmitMediaRejectionReason("imageToVideo", SINGLE_FRAME_A, { ...none, images: 1 }),
    ).toBeNull();
    expect(
      videoSubmitMediaRejectionReason("imageReference", SINGLE_FRAME_A, { ...none, images: 1 }),
    ).toBeNull();
    expect(videoSubmitMediaRejectionReason("textToVideo", SINGLE_FRAME_A, none)).toBeNull();
  });

  it("全能参考模型：全能参考消费视频/音频/多图 → 放行", () => {
    expect(
      videoSubmitMediaRejectionReason("allReference", HF_SEEDANCE_FAST, { images: 9, videos: 1, audios: 1 }),
    ).toBeNull();
  });

  it("视频编辑模型：视频编辑消费视频、图片参考消费多图 → 放行", () => {
    expect(
      videoSubmitMediaRejectionReason("videoEdit", VIDEO_EDIT_MODEL, { ...none, videos: 1 }),
    ).toBeNull();
    expect(
      videoSubmitMediaRejectionReason("imageReference", VIDEO_EDIT_MODEL, { images: 5, videos: 0, audios: 0 }),
    ).toBeNull();
  });

  it("目录显式开放音频后，视频编辑消费独立音频", () => {
    const audioVideoEditModel = {
      apiModel: "custom-video-editor",
      supportedModes: ["video_edit"],
      referenceAudioMax: 2,
    };
    expect(
      videoSubmitMediaRejectionReason("videoEdit", audioVideoEditModel, {
        images: 0,
        videos: 1,
        audios: 1,
      }),
    ).toBeNull();
    expect(
      videoSubmitMediaRejectionReason(
        "videoEdit",
        { ...audioVideoEditModel, referenceAudioMax: 0 },
        { images: 0, videos: 1, audios: 1 },
      ),
    ).toBeTruthy();
  });
});

describe("videoMultiImageAutoSwitchMode — 首帧接多图时的自动改模式", () => {
  // 这条是 bug 本体：i2v 端点按图片张数分流（1 张 = 图生视频，2-9 张 = 图片参考），
  // 接上第二张图后做的其实已经是图片参考了，界面上模式却还写着「首帧生成视频」。
  it("全能参考模型：首帧接到第 2 张图 → 切全能参考", () => {
    expect(videoMultiImageAutoSwitchMode("imageToVideo", HF_SEEDANCE_FAST, 2)).toBe(
      "allReference",
    );
    expect(videoMultiImageAutoSwitchMode("imageToVideo", HF_SEEDANCE_25, 9)).toBe(
      "allReference",
    );
  });

  it("单图 / 无图不动 —— 那正是首帧本来的用法", () => {
    for (const images of [0, 1]) {
      expect(
        videoMultiImageAutoSwitchMode("imageToVideo", HF_SEEDANCE_FAST, images),
      ).toBeNull();
    }
  });

  it("只管首帧模式，其它模式一律不插手", () => {
    for (const mode of [
      "textToVideo",
      "imageReference",
      "allReference",
      "firstLastFrame",
      "videoEdit",
    ] as VideoGenMode[]) {
      expect(videoMultiImageAutoSwitchMode(mode, HF_SEEDANCE_FAST, 5)).toBeNull();
    }
  });

  it("没有全能参考、但声明了图片参考的模型 → 图片参考", () => {
    expect(videoMultiImageAutoSwitchMode("imageToVideo", VIDEO_EDIT_MODEL, 3)).toBe(
      "imageReference",
    );
  });

  // 换模式救不了 单帧模型：它的 i2v 端点 >1 图直接 400，切到哪个模式都是 400。留在首帧上，
  // 让提交守卫那句「该模型单次仅支持 1 张图片」把「该换模型」这个真问题说清楚。
  it("单帧模型 不动：它多图必 400，问题在模型不在模式", () => {
    for (const id of [SINGLE_FRAME_A, SINGLE_FRAME_B]) {
      expect(videoMultiImageAutoSwitchMode("imageToVideo", id, 2)).toBeNull();
      expect(videoSubmitMediaRejectionReason("imageToVideo", id, {
        images: 2,
        videos: 0,
        audios: 0,
      })).toBeTruthy();
    }
  });

  // 媒体目录声明的 supportedModes 优先级高于启发式（与可见 tab 同一口径）。
  it("目录声明没有全能参考时退到图片参考；两个都没有则不动", () => {
    expect(
      videoMultiImageAutoSwitchMode(
        "imageToVideo",
        {
          ...HF_SEEDANCE_FAST,
          supportedModes: ["text_to_video", "first_frame", "image_reference"],
        },
        2,
      ),
    ).toBe("imageReference");
    expect(
      videoMultiImageAutoSwitchMode(
        "imageToVideo",
        {
          ...HF_SEEDANCE_FAST,
          supportedModes: ["text_to_video", "first_frame"],
        },
        2,
      ),
    ).toBeNull();
  });

  // 自动改模式与提交守卫必须闭合：切完之后那个模式得真能提交，否则等于把用户从
  // 一个坑挪到另一个坑。
  it("切过去的模式在提交守卫里放行", () => {
    const counts = { images: 3, videos: 0, audios: 0 };
    const target = videoMultiImageAutoSwitchMode("imageToVideo", HF_SEEDANCE_FAST, counts.images);
    expect(target).not.toBeNull();
    expect(
      videoSubmitMediaRejectionReason(target as VideoGenMode, HF_SEEDANCE_FAST, counts),
    ).toBeNull();
  });

  // 自动切模式只是把界面导对，真正兜底提交的是引用上限：万一模式没被切走（比如
  // effect 还没跑、或将来又有人加了 bail 条件），提交也只能带 1 张图，绝不能靠
  // 张数悄悄变成图片参考。这条上限是结构性的，不接受媒体目录 referenceImageMax 覆盖。
  it("首帧和单图图生视频的图片上限锁死在 1，且不被媒体目录配置覆盖", () => {
    const source = readFileSync("src/features/canvas/nodes/VideoNode.tsx", "utf8");
    expect(source).toContain("firstFrame: { image: 1, video: 0, audio: 0 }");
    expect(source).toContain("imageToVideo: { image: 1, video: 0, audio: 0 }");
    expect(source).toContain("const FIXED_IMAGE_CAP_BY_MODE");
    expect(source).toContain(
      "image: FIXED_IMAGE_CAP_BY_MODE[mode] ?? model?.referenceImageMax ?? defaults.image",
    );
  });
});

describe("声明了视频编辑的模型：单图默认模式", () => {
  it("默认进入图生视频，首帧仍是目录声明后的独立可选模式", () => {
    const configured = {
      apiModel: VIDEO_EDIT_MODEL.id,
      supportedModes: [
        "text_to_video",
        "first_frame",
        "image_to_video",
        "image_reference",
        "video_edit",
      ],
    };
    expect(videoUpstreamImageDefaultMode(configured)).toBe("imageToVideo");
    expect(isVideoModeSupportedByModel("firstFrame", configured)).toBe(true);
    expect(isVideoModeSupportedByModel("imageToVideo", configured)).toBe(true);
  });
});


describe("videoModelReferenceDisabledReason — 模型选择器置灰守卫", () => {
  const none = { images: 0, videos: 0, audios: 0 };

  it("单帧模型：单图 → 可选（不置灰）", () => {
    for (const id of [SINGLE_FRAME_A, SINGLE_FRAME_B]) {
      expect(videoModelReferenceDisabledReason(id, { ...none, images: 1 })).toBeNull();
      expect(videoModelReferenceDisabledReason(id, none)).toBeNull();
    }
  });

  it("单帧模型：>1 图 → 置灰", () => {
    expect(
      videoModelReferenceDisabledReason(SINGLE_FRAME_B, { ...none, images: 2 }),
    ).toBeTruthy();
  });

  it("单帧模型：接入视频 / 音频 → 置灰", () => {
    expect(
      videoModelReferenceDisabledReason(SINGLE_FRAME_A, { ...none, videos: 1 }),
    ).toBeTruthy();
    expect(
      videoModelReferenceDisabledReason(SINGLE_FRAME_A, { ...none, audios: 1 }),
    ).toBeTruthy();
  });

  // 两条守卫是一对，阈值漂开就会出现「能选但一提交就被拦」或反过来的自相矛盾。
  it("与提交守卫同阈值：单图放行 / 多图拦截的判定一致", () => {
    for (const images of [0, 1, 2, 9]) {
      const counts = { ...none, images };
      const pickerBlocked = videoModelReferenceDisabledReason(SINGLE_FRAME_B, counts) != null;
      const submitBlocked =
        videoSubmitMediaRejectionReason("imageToVideo", SINGLE_FRAME_B, counts) != null;
      expect(pickerBlocked).toBe(submitBlocked);
    }
  });

  it("全能参考模型：多图 + 视频 + 音频都不置灰（全能参考全吃）", () => {
    for (const id of [HF_SEEDANCE_FAST, HF_SEEDANCE_25]) {
      expect(
        videoModelReferenceDisabledReason(id, { images: 9, videos: 1, audios: 1 }),
      ).toBeNull();
    }
  });

  it("视频编辑模型：多图 / 视频不置灰，音频置灰", () => {
    expect(
      videoModelReferenceDisabledReason(VIDEO_EDIT_MODEL, { images: 9, videos: 1, audios: 0 }),
    ).toBeNull();
    expect(
      videoModelReferenceDisabledReason(VIDEO_EDIT_MODEL, { ...none, audios: 1 }),
    ).toBeTruthy();
  });

  it("目录声明 video_edit 且音频上限大于 0 时，带音频仍可选择模型", () => {
    expect(
      videoModelReferenceDisabledReason(
        {
          apiModel: "custom-video-editor",
          supportedModes: ["video_edit"],
          referenceAudioMax: 1,
        },
        { ...none, videos: 1, audios: 1 },
      ),
    ).toBeNull();
  });

  it("视频编辑模型：目录里没有 video_edit 时，接入视频 → 置灰", () => {
    const withoutVideoEdit = {
      apiModel: VIDEO_EDIT_MODEL.id,
      supportedModes: [
        "text_to_video",
        "first_frame",
        "image_reference",
        "first_last_frame",
      ],
    };
    expect(
      videoModelReferenceDisabledReason(withoutVideoEdit, { ...none, videos: 1 }),
    ).toBe("node.videoModel.reason.videoUnsupported");
    // 目录里还留着 video_edit 的话照旧放行，别把整个模型误伤掉。
    expect(
      videoModelReferenceDisabledReason(
        { apiModel: VIDEO_EDIT_MODEL.id, supportedModes: [...withoutVideoEdit.supportedModes, "video_edit"] },
        { ...none, videos: 1 },
      ),
    ).toBeNull();
  });

  it("模型选择器必须把整个 ModelOption 传给置灰守卫（而非只传 id）", () => {
    const source = readFileSync(
      "src/features/canvas/nodes/VideoOperationsPanel.tsx",
      "utf8",
    );
    expect(source).toContain("videoModelReferenceDisabledReason(model, {");
    expect(source).not.toContain(
      "videoModelReferenceDisabledReason(model.apiModel ?? model.id",
    );
  });
});

// 两条守卫真正要维持的不变量：「选得进去就必须走得通」。这条网格测试是死胡同的探照灯：
// 选择器放行、却没有任何一个它支持的模式能通过提交守卫。
describe("置灰守卫 × 提交守卫 — 不置灰的组合必须存在可提交的模式", () => {
  const ALL_MODES: VideoGenMode[] = [
    "textToVideo",
    "firstFrame",
    "imageToVideo",
    "imageReference",
    "allReference",
    "firstLastFrame",
    "videoEdit",
  ];
  const PICKER_MODELS = [
    SINGLE_FRAME_A,
    SINGLE_FRAME_B,
    HF_SEEDANCE_FAST,
    HF_SEEDANCE_25,
    VIDEO_EDIT_MODEL,
  ];

  it("对每个模型 × 每种素材组合都成立", () => {
    const deadEnds: string[] = [];
    for (const modelId of PICKER_MODELS) {
      for (const images of [0, 1, 2, 9]) {
        for (const videos of [0, 1]) {
          for (const audios of [0, 1]) {
            const counts = { images, videos, audios };
            if (videoModelReferenceDisabledReason(modelId, counts) != null) {
              continue; // 已置灰 —— 用户选不进来，谈不上死胡同
            }
            const usable = ALL_MODES.some(
              (mode) =>
                isVideoModeSupportedByModel(mode, modelId) &&
                videoSubmitMediaRejectionReason(mode, modelId, counts) == null,
            );
            if (!usable) {
              deadEnds.push(`${modelId} + ${JSON.stringify(counts)}`);
            }
          }
        }
      }
    }
    expect(deadEnds).toEqual([]);
  });
});

// 选择器顺序即目录顺序：救场目标是列表里第一个声明了 all_reference 的模型。
const MODELS = [SINGLE_FRAME_B, SINGLE_FRAME_A, HF_SEEDANCE, HF_SEEDANCE_FAST, HF_SEEDANCE_25];

function expectSwitch(action: VideoReferenceAutoSwitchAction) {
  if (action.kind !== "switch") {
    throw new Error(`期望 switch，实际拿到 ${action.kind}`);
  }
  return action;
}

describe("videoReferenceAutoSwitchAction — 接入视频/音频时换成支持全能参考的模型", () => {
  const pick = (
    currentModelId: string | null | undefined,
    counts: { videos: number; audios: number },
    models: readonly { id: string; apiModel?: string; supportedModes?: string[] }[],
  ) =>
    videoReferenceAutoSwitchAction({
      counts,
      currentModelId,
      models,
      modelsLoading: false,
      alreadySwitched: false,
    });

  it("单帧模型 + 视频 → 换成列表里第一个支持全能参考的模型，并落到全能参考", () => {
    expect(pick(SINGLE_FRAME_A.id, { videos: 1, audios: 0 }, MODELS)).toEqual({
      kind: "switch",
      modelId: HF_SEEDANCE.id,
      genMode: "allReference",
    });
  });

  it("单帧模型 + 音频 → 同样切换", () => {
    expect(pick(SINGLE_FRAME_B.id, { videos: 0, audios: 1 }, MODELS)).toEqual({
      kind: "switch",
      modelId: HF_SEEDANCE.id,
      genMode: "allReference",
    });
  });

  it("素材没接 / 已撤走 → release（松闩），不写任何 patch", () => {
    expect(pick(SINGLE_FRAME_A.id, { videos: 0, audios: 0 }, MODELS)).toEqual({
      kind: "release",
    });
  });

  it("返回的是 id 而非 apiModel —— 存进 VideoNodeData.model 的是 id", () => {
    const renamed = [SINGLE_FRAME_B, { ...HF_SEEDANCE, id: "picker-id" }];
    const action = pick(SINGLE_FRAME_B.id, { videos: 1, audios: 0 }, renamed);
    expect(expectSwitch(action).modelId).toBe("picker-id");
  });

  it("本来就能消费视频的模型不抢（全能参考 / 视频编辑）", () => {
    const models = [...MODELS, VIDEO_EDIT_MODEL];
    expect(pick(HF_SEEDANCE_FAST.id, { videos: 1, audios: 1 }, models)).toEqual({ kind: "none" });
    expect(pick(VIDEO_EDIT_MODEL.id, { videos: 1, audios: 0 }, models)).toEqual({ kind: "none" });
  });

  it("当前模型不在列表里（能力未知）或没有可切目标 → 不动", () => {
    expect(pick("unknown", { videos: 1, audios: 0 }, MODELS)).toEqual({ kind: "none" });
    expect(
      pick(SINGLE_FRAME_A.id, { videos: 1, audios: 0 }, [SINGLE_FRAME_A, SINGLE_FRAME_B]),
    ).toEqual({ kind: "none" });
    expect(pick(SINGLE_FRAME_A.id, { videos: 1, audios: 0 }, [])).toEqual({ kind: "none" });
  });

  it("切换结果自洽：目标模型在同样的素材下既不置灰也不被提交守卫拦", () => {
    const counts = { images: 1, videos: 1, audios: 1 };
    const next = expectSwitch(pick(SINGLE_FRAME_A.id, counts, MODELS));
    const target = MODELS.find((model) => model.id === next.modelId);
    expect(videoModelReferenceDisabledReason(target, counts)).toBeNull();
    expect(videoSubmitMediaRejectionReason(next.genMode, target, counts)).toBeNull();
    expect(isVideoModeSupportedByModel(next.genMode, target)).toBe(true);
  });
});

describe("videoReferenceAutoSwitchAction — 模型列表异步加载期间的闸门", () => {
  const FALLBACK = MODELS;
  const REAL_SINGLE_FRAME_ONLY = [SINGLE_FRAME_B, SINGLE_FRAME_A];
  const REAL_WITH_REFERENCE = [SINGLE_FRAME_B, { ...HF_SEEDANCE, id: "proj-ref" }];

  interface Frame {
    currentModelId: string;
    models: readonly { id: string; apiModel?: string; supportedModes?: string[] }[];
    modelsLoading: boolean;
    counts?: { videos: number; audios: number };
  }

  function replay(frames: readonly Frame[]) {
    let latched = false;
    const writes: { modelId: string; genMode: VideoGenMode }[] = [];
    for (const frame of frames) {
      const action = videoReferenceAutoSwitchAction({
        counts: frame.counts ?? { videos: 1, audios: 0 },
        currentModelId: frame.currentModelId,
        models: frame.models,
        modelsLoading: frame.modelsLoading,
        alreadySwitched: latched,
      });
      if (action.kind === "release") {
        latched = false;
      } else if (action.kind === "switch") {
        latched = true;
        writes.push({ modelId: action.modelId, genMode: action.genMode });
      }
    }
    return { writes, latched };
  }

  it("兜底列表有目标但真列表没有 → 全程不写，也不落闩", () => {
    const { writes, latched } = replay([
      { currentModelId: SINGLE_FRAME_B.id, models: FALLBACK, modelsLoading: true },
      { currentModelId: SINGLE_FRAME_B.id, models: REAL_SINGLE_FRAME_ONLY, modelsLoading: false },
      { currentModelId: SINGLE_FRAME_B.id, models: REAL_SINGLE_FRAME_ONLY, modelsLoading: false },
    ]);
    expect(writes).toEqual([]);
    expect(latched).toBe(false);
  });

  it("加载中不动；真列表到了才切，且切的是真列表里的 id，只切一次", () => {
    const { writes } = replay([
      { currentModelId: SINGLE_FRAME_B.id, models: FALLBACK, modelsLoading: true },
      { currentModelId: SINGLE_FRAME_B.id, models: REAL_WITH_REFERENCE, modelsLoading: false },
      { currentModelId: "proj-ref", models: REAL_WITH_REFERENCE, modelsLoading: false },
    ]);
    expect(writes).toEqual([{ modelId: "proj-ref", genMode: "allReference" }]);
  });

  it("落闩后即使模型被 undo 回去（素材边仍在）也不再纠正", () => {
    const { writes } = replay([
      { currentModelId: SINGLE_FRAME_B.id, models: MODELS, modelsLoading: false },
      { currentModelId: HF_SEEDANCE.id, models: MODELS, modelsLoading: false },
      { currentModelId: SINGLE_FRAME_B.id, models: MODELS, modelsLoading: false },
      { currentModelId: SINGLE_FRAME_B.id, models: MODELS, modelsLoading: false },
    ]);
    expect(writes).toHaveLength(1);
  });

  it("素材撤走后松闩，下次再接入还能救一次", () => {
    const { writes } = replay([
      { currentModelId: SINGLE_FRAME_B.id, models: MODELS, modelsLoading: false },
      {
        currentModelId: SINGLE_FRAME_B.id,
        models: MODELS,
        modelsLoading: false,
        counts: { videos: 0, audios: 0 },
      },
      { currentModelId: SINGLE_FRAME_B.id, models: MODELS, modelsLoading: false },
    ]);
    expect(writes).toHaveLength(2);
  });

  it("加载中素材就被撤走 → 仍然松闩", () => {
    const action = videoReferenceAutoSwitchAction({
      counts: { videos: 0, audios: 0 },
      currentModelId: SINGLE_FRAME_B.id,
      models: FALLBACK,
      modelsLoading: true,
      alreadySwitched: true,
    });
    expect(action).toEqual({ kind: "release" });
  });
});

describe("audioReferenceDurationRejection — 提交前音频时长守卫", () => {
  const clip = (label: string, durationMs: number | null) => ({ label, durationMs });

  it("单条短于 1.8s → 拦，并指名是哪条 (厂商 DurationTooShort)", () => {
    const rejection = audioReferenceDurationRejection([
      clip("bgm.mp3", 5_000),
      clip("sfx.wav", 900),
    ]);
    expect(rejection).toEqual({
      kind: "tooShort",
      clips: [{ label: "sfx.wav", durationMs: 900 }],
    });
  });

  it("恰好 1.8s 放行，1.799s 拦 (边界取闭区间，与厂商 1.8s 下限一致)", () => {
    expect(audioReferenceDurationRejection([clip("a", 1_800)])).toBeNull();
    expect(audioReferenceDurationRejection([clip("a", 1_799)])?.kind).toBe("tooShort");
  });

  it("单条长于 15.2s → 拦，并指名是哪条；15.2s 整放行", () => {
    expect(audioReferenceDurationRejection([clip("a", 15_200)])).toBeNull();
    expect(
      audioReferenceDurationRejection([clip("bgm.mp3", 5_000), clip("long.wav", 15_201)]),
    ).toEqual({
      kind: "tooLong",
      clips: [{ label: "long.wav", durationMs: 15_201 }],
    });
  });

  // 这条曾经断言「3 条 6s 共 18s 厂商也收」。2026-08-06 3060 环境实测打脸：厂商在
  // r2v 另有 `audio total duration ... must be less than or equal to 15.2` 一条，
  // 逐条全合规的 18s 组合照样 400。别把它改回放行。
  it("逐条都合规但总和超限 → 拦，并带上总时长和上限", () => {
    expect(
      audioReferenceDurationRejection([
        clip("a", 6_000),
        clip("b", 6_000),
        clip("c", 6_000),
      ]),
    ).toEqual({
      kind: "totalTooLong",
      clips: [
        { label: "a", durationMs: 6_000 },
        { label: "b", durationMs: 6_000 },
        { label: "c", durationMs: 6_000 },
      ],
      totalMs: 18_000,
      limitMs: 15_200,
    });
  });

  it("总和恰好顶格 15.2s 放行，多 1ms 才拦", () => {
    expect(
      audioReferenceDurationRejection([clip("a", 9_000), clip("b", 6_200)]),
    ).toBeNull();
    expect(
      audioReferenceDurationRejection([clip("a", 9_000), clip("b", 6_201)])?.kind,
    ).toBe("totalTooLong");
  });

  it("单条顶格 15.2s 不会被总和这条误伤 (兜底上限与单条上限同值)", () => {
    expect(audioReferenceDurationRejection([clip("a", 15_200)])).toBeNull();
  });

  it("单条太长优先于总和上报 (先换掉那条，再谈整体裁剪)", () => {
    expect(
      audioReferenceDurationRejection([clip("a", 20_000), clip("b", 6_000)])?.kind,
    ).toBe("tooLong");
  });

  it("总和上限走传入值 (后台 referenceAudioTotalMaxSeconds)", () => {
    const clips = [clip("a", 6_000), clip("b", 6_000)];
    expect(audioReferenceDurationRejection(clips)).toBeNull();
    expect(audioReferenceDurationRejection(clips, { totalLimitMs: 30_000 })).toBeNull();
    expect(audioReferenceDurationRejection(clips, { totalLimitMs: 10_000 })).toEqual({
      kind: "totalTooLong",
      clips: [
        { label: "a", durationMs: 6_000 },
        { label: "b", durationMs: 6_000 },
      ],
      totalMs: 12_000,
      limitMs: 10_000,
    });
  });

  it("perClipLimits: false 只判总和——1.8~15.2 是 seedance2 专属数字，别拿去卡别家", () => {
    // 后台给某个非 seedance2 模型配了 60s 总时长：一条 25s 的音频对它完全合法，
    // 若还套用逐条 15.2s 就是我们凭空 400 掉一次正常提交。
    const options = { totalLimitMs: 60_000, perClipLimits: false };
    expect(audioReferenceDurationRejection([clip("a", 25_000)], options)).toBeNull();
    expect(audioReferenceDurationRejection([clip("a", 500)], options)).toBeNull();
    // 总和这条仍然管着。
    expect(
      audioReferenceDurationRejection([clip("a", 40_000), clip("b", 25_000)], options),
    ).toMatchObject({ kind: "totalTooLong", totalMs: 65_000, limitMs: 60_000 });
  });

  it("测不出的条目不计入总和——和只会偏小，所以判超了必定真超", () => {
    // 3 条各 6s，其中一条测不出：可见部分 12s 未超，放行给后端 ffprobe 兜底。
    expect(
      audioReferenceDurationRejection([
        clip("a", 6_000),
        clip("b", null),
        clip("c", 6_000),
      ]),
    ).toBeNull();
  });

  it("既有太短又有太长时优先报太短 (一次只报一类，别混着列)", () => {
    expect(
      audioReferenceDurationRejection([clip("long", 20_000), clip("short", 500)]),
    ).toEqual({
      kind: "tooShort",
      clips: [{ label: "short", durationMs: 500 }],
    });
  });

  it("同类越界的多条一次全列出来", () => {
    expect(
      audioReferenceDurationRejection([
        clip("a", 900),
        clip("b", 5_000),
        clip("c", 1_000),
      ])?.clips,
    ).toEqual([
      { label: "a", durationMs: 900 },
      { label: "c", durationMs: 1_000 },
    ]);
  });

  it("探测不出时长 (null) 的不参与判定 → 放行给后端兜底", () => {
    expect(audioReferenceDurationRejection([clip("unknown", null)])).toBeNull();
    expect(
      audioReferenceDurationRejection([clip("unknown", null), clip("ok", 15_200)]),
    ).toBeNull();
  });

  it("没有音频引用时不拦", () => {
    expect(audioReferenceDurationRejection([])).toBeNull();
  });

  it("支持后台配置的总时长下限，且存在未探测素材时不误拦", () => {
    expect(
      audioReferenceDurationRejection([clip("a", 4_000), clip("b", 5_000)], {
        minMs: null,
        maxMs: null,
        totalMinMs: 10_000,
        totalLimitMs: null,
        perClipLimits: false,
      }),
    ).toMatchObject({ kind: "totalTooShort", totalMs: 9_000, limitMs: 10_000 });
    expect(
      audioReferenceDurationRejection([clip("a", 4_000), clip("unknown", null)], {
        minMs: null,
        maxMs: null,
        totalMinMs: 10_000,
        totalLimitMs: null,
        perClipLimits: false,
      }),
    ).toBeNull();
  });
});

describe("referenceDurationLimitsMs — 目录时长配置", () => {
  it("分别读取音频和视频的单条/总时长配置", () => {
    const model = {
      referenceAudioMinSeconds: 1.8,
      referenceAudioTotalMaxSeconds: 15.2,
      referenceVideoMaxSeconds: 12,
      referenceVideoTotalMinSeconds: 5,
    };
    expect(referenceDurationLimitsMs(model, "audio")).toEqual({
      minMs: 1_800,
      maxMs: undefined,
      totalMinMs: undefined,
      totalMaxMs: 15_200,
    });
    expect(referenceDurationLimitsMs(model, "video")).toEqual({
      minMs: undefined,
      maxMs: 12_000,
      totalMinMs: 5_000,
      totalMaxMs: undefined,
    });
  });
});

describe("audioReferenceTotalDurationLimitMs — 目录优先、15.2s 兜底", () => {
  it("没配 / 配了非法值 → 兜底 15.2s", () => {
    expect(audioReferenceTotalDurationLimitMs(null)).toBe(15_200);
    expect(audioReferenceTotalDurationLimitMs(undefined)).toBe(15_200);
    expect(audioReferenceTotalDurationLimitMs({})).toBe(15_200);
    for (const bad of [null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(
        audioReferenceTotalDurationLimitMs({ referenceAudioTotalMaxSeconds: bad }),
      ).toBe(MAX_AUDIO_REFERENCE_TOTAL_DURATION_MS);
    }
  });

  it("配了就听配置，且**接受小数**——15.2 本身就不是整数", () => {
    expect(
      audioReferenceTotalDurationLimitMs({ referenceAudioTotalMaxSeconds: 30 }),
    ).toBe(30_000);
    expect(
      audioReferenceTotalDurationLimitMs({ referenceAudioTotalMaxSeconds: 15.2 }),
    ).toBe(15_200);
    // 别顺手套 Number.isInteger：那会把管理员配的 12.5s 静默退回 15.2s，比后端更宽。
    expect(
      audioReferenceTotalDurationLimitMs({ referenceAudioTotalMaxSeconds: 12.5 }),
    ).toBe(12_500);
  });

  it("vendorCapMs 与目录值取小——配宽了不能越过厂商硬顶", () => {
    const capped = { vendorCapMs: MAX_AUDIO_REFERENCE_TOTAL_DURATION_MS };
    // 管理员给 seedance2 配 60s：3 条 6s 在本地全过、到厂商那儿照样 400。必须取小。
    expect(
      audioReferenceTotalDurationLimitMs(
        { referenceAudioTotalMaxSeconds: 60 },
        capped,
      ),
    ).toBe(15_200);
    // 收严的方向照收。
    expect(
      audioReferenceTotalDurationLimitMs({ referenceAudioTotalMaxSeconds: 8 }, capped),
    ).toBe(8_000);
    // 没配就是硬顶本身。
    expect(audioReferenceTotalDurationLimitMs(null, capped)).toBe(15_200);
    // 不传 vendorCapMs（边界未知的模型）就没有硬顶可取，60s 照用。
    expect(
      audioReferenceTotalDurationLimitMs({ referenceAudioTotalMaxSeconds: 60 }),
    ).toBe(60_000);
  });
});

describe("formatAudioDurationClips — 提示里的 {{clips}} 走 locale 排版", () => {
  // 用真实的 translation.json 而不是假 t()：这里要守的就是「en 用户别看到中文标点」，
  // 假串测不出来。解析 + {{var}} 插值与 i18next 的默认行为一致（escapeValue: false）。
  const locale = (language: "zh" | "en") => {
    const bundle = JSON.parse(
      readFileSync(`public/locales/${language}/translation.json`, "utf8"),
    );
    return (key: string, vars?: Record<string, string | number>) => {
      const value = key
        .split(".")
        .reduce<unknown>((node, part) => (node as Record<string, unknown>)?.[part], bundle);
      if (typeof value !== "string") throw new Error(`missing translation key: ${key}`);
      return value.replace(/{{(\w+)}}/g, (_, name: string) => String(vars?.[name] ?? ""));
    };
  };

  const CLIPS = [
    { label: "sfx.wav", durationMs: 900 },
    { label: "bgm.mp3", durationMs: 1_200 },
  ];

  it("中文用全角括号 + 顿号", () => {
    expect(formatAudioDurationClips(CLIPS, locale("zh"))).toBe(
      "sfx.wav（0.9s）、bgm.mp3（1.2s）",
    );
  });

  it("英文用半角括号 + 逗号，且不漏出任何中文标点", () => {
    const formatted = formatAudioDurationClips(CLIPS, locale("en"));
    expect(formatted).toBe("sfx.wav (0.9s), bgm.mp3 (1.2s)");
    expect(formatted).not.toMatch(/[（）、。：]/);
  });

  it("单条时不带分隔符", () => {
    expect(formatAudioDurationClips([CLIPS[0]], locale("en"))).toBe("sfx.wav (0.9s)");
  });

  it("紧贴阈值的毫秒不四舍五入到合法值 (否则提示自相矛盾)", () => {
    // 1.799s 曾被显示成「1.8s」、15.201s 曾被显示成「15.2s」——用户看到的正好是
    // 合法边界值，却被告知越界。展示按毫秒精度，别再退回 toFixed(1)。
    expect(
      formatAudioDurationClips([{ label: "a.wav", durationMs: 1_799 }], locale("en")),
    ).toBe("a.wav (1.799s)");
    expect(
      formatAudioDurationClips([{ label: "b.wav", durationMs: 15_201 }], locale("en")),
    ).toBe("b.wav (15.201s)");
  });

  it("整秒不拖尾随 0", () => {
    expect(
      formatAudioDurationClips([{ label: "c.wav", durationMs: 6_000 }], locale("en")),
    ).toBe("c.wav (6s)");
    expect(
      formatAudioDurationClips([{ label: "d.wav", durationMs: 15_200 }], locale("en")),
    ).toBe("d.wav (15.2s)");
  });

  it("缺文件名时的兜底标签也跟随语言（不再硬编码「音频N」）", () => {
    expect(locale("zh")("node.videoNode.audio.clipFallbackLabel", { index: 2 })).toBe(
      "音频2",
    );
    expect(locale("en")("node.videoNode.audio.clipFallbackLabel", { index: 2 })).toBe(
      "Audio 2",
    );
  });
});

describe("videoModeRequiresPrompt — submit validation by mode", () => {
  it("文生 / 全能参考必须带提示词", () => {
    expect(videoModeRequiresPrompt("textToVideo")).toBe(true);
    expect(videoModeRequiresPrompt("allReference")).toBe(true);
  });

  it("首帧 / 图片参考 / 首尾帧 / 视频编辑允许空提示词", () => {
    for (const mode of [
      "firstFrame",
      "imageToVideo",
      "imageReference",
      "firstLastFrame",
      "videoEdit",
    ] as VideoGenMode[]) {
      expect(videoModeRequiresPrompt(mode)).toBe(false);
    }
  });
});

describe("videoModeRequiresMedia — 该模式是否必须有上游素材", () => {
  it("文生视频是唯一不需要素材的模式", () => {
    expect(videoModeRequiresMedia("textToVideo")).toBe(false);
  });

  it("全能参考同样需要素材（omni 端点没素材就发不出去）", () => {
    // 与 videoModeRequiresPrompt 是**并列**关系而非二选一：全能参考两条都要。
    // 只看提示词的话，素材撤空后按钮仍然可点，点了却被 handleSubmit 的
    // references.length === 0 静默拦下 —— 用户看到的就是「点了没反应」。
    expect(videoModeRequiresMedia("allReference")).toBe(true);
    expect(videoModeRequiresPrompt("allReference")).toBe(true);
  });

  it("其余生成模式都要素材", () => {
    for (const mode of [
      "firstFrame",
      "imageToVideo",
      "imageReference",
      "firstLastFrame",
      "videoEdit",
    ] as VideoGenMode[]) {
      expect(videoModeRequiresMedia(mode)).toBe(true);
    }
  });
});

describe("videoNoUpstreamResetMode — 素材撤空后退回文生视频", () => {
  const EMPTY = { images: 0, videos: 0, audios: 0 };

  it("上游清空后，任何素材模式都退回文生视频", () => {
    for (const mode of [
      "allReference",
      "imageReference",
      "firstFrame",
      "imageToVideo",
      "firstLastFrame",
      "videoEdit",
    ] as VideoGenMode[]) {
      expect(videoNoUpstreamResetMode(mode, EMPTY)).toBe("textToVideo");
    }
  });

  it("已经是文生视频就不动（避免每帧都发一次 patch）", () => {
    expect(videoNoUpstreamResetMode("textToVideo", EMPTY)).toBeNull();
  });

  it.each([
    ["图片", { images: 1, videos: 0, audios: 0 }],
    ["视频", { images: 0, videos: 1, audios: 0 }],
    ["音频", { images: 0, videos: 0, audios: 1 }],
  ] as const)("上游还有%s素材时不动", (_label, counts) => {
    expect(videoNoUpstreamResetMode("allReference", counts)).toBeNull();
  });
});

describe("VideoNode 接线：素材撤空 → 文生视频", () => {
  const source = readFileSync("src/features/canvas/nodes/VideoNode.tsx", "utf8");

  it("反向复位 effect 按节点类型口径判定，不用已解析 URL 口径", () => {
    // upstreamCounts（已解析 URL）会把空态 CTA 刚铺好、还没出图的图片节点算成 0 张，
    // 用它当场就会把三个 CTA 顶回文生视频。
    expect(source).toContain(
      "videoNoUpstreamResetMode(genMode, upstreamTypeCounts)",
    );
  });

  it("复位不进撤销栈", () => {
    // 这是「用户删素材」的衍生结果而非独立改动；记进 past 会让 ⌘Z 被本 effect 立刻
    // 撤销回去、redo 栈还被清空，等于把「回到连线之前」这条路堵死。
    expect(source).toContain(
      "updateNodeData(id, { genMode: target }, { recordHistory: false });",
    );
  });

  it("提交闸门把提示词与素材拆成两条并列判定", () => {
    expect(source).toContain(
      "(videoModeRequiresPrompt(genMode) && !hasPromptText) ||\n      (videoModeRequiresMedia(genMode) && !hasRequiredMediaForMode);",
    );
    // 旧的三元写法：要提示词的模式就不再看素材 —— 全能参考因此漏网。
    expect(source).not.toContain(
      "videoModeRequiresPrompt(genMode)\n        ? !hasPromptText\n        : !hasRequiredMediaForMode",
    );
  });
});
