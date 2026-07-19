import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TodayTaskPage from "./TodayTaskPage";
import type { TodayTasksResponse } from "./types";

const dashboard: TodayTasksResponse = {
  date: "2026-06-16",
  hotspots: [
    {
      task_id: "hotspot_2",
      title: "AI 工具进入企业采购流程",
      subtitle: "Hacker News · AI",
      reason: "选题价值 96，收藏价值 92，热度 90。",
      next_action: "查看热点",
      target_type: "hotspot",
      target_id: 2,
      priority: 94,
      meta: {},
      is_done: false,
    },
  ],
  topic_ideas: [
    {
      task_id: "topic_12",
      title: "企业为什么开始采购 AI 工具",
      subtitle: "对应热点：AI 工具进入企业采购流程",
      reason: "用大白话讲清楚企业采购背后的商业逻辑。",
      next_action: "查看选题",
      target_type: "topic",
      target_id: 12,
      priority: 90,
      meta: {},
      is_done: false,
    },
  ],
  collectible_ideas: [
    {
      task_id: "collect_topic_13",
      title: "普通人判断 AI 工具值不值得用的清单",
      subtitle: "对应热点：AI 工具进入企业采购流程",
      reason: "可以沉淀成以后反复使用的判断框架。",
      next_action: "查看选题",
      target_type: "topic",
      target_id: 13,
      priority: 88,
      meta: {},
      is_done: false,
    },
  ],
  projects: [
    {
      task_id: "project_7",
      title: "AI 企业采购项目",
      subtitle: "当前步骤：生成素材任务",
      reason: "继续推进到导出素材库。",
      next_action: "继续推进",
      target_type: "project",
      target_id: 7,
      priority: 82,
      meta: {},
      is_done: false,
    },
  ],
  confirmations: [
    {
      task_id: "confirm_project_7",
      title: "AI 企业采购项目需要确认",
      subtitle: "状态：需要人工确认",
      reason: "素材任务需要人工确认。",
      next_action: "去确认",
      target_type: "project",
      target_id: 7,
      priority: 100,
      meta: {},
      is_done: false,
    },
  ],
  publish_reminders: [
    {
      task_id: "post_update_5_24小时",
      title: "AI 工具采购复盘该补数据",
      subtitle: "抖音 · 24小时",
      reason: "这条视频到了「24小时」数据更新时间点。",
      next_action: "更新数据",
      target_type: "tracking",
      target_id: 5,
      priority: 86,
      meta: {},
      is_done: false,
    },
    {
      task_id: "review_5_值得继续做",
      title: "AI 工具采购复盘值得继续",
      subtitle: "复盘判断：值得继续做",
      reason: "继续做同类选题，并保留当前结构。",
      next_action: "查看复盘",
      target_type: "review",
      target_id: 5,
      priority: 90,
      meta: {},
      is_done: false,
    },
  ],
  suggestions: [
    {
      task_id: "suggest_hotspot",
      title: "先看热点「AI 工具进入企业采购流程」。",
      subtitle: "Hacker News · AI",
      reason: "选题价值 96。",
      next_action: "查看热点",
      target_type: "hotspot",
      target_id: 2,
      priority: 94,
      meta: {},
      is_done: false,
    },
  ],
  decision_items: [
    {
      task_id: "confirm_daily_events",
      title: "待确认选题",
      subtitle: "推荐 5 件事件 · 已选 3 件",
      reason: "选 3-6 件，组合成今天一期高密度视频。",
      next_action: "去选事件",
      target_type: "topic_pool",
      target_id: null,
      priority: 96,
      meta: { decision_type: "topic_confirmation" },
      is_done: false,
    },
    {
      task_id: "confirm_voiceover_7_31",
      title: "脚本确认",
      subtitle: "AI 企业采购项目",
      reason: "检查脚本、事件顺序和每段表达。",
      next_action: "确认脚本",
      target_type: "workspace",
      target_id: 7,
      priority: 94,
      meta: { decision_type: "script_confirmation" },
      is_done: false,
    },
  ],
  running_items: [
    {
      task_id: "project_7",
      title: "正在处理素材",
      subtitle: "3/11 · AI 企业采购项目",
      reason: "继续推进到导出素材库。",
      next_action: "查看进度",
      target_type: "project",
      target_id: 7,
      priority: 82,
      meta: {
        project_name: "AI 企业采购项目",
        current_step: "生成素材任务",
        completed_steps: 3,
        total_steps: 11,
      },
      is_done: false,
    },
  ],
  finished_items: [
    {
      task_id: "finished_project_8",
      title: "今日3件事成片",
      subtitle: "成片导出任务已就绪",
      reason: "可以检查时间轴、字幕、剪辑说明和最终确认状态。",
      next_action: "预览",
      target_type: "editing",
      target_id: 8,
      priority: 76,
      meta: {},
      is_done: false,
    },
  ],
  handled_task_ids: [],
};

