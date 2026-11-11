"use client";

import { useEffect, useMemo, useState } from "react";
import { requestJson } from "@/lib/http";
import type { ModelOption } from "@/lib/agent-meta";

/** Quanto esperar a lista de um provedor antes de cair na lista fixa dele. */
export const MODELS_TIMEOUT_MS = 10_000;

/** A promessa, ou um erro de "tempo esgotado" se ela não terminar em `ms`. */
function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("tempo esgotado")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

interface ProviderModels {
  provider: string;
  models: { id: string; label: string; created?: number | null; channel?: ModelOption["channel"] }[];
}

/**
 * Os modelos que cada provedor oferece **agora** (`GET /model-providers/{p}/models`),
 * em vez da lista fixa do `agent-meta.ts` — modelos saem do ar e entram novos o
 * tempo todo. Por provedor: se a API dele não responder (chave sem permissão,
 * rede) ou passar de `timeoutMs`, fica a lista fixa daquele provedor, e o motivo vem em `errors`
 * — o carregamento nunca fica pendurado.
 */
export function useProviderModels(providers: string[], fallback: ModelOption[], timeoutMs = MODELS_TIMEOUT_MS) {
  const key = [...providers].sort().join(",");
  const [fetched, setFetched] = useState<Record<string, ModelOption[]>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const list = key ? key.split(",") : [];
    Promise.allSettled(
      list.map((provider) =>
        withTimeout(
          requestJson<ProviderModels>(`/api/model-providers/${encodeURIComponent(provider)}/models`, {
            fallbackError: "Falha ao listar modelos",
          }),
          timeoutMs
        )
      )
    ).then((results) => {
      if (cancelled) return;
      const ok: Record<string, ModelOption[]> = {};
      const failed: Record<string, string> = {};
      results.forEach((result, index) => {
        const provider = list[index];
        if (result.status === "fulfilled" && result.value.models.length > 0) {
          ok[provider] = result.value.models.map((m) => ({ provider, id: m.id, label: m.label, channel: m.channel }));
        } else if (result.status === "rejected") {
          failed[provider] = result.reason instanceof Error ? result.reason.message : String(result.reason);
        }
      });
      setFetched(ok);
      setErrors(failed);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [key, timeoutMs]);

  const models = useMemo(() => {
    const list = key ? key.split(",") : [];
    return list.flatMap((provider) => fetched[provider] ?? fallback.filter((m) => m.provider === provider));
  }, [key, fetched, fallback]);

  return { models, errors, loading, live: Object.keys(fetched) };
}
