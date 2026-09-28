// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const nodeSource = readFileSync(
  "src/features/canvas/nodes/VideoNode.tsx",
  "utf8",
);
const capabilitiesSource = readFileSync(
  "src/features/canvas/nodes/shared/videoModelCapabilities.ts",
  "utf8",
);

/**
 * 视频能力只看媒体目录条目（supportedModes / referenceAudioMax …），不按模型名猜：
 * 目录 id 是 `higgsfield:<ref>` / `h3c` 这类引擎选择，名字里推不出能力。
 */
describe("video capabilities come from the catalog, not model-name matching", () => {
  it("VideoNode 不再做模型家族判定", () => {
    expect(nodeSource).not.toMatch(/isSeedance\w*VideoModel|isHappyHorse|isGrok/);
    expect(capabilitiesSource).not.toMatch(/isSeedance\w*VideoModel|isHappyHorse|isGrok/);
  });

  it("modelId 只作为 catalogId 的兜底出现在提交参数里", () => {
    expect(nodeSource).toContain("selectedVideoModel?.catalogId ?? modelId");
  });
});
