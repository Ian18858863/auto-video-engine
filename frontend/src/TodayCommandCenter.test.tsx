import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TodayCommandCenter from "./TodayCommandCenter";

const project = {
  pipeline_id: 7,
  hotspot_id: 2,
  topic_idea_id: 12,
  script_asset_id: 31,
  project_name: "AI 企业采购项目",
  project_dir: "exports/短视频工厂/项目/AI 企业采购项目",
  status: "进行中",
  completion_reason_code: "preparation_incomplete",
  next_surface: "project",
  current_step: "生成素材任务",
  completed_steps: 3,
  total_steps: 11,
  has_failed: false,
  failure_reason: "",
  needs_confirmation: false,
  created_at: "2026-06-15T09:00:00+08:00",
  updated_at: "2026-06-15T09:20:00+08:00",
  next_suggestion: "继续推进到素材任务。",
};

const finalReviewProject = {
  ...project,
  pipeline_id: 8,
  project_name: "等待成片验收",
  status: "需要人工确认",
  completion_reason_code: "quality_report_invalid",
  next_surface: "finished",
  current_step: "成片验收",
  completed_steps: 11,
  needs_confirmation: true,
  next_suggestion: "打开成片页检查当前文件和验收状态",
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("今日任务指挥台", () => {
  beforeEach(() => {
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
            decision_items: [
              {
                task_id: "confirm_project_8",
                title: "成片验收",
                subtitle: "状态：需要人工确认",
                reason: "质量报告格式无效。",
                next_action: "去确认",
                target_type: "finished",
                target_id: 8,
                priority: 97,
                meta: {
                  completion_reason_code: "quality_report_invalid",
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
        if (url.startsWith("/api/pipeline?")) {
          return jsonResponse({
            items: [project, finalReviewProject],
            pagination: { page: 1, page_size: 20, total: 2, total_pages: 1 },
          });
        }
        if (url === "/api/pipeline/7") {
          return jsonResponse({
            ...project,
            steps: [
              { step_name: "生成候选选题", status: "已完成", error_message: "" },
              { step_name: "生成素材任务", status: "进行中", error_message: "" },
            ],
            generated_files: {},
            failure_reasons: [],
          });
        }
        if (url === "/api/pipeline/8") {
          return jsonResponse({
            ...finalReviewProject,
            steps: [],
            generated_files: {},
            failure_reasons: [],
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("带项目 id 打开时进入项目列表并定位项目详情", async () => {
    render(<TodayCommandCenter targetPipelineId={7} />);

    expect(await screen.findByRole("heading", { name: "项目列表" })).toBeInTheDocument();
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getAllByText("AI 企业采购项目").length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/pipeline/7", expect.any(Object)),
    );
  });

  it("项目列表的成片验收动作会透传同一 pipeline id", async () => {
    const user = userEvent.setup();
    const onOpenFinished = vi.fn();
    render(
      <TodayCommandCenter
        targetPipelineId={8}
        onOpenFinished={onOpenFinished}
      />,
    );
    await screen.findByRole("button", { name: "查看 等待成片验收" });

    await user.click(
      screen.getByRole("button", { name: "去成片验收 等待成片验收" }),
    );

    expect(onOpenFinished).toHaveBeenCalledWith(8);
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/pipeline/8/continue",
      expect.anything(),
    );
  });

  it("真实后端文案仍显示去成片验收并透传同一 pipeline id", async () => {
    const user = userEvent.setup();
    const onOpenFinished = vi.fn();
    render(
      <TodayCommandCenter
        targetPipelineId={null}
        onOpenFinished={onOpenFinished}
      />,
    );
    const action = await screen.findByRole("button", { name: "执行 成片验收" });

    expect(action).toHaveTextContent("去成片验收");
    expect(action).not.toHaveTextContent("去确认");
    expect(
      screen.queryByRole("button", { name: "完成 成片验收" }),
    ).not.toBeInTheDocument();
    await user.click(action);

    expect(onOpenFinished).toHaveBeenCalledWith(8);
  });
});
