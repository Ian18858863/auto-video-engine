import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ScriptWorkspace from "./ScriptWorkspace";
import type { ScriptVersionDetail } from "./types";

const workspace = {
  pipeline_id: 18,
  status: "已完成",
  provider: "fake",
  project_name: "为什么企业开始认真采购 AI 工具",
  project_dir: "D:/短视频工厂/项目/AI工具采购",
  hotspot: {
    id: 2,
    title: "AI 工具进入企业采购流程",
    summary: "企业开始把 AI 工具放进真实预算。",
  },
  topic: {
    id: 12,
    title: "为什么企业开始认真采购 AI 工具",
    core_view: "企业买的不是新鲜感，而是真正省钱提效的能力。",
    recommended_format: "数字人口播",
    recommended_duration: "45 秒",
    risk_note: "不做投资建议。",
    hook: "为什么企业突然开始认真买 AI 工具？",
  },
  script: {
    id: 42,
    title: "为什么企业开始认真采购 AI 工具",
    voiceover: "企业买的不是新鲜感，而是真正能省钱提效的能力。",
    storyboard: [
      {
        start: "00:00",
        end: "00:03",
        voiceover: "为什么企业突然开始认真买 AI 工具？",
        recommended_visual: "动态标题页",
      },
    ],
    subtitles: [
      {
        start: "00:00",
        end: "00:03",
        text: "为什么企业突然开始认真买 AI 工具？",
      },
    ],
    title_options: [
      { title: "企业为什么开始真买 AI 工具", type: "趋势解释型" },
      { title: "AI 工具采购进入真实预算", type: "备用标题" },
    ],
    cover_copy: [{ text: "AI 工具开始进入真实预算" }],
    publish_copy: {
      抖音: "不复述热闹，只拆商业逻辑。",
      小红书: "备用发布文案不应默认铺开。",
    },
    use_avatar: true,
  },
  material_tasks: [
    {
      shot_id: "shot-01",
      time_range: "00:00-00:03",
      voiceover: "为什么企业突然开始认真买 AI 工具？",
      recommended_visual: "动态标题页",
      material_type: "Codex 生成图文",
      jianying_keywords: ["AI工具", "企业采购"],
      needs_codex_graphic: true,
      needs_avatar: true,
      status: "未开始",
      material_path: "",
    },
  ],
  graphic_tasks: [
    {
      shot_id: "shot-01",
      time_range: "00:00-00:03",
      visual_form: "动态标题页",
      core_message: "企业开始认真采购 AI 工具",
      keywords: ["AI工具"],
      status: "未开始",
      save_path: "PPT动态图文",
      codex_skill_executed: false,
    },
  ],
  avatar_tasks: [
    {
      shot_id: "shot-01",
      required: true,
      position: "右下角",
      state: "正在讲解",
      avoid_subtitle: true,
      avoid_chart: true,
      lip_sync: false,
      material_path: "",
      generated: false,
    },
  ],
  exports: {
    script_md: {
      generated: true,
      path: "exports/短视频工厂/项目/AI工具采购/素材信息/script.md",
    },
    shotlist_csv: {
      generated: true,
      path: "exports/短视频工厂/项目/AI工具采购/素材信息/shotlist.csv",
    },
    edit_manifest_json: {
      generated: true,
      path: "exports/短视频工厂/项目/AI工具采购/素材信息/edit_manifest.json",
    },
  },
  script_factory: {
    status: "imported",
    job_id: "pipeline-18-test",
    job_dir:
      "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test",
    file_path:
      "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
    prompt_path:
      "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
    context_path:
      "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_context.json",
    template_path:
      "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result_template.json",
    result_path:
      "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result.json",
    result_exists: true,
    real_ai_called: false,
    failure_reason: "",
    next_suggestion: "脚本结果已导入。下一步自动验收。",
    accepted: false,
    dispatch_supported: false,
    thread_id: "",
    manual_prompt: "",
    script_version_id: "",
    script_version_hash: "",
    script_version_snapshot_hash: "",
    restore_operation_id: "",
    restored_from_version_id: "",
    ready_for_review: true,
  },
  script_versions: [
    {
      version_id: "sv-18-0001-demo",
      version_number: 1,
      input_hash: "input-hash",
      content_hash: "1".repeat(64),
      snapshot_hash: "a".repeat(64),
      integrity_status: "legacy_payload_only",
      approval_integrity_status: "legacy_version_id_only",
      status: "approved",
      source: "script_factory_import",
      created_at: "2026-07-18T10:00:00+08:00",
      approved_at: "2026-07-18T10:01:00+08:00",
      shot_count: 1,
    },
  ],
  active_script_version_id: "sv-18-0001-demo",
  script_state_sha256: "d".repeat(64),
  shot_plan: [
    {
      shot_id: "shot-01",
      beat_id: "beat-01",
      beat_type: "evidence",
      viewer_takeaway: "看懂企业为什么开始买",
      evidence_kind: "direct_evidence",
      visual_type: "官方证据画面",
      visual_intent: "展示采购证据",
      subject: "企业 AI 预算表",
      action: "高亮预算项目和金额",
      location: "官方报告页面",
      time_context: "2026 年",
      composition: "来源标题在上，预算数据居中，出处固定在底部",
      sound_notes: "数据出现时使用一次轻提示音",
      start: "00:00.00",
      end: "00:03.00",
      duration_seconds: 3,
      subtitle_load: 20,
      voiceover: "为什么企业突然开始认真买 AI 工具？",
      source_refs: [{ url: "https://example.com/report", kind: "source" }],
      source_priority: ["官方录屏", "信息动效"],
      forbidden_items: ["不得用气氛素材冒充事实证明"],
      acceptance_conditions: ["画面必须能回到记录的来源页或原始证据"],
    },
  ],
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

let workspaceResponse = workspace;

const scriptVersionDetails: Record<string, ScriptVersionDetail> = {
  "sv-18-0001-demo": {
    version_id: "sv-18-0001-demo",
    version_number: 1,
    input_hash: "input-hash-1",
    content_hash: "1".repeat(64),
    snapshot_hash: "a".repeat(64),
    integrity_status: "legacy_payload_only",
    approval_integrity_status: "legacy_version_id_only",
    status: "approved",
    source: "script_factory_import",
    created_at: "2026-07-18T10:00:00+08:00",
    approved_at: "2026-07-18T10:01:00+08:00",
    invalidated_at: "",
    invalidated_reason: "",
    payload: { voiceover: "第一版完整口播全文。" },
    fact_sources: [{ url: "https://example.com/report" }],
    shot_plan: [{ shot_id: "shot-01" }],
  },
  "sv-18-0002-demo": {
    version_id: "sv-18-0002-demo",
    version_number: 2,
    input_hash: "input-hash-2",
    content_hash: "2".repeat(64),
    snapshot_hash: "b".repeat(64),
    integrity_status: "full_envelope_verified",
    approval_integrity_status: "none",
    status: "draft",
    source: "manual_revision",
    created_at: "2026-07-18T11:00:00+08:00",
    approved_at: "",
    invalidated_at: "",
    invalidated_reason: "",
    payload: { voiceover: "第二版完整口播全文，保留了新的事实表述。" },
    fact_sources: [
      { url: "https://example.com/report" },
      { url: "https://example.com/notice" },
    ],
    shot_plan: [{ shot_id: "shot-01" }, { shot_id: "shot-02" }],
  },
};

function qualityResult(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    pipeline_id: 18,
    script_asset_id: 42,
    source_type: "脚本工作台",
    source_title: "为什么企业开始认真采购 AI 工具",
    checked_text: "企业买的不是新鲜感，而是真正能省钱提效的能力。",
    created_at: "2026-06-18T10:00:00+08:00",
    total_score: 92,
    publish_suggestion: "可以发布",
    max_issue: "整体符合账号风格，发布前只需核对事实和来源。",
    issues: [],
    suggestions: [],
    matched_banned_expressions: [],
    professional_terms_need_explanation: [],
    risk_level: "低",
    collect_value: "高",
    series_worthy: true,
    style_source: "账号风格库",
    checked_dimensions: ["禁用表达", "账号定位"],
    local_rules_only: true,
    real_ai_called: false,
    ...overrides,
  };
}

