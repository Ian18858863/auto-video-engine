import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

const backendUrl = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8000";
const frontendPort = Number(process.env.VITE_FRONTEND_PORT || 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: frontendPort,
    proxy: {
      "/api": backendUrl,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    exclude: [...configDefaults.exclude, "e2e/**"],
    css: true,
    fileParallelism: false,
    maxWorkers: 1,
    minWorkers: 1,
    testTimeout: 15_000,
  },
});
