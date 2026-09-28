// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import { useVideoBackends } from "@/lib/queries/video";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("video backend options query", () => {
  it("loads canonical backend options from the backend", async () => {
    let requestedPath = "";
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/video-backends", ({ request }) => {
        requestedPath = new URL(request.url).pathname;
        return HttpResponse.json({
          ok: true,
          data: [
            {
              value: "higgsfield:seedance_2_0?mode=fast",
              label: "Seedance 2.0 Fast",
              is_default: true,
              is_seedance2: true,
              supported_modes: ["text_to_video", "first_frame", "multimodal_reference"],
              resolution_options: ["480p", "720p"],
              supports_audio: true,
            },
            {
              value: "h3c",
              label: "h3.c (local)",
              is_default: false,
              is_seedance2: false,
              supported_modes: ["text_to_video", "first_frame", "first_last_frame"],
              supports_audio: true,
            },
          ],
        });
      }),
    );

    const { result } = renderHook(() => useVideoBackends("demo"), {
      wrapper,
    });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(requestedPath).toBe("/api/v1/projects/demo/video-backends");
    expect(result.current.data?.data[0]?.value).toBe(
      "higgsfield:seedance_2_0?mode=fast",
    );
    expect(result.current.data?.data[1]?.supported_modes).toContain("first_last_frame");
  });
});
