import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import ProjectListPage from "./ProjectListPage";

const projects = [
  {
    pipeline_id: 2,
    hotspot_id: 12,
    topic_idea_id: 22,
    script_asset_id: 32,
    project_name: "AI 企业采购",
    project_dir: "exports/短视频工厂/项目/AI 企业采购",
    status: "需要人工确认",
    completion_reason_code: "step_failed",
    next_surface: "project",
    current_step: "生成素材任务",
    completed_steps: 3,
    total_steps: 10,
    has_failed: true,
    failure_reason: "素材任务暂时失败",
    needs_confirmation: true,
    created_at: "2026-06-15T09:00:00+08:00",
    updated_at: "2026-06-15T09:20:00+08:00",
    next_suggestion: "修正问题后继续重试「生成素材任务」。",
  },
  {
    pipeline_id: 1,
    hotspot_id: 11,
    topic_idea_id: 21,
    script_asset_id: 31,
    project_name: "消费趋势清单",
    project_dir: "exports/短视频工厂/项目/消费趋势清单",
    status: "已完成",
    completion_reason_code: "approved_artifact_verified",
    next_surface: "none",
    current_step: "已完成",
    completed_steps: 10,
    total_steps: 10,
    has_failed: false,
    failure_reason: "",
    needs_confirmation: false,
    created_at: "2026-06-15T08:00:00+08:00",
    updated_at: "2026-06-15T08:30:00+08:00",
    next_suggestion: "流程已完成，可以检查内容、素材库和成片任务。",
  },
  {
    pipeline_id: 3,
    hotspot_id: 13,
    topic_idea_id: 23,
    script_asset_id: 33,
    project_name: "等待成片验收",
    project_dir: "exports/短视频工厂/项目/等待成片验收",
    status: "需要人工确认",
    completion_reason_code: "approval_missing",
    next_surface: "finished",
    current_step: "成片验收",
    completed_steps: 11,
    total_steps: 11,
    has_failed: false,
    failure_reason: "生产准备已完成，但尚未验收成片。",
    needs_confirmation: true,
    created_at: "2026-06-15T10:00:00+08:00",
    updated_at: "2026-06-15T10:20:00+08:00",
    next_suggestion: "打开成片页检查当前文件和验收状态",
  },
  {
    pipeline_id: 4,
    hotspot_id: 14,
    topic_idea_id: 24,
    script_asset_id: 34,
    project_name: "批准后文件变化",
    project_dir: "exports/短视频工厂/项目/批准后文件变化",
    status: "需要人工确认",
    completion_reason_code: "artifact_hash_mismatch",
    next_surface: "finished",
    current_step: "成片验收",
    completed_steps: 11,
    total_steps: 11,
    has_failed: false,
    failure_reason: "当前成片文件已发生变化。",
    needs_confirmation: true,
    created_at: "2026-06-15T11:00:00+08:00",
    updated_at: "2026-06-15T11:20:00+08:00",
    next_suggestion: "打开成片页检查当前文件和验收状态",
  },
];

