import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EditingExportPage from "./EditingExportPage";
import type { EditingExportPackage } from "./types";

const packageData: EditingExportPackage = {
  pipeline_id: 8,
  status: "已生成待确认",
  project_name: "今日三件事",
  project_key: "p-8-today",
  project_dir: "exports/今日三件事",
  legacy_project_dir: null,
  timeline: [
    {
      segment_id: "shot-1",
      start: "00:00",
      end: "00:05",
      voiceover: "今天先看三件事。",
      visual_type: "素材画面",
      visual_direction: "城市和科技新闻快切",
      material_keywords: ["AI", "平台"],
      material_sources: ["本地素材库"],
      material_status: "进行中",
      material_path: "",
      needs_graphic: false,
      graphic_task: {},
      avatar_task: {},
      subtitle_strategy: "跟随口播",
      edit_instruction: "开头节奏快一点。",
    },
  ],
  versions: [
    {
      version_id: "main",
      name: "默认成片方案",
      status: "待最终确认",
      estimated_duration_seconds: 45,
      strategy: "按重要性排序，先讲最重要的事件。",
      best_for: "V1 默认成片。",
    },
  ],
  recommended_template: {
    version_id: "main",
    name: "默认成片方案",
    status: "待最终确认",
    estimated_duration_seconds: 45,
    strategy: "按重要性排序，先讲最重要的事件。",
    best_for: "V1 默认成片。",
  },
  export_task: {
    pipeline_id: 8,
    status: "待最终确认",
    need_final_confirmation: true,
    version_count: 1,
    timeline_version_id: "ts-8-0001-demo",
    timeline_version_number: 1,
    material_ready_count: 0,
    timeline_segment_count: 1,
    real_jianying_automation: false,
    real_avatar_generation: false,
    real_publish_executed: false,
  },
  generated_files: {
    timeline_json: "exports/今日三件事/timeline.json",
  },
  timeline_sync: {
    status: "current",
    current_version_id: "ts-8-0001-demo",
    previous_version_id: "ts-8-0001-demo",
    desired_input_hash: "d".repeat(64),
    affected_segment_ids: [],
    created_new_version: false,
  },
  timeline_spec: {
    status: "已保存",
    version_id: "ts-8-0001-demo",
    version_number: 1,
    segments: [],
    tracks: [],
    settings: { width: 1080, height: 1920 },
  },
  subtitle_preview: "今天先看三件事。",
  note: "V1 只生成剪辑数据结构。",
};

const stalePackageData = {
  ...packageData,
  timeline_sync: {
    status: "stale",
    current_version_id: "ts-8-0001-demo",
    previous_version_id: "ts-8-0001-demo",
    desired_input_hash: "c".repeat(64),
    affected_segment_ids: ["shot-1"],
    created_new_version: false,
  },
};

