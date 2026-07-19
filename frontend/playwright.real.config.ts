import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.GOLDEN_E2E_FRONTEND_URL;
const artifactRoot = process.env.GOLDEN_E2E_ARTIFACT_ROOT;
const phase = process.env.GOLDEN_E2E_PHASE;

if (!baseURL) throw new Error("GOLDEN_E2E_FRONTEND_URL is required");
if (!artifactRoot || path.parse(path.resolve(artifactRoot)).root.toUpperCase() !== "D:\\") {
  throw new Error("GOLDEN_E2E_ARTIFACT_ROOT must be an absolute D: path");
}
if (phase !== "empty" && phase !== "seed" && phase !== "restart") {
  throw new Error("GOLDEN_E2E_PHASE must be empty, seed or restart");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["**/real-backend-golden.spec.ts"],
  outputDir: path.join(artifactRoot, "results", phase),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ["list"],
    [
      "html",
      {
        outputFolder: path.join(artifactRoot, "report", phase),
        open: "never",
      },
    ],
  ],
  use: {
    baseURL,
    channel: "msedge",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop-edge",
      use: { ...devices["Desktop Edge"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile-375x812-edge",
      use: {
        ...devices["Desktop Edge"],
        viewport: { width: 375, height: 812 },
        hasTouch: true,
      },
    },
  ],
});
