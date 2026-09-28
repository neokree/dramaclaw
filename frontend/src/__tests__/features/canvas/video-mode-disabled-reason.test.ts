// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import { videoModeDisabledReason } from "@/features/canvas/nodes/VideoOperationsPanel";
import { zhT } from "../../helpers/i18n-fixtures";

const NONE = { videos: 0, images: 0, audios: 0 };

/** 目录里声明了 video_edit 的模型。 */
const WITH_VIDEO_EDIT = [
  "text_to_video",
  "first_frame",
  "first_last_frame",
  "all_reference",
  "image_reference",
  "video_edit",
];
/** 没有 video_edit 的全能参考模型。 */
const WITHOUT_VIDEO_EDIT = [
  "text_to_video",
  "first_frame",
  "first_last_frame",
  "all_reference",
  "image_reference",
];
const WITH_VIDEO_EXTEND = [...WITHOUT_VIDEO_EDIT, "video_extend"];

describe("videoModeDisabledReason — 上游接了视频时的模式可用性", () => {
  /**
   * 回归：接了 1 个视频后「视频编辑」被置灰，hover 写着「上游含视频素材时只能用
   * 「全能参考」」。后来目录里的
   * 模型也声明了 video_edit，tab 露出来却被同一条规则连坐。
   */
  it("接 1 个视频时「视频编辑」可选（模型声明了 video_edit）", () => {
    expect(
      videoModeDisabledReason(
        "videoEdit",
        "higgsfield:video_edit_model",
        { ...NONE, videos: 1 },
        zhT,
        WITH_VIDEO_EDIT,
      ),
    ).toBeNull();
  });

  it("接了视频时其余模式仍被拦，且提示里带上「视频编辑」这条出路", () => {
    for (const mode of ["textToVideo", "firstFrame", "firstLastFrame", "imageReference"] as const) {
      expect(
        videoModeDisabledReason(mode, "higgsfield:video_edit_model", { ...NONE, videos: 1 }, zhT, WITH_VIDEO_EDIT),
      ).toBe("上游含视频素材时只能用「全能参考」或「视频编辑」");
    }
    expect(
      videoModeDisabledReason(
        "allReference",
        "higgsfield:video_edit_model",
        { ...NONE, videos: 1 },
        zhT,
        WITH_VIDEO_EDIT,
      ),
    ).toBeNull();
  });

  it("模型没有 video_edit 时提示不提这条出路", () => {
    expect(
      videoModeDisabledReason(
        "textToVideo",
        "higgsfield:seedance_2_0",
        { ...NONE, videos: 1 },
        zhT,
        WITHOUT_VIDEO_EDIT,
      ),
    ).toBe("上游含视频素材时只能用「全能参考」");
  });

  it("没接视频时「视频编辑」提示去连一个", () => {
    expect(
      videoModeDisabledReason("videoEdit", "higgsfield:video_edit_model", NONE, zhT, WITH_VIDEO_EDIT),
    ).toBe("需要连接视频节点（1个）");
  });

  it("接了多个视频时「视频编辑」不可用", () => {
    expect(
      videoModeDisabledReason(
        "videoEdit",
        "higgsfield:video_edit_model",
        { ...NONE, videos: 2 },
        zhT,
        WITH_VIDEO_EDIT,
      ),
    ).toBe("「视频编辑」仅支持连接 1 个视频节点");
  });

  it("模型不支持视频编辑时说清楚是模型的事，不是上游的事", () => {
    expect(
      videoModeDisabledReason(
        "videoEdit",
        "higgsfield:seedance_2_0",
        { ...NONE, videos: 1 },
        zhT,
        WITHOUT_VIDEO_EDIT,
      ),
    ).toBe("该模型不支持「视频编辑」");
  });

  it("视频延长要求模型声明能力且只连接一个源视频", () => {
    expect(
      videoModeDisabledReason(
        "videoExtend",
        "higgsfield:seedance_2_5",
        { ...NONE, videos: 1 },
        zhT,
        WITH_VIDEO_EXTEND,
      ),
    ).toBeNull();
    expect(
      videoModeDisabledReason(
        "videoExtend",
        "higgsfield:seedance_2_5",
        { ...NONE, videos: 1, images: 1 },
        zhT,
        WITH_VIDEO_EXTEND,
      ),
    ).toBe("「视频延长」只接受源视频，请移除图片或音频素材");
  });
});
