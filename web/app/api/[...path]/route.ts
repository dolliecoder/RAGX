import type { NextRequest } from "next/server";

// Server-side proxy to the RAGX API: keeps the API key out of the browser and avoids CORS.
const API = (process.env.RAGX_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = `${API}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  const contentType = req.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  if (process.env.RAGX_API_KEY) headers.set("x-api-key", process.env.RAGX_API_KEY);
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer();
  try {
    const upstream = await fetch(url, { method: req.method, headers, body, cache: "no-store" });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: `RAGX API unreachable at ${API}` }, { status: 502 });
  }
}

export const dynamic = "force-dynamic";
export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
