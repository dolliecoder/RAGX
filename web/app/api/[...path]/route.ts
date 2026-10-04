import type { NextRequest } from "next/server";

// Same-origin proxy to the RAGX API. People authenticate with their own session
// cookie, which is passed through unchanged; the browser never sees an API key.
const API = (process.env.RAGX_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const FORWARD_REQUEST = ["content-type", "cookie", "x-ragx-csrf", "user-agent", "authorization"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = `${API}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const h of FORWARD_REQUEST) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || req.headers.get("x-real-ip") || "";
  if (ip) headers.set("x-forwarded-for", ip);
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer();
  let upstream: Response;
  try {
    upstream = await fetch(url, { method: req.method, headers, body, cache: "no-store", redirect: "manual" });
  } catch {
    return Response.json({ detail: "The RAGX server is not reachable right now. Please try again in a moment." }, { status: 502 });
  }
  const out = new Headers({ "content-type": upstream.headers.get("content-type") ?? "application/json" });
  for (const c of upstream.headers.getSetCookie()) out.append("set-cookie", c);
  const retry = upstream.headers.get("retry-after");
  if (retry) out.set("retry-after", retry);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const dynamic = "force-dynamic";
export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
