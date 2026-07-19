import { expect, test } from "@playwright/test";

const basePackage = {
  pipeline_id: 8,
  status: "已生成待确认",
  project_name: "局部重建验收",
  project_key: "p-8-partial-rebuild",
  project_dir: "项目/局部重建验收",
  legacy_project_dir: null,
  timeline: [
    {
      segment_id: "shot-1",
      start: "00:00",
      end: "00:05",
      voiceover: "今天先看第一条消息。",
      visual_type: "素材画面",
      visual_direction: "科技新闻画面",
      material_keywords: ["AI"],
      material_sources: ["本地素材库"],
      material_status: "已锁定",
      material_path: "素材/shot-1.mp4",
      needs_graphic: false,
      graphic_task: {},
      avatar_task: {},
      subtitle_strategy: "跟随口播",
      edit_instruction: "开头节奏清晰。",
    },
  ],
  versions: [
    {
      version_id: "main",
      name: "默认成片方案",
      status: "待最终确认",
      estimated_duration_seconds: 5,
      strategy: "只重建改动镜头。",
      best_for: "局部重建验收。",
    },
  ],
  recommended_template: {
    version_id: "main",
    name: "默认成片方案",
    status: "待最终确认",
    estimated_duration_seconds: 5,
    strategy: "只重建改动镜头。",
    best_for: "局部重建验收。",
  },
  export_task: {
    pipeline_id: 8,
    status: "待最终确认",
    need_final_confirmation: true,
    version_count: 1,
    timeline_version_id: "ts-8-0001-demo",
    timeline_version_number: 1,
    material_ready_count: 1,
    timeline_segment_count: 1,
    real_jianying_automation: false,
    real_avatar_generation: false,
    real_publish_executed: false,
  },
  generated_files: {},
  timeline_spec: {
    status: "已保存",
    version_id: "ts-8-0001-demo",
    version_number: 1,
    segments: [],
    tracks: [],
    settings: { width: 1080, height: 1920 },
  },
  subtitle_preview: "今天先看第一条消息。",
  note: "等待变更时间线。",
};

function packageFor(status: "stale" | "current") {
  const current = status === "current";
  return {
    ...basePackage,
    export_task: {
      ...basePackage.export_task,
      timeline_version_id: current ? "ts-8-0002-demo" : "ts-8-0001-demo",
      timeline_version_number: current ? 2 : 1,
    },
    timeline_spec: {
      ...basePackage.timeline_spec,
      version_id: current ? "ts-8-0002-demo" : "ts-8-0001-demo",
      version_number: current ? 2 : 1,
    },
    timeline_sync: {
      status,
      current_version_id: current ? "ts-8-0002-demo" : "ts-8-0001-demo",
      previous_version_id: "ts-8-0001-demo",
      desired_input_hash: "d".repeat(64),
      affected_segment_ids: ["shot-1"],
      created_new_version: current,
    },
  };
}

