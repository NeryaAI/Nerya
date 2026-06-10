/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // NOTE: Do NOT put NERYA_API in the `env` block — Next.js inlines those
  // values at build time, so a stale port gets baked into the compiled
  // proxy route and survives `next dev` restarts.  The proxy route now
  // reads process.env.NERYA_API at request time via a helper function
  // to avoid build-time inlining.
  env: {},
  experimental: {
    typedRoutes: false,
  },
  // During E2E we frequently restart/clean and run alongside Playwright; the
  // webpack *filesystem* cache then races on `.next/cache/**/*.pack.gz` renames
  // on Windows (EPERM/ENOENT), which corrupts the build and makes proxy routes
  // 500 (see tests/e2e/notes.md RC2). Disabling the FS cache under NERYA_E2E=1
  // trades a little cold-compile time for a build that can't self-corrupt.
  webpack: (config, { dev }) => {
    if (dev && process.env.NERYA_E2E === "1") {
      config.cache = false;
    }
    return config;
  },
};

export default nextConfig;
