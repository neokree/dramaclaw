// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type {
  ImageModelDefinition,
  ImageModelRuntimeContext,
  ModelProviderDefinition,
  ResolutionOption,
} from './types';

const providerModules = import.meta.glob<{ provider: ModelProviderDefinition }>(
  './providers/*.ts',
  { eager: true }
);

const SUPERTALE_PROVIDER_IDS = new Set(['drawthings', 'higgsfield', 'openrouter']);

const providers: ModelProviderDefinition[] = Object.values(providerModules)
  .map((module) => module.provider)
  .filter((provider): provider is ModelProviderDefinition => Boolean(provider))
  .filter((provider) => SUPERTALE_PROVIDER_IDS.has(provider.id))
  .sort((a, b) => a.id.localeCompare(b.id));

const providerMap = new Map<string, ModelProviderDefinition>(
  providers.map((provider) => [provider.id, provider])
);

// 图片模型清单本身来自后台「媒体模型」目录（`/freezone/image/models`），前端不再
// 维护静态注册表；见 `domain/catalogImageModels.ts`。这里只留供应商的展示名 ——
// 目录只下发 providerId，没有展示名。
// 与后端默认图片选择一致；老画布节点上已下线的 id 由 `useCatalogImageModels` 回落到它。
export const DEFAULT_IMAGE_MODEL_ID = 'higgsfield:nano_banana_flash';

export function listModelProviders(): ModelProviderDefinition[] {
  return providers;
}

export function resolveImageModelResolutions(
  model: ImageModelDefinition,
  context: ImageModelRuntimeContext = {}
): ResolutionOption[] {
  const resolvedOptions = model.resolveResolutions?.(context);
  return resolvedOptions && resolvedOptions.length > 0 ? resolvedOptions : model.resolutions;
}

export function resolveImageModelResolution(
  model: ImageModelDefinition,
  requestedResolution: string | undefined,
  context: ImageModelRuntimeContext = {}
): ResolutionOption {
  const resolutionOptions = resolveImageModelResolutions(model, context);

  return (
    (requestedResolution
      ? resolutionOptions.find(
          (item) => item.value.toLowerCase() === requestedResolution.toLowerCase(),
        )
      : undefined) ??
    resolutionOptions.find(
      (item) => item.value.toLowerCase() === model.defaultResolution.toLowerCase(),
    ) ??
    resolutionOptions[0] ??
    model.resolutions[0]
  );
}

export function getModelProvider(providerId: string): ModelProviderDefinition {
  return (
    providerMap.get(providerId) ?? {
      id: providerId || 'unknown',
      // 后台新配了一个前端还不认识的供应商时，直接把 id 当展示名，
      // 而不是显示 "Unknown Provider"。
      name: providerId || 'Unknown Provider',
      label: providerId || 'Unknown',
    }
  );
}
