import { readFileSync } from "fs";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// Source of truth for the app version is the root VERSION file (kept in sync
// with the root package.json by scripts/release/version_sync_check.py). The
// web/ package.json version is independent and not the release version.
function readAppVersion() {
  try {
    return readFileSync(resolve(__dirname, "..", "VERSION"), "utf8").trim();
  } catch {
    return "";
  }
}

/** Baseline security headers for all routes. HSTS only when explicitly safe (see below). */
function securityHeaders() {
  const headers = [
    { key: "X-Frame-Options", value: "DENY" },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    {
      key: "Permissions-Policy",
      value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    },
  ];
  // Avoid HSTS on local `next start` (can pin broken HTTPS on localhost). Vercel sets VERCEL=1.
  if (process.env.VERCEL === "1" || process.env.XENON_ENABLE_HSTS === "1") {
    headers.push({
      key: "Strict-Transport-Security",
      value: "max-age=31536000; includeSubDomains; preload",
    });
  }
  return headers;
}

const config = {
  output: "standalone",
  outputFileTracingRoot: resolve(__dirname, ".."),
  // Inlined into the client bundle at build time so the sidebar can show the
  // shipped release version.
  env: {
    NEXT_PUBLIC_APP_VERSION: readAppVersion(),
  },
  turbopack: {},
  webpack: (config) => {
    config.resolve.alias["@tools"] = resolve(__dirname, "..", "lib", "tools");
    return config;
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders(),
      },
    ];
  },
};

export default config;
