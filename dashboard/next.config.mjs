/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: {
    NERYA_API: process.env.NERYA_API || "http://127.0.0.1:18317",
  },
  experimental: {
    typedRoutes: false,
    serverComponentsExternalPackages: ["undici"],
  },
};

export default nextConfig;
