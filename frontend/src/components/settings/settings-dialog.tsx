// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";
import { Loader2, RotateCw } from "lucide-react";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import {
  useEnginesStatus,
  type EngineStatus,
  type EnginesStatus,
} from "@/lib/queries/model-gateway";

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

type EngineId = keyof EnginesStatus["engines"];

// i18n-exempt: product names.
const ENGINE_LABELS: Record<EngineId, string> = {
  higgsfield: "Higgsfield",
  h3c: "h3.c",
  drawthings: "Draw Things",
  mtplx: "MTPLX",
  openrouter: "OpenRouter",
};

const ENGINE_ORDER: EngineId[] = [
  "higgsfield",
  "h3c",
  "drawthings",
  "mtplx",
  "openrouter",
];

export function SettingsDialog({ open, onOpenChange }: SettingsDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[min(82vh,760px)] max-w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-lg border border-border bg-black p-0 ring-0 sm:max-w-[640px]">
        <DialogHeader className="border-b border-border px-5 py-4">
          <DialogTitle>{t("settings.title")}</DialogTitle>
          <DialogDescription className="sr-only">
            {t("settings.engines.description")}
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="min-h-0 flex-1">
          <EnginesSection open={open} />
        </ScrollArea>
        <div className="flex justify-end border-t border-border px-5 py-3.5">
          <DialogClose render={<Button variant="outline" size="sm" />}>
            {t("settings.close")}
          </DialogClose>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function EnginesSection({ open }: { open: boolean }) {
  const { t } = useTranslation();
  const query = useEnginesStatus(open);
  const status = query.data;

  return (
    <section className="px-5 py-5">
      <div className="flex items-center gap-2">
        <h3 className="font-heading text-sm font-medium text-foreground">
          {t("settings.engines.title")}
        </h3>
        {status ? (
          <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
            {t("settings.engines.textEngine", {
              engine: ENGINE_LABELS[status.textEngine as EngineId] ?? status.textEngine,
            })}
          </span>
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="ml-auto"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          aria-label={t("settings.engines.refresh")}
          title={t("settings.engines.refresh")}
        >
          <RotateCw
            className={cn("size-3.5", query.isFetching && "animate-spin")}
            aria-hidden
          />
        </Button>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        {t("settings.engines.description")}
      </p>

      {query.isLoading ? (
        <div className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          {t("settings.engines.loading")}
        </div>
      ) : query.isError || !status ? (
        <p className="mt-4 text-xs text-destructive">
          {t("settings.engines.loadFailed")}
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-border rounded-md border border-border">
          {ENGINE_ORDER.map((id) => {
            const engine = status.engines[id];
            // OpenRouter is optional: the backend omits it when not configured.
            if (!engine) return null;
            return <EngineRow key={id} id={id} engine={engine} />;
          })}
        </ul>
      )}
    </section>
  );
}

function EngineRow({ id, engine }: { id: EngineId; engine: EngineStatus }) {
  const { t } = useTranslation();
  const detail = !engine.available
    ? engine.reason
    : id === "higgsfield" && typeof engine.credits === "number"
      ? t("settings.engines.credits", { credits: engine.credits })
      : id === "mtplx"
        ? t(engine.running ? "settings.engines.running" : "settings.engines.onDemand")
        : "";
  return (
    <li className="flex items-start gap-3 px-3 py-2.5" data-engine={id}>
      <div className="min-w-0 flex-1">
        <div className="text-sm text-foreground">{ENGINE_LABELS[id]}</div>
        <div className="text-[11px] text-muted-foreground">
          {t(`settings.engines.roles.${id}`)}
        </div>
        {detail ? (
          <div className="mt-1 text-[11px] break-words text-muted-foreground">
            {detail}
          </div>
        ) : null}
      </div>
      <span
        className={cn(
          "shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium",
          engine.available ? "text-success" : "text-destructive",
        )}
      >
        {t(engine.available ? "settings.engines.available" : "settings.engines.unavailable")}
      </span>
    </li>
  );
}
