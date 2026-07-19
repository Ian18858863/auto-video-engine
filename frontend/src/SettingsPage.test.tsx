import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SettingsPage from "./SettingsPage";

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

const sourceStatuses = [
  {
    source_name: "百度热搜",
    status: "成功",
    last_run_at: "2026-06-19T09:15:00+08:00",
    items_fetched: 30,
    success_count: 30,
    failure_reason: "",
    is_rate_limited: false,
  },
  {
    source_name: "TechCrunch",
    status: "成功",
    last_run_at: "2026-06-19T09:15:00+08:00",
    items_fetched: 20,
    success_count: 20,
    failure_reason: "",
    is_rate_limited: false,
  },
  {
    source_name: "Reddit/r/technology",
    status: "限流",
    last_run_at: "2026-06-19T09:15:00+08:00",
    items_fetched: 0,
    success_count: 0,
    failure_reason: "HTTP 429",
    is_rate_limited: true,
  },
  {
    source_name: "B站科技区排行",
    status: "跳过",
    last_run_at: null,
    items_fetched: 0,
    success_count: 0,
    failure_reason: "尚未抓取",
    is_rate_limited: false,
  },
  {
    source_name: "微博热搜网页版",
    status: "跳过",
    last_run_at: null,
    items_fetched: 0,
    success_count: 0,
    failure_reason: "尚未采集",
    is_rate_limited: false,
  },
  {
    source_name: "抖音热点网页版",
    status: "跳过",
    last_run_at: null,
    items_fetched: 0,
    success_count: 0,
    failure_reason: "尚未采集",
    is_rate_limited: false,
  },
  {
    source_name: "小红书公开发现页",
    status: "跳过",
    last_run_at: null,
    items_fetched: 0,
    success_count: 0,
    failure_reason: "尚未采集",
    is_rate_limited: false,
  },
];

const runtimeStatus = {
  provider: {
    name: "fake",
    status: "test_only",
    production_allowed: false,
    verified: false,
    reason: "fake provider 仅用于隔离测试",
    secret_configured: false,
  },
  providers: [
    {
      id: "script_ai",
      name: "脚本 AI（fake）",
      status: "test_only",
      installed: true,
      configured: false,
      verified: false,
      production_allowed: false,
      secret_configured: false,
      last_verified_at: null,
      purpose: "选题和脚本生成",
      affected_modules: ["选题", "脚本"],
      fallback: "无生产级 fallback",
      reason: "fake provider 仅用于隔离测试",
    },
    {
      id: "pexels",
      name: "Pexels",
      status: "configured_unverified",
      installed: true,
      configured: true,
      verified: false,
      production_allowed: false,
      secret_configured: true,
      internal_secret: "ui-secret-canary",
      declared_version: "",
      last_verified_at: null,
      purpose: "普通镜头的图库候选来源",
      applicable_route: "素材（普通镜头候选）",
      affected_modules: ["素材"],
      fallback: "Pixabay -> 自有素材 -> 信息动效",
      cost_class: "外部服务；费用与配额以 provider 账户为准",
      reason: "Key 已存在，但本次只读检查没有联网验证",
    },
  ],
  skills: [
    {
      id: "hyperframes",
      name: "HyperFrames",
      status: "installed_not_integrated",
      installed: true,
      configured: false,
      verified: false,
      production_allowed: false,
      declared_version: "1.2.3",
      last_verified_at: null,
      purpose: "HTML 场景动画与视频渲染候选",
      applicable_route: "剪辑 -> 成片（同题试点通过后才可接主线）",
      affected_modules: ["剪辑", "成片"],
      fallback: "当前 TimelineSpec + FFmpeg 本地草稿链",
      cost_class: "本地工具；外部素材费用另计",
      reason: "已安装，但尚未接入产品适配器并通过真实黄金项目验证",
    },
    {
      id: "video-use",
      name: "video-use",
      status: "not_installed",
      installed: false,
      configured: false,
      verified: false,
      production_allowed: false,
      declared_version: "",
      last_verified_at: null,
      purpose: "已有视频的转写、剪裁、字幕和通用编辑",
      applicable_route: "素材 -> 剪辑（条件接入）",
      affected_modules: ["素材", "剪辑"],
      fallback: "现有 FFmpeg 兼容编辑链",
      cost_class: "本地工具；模型依赖可能产生费用",
      reason: "未在服务端批准的 Skills 根目录找到 SKILL.md",
    },
    {
      id: "blocked-skill",
      name: "受阻 Skill",
      status: "blocked_path_policy",
      installed: null,
      configured: null,
      verified: false,
      production_allowed: false,
      declared_version: "",
      last_verified_at: null,
      purpose: "验证未知状态不被伪装成未安装",
      applicable_route: "设置体检",
      affected_modules: ["设置"],
      fallback: "修复 D 盘路径后重新检查",
      cost_class: "无",
      reason: "路径策略阻断",
    },
  ],
  paths: [],
  capabilities: {
    modern_render: {
      status: "local_real",
      production_allowed: true,
      reason: "项目内 TimelineSpec/FFmpeg 草稿可生成",
    },
  },
  storage_policy: {
    system_drive: "C:",
    payload_drive_required: "non-system drive (normally D:)",
    runtime_paths_are_resolved: true,
  },
};