test("八模块中的剪辑页能安全完成局部重建状态切换", async ({ page }, testInfo) => {
  let timelineStatus: "stale" | "current" = "stale";
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/pipeline/8/editing-export" && request.method() === "GET") {
      await route.fulfill({ json: packageFor(timelineStatus) });
      return;
    }
    if (
      path === "/api/pipeline/8/editing-export/rebuild" &&
      request.method() === "POST"
    ) {
      timelineStatus = "current";
      await route.fulfill({ json: packageFor("current") });
      return;
    }
    if (path === "/api/pipeline/8/quality-report/candidates") {
      await route.fulfill({ json: { pipeline_id: 8, items: [], publish_supported: false } });
      return;
    }
    if (path === "/api/editing-export/end-to-end-test/latest") {
      await route.fulfill({ json: { 成功: false, 提示: "", 导出文件: {}, 素材匹配: [] } });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: `unhandled ${path}` } });
  });

  await page.goto("/editing/8");
  await expect(page.getByRole("heading", { name: "剪辑" })).toBeVisible();
  for (const label of ["任务", "选题", "脚本", "素材", "剪辑", "成片", "复盘", "设置"]) {
    const moduleButton = page.getByRole("button", { name: label, exact: true });
    await expect(moduleButton).toBeVisible();
    const bounds = await moduleButton.evaluate((button) => {
      const rect = button.getBoundingClientRect();
      return { left: rect.left, right: rect.right, viewportWidth: window.innerWidth };
    });
    expect(bounds.left, `${label} 模块左侧不应藏到屏幕外`).toBeGreaterThanOrEqual(-1);
    expect(bounds.right, `${label} 模块右侧不应藏到屏幕外`).toBeLessThanOrEqual(
      bounds.viewportWidth + 1,
    );
  }
  await expect(page.getByText("待生成新时间线：shot-1")).toBeVisible();
  await expect(page.getByRole("button", { name: "生成现代媒体草稿" })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("before-rebuild.png"), fullPage: true });

  const rebuildButton = page.getByRole("button", { name: "生成变更时间线" });
  await rebuildButton.scrollIntoViewIfNeeded();
  const hitTest = await rebuildButton.evaluate((button) => {
    const rect = button.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const hits = document.elementsFromPoint(x, y).slice(0, 6);
    return {
      scrollY: window.scrollY,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      button: {
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
      },
      targetReceivesPointer: hits[0] === button || button.contains(hits[0]),
      hits: hits.map((element) => {
        const itemRect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          tag: element.tagName,
          className: element.className,
          text: element.textContent?.trim().slice(0, 80),
          position: style.position,
          zIndex: style.zIndex,
          pointerEvents: style.pointerEvents,
          top: itemRect.top,
          bottom: itemRect.bottom,
        };
      }),
    };
  });
  expect(hitTest.targetReceivesPointer, JSON.stringify(hitTest, null, 2)).toBe(true);
  await rebuildButton.click();
  await expect(
    page.getByText("已生成新时间线 ts-8-0002-demo；受影响镜头：shot-1。"),
  ).toBeVisible();
  await expect(page.getByText("当前版本：ts-8-0002-demo")).toBeVisible();
  await expect(page.getByRole("button", { name: "生成现代媒体草稿" })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("after-rebuild.png"), fullPage: true });

  const layout = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    htmlWidth: document.documentElement.scrollWidth,
    htmlClientWidth: document.documentElement.clientWidth,
    viewportWidth: window.innerWidth,
    scrollX: window.scrollX,
    containers: ["body", ".app-layout", ".app-sidebar", ".app-navigation", ".app-main", ".app-shell"].map(
      (selector) => {
        const element = document.querySelector(selector) as HTMLElement;
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          selector,
          left: rect.left,
          right: rect.right,
          width: rect.width,
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
          overflowX: style.overflowX,
          minWidth: style.minWidth,
          maxWidth: style.maxWidth,
          paddingLeft: style.paddingLeft,
          paddingRight: style.paddingRight,
        };
      },
    ),
    appShellOverflowing: (() => {
      const shell = document.querySelector(".app-shell") as HTMLElement;
      const shellRect = shell.getBoundingClientRect();
      return Array.from(shell.querySelectorAll("*"))
        .map((element) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return {
            tag: element.tagName,
            className: element.className,
            text: element.textContent?.trim().slice(0, 80),
            left: rect.left,
            right: rect.right,
            width: rect.width,
            scrollWidth: (element as HTMLElement).scrollWidth,
            minWidth: style.minWidth,
            whiteSpace: style.whiteSpace,
          };
        })
        .filter((item) => item.right > shellRect.right + 1 || item.width > shellRect.width + 1)
        .slice(0, 30);
    })(),
    overflowing: Array.from(document.querySelectorAll("body *"))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          className: element.className,
          text: element.textContent?.trim().slice(0, 60),
          left: rect.left,
          right: rect.right,
          width: rect.width,
          scrollWidth: (element as HTMLElement).scrollWidth,
        };
      })
      .filter((item) => item.right > window.innerWidth + 1 || item.width > window.innerWidth + 1)
      .slice(0, 20),
    overlay: Boolean(
      document.querySelector(
        "[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay",
      ),
    ),
    hasContent: document.body.innerText.trim().length > 0,
  }));
  expect(layout.hasContent).toBe(true);
  expect(layout.overlay).toBe(false);
  expect(layout.bodyWidth, JSON.stringify(layout, null, 2)).toBeLessThanOrEqual(
    layout.viewportWidth + 1,
  );
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
