import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

const emptyPagination = { page: 1, page_size: 20, total: 0, total_pages: 0 };

describe("全局导航", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/today/tasks") {
          return jsonResponse({
            date: "2026-06-16",
            hotspots: [],
            topic_ideas: [],
            collectible_ideas: [],
            projects: [],
            confirmations: [],
            publish_reminders: [],
            suggestions: [],
            handled_task_ids: [],
          });
        }
        if (url.startsWith("/api/hotspots?")) {
          return jsonResponse({
            items: [],
            pagination: emptyPagination,
            filters: { categories: [], sources: [] },
          });
        }
        if (url.startsWith("/api/topic-ideas?")) {
          return jsonResponse({
            items: [],
            pagination: emptyPagination,
            filters: { hotspots: [] },
          });
        }
        if (url === "/api/automation/sources/status") {
          return jsonResponse({
            items: [],
            source_count: 0,
            manual_import_is_fallback: true,
            real_ai_called: false,
          });
        }
        if (url.startsWith("/api/pipeline?")) {
          return jsonResponse({ items: [], pagination: emptyPagination });
        }
        if (url.startsWith("/api/video-posts?")) {
          return jsonResponse({
            items: [],
            pagination: emptyPagination,
            filters: { platforms: [], projects: [], statuses: [] },
          });
        }
        if (url.startsWith("/api/reviews?")) {
          return jsonResponse({
            items: [],
            pagination: emptyPagination,
            filters: { platforms: [], projects: [], judgements: [] },
          });
        }
        if (url === "/api/account-style") {
          return jsonResponse(defaultAccountStyle());
        }
    if (url.startsWith("/api/script-engine?")) {
          return jsonResponse({
            items: [],
            pagination: emptyPagination,
            filters: {
              sample_types: [],
              scenes: [],
              content_directions: [],
              tags: [],
            },
            reference_rule: "只学习表达方式和结构，不直接复制原文。",
            style_priority: "账号风格库优先；样本库只做参考。",
            real_ai_called: false,
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("显示 8 个主导航入口，并默认打开任务", async () => {
    render(<App />);

    const nav = screen.getByRole("navigation", { name: "主导航" });
    for (const label of [
      "任务",
      "选题",
      "脚本",
      "素材",
      "剪辑",
      "成片",
      "复盘",
      "设置",
    ]) {
      expect(within(nav).getByRole("button", { name: label })).toBeInTheDocument();
    }
    for (const oldLabel of [
      "热点池",
      "候选选题池",
      "项目列表",
      "脚本工作台",
      "发布数据追踪",
      "今日任务",
      "热点选题",
      "内容工作台",
      "素材库",
      "自动剪辑",
      "发布复盘",
      "数据复盘",
      "账号风格库",
      "内容质检",
      "脚本引擎",
    ]) {
      expect(within(nav).queryByRole("button", { name: oldLabel })).not.toBeInTheDocument();
    }

    expect(await screen.findByRole("heading", { name: "任务" })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: "任务" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("每个页面都能通过导航进入，当前页面有明显标记", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "任务" });
    const nav = screen.getByRole("navigation", { name: "主导航" });

    const pages: Array<[string, string]> = [
      ["选题", "选题"],
      ["脚本", "脚本"],
      ["素材", "素材"],
      ["剪辑", "剪辑"],
      ["成片", "成片"],
      ["复盘", "发布数据追踪"],
      ["设置", "账号风格库"],
      ["任务", "任务"],
    ];

    for (const [page, heading] of pages) {
      await user.click(within(nav).getByRole("button", { name: page }));
      await waitFor(() =>
        expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument(),
      );
      expect(within(nav).getByRole("button", { name: page })).toHaveAttribute(
        "aria-current",
        "page",
      );
    }
  });

  it("通过导航更新稳定 URL，并支持后退恢复页面", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "任务" });
    const nav = screen.getByRole("navigation", { name: "主导航" });

    await user.click(within(nav).getByRole("button", { name: "复盘" }));
    await screen.findByRole("heading", { name: "发布数据追踪" });
    expect(window.location.pathname).toBe("/review");

    await user.click(within(nav).getByRole("button", { name: "设置" }));
    await screen.findByRole("heading", { name: "账号风格库" });
    expect(window.location.pathname).toBe("/settings");

    window.history.back();
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "发布数据追踪" })).toBeInTheDocument(),
    );
    expect(window.location.pathname).toBe("/review");
  });

  it("从项目 URL 恢复脚本模块的 pipeline id", async () => {
    window.history.replaceState({}, "", "/scripts/42");
    render(<App />);

    expect(await screen.findByRole("heading", { name: "脚本" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/scripts/42");
    expect(fetch).toHaveBeenCalledWith("/api/pipeline/42/workspace", expect.anything());
  });

  it("复盘导航保留当前项目的 pipeline query", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/review?pipeline_id=42");
    render(<App />);
    await screen.findByRole("heading", { name: "发布数据追踪" });
    const nav = screen.getByRole("navigation", { name: "主导航" });

    await user.click(within(nav).getByRole("button", { name: "脚本" }));
    expect(window.location.pathname).toBe("/scripts/42");

    await user.click(within(nav).getByRole("button", { name: "复盘" }));
    expect(window.location.pathname).toBe("/review");
    expect(window.location.search).toBe("?pipeline_id=42");
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/video-posts?"),
        expect.any(Object),
      ),
    );
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("pipeline_id=42"),
      expect.any(Object),
    );
  });
});

function defaultAccountStyle() {
  return {
    account_positioning: "用大白话讲清楚新趋势、新环境、新风口、新工具背后的底层商业逻辑。",
    target_audiences: [{ id: "students", label: "小白学生", enabled: true }],
    content_directions: [{ id: "ai_tools", label: "AI 工具", enabled: true }],
    expression_styles: [{ id: "plain", label: "大白话", enabled: true }],
    banned_expressions: [
      { id: "shock", label: "震惊", category: "营销号", enabled: true },
      {
        id: "everyone_knows",
        label: "大家都知道",
        category: "默认常识句式",
        enabled: true,
      },
    ],
    structure_preferences: [
      {
        id: "beginner",
        name: "小白转译型",
        framework: "事件影响 → 通俗解释 → 背后逻辑 → 注意提醒",
        enabled: true,
      },
    ],
    opening_templates: [
      {
        id: "hotspot_opening",
        scene: "热点类开头",
        template: "这件事表面上是……",
        enabled: true,
      },
    ],
    ending_templates: [
      {
        id: "hotspot_ending",
        scene: "热点类结尾",
        template: "总结普通人该看懂的逻辑。",
        enabled: true,
      },
    ],
    validation_rules: {
      professional_terms_require_explanation: true,
      professional_terms: ["流动性收紧"],
      plain_language_examples: [
        {
          term: "流动性收紧",
          plain_explanation: "市场上的钱变少了。",
        },
      ],
      default_common_knowledge_phrases: ["大家都知道"],
      checks: ["是否用了禁用表达"],
    },
    last_saved_at: "2026-06-16T10:00:00+08:00",
    is_active: true,
    using_default: false,
  };
}