const pipelineDetail = {
  pipeline_id: 2,
  hotspot_id: 12,
  topic_idea_id: 22,
  script_asset_id: 32,
  project_name: "AI 企业采购",
  project_dir: "exports/短视频工厂/项目/AI 企业采购",
  status: "需要人工确认",
  completion_reason_code: "step_failed",
  next_surface: "project",
  current_step: "生成素材任务",
  steps: [
    { step_name: "生成候选选题", status: "已完成", error_message: "" },
    {
      step_name: "生成素材任务",
      status: "失败",
      error_message: "素材任务暂时失败",
    },
  ],
  generated_files: {},
  failure_reasons: [
    { step_name: "生成素材任务", reason: "素材任务暂时失败" },
  ],
  needs_confirmation: true,
  next_suggestion: "修正问题后继续重试「生成素材任务」。",
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("项目列表页面", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
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
            decision_items: [
              {
                task_id: "confirm_project_3",
                title: "成片验收",
                subtitle: "等待成片验收",
                reason: "生产准备已完成，但尚未验收成片。",
                next_action: "去确认",
                target_type: "finished",
                target_id: 3,
                priority: 97,
                meta: {
                  completion_reason_code: "approval_missing",
                  next_surface: "finished",
                  current_step: "成片验收",
                },
                is_done: false,
              },
            ],
            running_items: [],
            finished_items: [],
            handled_task_ids: [],
          });
        }
        if (url === "/api/editing-export/finished-videos") {
          return jsonResponse({ items: [], note: "测试空列表" });
        }
        if (
          url === "/api/pipeline/3/finished-video/export" &&
          init?.method === "POST"
        ) {
          return jsonResponse({ 成功: false, 提示: "仅验证 pipeline id" });
        }
        if (url === "/api/pipeline/2/continue" && init?.method === "POST") {
          return jsonResponse({ ...pipelineDetail, status: "已完成" });
        }
        if (url === "/api/pipeline/2") {
          return jsonResponse(pipelineDetail);
        }
        if (url.startsWith("/api/pipeline")) {
          return jsonResponse({
            items: projects,
            pagination: {
              page: 1,
              page_size: 20,
              total: 4,
              total_pages: 1,
            },
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("打开页面后请求并显示多个项目", async () => {
    render(<ProjectListPage />);

    expect(screen.getByRole("heading", { name: "项目列表" })).toBeInTheDocument();
    expect(await screen.findByText("AI 企业采购")).toBeInTheDocument();
    expect(screen.getByText("消费趋势清单")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/pipeline?"),
      expect.any(Object),
    );
  });

  it("批准失效项目显示具体原因而不是只有泛化状态", async () => {
    render(<ProjectListPage />);

    await screen.findByText("批准后文件变化");
    expect(screen.getByText("当前成片文件已发生变化。")).toBeInTheDocument();
  });

  it("可以筛选失败和需要人工确认的项目", async () => {
    const user = userEvent.setup();
    render(<ProjectListPage />);
    await screen.findByText("AI 企业采购");

    await user.click(screen.getByRole("checkbox", { name: "只看失败项目" }));
    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("failed_only=true"),
        expect.any(Object),
      );
    });

    await user.click(
      screen.getByRole("checkbox", { name: "只看需要人工确认" }),
    );
    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("needs_confirmation_only=true"),
        expect.any(Object),
      );
    });
  });

  it("可以按状态筛选并刷新", async () => {
    const user = userEvent.setup();
    render(<ProjectListPage />);
    await screen.findByText("AI 企业采购");

    await user.selectOptions(screen.getByLabelText("按状态筛选"), "已完成");
    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining(
          `${encodeURIComponent("status")}=${encodeURIComponent("已完成")}`,
        ),
        expect.any(Object),
      );
    });

    await user.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
  });

  it("点击项目可以查看步骤详情", async () => {
    const user = userEvent.setup();
    render(<ProjectListPage />);
    await screen.findByText("AI 企业采购");

    await user.click(screen.getByRole("button", { name: "查看 AI 企业采购" }));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("生成候选选题")).toBeInTheDocument();
    expect(screen.getAllByText("素材任务暂时失败").length).toBeGreaterThan(0);
    expect(fetch).toHaveBeenCalledWith("/api/pipeline/2", expect.any(Object));
  });

  it("可以继续执行项目并刷新列表", async () => {
    const user = userEvent.setup();
    render(<ProjectListPage />);
    await screen.findByText("AI 企业采购");

    await user.click(screen.getByRole("button", { name: "继续 AI 企业采购" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/pipeline/2/continue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
    });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
  });

  it("已有脚本的项目可以打开脚本工作台", async () => {
    const user = userEvent.setup();
    const onOpenWorkspace = vi.fn();
    render(<ProjectListPage onOpenWorkspace={onOpenWorkspace} />);
    await screen.findByText("AI 企业采购");

    await user.click(
      screen.getByRole("button", {
        name: "打开 AI 企业采购 的脚本工作台",
      }),
    );

    expect(onOpenWorkspace).toHaveBeenCalledWith(2);
  });

  it("成片验收项目只导航到现有成片页，不调用继续执行", async () => {
    const user = userEvent.setup();
    const onOpenFinished = vi.fn();
    render(<ProjectListPage onOpenFinished={onOpenFinished} />);
    await screen.findByText("等待成片验收");

    const action = screen.getByRole("button", {
      name: "去成片验收 等待成片验收",
    });
    expect(action).toHaveTextContent("去成片验收");
    await user.click(action);

    expect(onOpenFinished).toHaveBeenCalledWith(3);
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/pipeline/3/continue",
      expect.anything(),
    );
  });

  it("今日成片验收动作携带同一项目 id 打开现有成片模块", async () => {
    const user = userEvent.setup();
    render(<App />);
    const action = await screen.findByRole("button", { name: "执行 成片验收" });

    expect(action).toHaveTextContent("去成片验收");
    expect(
      screen.queryByRole("button", { name: "完成 成片验收" }),
    ).not.toBeInTheDocument();
    await user.click(action);

    expect(await screen.findByRole("heading", { name: "成片" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/finished/3");
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/pipeline/3/continue",
      expect.anything(),
    );
    await user.click(screen.getByRole("button", { name: "生成成片" }));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/3/finished-video/export",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("从成片项目 URL 恢复模块和 pipeline id", async () => {
    window.history.replaceState({}, "", "/finished/3");
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "成片" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/finished/3");

    await user.click(screen.getByRole("button", { name: "生成成片" }));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/3/finished-video/export",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
