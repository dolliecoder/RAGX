"use client";

// All calls go through the Next.js proxy (app/api/[...path]); the session cookie
// rides along automatically. Every request carries the CSRF header the API expects.

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public data: Record<string, unknown> | null = null,
  ) {
    super(message);
  }
}

type Init = Omit<RequestInit, "body"> & { json?: unknown; body?: BodyInit };

const PUBLIC_PAGES = ["/login", "/signup", "/forgot", "/reset", "/verify"];

export async function api<T = unknown>(path: string, init: Init = {}): Promise<T> {
  const { json, ...rest } = init;
  const headers = new Headers(rest.headers);
  headers.set("x-ragx-csrf", "1");
  let body = rest.body;
  if (json !== undefined) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(json);
  }
  let res: Response;
  try {
    res = await fetch(path.startsWith("/api") ? path : `/api${path}`, { ...rest, headers, body, cache: "no-store", credentials: "same-origin" });
  } catch {
    throw new ApiError("Network error: check your connection.", 0);
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const obj = (data && typeof data === "object" ? data : null) as Record<string, unknown> | null;
    const detail = obj?.detail;
    const msg = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `HTTP ${res.status}`;
    if (res.status === 401 && typeof window !== "undefined" && window.location.pathname !== "/" && !PUBLIC_PAGES.some((p) => window.location.pathname.startsWith(p)) && !path.startsWith("/auth/")) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.assign(`/login?next=${next}`);
    }
    throw new ApiError(msg, res.status, obj);
  }
  return data as T;
}

export const post = <T = unknown>(path: string, json?: unknown) => api<T>(path, { method: "POST", json: json ?? {} });

// AGPL-3.0 section 13: people using a network deployment can get its source.
export const SOURCE_URL = process.env.NEXT_PUBLIC_RAGX_SOURCE_URL || "https://github.com/dolliecoder/RAGX";
