import { defineConfig } from "vitest/config";
import { resolve } from "path";

export default defineConfig({
  // Repo root so `web/tests/**` includes match when `npm run test` runs from `web/`.
  root: resolve(__dirname),
  resolve: {
    alias: {
      "@tools": resolve(__dirname, "lib/tools"),
      "@/lib": resolve(__dirname, "web/lib"),
      "@": resolve(__dirname, "web"),
    },
  },
  test: {
    include: [
      "lib/tools/__tests__/**/*.test.ts",
      "site/lib/**/*.test.ts",
      "web/tests/**/*.test.ts",
      "web/tests/**/*.test.tsx",
    ],
    environment: "node",
    coverage: {
      provider: "v8",
      include: [
        "site/app/**/*.ts",
        "site/lib/**/*.ts",
        "web/lib/**/*.ts",
        "web/app/api/**/*.ts",
        "lib/tools/**/*.ts",
      ],
      exclude: [
        "**/*.test.ts",
        "**/node_modules/**",
        // Hooks that require Next.js navigation context (can't run in node or jsdom)
        "web/lib/perfTracker.ts",
        // React context providers (need full component tree)
        "web/lib/OrderActionsContext.tsx",
        "web/lib/TickerDetailContext.tsx",
        "web/lib/accountContext.ts",
        // Pure type definitions
        "web/lib/types.ts",
        "web/lib/orderModify.ts",
        // PI framework
        "lib/tools/pi-tools.ts",
        "lib/tools/schemas/index.ts",
        "lib/tools/wrappers/index.ts",
        "lib/tools/wrappers/fetch-ticker.ts",
        "lib/tools/wrappers/ib-order-manage.ts",
        "lib/tools/wrappers/ib-orders.ts",
        "lib/tools/wrappers/ib-sync.ts",
        "lib/tools/wrappers/scanner.ts",
        // Routes that spawn subprocesses or need live services
        "web/app/api/pi/**",
        "web/app/api/prices/**",
        "web/app/api/blotter/**",
        "web/app/api/discover/**",
        "web/app/api/flow-analysis/**",
        "web/app/api/scanner/**",
      ],
      thresholds: {
        lines: 80,
        functions: 75,
        branches: 70,
      },
    },
  },
});
