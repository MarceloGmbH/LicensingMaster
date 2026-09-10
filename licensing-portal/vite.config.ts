import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy /admin to a local licensing-master (run it with `--port 8010` and
// APP_ENV=development). The proxy injects X-Dev-Admin-Email so admin_identity's
// dev fallback lets the portal in without Cloudflare Access. Prod: same origin,
// no proxy, Access supplies the identity.
const DEV_ADMIN_EMAIL = process.env.LM_DEV_ADMIN_EMAIL ?? "dev@alanadev.com";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5190,
    proxy: {
      "/admin": {
        target: "http://localhost:8010",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            proxyReq.setHeader("X-Dev-Admin-Email", DEV_ADMIN_EMAIL);
          });
        },
      },
    },
  },
  build: { outDir: "dist" },
});
