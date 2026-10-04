"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Job } from "./types";

export function useFetch<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);

  const reload = useCallback(async () => {
    if (!path) return;
    const my = ++seq.current;
    setLoading(true);
    try {
      const d = await api<T>(path);
      if (my === seq.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (my === seq.current) setError((e as Error).message);
    } finally {
      if (my === seq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);

  useEffect(() => {
    setData(null);
    void reload();
  }, [reload]);

  return { data, error, loading, reload, setData };
}

/** Poll a background job until it finishes. */
export function useJob(onDone?: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cb = useRef(onDone);
  cb.current = onDone;

  const stop = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };

  const track = useCallback((jobId: string) => {
    stop();
    const tick = async () => {
      try {
        const j = await api<Job>(`/jobs/${jobId}`);
        setJob(j);
        if (j.status === "done" || j.status === "failed") {
          cb.current?.(j);
          return;
        }
      } catch {
        /* transient: keep polling */
      }
      timer.current = setTimeout(tick, 1500);
    };
    void tick();
  }, []);

  useEffect(() => stop, []);
  return { job, track, running: !!job && (job.status === "queued" || job.status === "running") };
}

export function storageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function storageSet(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: ignore */
  }
}
