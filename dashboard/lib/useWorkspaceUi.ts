"use client";

import { useCallback, useEffect, useState } from "react";
import { clientApi } from "./clientApi";
import type {
  WorkspaceUiEnvelope,
  WorkspaceUiManifest,
} from "./workspaceUiTypes";

const EMPTY_MANIFEST: WorkspaceUiManifest = {
  version: 1,
  home: { widgets: [] },
  pages: [],
};

const EMPTY_CATALOG = {
  widget_kinds: [],
};

function normalise(raw: WorkspaceUiEnvelope): WorkspaceUiEnvelope {
  // Older runtime builds wrapped BFF payloads under `data`; accepting both
  // shapes keeps the dashboard useful while the API rolls forward.
  const candidate =
    raw && typeof raw === "object" && raw.data && typeof raw.data === "object"
      ? ({ ...raw, ...(raw.data as Record<string, unknown>) } as WorkspaceUiEnvelope)
      : raw;
  const manifest = candidate?.manifest;
  return {
    ...candidate,
    ok: candidate?.ok !== false,
    manifest: {
      ...EMPTY_MANIFEST,
      ...(manifest && typeof manifest === "object" ? manifest : {}),
      home: {
        ...EMPTY_MANIFEST.home,
        ...(manifest?.home && typeof manifest.home === "object" ? manifest.home : {}),
        widgets: Array.isArray(manifest?.home?.widgets)
          ? manifest.home.widgets
          : [],
      },
      pages: Array.isArray(manifest?.pages) ? manifest.pages : [],
    },
    catalog: {
      ...EMPTY_CATALOG,
      ...(candidate?.catalog && typeof candidate.catalog === "object"
        ? candidate.catalog
        : {}),
      widget_kinds: Array.isArray(candidate?.catalog?.widget_kinds)
        ? candidate.catalog.widget_kinds
        : [],
    },
  };
}

export function useWorkspaceUi(intervalMs = 60_000) {
  const [data, setData] = useState<WorkspaceUiEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await clientApi.workspaceUi();
      setData(normalise(response));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setData((previous) => previous ?? normalise({
        ok: false,
        status: "unavailable",
        manifest: EMPTY_MANIFEST,
        catalog: EMPTY_CATALOG,
        warnings: ["Workspace UI manifest is unavailable."],
      }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    if (!intervalMs) return;
    const timer = window.setInterval(() => void refresh(), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, refresh]);

  return {
    data,
    manifest: data?.manifest ?? EMPTY_MANIFEST,
    catalog: data?.catalog ?? EMPTY_CATALOG,
    loading,
    error,
    refresh,
  };
}

export { EMPTY_MANIFEST };

