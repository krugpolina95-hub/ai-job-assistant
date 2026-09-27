import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

import { getServerEnv } from "./lib/env";

const withNextIntl = createNextIntlPlugin("./lib/i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    // Resolved at build time for `next build`; the Docker image bakes in the internal backend URL.
    const { BACKEND_INTERNAL_URL } = getServerEnv();
    return [
      {
        source: "/api/v1/:path*",
        destination: `${BACKEND_INTERNAL_URL}/api/v1/:path*`,
      },
    ];
  },
};

export default withNextIntl(nextConfig);
