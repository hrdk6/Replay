"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

export type ApiState<T> = {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
};

type Result<T> = { path: string | null; data?: T; error?: unknown };

/**
 * Fetch `path` (null = skip). With `refreshMs`, re-fetch on an interval while the tab is
 * visible and `poll(data)` returns true (e.g. "experiment still running").
 */
export function useApi<T>(
  path: string | null,
  options: { refreshMs?: number; poll?: (data: T | undefined) => boolean } = {},
): ApiState<T> {
  const [result, setResult] = useState<Result<T>>({ path: null });
  const pollRef = useRef(options.poll);
  const dataRef = useRef<T | undefined>(undefined);

  useEffect(() => {
    pollRef.current = options.poll;
  });

  const load = useCallback(async () => {
    if (path === null) return;
    try {
      const data = await api<T>(path);
      dataRef.current = data;
      setResult({ path, data });
    } catch (error) {
      setResult((prev) => ({ path, data: prev.path === path ? prev.data : undefined, error }));
    }
  }, [path]);

  useEffect(() => {
    void load();
  }, [load]);

  const { refreshMs } = options;
  useEffect(() => {
    if (!refreshMs || path === null) return;
    const id = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (pollRef.current && !pollRef.current(dataRef.current)) return;
      void load();
    }, refreshMs);
    return () => window.clearInterval(id);
  }, [refreshMs, load, path]);

  const current = result.path === path;
  return {
    data: current ? result.data : undefined,
    error: current ? result.error : undefined,
    loading: path !== null && !current,
    reload: load,
  };
}
