// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";
import type { OkResponse } from "@/types/api";

export interface EngineStatus {
  available: boolean;
  reason?: string;
  /** Higgsfield only: credits left on the account (null when unknown). */
  credits?: number | null;
  /** MTPLX only: a server is already serving the model. */
  running?: boolean;
}

/** GET /model-gateway/engines — read-only reachability of every engine. */
export interface EnginesStatus {
  textEngine: string;
  engines: Partial<
    Record<"higgsfield" | "h3c" | "drawthings" | "mtplx" | "openrouter", EngineStatus>
  >;
}

export function useEnginesStatus(enabled = true) {
  return useQuery({
    queryKey: queryKeys.modelGatewayEngines(),
    queryFn: ({ signal }) =>
      api
        .get("api/v1/model-gateway/engines", { signal })
        .json<OkResponse<EnginesStatus>>()
        .then((response) => response.data),
    enabled,
    staleTime: 30_000,
  });
}
