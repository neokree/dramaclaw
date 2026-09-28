// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options ? `${key}:${JSON.stringify(options)}` : key,
  }),
}));

import { SettingsDialog } from "@/components/settings/settings-dialog";

const server = setupServer();
beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("settings engines section", () => {
  it("shows text engine, Higgsfield credits and unavailable reasons; skips absent OpenRouter", async () => {
    server.use(
      http.get("http://localhost:3000/api/v1/model-gateway/engines", () =>
        HttpResponse.json({
          ok: true,
          data: {
            textEngine: "mtplx",
            engines: {
              higgsfield: { available: true, credits: 42.5, reason: "" },
              h3c: { available: false, reason: "h3.c non trovato" },
              drawthings: { available: true, reason: "" },
              mtplx: { available: true, running: false, reason: "" },
            },
          },
        }),
      ),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SettingsDialog open onOpenChange={() => {}} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByText("Higgsfield")).toBeTruthy());
    expect(screen.getByText('settings.engines.textEngine:{"engine":"MTPLX"}')).toBeTruthy();
    expect(screen.getByText('settings.engines.credits:{"credits":42.5}')).toBeTruthy();
    expect(screen.getByText("h3.c non trovato")).toBeTruthy();
    expect(screen.getByText("settings.engines.onDemand")).toBeTruthy();
    expect(screen.getAllByText("settings.engines.unavailable")).toHaveLength(1);
    expect(screen.queryByText("OpenRouter")).toBeNull();
  });
});
