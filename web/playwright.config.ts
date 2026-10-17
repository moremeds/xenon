import { defineConfig, devices } from "@playwright/test";

const PORT = process.env.PLAYWRIGHT_PORT
  ? Number(process.env.PLAYWRIGHT_PORT)
  : 3200;

// Specs that need a FastAPI stand-in host an in-process mock on this fixed
// port (e2e/order-cancel-error-propagation.spec.ts binds it). Next's /api/*
// routes proxy to XENON_API_URL, so pointing it here makes upstream error
// statuses propagate to the client. When no spec owns the port the upstream
// is simply unreachable — same failure mode as no FastAPI. workers=1 keeps
// the single port collision-free.
const MOCK_BACKEND_PORT = 8325;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Start Next.js dev server before tests. Default to paper mode so E2E
  // tests never accidentally talk to a live IB account.
  webServer: {
    command: `XENON_DISABLE_AUTH=1 XENON_TRADING_MODE=paper ASSISTANT_MOCK=1 XENON_API_URL=http://127.0.0.1:${MOCK_BACKEND_PORT} npx next dev --turbopack -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
