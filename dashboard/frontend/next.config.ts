
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  experimental: { workerThreads: true, useTypeScriptCli: false },
  allowedDevOrigins: ["172.19.160.1"],
};

export default nextConfig;
