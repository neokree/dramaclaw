// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = process.cwd();
const read = (path: string) => readFileSync(resolve(root, path), "utf8");

describe("video backend options alignment", () => {
  it("does not hardcode the VideoPane backend list", () => {
    const videoPane = read("src/components/episode/beat-workbench/video-pane.tsx");

    expect(videoPane).not.toContain("const VIDEO_BACKENDS");
    expect(videoPane).toContain("useVideoBackends");
  });

  it("reads capabilities from the backend option, not model names", () => {
    const videoPane = read("src/components/episode/beat-workbench/video-pane.tsx");
    const videoQueries = read("src/lib/queries/video.ts");

    for (const field of ["supported_modes", "resolution_options", "ratio_options"]) {
      expect(videoPane).toContain(field);
    }
    for (const retired of ["dialogue_only", "is_grok_video", "is_happyhorse"]) {
      expect(videoQueries).not.toContain(retired);
      expect(videoPane).not.toContain(retired);
    }
    expect(videoPane).not.toMatch(/newapi_|huimeng/);
  });

  it("defaults to the Higgsfield Seedance 2.0 fast backend", () => {
    const beatsRoute = read("src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx");
    const videoQueries = read("src/lib/queries/video.ts");

    expect(beatsRoute).toContain("DEFAULT_VIDEO_BACKEND");
    expect(videoQueries).toContain('"higgsfield:seedance_2_0?mode=fast"');
  });
});
