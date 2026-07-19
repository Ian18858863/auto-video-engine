import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ContentQualityPage from "./ContentQualityPage";

const workspace = {
  pipeline_id: 18,
  status: "已完成",
  provider: "fake",
  project_name: "AI 企业采购脚本",
  project_dir: "exports/短视频工厂/项目/AI 企业采购脚本",
  hotspot: { id: 2, title: "AI 工具进入企业采购流程", summary: "" },
  topic: {
    id: 12,
    title: "为什么企业开始采购 AI 工具",
    core_view: "企业买的是省钱提效能力。",
    recommended_format: "数字人口播",
    recommended_duration: "45 秒",
    risk_note: "不做投资建议。",
    hook: "为什么企业突然开始买 AI 工具？",
  },
  script: {
    id: 42,
    title: "为什么企业开始采购 AI 工具",
    voiceover: "大家都知道，这个风口炸裂，背后是流动性收紧。",
    storyboard: [],
    subtitles: [],
    title_options: [{ title: "AI 工具全网疯传", type: "趋势解释型" }],
    cover_copy: [{ text: "财富密码" }],
    publish_copy: { 抖音: "马上上车" },
  },
  material_tasks: [],
  graphic_tasks: [],
  avatar_tasks: [],
  exports: {
    script_md: { generated: true, path: "script.md" },
    shotlist_csv: { generated: true, path: "shotlist.csv" },
    edit_manifest_json: { generated: true, path: "edit_manifest.json" },
  },
};

function qualityResult(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    pipeline_id: null,
    script_asset_id: null,
    source_type: "手动粘贴",
    source_title: "手动粘贴内容",
    checked_text: "大家都知道，这个风口炸裂。",
    created_at: "2026-06-16T12:00:00+08:00",
    total_score: 52,
    publish_suggestion: "不建议发布",
    max_issue: "命中禁用表达：大家都知道、炸裂",
    issues: ["命中禁用表达：大家都知道、炸裂", "专业词需要解释：流动性收紧"],
    suggestions: ["删掉夸张表达", "专业词后面补一句大白话解释"],
    matched_banned_expressions: ["大家都知道", "炸裂"],
    professional_terms_need_explanation: ["流动性收紧"],
    risk_level: "高",
    collect_value: "低",
    series_worthy: false,
    style_source: "账号风格库",
    checked_dimensions: ["禁用表达", "专业术语解释", "投资建议风险"],
    local_rules_only: true,
    real_ai_called: false,
    ...overrides,
  };
}

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("内容质检页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/pipeline/18/workspace") {
          return jsonResponse(workspace);
        }
        if (
          url === "/api/content-quality/pipeline/18/check" &&
          init?.method === "POST"
        ) {
          return jsonResponse(
            qualityResult({
              pipeline_id: 18,
              script_asset_id: 42,
              source_type: "脚本工作台",
              source_title: "AI 企业采购脚本",
            }),
          );
        }
        if (
          url === "/api/content-quality/manual/check" &&
          init?.method === "POST"
        ) {
          return jsonResponse(qualityResult());
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("内容质检页面能打开，并显示脚本工作台脚本", async () => {
    render(<ContentQualityPage pipelineId={18} />);

    expect(screen.getByRole("heading", { name: "内容质检" })).toBeInTheDocument();
    expect(await screen.findByText("AI 企业采购脚本")).toBeInTheDocument();
    expect(screen.getByText("为什么企业开始采购 AI 工具")).toBeInTheDocument();
  });

  it("能从脚本工作台选择脚本质检并显示结果", async () => {
    const user = userEvent.setup();
    render(<ContentQualityPage pipelineId={18} />);
    await screen.findByText("AI 企业采购脚本");

    await user.click(screen.getByRole("button", { name: "质检当前脚本" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/content-quality/pipeline/18/check",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(await screen.findByText("质检记录已保存：#7")).toBeInTheDocument();
    expect(screen.getByText("不建议发布")).toBeInTheDocument();
    expect(screen.getByText("大家都知道")).toBeInTheDocument();
    expect(screen.getByText("流动性收紧")).toBeInTheDocument();
    expect(screen.getByText(/未调用真实 AI/)).toBeInTheDocument();
  });

  it("能手动粘贴文本质检并保存记录", async () => {
    const user = userEvent.setup();
    render(<ContentQualityPage />);

    await user.clear(screen.getByLabelText("内容名称"));
    await user.type(screen.getByLabelText("内容名称"), "手动文案");
    await user.type(
      screen.getByLabelText("要检查的文案"),
      "大家都知道，这个风口炸裂，背后是流动性收紧。",
    );
    await user.click(screen.getByRole("button", { name: "开始质检" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/content-quality/manual/check",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const manualCall = vi.mocked(fetch).mock.calls.find(
      ([url, init]) =>
        String(url) === "/api/content-quality/manual/check" &&
        init?.method === "POST",
    );
    const payload = JSON.parse(String(manualCall?.[1]?.body));
    expect(payload.source_title).toBe("手动文案");
    expect(payload.text).toContain("流动性收紧");
    expect(await screen.findByText("质检记录已保存：#7")).toBeInTheDocument();
  });

  it("能展示风险、收藏价值、问题和建议", async () => {
    const user = userEvent.setup();
    render(<ContentQualityPage />);
    await user.type(screen.getByLabelText("要检查的文案"), "显而易见，这就是财富密码。");
    await user.click(screen.getByRole("button", { name: "开始质检" }));

    const resultPanel = await screen.findByRole("heading", { name: "质检结果" });
    expect(resultPanel).toBeInTheDocument();
    expect(screen.getByText("总分")).toBeInTheDocument();
    expect(screen.getByText("风险等级")).toBeInTheDocument();
    expect(screen.getByText("收藏价值")).toBeInTheDocument();
    expect(screen.getByText("修改建议")).toBeInTheDocument();
    expect(
      within(screen.getByText("检查维度").closest("div") as HTMLElement).getByText(
        "投资建议风险",
      ),
    ).toBeInTheDocument();
  });
});
