import type { NextRequest } from "next/server";

// Proxies dashboard API calls to FastAPI. The target is read at request time so one
// image can run in staging and production with different API_INTERNAL_URL values.
export const dynamic = "force-dynamic";

const FORWARD_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "cookie",
  "origin",
  "referer",
  "user-agent",
  "x-csrf-token",
  "x-request-id",
  "x-forwarded-for",
];
const DROP_RESPONSE_HEADERS = new Set([
  "connection",
  "keep-alive",
  "transfer-encoding",
  "content-encoding",
  "content-length",
  "set-cookie",
]);

function apiBase(): string {
  // Accepts a full URL or a bare host:port (how some platforms expose private services).
  const raw = (process.env.API_INTERNAL_URL ?? "http://localhost:8000").replace(/\/$/, "");
  return /^https?:\/\//.test(raw) ? raw : `http://${raw}`;
}

async function forward(request: NextRequest, ctx: RouteContext<"/api/[...path]">): Promise<Response> {
  const { path } = await ctx.params;
  const search = new URL(request.url).search;
  const target = `${apiBase()}/api/${path.map(encodeURIComponent).join("/")}${search}`;

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = !["GET", "HEAD"].includes(request.method);
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
      cache: "no-store",
    });
  } catch {
    return Response.json(
      { error: { code: "api_unreachable", message: "The Replay API is not reachable. Check that it is running." } },
      { status: 502 },
    );
  }

  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!DROP_RESPONSE_HEADERS.has(key.toLowerCase())) out.set(key, value);
  });
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { forward as GET, forward as POST, forward as PATCH, forward as PUT, forward as DELETE };