describe("设置页数据源配置", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/account-style") {
          return jsonResponse(defaultAccountStyle());
        }
        if (url === "/api/automation/sources/status") {
          return jsonResponse({
            items: sourceStatuses,
            source_count: sourceStatuses.length,
            manual_import_is_fallback: true,
            real_ai_called: false,
          });
        }
        if (url === "/api/settings/runtime-status") {
          return jsonResponse(runtimeStatus);
        }
        if (url === "/api/automation/hotspots/run" && init?.method === "POST") {
          return jsonResponse({
            source_count: 3,
            skipped_count: 0,
            fetched_count: 50,
            saved_count: 12,
            duplicate_count: 4,
            filtered_count: 2,
            failed_sources: [],
            rate_limited_sources: ["Reddit/r/technology"],
            source_statuses: sourceStatuses,
            generated_hotspot_count: 5,
            generated_topic_idea_count: 5,
            topic_generation: [],
            provider: "local_rule",
            real_ai_called: false,
            manual_import_is_fallback: true,
            message: "完成",
            ran_at: "2026-06-19T09:15:00+08:00",
            next_due_at: "2026-06-20T09:15:00+08:00",
          });
        }
        if (
          url === "/api/automation/browser-hotspots/run" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            source_count: 3,
            fetched_count: 18,
            saved_count: 9,
            duplicate_count: 2,
            csv_written_count: 9,
            failed_sources: [],
            needs_login_sources: [],
            skipped_sources: [],
            csv_path: "exports/热点采集/browser_hotspots.csv",
            message: "浏览器采集完成。",
            safety_note: "低频采集",
          });
        }
        if (
          url === "/api/automation/browser-hotspots/login-browser" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            message: "登录浏览器已打开。",
            pid: 1234,
            sources: [
              { name: "微博热搜网页版", url: "https://s.weibo.com/top/summary" },
            ],
            profile_dir: "outputs/browser-profiles/hotspots",
            real_collection_started: false,
            note: "只用于登录。",
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("用统一卡片和分组列表展示数据源状态，并支持立即抓取", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.click(screen.getByRole("tab", { name: "数据源" }));

    expect(await screen.findByRole("heading", { name: "数据源配置" })).toBeInTheDocument();
    expect(screen.getByText("启用来源")).toBeInTheDocument();
    expect(screen.getByText("国内公开热榜")).toBeInTheDocument();
    expect(screen.getByText("公共 RSS")).toBeInTheDocument();
    expect(screen.getByText("Reddit")).toBeInTheDocument();
    expect(screen.getByText("浏览器公开采集")).toBeInTheDocument();
    expect(screen.getByText("B站科技区排行")).toBeInTheDocument();
    expect(screen.getByText("微博热搜网页版")).toBeInTheDocument();
    expect(screen.getByText("抖音热点网页版")).toBeInTheDocument();
    expect(screen.getByText("小红书公开发现页")).toBeInTheDocument();
    expect(screen.getByText("config/source_config.yaml")).toBeInTheDocument();
    expect(screen.getByText("低频采集、不绕验证码、不批量下载视频、失败隔离")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "立即抓取公开源" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/automation/hotspots/run",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(screen.getByText("抓取 50 条，新增 12 条，候选事件 5 个。")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "采集浏览器热点" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/automation/browser-hotspots/run",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(screen.getByText("浏览器采集完成。")).toBeInTheDocument();

    const actionButtons = screen.getAllByRole("button").map((button) => button.textContent);
    expect(actionButtons.indexOf("采集浏览器热点")).toBeLessThan(
      actionButtons.indexOf("打开登录浏览器"),
    );
    expect(actionButtons.indexOf("打开登录浏览器")).toBeLessThan(
      actionButtons.indexOf("刷新状态"),
    );

    await user.click(screen.getByRole("button", { name: "打开登录浏览器" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/automation/browser-hotspots/login-browser",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(screen.getByText("登录浏览器已打开。")).toBeInTheDocument();
  });

  it("把 Skill 和 provider 的安装、配置、验证与生产可用性分开显示", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.click(screen.getByRole("tab", { name: "AI 配置" }));

    expect(await screen.findByRole("heading", { name: "AI Provider 配置" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Providers 能力状态" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Skills 能力状态" })).toBeInTheDocument();
    expect(screen.getByText("脚本 AI（fake）")).toBeInTheDocument();
    expect(screen.getByText("Pexels")).toBeInTheDocument();
    expect(screen.getByText("HyperFrames")).toBeInTheDocument();
    expect(screen.getByText("video-use")).toBeInTheDocument();
    expect(screen.getAllByText("已安装").length).toBeGreaterThan(0);
    expect(screen.getAllByText("已内置").length).toBeGreaterThan(0);
    expect(screen.getAllByText("未配置").length).toBeGreaterThan(0);
    expect(screen.getAllByText("无法判断").length).toBeGreaterThan(0);
    expect(screen.getAllByText("从未验证").length).toBeGreaterThan(0);
    expect(screen.getAllByText("当前阻断").length).toBeGreaterThan(0);
    expect(screen.getByText("版本 1.2.3")).toBeInTheDocument();
    expect(screen.getByText("Key 已配置（不显示内容）")).toBeInTheDocument();
    expect(screen.getByText("无生产级 fallback")).toBeInTheDocument();
    expect(screen.queryByText(/secret/i)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("ui-secret-canary");
  });

  it("后端若误报 fake 为生产可用，前端仍显示状态冲突并阻断", async () => {
    const contradictoryStatus = {
      ...runtimeStatus,
      providers: runtimeStatus.providers.map((item) =>
        item.id === "script_ai"
          ? { ...item, production_allowed: true }
          : item,
      ),
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/account-style") return jsonResponse(defaultAccountStyle());
        if (url === "/api/settings/runtime-status") return jsonResponse(contradictoryStatus);
        throw new Error(`未处理的请求：${url}`);
      }),
    );

    const user = userEvent.setup();
    render(<SettingsPage />);
    await user.click(screen.getByRole("tab", { name: "AI 配置" }));

    expect(await screen.findByText("状态冲突（已阻断）")).toBeInTheDocument();
    expect(screen.queryByText("生产可用")).not.toBeInTheDocument();
  });
});

function defaultAccountStyle() {
  return {
    account_positioning: "用大白话讲清楚趋势。",
    target_audiences: [],
    content_directions: [],
    expression_styles: [],
    banned_expressions: [],
    structure_preferences: [],
    opening_templates: [],
    ending_templates: [],
    validation_rules: {
      professional_terms_require_explanation: true,
      professional_terms: [],
      plain_language_examples: [],
      default_common_knowledge_phrases: [],
      checks: [],
    },
    last_saved_at: "",
    is_active: true,
    using_default: false,
  };
}
