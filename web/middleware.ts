import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

// API routes are public at the middleware level because server-side page
// fetches don't carry Clerk session cookies. External API access is still
// protected by FastAPI's Clerk JWT auth middleware.
const isPublicRoute = createRouteMatcher([
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/api/(.*)",
]);

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
