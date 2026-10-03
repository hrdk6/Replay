import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Cheap gate: pages need a session cookie. The API still validates the session on
// every request; this only avoids rendering app chrome for signed-out visitors.
const PUBLIC = ["/login", "/privacy", "/terms"];

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return NextResponse.next();
  if (request.cookies.get("replay_session")) return NextResponse.next();
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname + search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|icon.svg).*)"],
};