const rebuiltPackageData = {
  ...packageData,
  export_task: {
    ...packageData.export_task,
    timeline_version_id: "ts-8-0002-demo",
    timeline_version_number: 2,
  },
  timeline_spec: {
    ...packageData.timeline_spec,
    version_id: "ts-8-0002-demo",
    version_number: 2,
  },
  timeline_sync: {
    status: "current",
    current_version_id: "ts-8-0002-demo",
    previous_version_id: "ts-8-0001-demo",
    desired_input_hash: "c".repeat(64),
    affected_segment_ids: ["shot-1"],
    created_new_version: true,
  },
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

function errorResponse(status: number): Promise<Response> {
  return Promise.resolve({
    ok: false,
    status,
    json: async () => {
      throw new Error("no json");
    },
    text: async () => "",
  } as unknown as Response);
}

describe("剪辑页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/pipeline/8/editing-export") {
          return jsonResponse(packageData);
        }
        if (
          url === "/api/pipeline/8/editing-export/rebuild" &&
          init?.method === "POST"
        ) {
          return jsonResponse(packageData);
        }
        if (
          url === "/api/pipeline/8/voiceover/artifact" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            artifact_id: "as-8-1234567890abcdef",
            audio: {
              project_relative_path: "素材信息/音频字幕版本/as-8-1234567890abcdef/voiceover.wav",
              sha256: "a".repeat(64),
              duration_seconds: 5,
              mime: "audio/wav",
            },
            subtitles: {
              project_relative_path: "素材信息/音频字幕版本/as-8-1234567890abcdef/subtitles.srt",
              sha256: "b".repeat(64),
              mime: "application/x-subrip",
            },
            manifest_project_relative_path:
              "素材信息/音频字幕版本/as-8-1234567890abcdef/manifest.json",
            provider: "windows_sapi",
            created_at: "2026-07-18T00:00:00+00:00",
          });
        }
        if (
          url === "/api/pipeline/8/timeline-media/render" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            binding: {
              pipeline_id: 8,
              artifact_id: "as-8-1234567890abcdef",
              timeline_spec: {},
              artifact: {},
              publish_supported: false,
            },
            render: {
              version_id: "media-ts-8-0001",
              timeline_version_id: "ts-8-0001-demo",
              timeline_input_hash: "d".repeat(64),
              mode: "full_media_isolated",
              output_project_relative_path:
                "成片/versions/media-ts-8-0001/rendered.mp4",
              report_project_relative_path:
                "成片/versions/media-ts-8-0001/technical-report.json",
              visual_render_manifest_project_relative_path:
                "成片/versions/media-ts-8-0001/visual/render-manifest.json",
              artifact_sha256: "e".repeat(64),
              visual_rebuild: {
                source_timeline_version_id: "ts-8-0000-demo",
                reused_segment_ids: ["shot-0"],
                rebuilt_segment_ids: ["shot-1"],
              },
              video_report: {},
              media_report: {},
            },
            publish_supported: false,
          });
        }
        if (url === "/api/pipeline/8/quality-report/candidates") {
          return jsonResponse({ items: [] });
        }
        if (url === "/api/editing-export/end-to-end-test/latest") {
          return jsonResponse({
            成功: false,
            提示: "还没有生成过测试成片。",
            导出文件: {},
            素材匹配: [],
          });
        }
        if (
          url === "/api/editing-export/end-to-end-test" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            成功: true,
            选题: "AI 搜索正在改变普通人获取信息的方式",
            简略脚本: ["先说结论：信息入口开始换位置。"],
            画面数量: 8,
            素材匹配: [
              {
                镜头: "shot-01",
                时间段: "00:00-00:07",
                口播: "先说结论。",
                字幕: "信息入口开始换位置",
                画面说明: "官方证据页慢速滚动",
                素材关键词: ["AI 搜索"],
                素材状态: "已匹配",
                素材类型: "官方证据画面",
                素材路径: "D:/素材/自动下载/官方页面/test.mp4",
                匹配原因: "关键词匹配：AI搜索",
              },
            ],
            导出文件: {
              "最终成片 MP4": "exports/成片测试/一分钟测试成片.mp4",
            },
            剪辑参数: { 比例: "9:16" },
            提醒: [],
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("剪辑页能触发一分钟测试成片并显示结果", async () => {
    const user = userEvent.setup();
    render(<EditingExportPage pipelineId={8} />);

    expect(screen.getByRole("heading", { name: "剪辑" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "时间轴结构" })).toBeInTheDocument();
    expect(screen.getByText("ts-8-0001-demo")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "生成一分钟测试成片" }));

    expect(await screen.findByRole("heading", { name: "一分钟测试成片" })).toBeInTheDocument();
    expect(screen.getByText("exports/成片测试/一分钟测试成片.mp4")).toBeInTheDocument();
    expect(screen.getByText("关键词匹配：AI搜索")).toBeInTheDocument();
  });

  it("上一步没完成时显示空状态，不显示 500 报错", async () => {
    vi.mocked(fetch).mockImplementationOnce(() => errorResponse(500));

    render(<EditingExportPage pipelineId={8} />);

    expect(await screen.findByText("剪辑方案还没生成。")).toBeInTheDocument();
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/editing-export/end-to-end-test/latest",
        expect.anything(),
      );
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/请求失败/)).not.toBeInTheDocument();
  });

  it("剪辑页能从现代媒体草稿入口生成相对路径产物", async () => {
    const user = userEvent.setup();
    render(<EditingExportPage pipelineId={8} />);

    await screen.findByRole("heading", { name: "时间轴结构" });
    await user.click(screen.getAllByRole("button", { name: "生成现代媒体草稿" })[0]);

    expect(
      (await screen.findAllByText("成片/versions/media-ts-8-0001/rendered.mp4", {
        exact: false,
      })).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("本次复用 1 段，重建 1 段。")).toBeInTheDocument();
    const renderCall = vi
      .mocked(fetch)
      .mock.calls.find(([input]) => String(input) === "/api/pipeline/8/timeline-media/render");
    expect(renderCall?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(renderCall?.[1]?.body))).toMatchObject({
      artifact_id: "as-8-1234567890abcdef",
      timeline_version_id: "ts-8-0001-demo",
    });
  });

  it("镜头改选后先显示过期状态，生成新时间线前禁止渲染旧版本", async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/8/editing-export") {
        return jsonResponse(stalePackageData);
      }
      if (
        url === "/api/pipeline/8/editing-export/rebuild" &&
        init?.method === "POST"
      ) {
        return jsonResponse(rebuiltPackageData);
      }
      if (url === "/api/editing-export/end-to-end-test/latest") {
        return jsonResponse({ 成功: false, 提示: "", 导出文件: {}, 素材匹配: [] });
      }
      throw new Error(`未处理的请求：${url}`);
    });

    const user = userEvent.setup();
    render(<EditingExportPage pipelineId={8} />);

    expect(await screen.findByText("待生成新时间线：shot-1")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: /生成.*现代.*草稿/ })) {
      expect(button).toBeDisabled();
    }

    await user.click(screen.getByRole("button", { name: "生成变更时间线" }));

    expect(
      await screen.findByText("已生成新时间线 ts-8-0002-demo；受影响镜头：shot-1。"),
    ).toBeInTheDocument();
    expect(screen.getByText("当前版本：ts-8-0002-demo")).toBeInTheDocument();
  });

  it("刷新剪辑页后恢复已验证的当前媒体草稿状态", async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/pipeline/8/editing-export") {
        return jsonResponse(packageData);
      }
      if (url === "/api/pipeline/8/quality-report/candidates") {
        return jsonResponse({
          items: [
            {
              artifact_relative_path: "成片/versions/current/rendered.mp4",
              artifact_sha256: "a".repeat(64),
              size_bytes: 120,
              modified_at: "2026-07-18T01:00:00+00:00",
              timeline_version_id: "ts-8-0001-demo",
              timeline_input_hash: "d".repeat(64),
              timeline_status: "current",
              can_quality_check: true,
            },
          ],
        });
      }
      if (url === "/api/editing-export/end-to-end-test/latest") {
        return jsonResponse({ 成功: false, 提示: "", 导出文件: {}, 素材匹配: [] });
      }
      throw new Error(`未处理的请求：${url}`);
    });

    render(<EditingExportPage pipelineId={8} />);

    expect(await screen.findByText("已存在当前草稿")).toBeInTheDocument();
    expect(screen.getByText("成片/versions/current/rendered.mp4", { exact: false })).toBeInTheDocument();
  });
});