const finalReviewTask = {
  task_id: "confirm_project_9",
  title: "成片验收",
  subtitle: "状态：需要人工确认",
  reason: "当前成片文件已发生变化。",
  next_action: "去确认",
  target_type: "finished" as const,
  target_id: 9,
  priority: 97,
  meta: {
    completion_reason_code: "artifact_hash_mismatch",
    next_surface: "finished",
    current_step: "成片验收",
  },
  is_done: false,
};

const failedProjectTask = {
  task_id: "confirm_project_10",
  title: "素材失败项目",
  subtitle: "状态：失败",
  reason: "素材下载失败。",
  next_action: "去处理",
  target_type: "materials" as const,
  target_id: 10,
  priority: 100,
  meta: {
    status: "失败",
    completion_reason_code: "step_failed",
    next_surface: "materials",
    current_step: "生成素材任务",
    needs_confirmation: true,
    has_failed: true,
  },
  is_done: false,
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("今日任务页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/today/tasks") {
          return jsonResponse(dashboard);
        }
        if (
          url === "/api/today/tasks/confirm_daily_events/complete" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            task_id: "confirm_daily_events",
            handled_task_ids: ["confirm_daily_events"],
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("能打开并显示今日任务聚合内容", async () => {
    render(<TodayTaskPage />);

    expect(screen.getByRole("heading", { name: "任务" })).toBeInTheDocument();
    expect(await screen.findByText("决策项目")).toBeInTheDocument();
    expect(screen.getByText("进行项目")).toBeInTheDocument();
    expect(screen.queryByText("成片")).not.toBeInTheDocument();
    expect(screen.getByText("待确认选题")).toBeInTheDocument();
    expect(screen.getByText("脚本确认")).toBeInTheDocument();
    expect(screen.getByText("AI 企业采购项目需要确认")).toBeInTheDocument();
    expect(screen.getByText("正在处理素材")).toBeInTheDocument();
    expect(screen.getByText("3/11 · AI 企业采购项目")).toBeInTheDocument();
    expect(screen.queryByText("今日3件事成片")).not.toBeInTheDocument();
    expect(screen.queryByText("今日推荐")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("今日概况")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /标记/ })).not.toBeInTheDocument();
  });

  it("能跳转到对应页面入口", async () => {
    const user = userEvent.setup();
    const onOpenHotspot = vi.fn();
    const onOpenTopic = vi.fn();
    const onOpenProject = vi.fn();
    const onOpenWorkspace = vi.fn();
    const onOpenEditing = vi.fn();
    const onOpenTracking = vi.fn();
    const onOpenReview = vi.fn();
    const onOpenTopics = vi.fn();
    render(
      <TodayTaskPage
        onOpenHotspot={onOpenHotspot}
        onOpenTopic={onOpenTopic}
        onOpenProject={onOpenProject}
        onOpenWorkspace={onOpenWorkspace}
        onOpenEditing={onOpenEditing}
        onOpenTracking={onOpenTracking}
        onOpenReview={onOpenReview}
        onOpenTopics={onOpenTopics}
      />,
    );
    await screen.findByText("待确认选题");

    await user.click(
      screen.getByRole("button", { name: "执行 待确认选题" }),
    );
    await user.click(screen.getByRole("button", { name: "执行 脚本确认" }));

    expect(onOpenHotspot).not.toHaveBeenCalled();
    expect(onOpenTopics).toHaveBeenCalledTimes(1);
    expect(onOpenWorkspace).toHaveBeenCalledWith(7);
    expect(onOpenProject).not.toHaveBeenCalled();
    expect(onOpenEditing).not.toHaveBeenCalled();
    expect(onOpenTracking).not.toHaveBeenCalled();
    expect(onOpenReview).not.toHaveBeenCalled();
  });

  it("进行项目只显示当前阶段，不提供手动处理按钮", async () => {
    render(<TodayTaskPage />);
    await screen.findByText("正在处理素材");

    expect(screen.queryByRole("button", { name: "执行 正在处理素材" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /标记/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /预览 今日3件事成片/ })).not.toBeInTheDocument();
  });

  it("决策提醒完成后会从今日任务里隐藏", async () => {
    const user = userEvent.setup();
    render(<TodayTaskPage />);
    await screen.findByText("待确认选题");

    await user.click(screen.getByRole("button", { name: "完成 待确认选题" }));

    await waitFor(() => {
      expect(screen.queryByText("待确认选题")).not.toBeInTheDocument();
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/today/tasks/confirm_daily_events/complete",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      },
    );
  });

  it("成片验收决策只导航，不显示或调用通用完成控件", async () => {
    const user = userEvent.setup();
    const onOpenFinished = vi.fn();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/today/tasks") {
        return jsonResponse({
          ...dashboard,
          decision_items: [finalReviewTask],
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    render(<TodayTaskPage onOpenFinished={onOpenFinished} />);
    const action = await screen.findByRole("button", { name: "执行 成片验收" });

    expect(action).toHaveTextContent("去成片验收");
    expect(
      screen.queryByRole("button", { name: "完成 成片验收" }),
    ).not.toBeInTheDocument();
    await user.click(action);

    expect(onOpenFinished).toHaveBeenCalledWith(9);
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([input]) => String(input).includes("/complete")),
    ).toBe(false);
  });

  it("同时有运行项目时仍显示失败阻断，且不能用完成按钮隐藏", async () => {
    const onOpenMaterials = vi.fn();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/today/tasks") {
        return jsonResponse({
          ...dashboard,
          confirmations: [failedProjectTask],
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });

    render(<TodayTaskPage onOpenMaterials={onOpenMaterials} />);

    expect(await screen.findByText("素材失败项目")).toBeInTheDocument();
    expect(screen.getByText("状态：失败")).toBeInTheDocument();
    expect(screen.getByText("素材下载失败。")).toBeInTheDocument();
    expect(screen.getByText("正在处理素材")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "完成 素材失败项目" }),
    ).not.toBeInTheDocument();
  });

  it("能刷新今日任务", async () => {
    const user = userEvent.setup();
    render(<TodayTaskPage />);
    await screen.findByText("待确认选题");

    await user.click(screen.getByRole("button", { name: "刷新任务" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });

  it("只请求今日任务接口，不调用生成或真实 AI 接口", async () => {
    render(<TodayTaskPage />);
    await screen.findByText("待确认选题");

    const urls = vi.mocked(fetch).mock.calls.map((call) => String(call[0]));
    expect(urls).toEqual(["/api/today/tasks"]);
    expect(urls.some((url) => url.includes("generate"))).toBe(false);
    expect(urls.some((url) => url.includes("build-project"))).toBe(false);
  });

  it("不再在今日任务显示辅助验收入口", async () => {
    render(<TodayTaskPage />);
    await screen.findByText("待确认选题");

    expect(screen.queryByText("辅助验收")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "手动跑通测试" })).not.toBeInTheDocument();
  });
});
