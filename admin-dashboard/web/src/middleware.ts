import { type NextRequest, NextResponse } from "next/server";

import { isPublicPage } from "./lib/routes";

// A quick redirect for signed-out visitors; the uptime API still checks every session and workspace.
export function middleware(request: NextRequest) {
  const path = request.nextUrl.pathname;
  const signedIn = request.cookies.has("opsrelay_session");
  if (path === "/" && signedIn) return NextResponse.redirect(new URL("/overview", request.url));
  if (!isPublicPage(path) && !signedIn) return NextResponse.redirect(new URL("/sign-in", request.url));
}

// Node.js, not Edge: Vercel Services rejects Edge output.
export const config = { runtime: "nodejs", matcher: ["/((?!api/|_next/|icon.svg|favicon.ico).*)"] };