function workspaceWithAvatar(useAvatar: boolean) {
  return {
    ...workspace,
    script: {
      ...workspace.script,
      use_avatar: useAvatar,
    },
    material_tasks: workspace.material_tasks.map((task) => ({
      ...task,
      needs_avatar: useAvatar,
    })),
    avatar_tasks: workspace.avatar_tasks.map((task) => ({
      ...task,
      required: useAvatar,
      enabled: useAvatar,
      disabled_reason: useAvatar ? "" : "本期选择不添加数字人",
    })),
  };
}

function workspaceWithScriptFactory(overrides: Record<string, unknown>) {
  return {
    ...workspace,
    script_factory: {
      ...workspace.script_factory,
      ...overrides,
    },
  };
}

describe("脚本工作台页面", () => {
  beforeEach(() => {
    workspaceResponse = workspace;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/pipeline/18/workspace" && !init?.method) {
          return jsonResponse(workspaceResponse);
        }
        const versionId = url.match(
          /^\/api\/pipeline\/18\/script-versions\/([^/]+)$/,
        )?.[1];
        if (versionId && !init?.method && scriptVersionDetails[versionId]) {
          return jsonResponse(scriptVersionDetails[versionId]);
        }
        const restoreVersionId = url.match(
          /^\/api\/pipeline\/18\/script-versions\/([^/]+)\/restore$/,
        )?.[1];
        if (restoreVersionId && init?.method === "POST") {
          const request = JSON.parse(String(init.body)) as { operation_id: string };
          const source = scriptVersionDetails[restoreVersionId];
          const nextVersion = {
            version_id: "sv-18-0003-restored",
            version_number: 3,
            input_hash: source.input_hash,
            content_hash: source.content_hash,
            snapshot_hash: "c".repeat(64),
            integrity_status: "full_envelope_verified",
            approval_integrity_status: "none",
            status: "draft",
            source: `restore:${restoreVersionId}`,
            created_at: "2026-07-18T12:00:00+08:00",
            approved_at: "",
            shot_count: Array.isArray(source.shot_plan) ? source.shot_plan.length : 0,
          };
          workspaceResponse = {
            ...workspaceResponse,
            script: {
              ...workspaceResponse.script,
              voiceover: String((source.payload as { voiceover?: string }).voiceover ?? ""),
            },
            script_factory: {
              ...workspaceResponse.script_factory,
              status: "draft_edited",
              accepted: false,
              script_version_id: nextVersion.version_id,
              script_version_hash: nextVersion.content_hash,
              script_version_snapshot_hash: nextVersion.snapshot_hash,
              restore_operation_id: request.operation_id,
              restored_from_version_id: restoreVersionId,
              next_suggestion: "历史版本已复制为新草稿，请重新生成任务包并验收。",
            },
            script_versions: [...(workspaceResponse.script_versions ?? []), nextVersion],
            active_script_version_id: nextVersion.version_id,
            script_state_sha256: "e".repeat(64),
          };
          return jsonResponse({
            operation: {
              operation_id: request.operation_id,
              source_version_id: restoreVersionId,
              restored_version_id: nextVersion.version_id,
              restored_version_number: nextVersion.version_number,
              status: "draft",
              idempotent: false,
            },
            workspace: workspaceResponse,
          });
        }
        if (
          url === "/api/content-quality/pipeline/18/check" &&
          init?.method === "POST"
        ) {
          return jsonResponse(qualityResult());
        }
        if (url === "/api/pipeline/18/continue" && init?.method === "POST") {
          return jsonResponse({
            pipeline_id: 18,
            hotspot_id: 2,
            topic_idea_id: 12,
            script_asset_id: 42,
            project_name: "为什么企业开始认真采购 AI 工具",
            project_dir: "exports/短视频工厂/项目/AI工具采购",
            status: "已完成",
            steps: [],
            generated_files: {},
            failure_reasons: [],
            needs_confirmation: false,
            next_suggestion: "可以进入自动剪辑查看导出文件。",
          });
        }
        if (
          url === "/api/pipeline/18/workspace/script" &&
          init?.method === "PUT"
        ) {
          return jsonResponse(workspace);
        }
        if (
          url === "/api/pipeline/18/workspace/presentation" &&
          init?.method === "PUT"
        ) {
          const payload = JSON.parse(String(init.body)) as { use_avatar: boolean };
          return jsonResponse(workspaceWithAvatar(payload.use_avatar));
        }
        if (
          url === "/api/pipeline/18/voiceover/generate" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            pipeline_id: 18,
            script_asset_id: 42,
            success: true,
            provider: "windows_sapi",
            audio_path: "D:/短视频工厂/项目/AI工具采购/素材信息/口播音频.wav",
            subtitle_path: "D:/短视频工厂/项目/AI工具采购/素材信息/字幕.srt",
            subtitle_preview: "1\n00:00:00,000 --> 00:00:03,000\n企业买的不是新鲜感",
            message: "本地 TTS 配音已生成。",
            real_tts_api_called: false,
            huoshan_tts_ready: true,
          });
        }
        if (
          url === "/api/pipeline/18/script-work-order/build" &&
          init?.method === "POST"
        ) {
          workspaceResponse = workspaceWithScriptFactory({
            status: "built",
            result_exists: false,
            ready_for_review: false,
          });
          return jsonResponse({
            pipeline_id: 18,
            script_asset_id: 42,
            status: "built",
            job_id: "pipeline-18-test",
            job_dir:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test",
            file_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            prompt_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            context_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_context.json",
            template_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result_template.json",
            result_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result.json",
            result_exists: false,
            real_ai_called: false,
            failure_reason: "",
            next_suggestion: "任务包已生成。下一步尝试派发 Codex 专门线程。",
            quality_check: null,
            accepted: false,
            dispatch_supported: false,
            thread_id: "",
            manual_prompt: "",
          });
        }
        if (
          url === "/api/pipeline/18/script-work-order/dispatch" &&
          init?.method === "POST"
        ) {
          workspaceResponse = workspaceWithScriptFactory({
            status: "manual_dispatch_required",
            result_exists: false,
            ready_for_review: false,
            failure_reason: "后端网页不能直接新建 Codex Desktop 对话。",
            manual_prompt:
              "复制下面内容到新的 Codex 对话。\n请读取 script_factory_prompt.md，完成后写 script_factory_result.json。",
          });
          return jsonResponse({
            pipeline_id: 18,
            script_asset_id: 42,
            status: "manual_dispatch_required",
            job_id: "pipeline-18-test",
            job_dir:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test",
            file_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            prompt_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            context_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_context.json",
            template_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result_template.json",
            result_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result.json",
            result_exists: false,
            real_ai_called: false,
            failure_reason: "后端网页不能直接新建 Codex Desktop 对话。",
            next_suggestion: "复制下面内容到新的 Codex 对话。",
            quality_check: null,
            accepted: false,
            dispatch_supported: false,
            thread_id: "",
            manual_prompt:
              "复制下面内容到新的 Codex 对话。\n请读取 script_factory_prompt.md，完成后写 script_factory_result.json。",
          });
        }
        if (
          url === "/api/pipeline/18/script-work-order/import" &&
          init?.method === "PUT"
        ) {
          workspaceResponse = workspaceWithScriptFactory({
            status: "imported",
            result_exists: true,
            ready_for_review: true,
            failure_reason: "",
            manual_prompt: "",
          });
          return jsonResponse({
            pipeline_id: 18,
            script_asset_id: 42,
            status: "imported",
            job_id: "pipeline-18-test",
            job_dir:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test",
            file_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            prompt_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            context_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_context.json",
            template_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result_template.json",
            result_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result.json",
            result_exists: true,
            real_ai_called: false,
            failure_reason: "",
            next_suggestion: "脚本结果已导入。下一步自动验收。",
            quality_check: null,
            accepted: false,
            dispatch_supported: false,
            thread_id: "",
            manual_prompt: "",
          });
        }
        if (
          url === "/api/pipeline/18/script-work-order/accept" &&
          init?.method === "POST"
        ) {
          workspaceResponse = workspaceWithScriptFactory({
            status: "accepted",
            result_exists: true,
            ready_for_review: true,
            accepted: true,
            failure_reason: "",
            manual_prompt: "",
          });
          return jsonResponse({
            pipeline_id: 18,
            script_asset_id: 42,
            status: "accepted",
            job_id: "pipeline-18-test",
            job_dir:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test",
            file_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            prompt_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_prompt.md",
            context_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_context.json",
            template_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result_template.json",
            result_path:
              "D:/短视频工厂/项目/AI工具采购/素材信息/脚本工厂/pipeline-18-test/script_factory_result.json",
            result_exists: true,
            real_ai_called: false,
            failure_reason: "",
            next_suggestion: "脚本已通过验收，已进入素材流程。",
            quality_check: qualityResult(),
            accepted: true,
            dispatch_supported: false,
            thread_id: "",
            manual_prompt: "",
          });
        }
        if (url === "/api/pipeline/18/rewrite" && init?.method === "POST") {
          const part = JSON.parse(String(init.body)).part as string;
          const partLabels: Record<string, string> = {
            voiceover: "口播稿",
            storyboard: "分镜脚本",
            title: "标题",
            publish_copy: "发布文案",
          };
          return jsonResponse({
            pipeline_id: 18,
            script_asset_id: 42,
            part,
            part_label: partLabels[part] || "局部内容",
            used_account_style: true,
            used_copywriting_samples: false,
            real_ai_called: false,
            message: "已用账号风格库完成本地重写。",
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("能打开并显示脚本内容", async () => {
    render(<ScriptWorkspace pipelineId={18} />);

    expect(screen.getByRole("heading", { name: "脚本" })).toBeInTheDocument();
    expect(
      await screen.findByDisplayValue("为什么企业突然开始认真买 AI 工具？"),
    ).toBeInTheDocument();
    expect(screen.getByText("为什么企业开始认真采购 AI 工具")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "脚本确认" })).toBeInTheDocument();
    expect(screen.queryByText("人工决策点")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "确认数字人口播稿" })).not.toBeInTheDocument();
    const summary = screen.getByLabelText("脚本联动摘要");
    expect(summary).toHaveTextContent("分镜");
    expect(summary).toHaveTextContent("1 段");
    expect(summary).toHaveTextContent("素材关键词");
    expect(summary).toHaveTextContent("2 个");
    expect(summary).toHaveTextContent("数字人字段");
    expect(summary).toHaveTextContent("1 / 1 段");
    expect(summary).toHaveTextContent("剪辑节奏");
    expect(summary).toHaveTextContent("1 段 / 3秒");
    expect(screen.getByRole("checkbox", { name: "添加数字人" })).toBeChecked();
    expect(screen.queryByRole("button", { name: "素材任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "图文任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "数字人字段" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "导出文件" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "字幕 / 脚本分镜" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Shot Director 语义分镜" })).toBeInTheDocument();
    expect(screen.getByText("evidence · 直接证据")).toBeInTheDocument();
    expect(screen.getByText("主体：企业 AI 预算表")).toBeInTheDocument();
    expect(screen.getByText("动作：高亮预算项目和金额")).toBeInTheDocument();
    expect(screen.getByText("地点：官方报告页面")).toBeInTheDocument();
    expect(screen.getByText("时间：2026 年")).toBeInTheDocument();
    expect(
      screen.getByText("构图：来源标题在上，预算数据居中，出处固定在底部"),
    ).toBeInTheDocument();
    expect(screen.getByText("声音：数据出现时使用一次轻提示音")).toBeInTheDocument();
    expect(screen.getByText("不得用气氛素材冒充事实证明")).toBeInTheDocument();
    expect(screen.getByText("当前版本 sv-18-0001-demo")).toBeInTheDocument();
    expect(screen.getByText("字幕（可配音）")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "发布文案" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重写口播" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重写分镜" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重写标题" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重写文案" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成配音字幕" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成 Codex 脚本任务包" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "派发 Codex 线程" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "从结果文件导入" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "验收并进入素材" })).toBeInTheDocument();
    expect(screen.queryByLabelText("导入 Codex JSON")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "添加一段" })).toBeInTheDocument();
    expect(screen.getByLabelText("编辑标题")).toHaveValue("企业为什么开始真买 AI 工具");
    expect(screen.getByLabelText("编辑发布文案")).toHaveValue("不复述热闹，只拆商业逻辑。");
    expect(screen.queryByDisplayValue("AI 工具采购进入真实预算")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("备用发布文案不应默认铺开。")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "封面文案" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "字幕内容" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("需要修改")).not.toBeInTheDocument();
    expect(screen.queryByText("对应热点：AI 工具进入企业采购流程")).not.toBeInTheDocument();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) => String(url).includes("/script-versions/")),
    ).toHaveLength(0);
  });

  it("两版脚本可按需加载只读对比，不提前读取版本详情", async () => {
    const user = userEvent.setup();
    workspaceResponse = {
      ...workspace,
      script_factory: {
        ...workspace.script_factory,
        status: "draft_edited",
      },
      script_versions: [
        workspace.script_versions[0],
        {
          version_id: "sv-18-0002-demo",
          version_number: 2,
          input_hash: "input-hash-2",
          content_hash: "2".repeat(64),
          snapshot_hash: "b".repeat(64),
          integrity_status: "full_envelope_verified",
          approval_integrity_status: "none",
          status: "draft",
          source: "manual_revision",
          created_at: "2026-07-18T11:00:00+08:00",
          approved_at: "",
          shot_count: 2,
        },
      ],
    };

    render(<ScriptWorkspace pipelineId={18} />);

    expect(await screen.findByRole("heading", { name: "脚本版本对比" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "派发 Codex 线程" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "从结果文件导入" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "验收并进入素材" })).toBeDisabled();
    expect(screen.getByText("当前是手动修改草稿；旧任务包不能验收，请重新生成任务包。")).toBeInTheDocument();
    expect(
      screen.getByText("历史版本保持只读；恢复会复制成新草稿，不覆盖旧版本，也不会自动批准。"),
    ).toBeInTheDocument();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) => String(url).includes("/script-versions/")),
    ).toHaveLength(0);

    await user.selectOptions(screen.getByLabelText("对比版本 A"), "sv-18-0002-demo");
    await user.selectOptions(screen.getByLabelText("对比版本 B"), "sv-18-0001-demo");
    await user.click(screen.getByRole("button", { name: "加载版本对比" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/script-versions/sv-18-0002-demo",
        expect.objectContaining({ headers: { Accept: "application/json" } }),
      );
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/script-versions/sv-18-0001-demo",
        expect.objectContaining({ headers: { Accept: "application/json" } }),
      );
    });
    expect(await screen.findByText("第二版完整口播全文，保留了新的事实表述。")).toBeInTheDocument();
    expect(screen.getByText("第一版完整口播全文。")).toBeInTheDocument();
    expect(screen.getByText("状态：草稿")).toBeInTheDocument();
    expect(screen.getByText("状态：已批准")).toBeInTheDocument();
    expect(screen.getByText("来源：manual_revision")).toBeInTheDocument();
    expect(screen.getByText("来源：script_factory_import")).toBeInTheDocument();
    expect(screen.getByText("创建时间：2026-07-18T11:00:00+08:00")).toBeInTheDocument();
    expect(screen.getByText("创建时间：2026-07-18T10:00:00+08:00")).toBeInTheDocument();
    expect(screen.getByText("事实来源：2 条")).toBeInTheDocument();
    expect(screen.getByText("事实来源：1 条")).toBeInTheDocument();
    expect(screen.getByText("分镜：2 个")).toBeInTheDocument();
    expect(screen.getByText("分镜：1 个")).toBeInTheDocument();
    expect(
      screen.getByText("版本完整性：旧版仅校验脚本内容，ShotPlan 不可用于生产"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("批准绑定：旧批准未绑定完整快照，不可用于生产"),
    ).toBeInTheDocument();
    expect(screen.getByText("验收证据：未绑定，不能用于生产")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "恢复版本 2 为新草稿" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批准版本/ })).not.toBeInTheDocument();
  });

  it("恢复历史版本前明确说明影响，确认后复制为新草稿并保留旧版本", async () => {
    const user = userEvent.setup();
    workspaceResponse = {
      ...workspace,
      script_factory: {
        ...workspace.script_factory,
        status: "accepted",
        accepted: true,
        script_version_id: "sv-18-0002-demo",
        script_version_hash: "2".repeat(64),
        script_version_snapshot_hash: "b".repeat(64),
      },
      script_versions: [
        workspace.script_versions[0],
        {
          version_id: "sv-18-0002-demo",
          version_number: 2,
          input_hash: "input-hash-2",
          content_hash: "2".repeat(64),
          snapshot_hash: "b".repeat(64),
          integrity_status: "full_envelope_verified",
          approval_integrity_status: "snapshot_hash_bound",
          status: "approved",
          source: "script_factory_import",
          created_at: "2026-07-18T11:00:00+08:00",
          approved_at: "2026-07-18T11:01:00+08:00",
          shot_count: 2,
        },
      ],
      active_script_version_id: "sv-18-0002-demo",
    };

    render(<ScriptWorkspace pipelineId={18} />);
    await screen.findByRole("heading", { name: "脚本版本对比" });
    await user.click(screen.getByRole("button", { name: "加载版本对比" }));
    await screen.findByText("第一版完整口播全文。");

    await user.click(screen.getByRole("button", { name: "恢复版本 1 为新草稿" }));

    const confirmation = screen.getByRole("alertdialog", {
      name: "确认恢复脚本版本 1",
    });
    expect(confirmation).toHaveTextContent("创建新的版本 3 草稿");
    expect(confirmation).toHaveTextContent("当前批准会失效");
    expect(confirmation).toHaveTextContent("历史版本不会被修改");
    expect(
      vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/restore")),
    ).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "确认恢复为新草稿" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/script-versions/sv-18-0001-demo/restore",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const restoreCall = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => String(url).endsWith("/restore"));
    const body = JSON.parse(String(restoreCall?.[1]?.body));
    expect(body.operation_id).toMatch(/^[0-9a-f]{32}$/);
    expect(body.source_content_hash).toBe("1".repeat(64));
    expect(body.source_snapshot_hash).toBe("a".repeat(64));
    expect(body.expected_current_version_id).toBe("sv-18-0002-demo");
    expect(body.expected_current_content_hash).toBe("2".repeat(64));
    expect(body.expected_current_snapshot_hash).toBe("b".repeat(64));
    expect(body.expected_script_state_sha256).toBe("d".repeat(64));
    expect(
      await screen.findByText(
        "已从版本 1 创建版本 3 草稿。旧版本仍保留；重新生成任务包并验收后才能进入素材。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("可以关闭数字人并切换为配音字幕模式", async () => {
    const user = userEvent.setup();
    render(<ScriptWorkspace pipelineId={18} />);
    await screen.findByRole("heading", { name: "脚本确认" });

    await user.click(screen.getByRole("checkbox", { name: "添加数字人" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/workspace/presentation",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    expect(screen.getByRole("checkbox", { name: "添加数字人" })).not.toBeChecked();
    expect(screen.getByLabelText("脚本联动摘要")).toHaveTextContent("未启用");
    expect(screen.queryByRole("button", { name: "素材任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "数字人字段" })).not.toBeInTheDocument();
  });

  it("头部只保留刷新，不再放返回、质检和素材库入口", async () => {
    const user = userEvent.setup();
    render(<ScriptWorkspace pipelineId={18} />);
    await screen.findByRole("heading", { name: "脚本确认" });

    await user.click(screen.getByRole("button", { name: "刷新当前项目状态" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));

    expect(screen.queryByRole("button", { name: "返回项目列表" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "返回候选选题池" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开内容质检" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开素材库" })).not.toBeInTheDocument();
  });

  it("确认脚本后先保存草稿、自动质检、继续流程并打开素材库", async () => {
    const user = userEvent.setup();
    const onOpenMaterials = vi.fn();
    render(
      <ScriptWorkspace
        pipelineId={18}
        onOpenMaterials={onOpenMaterials}
      />,
    );
    await screen.findByRole("heading", { name: "脚本确认" });
    await user.click(screen.getByRole("button", { name: "添加一段" }));
    await user.type(screen.getByLabelText("编辑第2段画面"), "第二段素材画面");
    await user.type(screen.getByLabelText("编辑第2段字幕口播"), "第二段口播。");

    await user.click(screen.getByRole("button", { name: "确认脚本" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/workspace/script",
        expect.objectContaining({ method: "PUT" }),
      );
      expect(fetch).toHaveBeenCalledWith(
        "/api/content-quality/pipeline/18/check",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/pipeline/18/continue",
      expect.objectContaining({ method: "POST" }),
    );
    const saveCall = lastWorkspaceSaveCall();
    const payload = JSON.parse(String(saveCall?.[1]?.body));
    expect(payload.storyboard).toHaveLength(2);
    expect(payload.storyboard[1].recommended_visual).toBe("第二段素材画面");
    expect(payload.subtitles[1].text).toBe("第二段口播。");
    expect(onOpenMaterials).toHaveBeenCalledWith(18);
  });

  it("能手动编辑脚本并保存，下游会重新衔接", async () => {
    const user = userEvent.setup();
    render(<ScriptWorkspace pipelineId={18} />);
    await screen.findByRole("heading", { name: "脚本确认" });

    await user.clear(screen.getByLabelText("编辑第1段字幕口播"));
    await user.type(screen.getByLabelText("编辑第1段字幕口播"), "我改过的口播稿。");
    await user.click(screen.getByRole("button", { name: "添加一段" }));
    await user.type(screen.getByLabelText("编辑第2段画面"), "第二段画面");
    await user.type(screen.getByLabelText("编辑第2段字幕口播"), "第二段口播。");
    await user.click(screen.getByRole("button", { name: "保存脚本修改" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/workspace/script",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    const saveCall = lastWorkspaceSaveCall();
    const payload = JSON.parse(String(saveCall?.[1]?.body));
    expect(payload.voiceover).toBe("我改过的口播稿。\n第二段口播。");
    expect(payload.storyboard[0].voiceover).toBe("我改过的口播稿。");
    expect(payload.storyboard[0].recommended_visual).toBe("动态标题页");
    expect(payload.storyboard[1].voiceover).toBe("第二段口播。");
    expect(payload.storyboard[1].recommended_visual).toBe("第二段画面");
    expect(payload.subtitles[0].text).toBe("我改过的口播稿。");
    expect(payload.subtitles[1].text).toBe("第二段口播。");
    expect(payload.cover_copy[0].text).toBe("AI 工具开始进入真实预算");
    expect(payload.title_options).toEqual([
      { title: "企业为什么开始真买 AI 工具", type: "手动编辑" },
    ]);
    expect(payload.publish_copy).toEqual({
      通用: "不复述热闹，只拆商业逻辑。",
    });
  });

  it("重写按钮只发送自己负责的字段", async () => {
    const user = userEvent.setup();
    render(<ScriptWorkspace pipelineId={18} />);
    await screen.findByRole("heading", { name: "脚本确认" });

    await user.click(screen.getByRole("button", { name: "重写文案" }));
    await waitFor(() => {
      const rewriteCall = vi.mocked(fetch).mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/pipeline/18/rewrite" &&
          init?.method === "POST" &&
          JSON.parse(String(init.body)).part === "publish_copy",
      );
      expect(rewriteCall).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: "重写标题" }));
    await waitFor(() => {
      const rewriteCall = vi.mocked(fetch).mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/pipeline/18/rewrite" &&
          init?.method === "POST" &&
          JSON.parse(String(init.body)).part === "title",
      );
      expect(rewriteCall).toBeTruthy();
    });
  });

  it("能用本地免费 TTS 生成配音和字幕", async () => {
    const user = userEvent.setup();
    render(<ScriptWorkspace pipelineId={18} />);
    await screen.findByRole("heading", { name: "脚本确认" });

    await user.click(screen.getByRole("button", { name: "生成配音字幕" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/voiceover/generate",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(
      await screen.findByText(/配音和字幕已生成：D:\/短视频工厂/),
    ).toBeInTheDocument();
  });

  it("能生成任务包、派发失败时显示手动方案、从结果文件导入，并验收后进入素材页", async () => {
    const user = userEvent.setup();
    const onOpenMaterials = vi.fn();
    render(<ScriptWorkspace pipelineId={18} onOpenMaterials={onOpenMaterials} />);
    await screen.findByRole("heading", { name: "脚本确认" });

    await user.click(screen.getByRole("button", { name: "生成 Codex 脚本任务包" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/script-work-order/build",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect((await screen.findAllByText(/script_factory_prompt\.md/)).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "派发 Codex 线程" }));
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/script-work-order/dispatch",
        expect.objectContaining({ method: "POST" }),
      );
    });
    await waitFor(() => {
      expect(
        screen.getAllByText(/后端网页不能直接新建 Codex Desktop 对话/).length,
      ).toBeGreaterThan(0);
    });
    expect(
      (screen.getByLabelText("手动新建 Codex 对话内容") as HTMLTextAreaElement)
        .value,
    ).toContain("script_factory_result.json");

    expect(screen.getByRole("button", { name: "从结果文件导入" })).toBeDisabled();
    expect(screen.getByText(/结果文件还没生成/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "等待 Codex 脚本结果" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "脚本确认" })).not.toBeInTheDocument();
  });

  it("发现结果文件后才能导入并验收进入素材页", async () => {
    const user = userEvent.setup();
    const onOpenMaterials = vi.fn();
    workspaceResponse = workspaceWithScriptFactory({
      status: "result_found",
      result_exists: true,
      ready_for_review: false,
    });
    render(<ScriptWorkspace pipelineId={18} onOpenMaterials={onOpenMaterials} />);
    await screen.findByRole("heading", { name: "等待 Codex 脚本结果" });

    await user.click(screen.getByRole("button", { name: "从结果文件导入" }));

    await waitFor(() => {
      const importCall = vi.mocked(fetch).mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/pipeline/18/script-work-order/import" &&
          init?.method === "PUT",
      );
      expect(importCall).toBeTruthy();
      expect(importCall?.[1]?.body).toBeUndefined();
    });

    expect(await screen.findByRole("heading", { name: "脚本确认" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "验收并进入素材" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/18/script-work-order/accept",
        expect.objectContaining({ method: "POST" }),
      );
      expect(onOpenMaterials).toHaveBeenCalledWith(18);
    });
  });
});

function lastWorkspaceSaveCall() {
  return vi
    .mocked(fetch)
    .mock.calls.filter(
      ([url, init]) =>
        String(url) === "/api/pipeline/18/workspace/script" &&
        init?.method === "PUT",
    )
    .at(-1);
}
