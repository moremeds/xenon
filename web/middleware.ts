import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

// Default-private with an explicit public allowlist. New routes inherit auth
// automatically — opt out by adding to PUBLIC_ROUTES below. This fail-closed
// default is required by the project's universal-auth-gating policy: per-flow
// gates have leaked private surface in two prior regressions.
//
// API routes are public at the middleware layer because server-side page
// fetches don't carry Clerk session cookies. FastAPI's Clerk JWT middleware
// still gates the actual data calls — middleware controls page reachability,
// FastAPI controls data access.
export const PUBLIC_ROUTES = ["/sign-in(.*)", "/sign-up(.*)", "/api/(.*)"];

const isPublicRoute = createRouteMatcher(PUBLIC_ROUTES);

// Auth can be bypassed for local dev / E2E by setting XENON_DISABLE_AUTH=1.
// PLAYWRIGHT_DISABLE_AUTH is the legacy name (still honored).
export default clerkMiddleware(async (auth, request) => {
  const authBypassEnabled =
    process.env.XENON_DISABLE_AUTH === "1" ||
    process.env.PLAYWRIGHT_DISABLE_AUTH === "1";
  if (!authBypassEnabled && !isPublicRoute(request)) {
    await auth.protect();
  }
});

export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/(api|trpc)(.*)",
  ],
};
