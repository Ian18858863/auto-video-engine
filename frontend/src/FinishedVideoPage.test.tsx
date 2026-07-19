import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import FinishedVideoPage from "./FinishedVideoPage";

const finishedVideo = {
  成片ID: "AI_搜索_20260623_004937",
  标题: "AI 搜索正在改变普通人获取信息的方式",
  生成时间: "2026-06-23T00:49:37",
  画面数量: 8,
  状态: "已生成",
  成片目录: "D:/短视频工厂/归档/成片测试/AI_搜索_20260623_004937",
  最终成片路径:
    "D:/短视频工厂/归档/成片测试/AI_搜索_20260623_004937/最终成片.mp4",
  预览地址:
    "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937/video",
  提醒: [],
};

let completionIsComplete = false;

const qualityContract = {
  pipeline_id: 12,
  quality_contract_relative_path: "质量合同/quality-contract-v1.json",
  quality_contract_sha256: "e".repeat(64),
  contract: {
    format: "auto-video-engine-quality-contract-v1",
    contract_id: "qc-demo-r1",
    pipeline_id: 12,
    project_key: "demo",
    baseline: "r1-unified",
    target: {
      platform: "抖音",
      aspect_ratio: "9:16",
      duration_seconds: 60,
    },
    reference_pack: {
      status: "frozen",
      items: [
        {
          id: "reference_video",
          project_relative_path: "质量合同/references/reference.mp4",
          sha256: "f".repeat(64),
        },
      ],
    },
    must_preserve: [
      {
        id: "facts_traceable",
        label: "事实可追溯",
        criterion: "关键事实能追溯到项目内证据",
      },
      {
        id: "rights_auditable",
        label: "权利可审计",
        criterion: "正式素材已记录来源与许可",
      },
    ],
    allowed_differences: [
      {
        id: "audio_first_timing",
        label: "允许按实测旁白时长调整镜头秒数",
      },
    ],
    planned_exceed_dimensions: [
      {
        id: "information_clarity",
        label: "信息清晰度",
        criterion: "核心观点比 R1 基线更易理解",
        required_result: "exceeded",
      },
    ],
    created_at: "2026-07-18T00:00:00+00:00",
    source: "master-refactor-phase1-unified-baseline",
  },
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("成片页面", () => {
  beforeEach(() => {
    completionIsComplete = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (
          url === "/api/editing-export/finished-videos" ||
          url === "/api/pipeline/12/finished-videos"
        ) {
          return jsonResponse({
            items: [finishedVideo],
            note: "成片页只展示最终 MP4 管理。",
          });
        }
        if (url === "/api/pipeline/12/completion-status") {
          if (completionIsComplete) {
            return jsonResponse({
              pipeline_id: 12,
              status: "已完成",
              is_complete: true,
              completion_reason_code: "approved_artifact_verified",
              message: "当前成片与五项质量门均已通过验收。",
              next_action: "",
              next_surface: "none",
              current_step: "成片验收",
              needs_confirmation: false,
              evidence_present: true,
              quality_gate_state: "passed",
              artifact_relative_path: "成片/versions/current/rendered.mp4",
              quality_report_relative_path: "成片/quality-reports/current.json",
            });
          }
          return jsonResponse({
            pipeline_id: 12,
            status: "需要人工确认",
            is_complete: false,
            completion_reason_code: "approval_missing",
            message: "生产准备已完成，但尚未验收成片。",
            next_action: "打开成片页检查当前文件和验收状态",
            next_surface: "finished",
            current_step: "成片验收",
            needs_confirmation: true,
            evidence_present: false,
            quality_gate_state: "not_evaluated",
          });
        }
        if (url === "/api/pipeline/12/quality-contract") {
          return jsonResponse(qualityContract);
        }
        if (url === "/api/pipeline/12/quality-report/candidates") {
          return jsonResponse({
            items: [
              {
                artifact_relative_path: "成片/versions/old/rendered.mp4",
                artifact_sha256: "a".repeat(64),
                size_bytes: 100,
                modified_at: "2026-07-18T02:00:00+00:00",
                timeline_version_id: "ts-old",
                timeline_input_hash: "b".repeat(64),
                timeline_status: "historical",
                can_quality_check: false,
              },
              {
                artifact_relative_path: "成片/versions/current/rendered.mp4",
                artifact_sha256: "c".repeat(64),
                size_bytes: 120,
                modified_at: "2026-07-18T01:00:00+00:00",
                timeline_version_id: "ts-current",
                timeline_input_hash: "d".repeat(64),
                timeline_status: "current",
                can_quality_check: true,
              },
            ],
          });
        }
        if (
          url ===
            "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937/open-folder" &&
          init?.method === "POST"
        ) {
          return jsonResponse({ 已打开: true });
        }
        if (
          url ===
            "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937/export" &&
          init?.method === "POST"
        ) {
          return jsonResponse({ 已导出: true, 导出路径: "D:/短视频工厂/成片导出/AI.mp4" });
        }
        if (
          url ===
            "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937" &&
          init?.method === "DELETE"
        ) {
          return jsonResponse({ 已删除: true });
        }
        if (url === "/api/pipeline/12/finished-video/export" && init?.method === "POST") {
          return jsonResponse({ 成功: true });
        }
        if (url === "/api/pipeline/12/quality-report" && init?.method === "POST") {
          return jsonResponse({
            pipeline_id: 12,
            artifact_relative_path: "成片/versions/current/rendered.mp4",
            artifact_sha256: "c".repeat(64),
            quality_report_relative_path: "成片/quality-reports/current.json",
            quality_report_sha256: "1".repeat(64),
            report: { release_blockers: [], gates: {} },
            approval_required: true,
            publish_supported: false,
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("只展示成片管理，不显示测试脚本、字幕和素材匹配明细", async () => {
    render(<FinishedVideoPage pipelineId={null} />);

    expect(await screen.findByText("AI 搜索正在改变普通人获取信息的方式")).toBeInTheDocument();
    expect(screen.getByText("8 个画面")).toBeInTheDocument();
    expect(screen.getByText(/D:\/短视频工厂\/归档/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "预览" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开文件夹" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "修改" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
    expect(screen.queryByText("一分钟测试成片")).not.toBeInTheDocument();
    expect(screen.queryByText("测试脚本")).not.toBeInTheDocument();
    expect(screen.queryByText("字幕 SRT")).not.toBeInTheDocument();
    expect(screen.queryByText("素材匹配")).not.toBeInTheDocument();
  });

  it("可以打开文件夹、导出、删除成片记录", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={null} />);

    await screen.findByText("AI 搜索正在改变普通人获取信息的方式");
    await user.click(screen.getByRole("button", { name: "打开文件夹" }));
    await user.click(screen.getByRole("button", { name: "导出" }));
    await user.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937/open-folder",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        },
      );
      expect(fetch).toHaveBeenCalledWith(
        "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937/export",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        },
      );
      expect(fetch).toHaveBeenCalledWith(
        "/api/editing-export/finished-videos/AI_%E6%90%9C%E7%B4%A2_20260623_004937",
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
        },
      );
    });
    await waitFor(() => {
      expect(
        screen.queryByText("AI 搜索正在改变普通人获取信息的方式"),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByText("这条成片和生成目录已删除。")).toBeInTheDocument();
  });

  it("点击修改会进入剪辑页", async () => {
    const user = userEvent.setup();
    const onOpenEditing = vi.fn();
    render(<FinishedVideoPage pipelineId={12} onOpenEditing={onOpenEditing} />);

    await screen.findByText("AI 搜索正在改变普通人获取信息的方式");
    await user.click(screen.getByRole("button", { name: "修改" }));

    expect(onOpenEditing).toHaveBeenCalledWith(12);
  });

  it("成片批准后提供手动发布记录入口", async () => {
    completionIsComplete = true;
    const user = userEvent.setup();
    const onOpenTracking = vi.fn();
    render(
      <FinishedVideoPage
        pipelineId={12}
        onOpenTracking={onOpenTracking}
      />,
    );

    const button = await screen.findByRole("button", { name: "记录手动发布" });
    await user.click(button);

    expect(onOpenTracking).toHaveBeenCalledTimes(1);
  });

  it("有项目时生成正式项目成片，不再调用固定测试脚本", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={12} />);

    await screen.findByText("AI 搜索正在改变普通人获取信息的方式");
    expect(screen.getByText("尚未提交五门验收证据")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "生成成片" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/12/finished-video/export",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        },
      );
    });
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/editing-export/end-to-end-test",
      expect.anything(),
    );
    expect(screen.getByText("项目成片已生成。")).toBeInTheDocument();
  });

  it("人工验收默认选择当前时间线，历史成片只可查看不可验收", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={12} />);

    await screen.findByText("尚未提交五门验收证据");
    await user.click(screen.getByRole("button", { name: "加载可验收成片" }));

    const select = await screen.findByRole("combobox", {
      name: "选择项目内渲染版本",
    });
    expect(select).toHaveValue("成片/versions/current/rendered.mp4");
    expect(
      screen.getByRole("option", { name: /old.*历史版本.*不可验收/ }),
    ).toBeDisabled();
  });

  it("读取只读质量合同并提交逐项结构化验收结果", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={12} />);

    const contractPanel = await screen.findByRole("region", { name: "只读质量合同" });
    expect(contractPanel).toHaveTextContent("发布平台抖音");
    expect(contractPanel).toHaveTextContent("画面比例9:16");
    expect(contractPanel).toHaveTextContent("目标时长60 秒");
    const contractHash = screen.getByLabelText(`质量合同 SHA-256：${"e".repeat(64)}`);
    expect(contractHash).toHaveTextContent("eeeeeeeeeeee…eeeeeeee");
    expect(contractHash).not.toHaveTextContent("e".repeat(64));
    expect(
      screen.getByText("质量合同/references/reference.mp4"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("我确认节奏、字幕、声音符合质量标准"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "加载可验收成片" }));
    await user.click(screen.getByRole("checkbox", { name: "保持项：事实可追溯" }));
    await user.type(screen.getByLabelText("保持项证据：事实可追溯"), "来源清单已逐条核对");
    await user.click(screen.getByRole("checkbox", { name: "保持项：权利可审计" }));
    await user.type(screen.getByLabelText("保持项证据：权利可审计"), "素材许可记录已复核");
    await user.selectOptions(
      screen.getByLabelText("允许差异结果：允许按实测旁白时长调整镜头秒数"),
      "used",
    );
    await user.type(
      screen.getByLabelText("实际差异：允许按实测旁白时长调整镜头秒数"),
      "旁白长 2 秒，镜头已重新定时",
    );
    await user.type(
      screen.getByLabelText("允许差异证据：允许按实测旁白时长调整镜头秒数"),
      "TimelineSpec v2",
    );
    await user.selectOptions(screen.getByLabelText("超越结果：信息清晰度"), "exceeded");
    await user.type(screen.getByLabelText("实际变化：信息清晰度"), "信息层级更清楚");
    await user.type(screen.getByLabelText("超越证据：信息清晰度"), "逐项对照 R1");
    await user.click(screen.getByRole("checkbox", { name: "我确认画面与脚本叙事一致" }));
    await user.click(
      screen.getByRole("checkbox", { name: "我确认事实来源与素材权利已检查" }),
    );
    await user.click(screen.getByRole("button", { name: "生成质量报告" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/pipeline/12/quality-report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          artifact_relative_path: "成片/versions/current/rendered.mp4",
          gates: {
            visual_narration: {
              status: "passed",
              evidence: ["Ian 已完成视频播放并确认画面与脚本一致"],
            },
            facts_rights: {
              status: "passed",
              evidence: ["Ian 已检查事实来源与素材权利"],
            },
          },
          quality_contract_review: {
            contract_sha256: "e".repeat(64),
            must_preserve_results: {
              facts_traceable: {
                result: "passed",
                evidence: "来源清单已逐条核对",
              },
              rights_auditable: {
                result: "passed",
                evidence: "素材许可记录已复核",
              },
            },
            allowed_difference_results: {
              audio_first_timing: {
                result: "used",
                actual_difference: "旁白长 2 秒，镜头已重新定时",
                evidence: "TimelineSpec v2",
              },
            },
            planned_exceed_results: {
              information_clarity: {
                result: "exceeded",
                difference: "信息层级更清楚",
                evidence: "逐项对照 R1",
              },
            },
          },
        }),
      });
    });
  });

  it("质量合同表单变化会清空旧报告和播放确认", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={12} />);

    await screen.findByRole("region", { name: "只读质量合同" });
    await user.click(screen.getByRole("button", { name: "加载可验收成片" }));
    await user.click(screen.getByRole("checkbox", { name: "保持项：事实可追溯" }));
    await user.type(screen.getByLabelText("保持项证据：事实可追溯"), "旧证据");
    await user.click(screen.getByRole("checkbox", { name: "保持项：权利可审计" }));
    await user.type(screen.getByLabelText("保持项证据：权利可审计"), "许可已核对");
    await user.type(
      screen.getByLabelText("允许差异证据：允许按实测旁白时长调整镜头秒数"),
      "本片未使用",
    );
    await user.selectOptions(screen.getByLabelText("超越结果：信息清晰度"), "exceeded");
    await user.type(screen.getByLabelText("实际变化：信息清晰度"), "层级更清楚");
    await user.type(screen.getByLabelText("超越证据：信息清晰度"), "逐项比较记录");
    fireEvent.ended(
      document.querySelector('video[src*="quality-report/preview"]') as HTMLVideoElement,
    );
    await user.click(screen.getByRole("button", { name: "生成质量报告" }));
    expect(await screen.findByText(/质量报告：成片\/quality-reports\/current.json/)).toBeInTheDocument();
    expect(screen.getByText("已完整播放当前版本。")).toBeInTheDocument();

    await user.type(screen.getByLabelText("保持项证据：事实可追溯"), "，新证据");

    expect(screen.queryByText(/质量报告：成片\/quality-reports\/current.json/)).not.toBeInTheDocument();
    expect(
      screen.getByText("请完整播放当前版本；播放结束才算完成视觉验收。"),
    ).toBeInTheDocument();
  });

  it("质量合同漏填时不会发送伪完整报告", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={12} />);

    await screen.findByRole("region", { name: "只读质量合同" });
    await user.click(screen.getByRole("button", { name: "加载可验收成片" }));
    await user.click(screen.getByRole("button", { name: "生成质量报告" }));

    expect(
      await screen.findByText(
        "请为质量合同每一项填写结果和证据；使用允许差异时还要写明实际差异。",
      ),
    ).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/pipeline/12/quality-report",
      expect.anything(),
    );
  });

  it("没有项目时不会偷偷生成测试成片", async () => {
    const user = userEvent.setup();
    render(<FinishedVideoPage pipelineId={null} />);

    await screen.findByText("AI 搜索正在改变普通人获取信息的方式");
    await user.click(screen.getByRole("button", { name: "生成成片" }));

    expect(screen.getByText("请先从任务或剪辑选择一个项目，再生成成片。")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/editing-export/end-to-end-test",
      expect.anything(),
    );
  });
});
