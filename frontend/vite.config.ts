import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react()],
    server: {
      port: 4173,
      proxy: {
        "/api": {
          target: env.VITE_DEV_API_TARGET || "http://127.0.0.1:8090",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "../backend/app/static",
      emptyOutDir: true,
      sourcemap: false,
    },
  };
});
