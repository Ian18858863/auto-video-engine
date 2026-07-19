import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["**/real-backend-golden.spec.ts"],
  outputDir: "../artifacts/playwright/results",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { outputFolder: "../artifacts/playwright/report", open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    channel: "msedge",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173/editing/8",
    reuseExistingServer: false,
    timeout: 120_000,
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
