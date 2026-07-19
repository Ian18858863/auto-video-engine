import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import MaterialPackagePage from "./MaterialPackagePage";
import type { MaterialCandidatePool, MaterialPackageData } from "./types";

const materialPackage = {
  pipeline_id: 18,
  status: "已完成",
  provider: "fake",
  project_name: "为什么企业开始认真采购 AI 工具",
  project_dir: "exports/AI工具采购",
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
    title_options: [{ title: "企业为什么开始真买 AI 工具", type: "趋势解释型" }],
    cover_copy: [{ text: "AI 工具开始进入真实预算" }],
    publish_copy: {
      抖音: "不复述热闹，只拆商业逻辑。",
    },
    use_avatar: true,
  },
  material_tasks: [
    {
      shot_id: "shot-01",
      time_range: "00:00-00:03",
      voiceover: "为什么企业突然开始认真买 AI 工具？",
      recommended_visual: "动态标题页",
      material_type: "待匹配",
      jianying_keywords: ["AI工具", "企业采购"],
      asset_keywords: ["AI工具", "企业采购"],
      source_priority: ["官方证据画面", "Pexels视频", "信息动效", "备用素材"],
      needs_official_capture: true,
      needs_pexels: true,
      needs_info_motion: true,
      needs_backup_asset: false,
      needs_codex_graphic: true,
      needs_avatar: true,
      status: "待执行",
      material_path: "",
      source_type: "",
      reject_reason: "",
      notes: "",
      beat_id: "beat-01",
      beat_type: "evidence",
      viewer_takeaway: "观众要看懂官方数据如何支持结论。",
      visual_intent: "展示官方数据页面",
      evidence_kind: "direct_evidence",
      source_refs: [{ url: "https://example.com/evidence", kind: "source" }],
      acceptance_conditions: ["必须显示来源"],
      forbidden_items: ["不得用气氛素材冒充事实"],
      director_version: "semantic-director-v1",
      subject: "企业 AI 预算表",
      action: "高亮预算项目和金额",
      location: "官方报告页面",
      time_context: "2026 年",
      composition: "来源标题在上，预算数据居中，出处固定在底部",
      visual_type: "官方证据画面",
      sound_notes: "数据出现时使用一次轻提示音",
      candidate_pool: {
        shot_id: "shot-01",
        revision: 0,
        locked_candidate_id: null,
        candidates: [
          {
            candidate_id: "cand-1",
            source: "Pexels",
            preview_path: "/preview/cand-1.jpg",
            preview_url: "/preview/cand-1.jpg",
            preview_media_type: "image/jpeg",
            url: "https://www.pexels.com/photo/example-101/",
            rights: "Pexels License",
            evidence_kind: "atmosphere",
            status: "candidate",
            reason: "画面主体与语义任务匹配",
            media_validation: {
              status: "verified",
              media_kind: "image",
              mime_type: "image/jpeg",
              sha256: "a".repeat(64),
              size_bytes: 631,
              width: 12,
              height: 8,
              duration_seconds: null,
              codec: "jpeg",
              container: "jpeg",
              decode_verified: true,
            },
            rights_evidence: {
              schema_version: 1,
              provider: "Pexels",
              provider_asset_id: "101",
              media_type: "photo",
              asset_page_url: "https://www.pexels.com/photo/example-101/",
              contributor: {
                provider_user_id: "7",
                name: "Ada",
                profile_url: "https://www.pexels.com/@ada/",
              },
              license_snapshot: {
                license_name: "Pexels License",
                license_url: "https://www.pexels.com/legal-pages/license/",
                terms_url: "https://help.pexels.com/hc/en-us/articles/900005880463-What-are-the-Terms-and-Conditions",
                observed_at: "2026-07-18T09:30:00+00:00",
                terms_version_or_last_updated: "",
              },
              attribution: {
                license_required: false,
                api_display_requirement: "在使用 API 结果的界面显著链接至 Pexels，并尽可能标注创作者。",
                recommended_credit: "Photo by Ada on Pexels",
              },
              rights_review: {
                status: "unreviewed",
                intended_use: "short-video",
                identifiable_people: "unknown",
                brands_or_logos: "unknown",
                property_or_architecture: "unknown",
                music_or_third_party_audio: "unknown",
                third_party_clearance: "unknown",
                restriction_notes: [],
                reviewed_by: "",
                reviewed_at: "",
              },
              source_record: {
                search_query: "office",
                record_checked_at: "2026-07-18T09:30:00+00:00",
              },
            },
          },
          {
            candidate_id: "cand-2",
            source: "Pixabay",
            preview_path: "/preview/cand-2.jpg",
            preview_url: "/preview/cand-2.jpg",
            preview_media_type: "image/jpeg",
            url: "https://example.com/cand-2",
            rights: "commercial-use terms captured",
            evidence_kind: "atmosphere",
            status: "candidate",
            reason: "备用构图",
          },
          {
            candidate_id: "cand-3",
            source: "官方页面",
            preview_path: "/preview/cand-3.jpg",
            preview_url: "/preview/cand-3.jpg",
            preview_media_type: "image/jpeg",
            url: "https://example.com/cand-3",
            rights: "official source capture",
            evidence_kind: "direct_evidence",
            status: "candidate",
            reason: "保留直接证据入口",
          },
        ],
      },
    },
    {
      shot_id: "shot-02",
      time_range: "00:03-00:10",
      voiceover: "企业真正买的是能省钱提效的能力。",
      recommended_visual: "Pexels 科技办公背景",
      material_type: "待匹配",
      jianying_keywords: ["科技办公"],
      asset_keywords: ["科技办公"],
      source_priority: ["Pexels视频", "信息动效"],
      needs_official_capture: false,
      needs_pexels: true,
      needs_info_motion: false,
      needs_backup_asset: false,
      needs_codex_graphic: false,
      needs_avatar: true,
      status: "等待",
      material_path: "",
      source_type: "",
      reject_reason: "",
      notes: "",
    },
    {
      shot_id: "shot-03",
      time_range: "00:10-00:18",
      voiceover: "如果没有合适素材，要把失败原因写清楚。",
      recommended_visual: "官方页面慢速滚动",
      material_type: "待匹配",
      jianying_keywords: ["官方页面"],
      asset_keywords: ["官方页面"],
      source_priority: ["官方证据画面"],
      needs_official_capture: true,
      needs_pexels: false,
      needs_info_motion: false,
      needs_backup_asset: false,
      needs_codex_graphic: false,
      needs_avatar: false,
      status: "失败",
      material_path: "",
      source_type: "",
      reject_reason: "缺少新闻原文 URL，无法抓取官方证据画面。",
      notes: "",
    },
  ],
  graphic_tasks: [
    {
      shot_id: "shot-01",
      time_range: "00:00-00:03",
      visual_form: "动态图文",
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
    edit_manifest_json: {
      generated: true,
      path: "exports/AI工具采购/edit_manifest.json",
    },
  },
  package_note: "素材包已生成。",
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

function errorResponse(status: number, detail: string): Promise<Response> {
  return Promise.resolve({
    ok: false,
    status,
    json: async () => ({ detail }),
    text: async () => detail,
  } as Response);
}

describe("素材库页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/pipeline/18/material-package") {
          return jsonResponse(materialPackage);
        }
        if (url === "/api/pipeline/18/material-tasks/run-next") {
          return jsonResponse({
            pipeline_id: 18,
            message: "素材任务失败，已记录原因",
            executed_task: materialPackage.material_tasks[0],
            tasks: materialPackage.material_tasks,
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("集中展示自动素材任务、图文补充任务和数字人字段，不显示导出文件和项目标题", async () => {
    render(<MaterialPackagePage pipelineId={18} />);
    await waitFor(() => {
      expect(screen.getAllByRole("heading").length).toBeGreaterThan(1);
    });

    expect(screen.getByRole("heading", { name: "素材任务" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "语义素材约束" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "素材预览" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "图文补充任务" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "数字人字段" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "导出文件" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "为什么企业开始认真采购 AI 工具" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "需要人工确认" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "已完成" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "已拒绝" })).not.toBeInTheDocument();
    expect(screen.getAllByText("动态标题页").length).toBeGreaterThan(0);
    expect(screen.getByText("官方页面录屏 / Pexels / Pixabay / 备选素材 / 信息动效")).toBeInTheDocument();
    expect(screen.getAllByText("待执行").length).toBeGreaterThan(0);
    expect(screen.getByText("等待")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("缺少新闻原文 URL，无法抓取官方证据画面。")).toBeInTheDocument();
    expect(screen.getByText("观众要看懂官方数据如何支持结论。")).toBeInTheDocument();
    expect(screen.getByText("主体：企业 AI 预算表")).toBeInTheDocument();
    expect(screen.getByText("动作：高亮预算项目和金额")).toBeInTheDocument();
    expect(screen.getByText("地点：官方报告页面")).toBeInTheDocument();
    expect(screen.getByText("时间：2026 年")).toBeInTheDocument();
    expect(
      screen.getByText("构图：来源标题在上，预算数据居中，出处固定在底部"),
    ).toBeInTheDocument();
    expect(screen.getByText("声音：数据出现时使用一次轻提示音")).toBeInTheDocument();
    expect(screen.getByText("必须显示来源")).toBeInTheDocument();
    expect(screen.getByText("暂无已匹配素材，等待官方页面录屏、Pexels 或信息动效任务完成。")).toBeInTheDocument();
    expect(screen.getByText("等待素材缺口确认")).toBeInTheDocument();
    expect(screen.getByText("右下角")).toBeInTheDocument();
    expect(screen.getByText("正在讲解")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "素材候选池" })).toBeInTheDocument();
    expect(screen.getByText("3 个跨来源候选 · 第 0 版")).toBeInTheDocument();
    expect(screen.getByText("画面主体与语义任务匹配")).toBeInTheDocument();
    expect(screen.getByText("媒体已验证")).toBeInTheDocument();
    expect(screen.getByText("JPEG · 12×8 · 631 B")).toBeInTheDocument();
    expect(screen.getByText("来源与许可已留证")).toBeInTheDocument();
    expect(screen.getByText("Pexels License · Ada")).toBeInTheDocument();
    expect(screen.getByText("待人工清权")).toBeInTheDocument();
    expect(screen.getAllByText("旧候选，锁定前会复核")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "锁定候选 cand-1" })).toBeDisabled();
  });

  it("必须填写理由后才能锁定或拒绝候选，并把状态写回项目", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(materialPackage);
      }
      if (url.endsWith("/candidate-pool/cand-1/lock")) {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({ reason: "保留直接证据" });
        return jsonResponse({
          ...materialPackage.material_tasks[0].candidate_pool,
          revision: 1,
          locked_candidate_id: "cand-1",
          candidates: materialPackage.material_tasks[0].candidate_pool!.candidates.map(
            (candidate) =>
              candidate.candidate_id === "cand-1"
                ? { ...candidate, status: "locked", reason: "保留直接证据" }
                : candidate,
          ),
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    await waitFor(() => {
      expect(screen.getAllByRole("heading").length).toBeGreaterThan(1);
    });
    const reason = screen.getByRole("textbox", {
      name: "候选 cand-1 操作理由",
    });
    await userEvent.type(reason, "保留直接证据");
    await userEvent.click(screen.getByRole("button", { name: "锁定候选 cand-1" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/pipeline/18/material-tasks/shot-01/candidate-pool/cand-1/lock",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(await screen.findByText("已锁定 cand-1")).toBeInTheDocument();
    expect(await screen.findByText("已锁定候选 cand-1，重跑不会覆盖它。")).toBeInTheDocument();
  });

  it("上游变化后把旧锁显示为不可操作的历史，而不是正常已锁定", async () => {
    const stalePackage = structuredClone(materialPackage) as typeof materialPackage & {
      material_selection_history?: MaterialPackageData["material_selection_history"];
    };
    const pool = stalePackage.material_tasks[0].candidate_pool as MaterialCandidatePool;
    pool.locked_candidate_id = "cand-1";
    pool.stale = true;
    pool.stale_reason_code = "upstream_script_changed";
    pool.stale_reason = "上游脚本已经变化；旧锁定仅保留为历史，需重新生成并确认素材。";
    pool.candidates[0].status = "locked";
    stalePackage.material_selection_history = [
      {
        event: "material_selection_stale",
        reason_code: "upstream_script_changed",
        reason: "上游脚本已经变化；旧锁定仅保留为历史，需重新生成并确认素材。",
        stale_at: "2026-07-19T07:00:00+00:00",
        source_script_version_id: "sv-18-0001-old",
        target_script_version_id: "sv-18-0002-new",
        locked_selections: [
          {
            shot_id: "shot-01",
            candidate_id: "cand-1",
            reason: "第一版已确认",
            source: "Pexels",
            media_sha256: "a".repeat(64),
          },
        ],
        entry_sha256: "b".repeat(64),
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/pipeline/18/material-package") {
          return jsonResponse(stalePackage);
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );

    render(<MaterialPackagePage pipelineId={18} />);

    expect(await screen.findByText("历史锁定已过期")).toBeInTheDocument();
    expect(
      screen.getAllByText("上游脚本已经变化；旧锁定仅保留为历史，需重新生成并确认素材。"),
    ).toHaveLength(2);
    expect(screen.getByText("历史锁定（已过期）")).toBeInTheDocument();
    expect(screen.queryByText("已锁定 cand-1")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "候选 cand-2 操作理由" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "锁定候选 cand-2" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "历史素材锁定" })).toBeInTheDocument();
    expect(screen.getByText("shot-01 · cand-1 · 第一版已确认")).toBeInTheDocument();
    expect(screen.getAllByText("历史预览已停用")).toHaveLength(3);
    expect(screen.queryByRole("img", { name: /候选 .* 预览/ })).not.toBeInTheDocument();
  });

  it("较晚返回的旧素材读取不能覆盖刚完成的候选锁定", async () => {
    let packageReads = 0;
    let resolveStaleRead!: (response: Response) => void;
    const staleRead = new Promise<Response>((resolve) => {
      resolveStaleRead = resolve;
    });
    const lockedPool = {
      ...materialPackage.material_tasks[0].candidate_pool,
      revision: 1,
      locked_candidate_id: "cand-1",
      candidates: materialPackage.material_tasks[0].candidate_pool!.candidates.map(
        (candidate) =>
          candidate.candidate_id === "cand-1"
            ? { ...candidate, status: "locked", reason: "保留直接证据" }
            : candidate,
      ),
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        packageReads += 1;
        return packageReads === 1 ? jsonResponse(materialPackage) : staleRead;
      }
      if (url.endsWith("/candidate-pool/cand-1/lock")) {
        return jsonResponse(lockedPool);
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);
    const reason = await screen.findByRole("textbox", {
      name: "候选 cand-1 操作理由",
    });
    await userEvent.click(screen.getByRole("button", { name: "刷新" }));
    await userEvent.type(reason, "保留直接证据");
    await userEvent.click(screen.getByRole("button", { name: "锁定候选 cand-1" }));
    expect(await screen.findByText("已锁定 cand-1")).toBeInTheDocument();

    await act(async () => {
      resolveStaleRead(await jsonResponse(materialPackage));
      await staleRead;
    });

    await waitFor(() => {
      expect(screen.getByText("已锁定 cand-1")).toBeInTheDocument();
    });
    expect(screen.queryByText("待人工选择")).not.toBeInTheDocument();
  });

  it("按本次用途完成人工清权，并只提交复核字段与当前 revision", async () => {
    const reviewedPool = structuredClone(
      materialPackage.material_tasks[0].candidate_pool,
    ) as MaterialCandidatePool;
    reviewedPool.revision = 1;
    const firstEvidence = reviewedPool.candidates[0].rights_evidence!;
    firstEvidence.rights_review = {
      status: "cleared_for_intended_use",
      intended_use: "当前项目短视频",
      identifiable_people: "no",
      brands_or_logos: "no",
      property_or_architecture: "no",
      music_or_third_party_audio: "no",
      third_party_clearance: "not_required",
      restriction_notes: ["仅用于当前批准版本"],
      reviewed_by: "Ian",
      reviewed_at: "2026-07-18T10:00:00+00:00",
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(materialPackage);
      }
      if (url.endsWith("/candidate-pool/cand-1/rights-review")) {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          expected_revision: 0,
          status: "cleared_for_intended_use",
          intended_use: "当前项目短视频",
          identifiable_people: "no",
          brands_or_logos: "no",
          property_or_architecture: "no",
          music_or_third_party_audio: "no",
          third_party_clearance: "not_required",
          restriction_notes: ["仅用于当前批准版本"],
        });
        return jsonResponse(reviewedPool);
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    await userEvent.click(await screen.findByText("核对本次使用权利"));
    await userEvent.clear(screen.getByLabelText("本次用途"));
    await userEvent.type(screen.getByLabelText("本次用途"), "当前项目短视频");
    await userEvent.selectOptions(screen.getByLabelText("可识别人像"), "no");
    await userEvent.selectOptions(screen.getByLabelText("品牌或 Logo"), "no");
    await userEvent.selectOptions(screen.getByLabelText("场地或建筑"), "no");
    await userEvent.selectOptions(
      screen.getByLabelText("音乐或第三方音频"),
      "no",
    );
    await userEvent.selectOptions(screen.getByLabelText("第三方许可"), "not_required");
    await userEvent.type(screen.getByLabelText("限制备注"), "仅用于当前批准版本");
    await userEvent.click(
      screen.getByRole("button", { name: "确认本次用途已清权" }),
    );

    expect(await screen.findByText("本次用途已清权")).toBeInTheDocument();
    expect(
      await screen.findByText("候选 cand-1 的本次用途清权结果已保存。"),
    ).toBeInTheDocument();
  });

  it("已锁定镜头只能通过明确确认改选，并发送当前 revision", async () => {
    const lockedPackage = structuredClone(materialPackage);
    const lockedPool = lockedPackage.material_tasks[0]
      .candidate_pool as MaterialCandidatePool;
    lockedPool.revision = 1;
    lockedPool.locked_candidate_id = "cand-1";
    lockedPool.candidates = lockedPool.candidates.map((candidate) =>
      candidate.candidate_id === "cand-1"
        ? { ...candidate, status: "locked", reason: "第一版已批准" }
        : candidate,
    );
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(lockedPackage);
      }
      if (url.endsWith("/candidate-pool/cand-2/reselect")) {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          reason: "证据画面更准确",
          expected_revision: 1,
        });
        return jsonResponse({
          ...lockedPool,
          revision: 2,
          locked_candidate_id: "cand-2",
          candidates: lockedPool.candidates.map((candidate) => ({
            ...candidate,
            status:
              candidate.candidate_id === "cand-2"
                ? "locked"
                : candidate.candidate_id === "cand-1"
                  ? "candidate"
                  : candidate.status,
          })),
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<MaterialPackagePage pipelineId={18} />);

    expect(
      await screen.findByText("改选后，现有剪辑输入会变旧；请到剪辑页重建新版本，旧素材记录仍会保留。"),
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: "候选 cand-2 操作理由" }),
      "证据画面更准确",
    );
    await userEvent.click(screen.getByRole("button", { name: "改选为候选 cand-2" }));

    expect(confirm).toHaveBeenCalledWith(
      "确认把 shot-01 改选为 cand-2？现有剪辑输入会变旧，需到剪辑页重建；旧素材记录仍会保留。",
    );
    expect(await screen.findByText("已锁定 cand-2")).toBeInTheDocument();
    expect(
      await screen.findByText("已改选 shot-01 的镜头素材；请到剪辑页重建，新旧版本都会保留。"),
    ).toBeInTheDocument();
  });

  it("页面加载和刷新都不会自动执行素材任务", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(materialPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/run-next") {
        return jsonResponse({
          pipeline_id: 18,
          message: "不应自动调用",
          executed_task: null,
          tasks: [],
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);
    await screen.findByRole("button", { name: "开始/继续素材获取" });
    await userEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([url]) => String(url) === "/api/pipeline/18/material-package",
        ).length,
      ).toBeGreaterThanOrEqual(2);
    });
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);
  });

  it("可以手动跳过单条素材任务并刷新状态", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(materialPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/run-next") {
        return jsonResponse({
          pipeline_id: 18,
          message: "素材任务失败，已记录原因",
          executed_task: materialPackage.material_tasks[0],
          tasks: materialPackage.material_tasks,
        });
      }
      if (url === "/api/pipeline/18/material-tasks/shot-02/skip") {
        return jsonResponse({
          pipeline_id: 18,
          message: "已跳过当前素材任务，并继续下一条。",
          executed_task: { ...materialPackage.material_tasks[1], status: "已跳过" },
          tasks: materialPackage.material_tasks,
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    await screen.findByRole("heading", { name: "素材候选池" });
    const skipButtons = await screen.findAllByRole("button", { name: "跳过" });
    await userEvent.click(skipButtons[1]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/pipeline/18/material-tasks/shot-02/skip",
      expect.objectContaining({ method: "POST" }),
    );
    expect(await screen.findByText("已跳过当前素材任务，并继续下一条。")).toBeInTheDocument();
  });

  it("跳过提交结果未知时立即回读服务端状态，避免重复提交", async () => {
    const committedPackage = {
      ...materialPackage,
      material_tasks: materialPackage.material_tasks.map((task) =>
        task.shot_id === "shot-02"
          ? { ...task, status: "已跳过", reject_reason: "手动跳过" }
          : task,
      ),
    };
    let packageRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        packageRequests += 1;
        return jsonResponse(packageRequests >= 2 ? committedPackage : materialPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/shot-02/skip") {
        return errorResponse(503, "素材任务提交确认丢失，请先刷新核对后再操作。");
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);
    const skipButtons = await screen.findAllByRole("button", { name: "跳过" });
    await userEvent.click(skipButtons[1]);

    expect(
      await screen.findByText("素材任务提交确认丢失，请先刷新核对后再操作。"),
    ).toBeInTheDocument();
    await waitFor(() => expect(packageRequests).toBeGreaterThanOrEqual(2));
    expect(await screen.findByText("手动跳过")).toBeInTheDocument();
  });

  it("素材任务执行期间会轮询刷新状态，避免一直卡在旧的进行中", async () => {
    const runningPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "官方页面录屏中",
          reject_reason: "",
          notes: "正在尝试官方页面录屏，超过 3 分钟会自动转 Pexels。",
        },
      ],
    };
    const waitingPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "待执行",
        },
      ],
    };
    let resolveRun: ((value: Response) => void) | null = null;
    let packageRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        packageRequests += 1;
        return jsonResponse(packageRequests >= 2 ? runningPackage : waitingPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/run-next") {
        return new Promise<Response>((resolve) => {
          resolveRun = resolve;
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    await screen.findByRole("heading", { name: "素材" });
    await userEvent.click(screen.getByRole("button", { name: "开始/继续素材获取" }));

    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 5200));
    });

    expect(
      fetchMock.mock.calls.filter(([url]) => String(url) === "/api/pipeline/18/material-package").length,
    ).toBeGreaterThanOrEqual(2);
    expect(await screen.findByText("官方页面录屏中")).toBeInTheDocument();
    expect(screen.getByText("正在尝试官方页面录屏，超过 3 分钟会自动转 Pexels。")).toBeInTheDocument();

    await act(async () => {
      resolveRun?.(
        {
          ok: true,
          status: 200,
          json: async () => ({
            pipeline_id: 18,
            message: "素材任务已完成",
            executed_task: runningPackage.material_tasks[0],
            tasks: runningPackage.material_tasks,
          }),
        } as Response,
      );
    });
  });

  it("打开页面只读取状态；中断任务必须由用户显式检查", async () => {
    const stuckPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "官方页面录屏中",
          recovery_action_available: true,
          reject_reason: "",
          notes: "上次官方页面录屏请求中断，可以继续执行。",
        },
      ],
    };
    let packageRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        packageRequests += 1;
        return jsonResponse(stuckPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/shot-01/reconcile-and-retry") {
        expect(init?.method).toBe("POST");
        return Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({ detail: "旧执行仍在运行。" }),
        } as Response);
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    expect(await screen.findByText("官方页面录屏中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "正在获取" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "检查上次执行状态" })).toBeEnabled();
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "检查上次执行状态" }));

    await waitFor(() => {
      expect(packageRequests).toBe(2);
    });
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) =>
          String(url).endsWith("/reconcile-and-retry") && init?.method === "POST",
      ),
    ).toHaveLength(1);
    expect(screen.getByRole("alert")).toHaveTextContent("旧执行仍在运行。");
  });

  it("显示当前任务的执行与恢复元数据，但初次读取不提供假重试", async () => {
    const activePackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "官方页面录屏中",
          execution_state: "running",
          execution_id: "execution-01",
          active_attempt_id: "attempt-01",
          publication_phase: "preparing",
          reason_code: "recovery_pending",
          recovery_action_available: true,
          retry_available: false,
          reject_reason: "",
          notes: "仍在等待官方页面录屏结果。",
        },
      ],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(activePackage);
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    expect(await screen.findByText("执行状态：running")).toBeInTheDocument();
    expect(screen.getByText("执行 ID：execution-01")).toBeInTheDocument();
    expect(screen.getByText("尝试 ID：attempt-01")).toBeInTheDocument();
    expect(screen.getByText("发布阶段：preparing")).toBeInTheDocument();
    expect(screen.getByText("可检查上次执行状态")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "正在获取" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "检查上次执行状态" })).toBeEnabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  });

  it("blocked 且不可重试时显示原因并禁用恢复和跳过", async () => {
    const blockedPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "被阻止",
          reason_code: "recovery_metadata_missing",
          execution_state: "blocked",
          execution_id: "execution-02",
          active_attempt_id: "attempt-02",
          publication_phase: "audit_required",
          retry_available: false,
          recovery_action_available: false,
          reject_reason: "恢复证据不足，旧进程是否停止无法确认。",
          notes: "",
        },
      ],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(blockedPackage);
      }
      throw new Error(`不应发送请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);

    expect(await screen.findByText("恢复证据不足，旧进程是否停止无法确认。"))
      .toBeInTheDocument();
    expect(screen.getByText("恢复证据不足，需人工处理")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "需要人工处理" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过" })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  });

  it("blocked 且明确可重试时只发送一次专用恢复请求", async () => {
    const blockedPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "被阻止",
          reason_code: "owner_exit_kill_on_close",
          execution_state: "blocked",
          execution_id: "execution-03",
          active_attempt_id: "attempt-03",
          publication_phase: "rolled_back",
          retry_available: true,
          recovery_action_available: false,
          reject_reason: "旧进程已确认停止，可以重新检查。",
          notes: "",
        },
      ],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(blockedPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/shot-01/reconcile-and-retry") {
        return jsonResponse({
          pipeline_id: 18,
          message: "已重新检查并重试。",
          executed_task: blockedPackage.material_tasks[0],
          tasks: blockedPackage.material_tasks,
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);
    await screen.findByRole("button", { name: "重新检查并重试" });
    await userEvent.click(screen.getByRole("button", { name: "重新检查并重试" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([url, request]) =>
            String(url).endsWith("/reconcile-and-retry") &&
            request?.method === "POST",
        ),
      ).toHaveLength(1);
    });
    expect(await screen.findByText("已重新检查并重试。"))
      .toBeInTheDocument();
  });

  it("一次点击只执行一条；下一条待执行不会自动 POST", async () => {
    const firstPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "待执行",
          material_path: "",
          status_history: ["待执行"],
        },
        {
          ...materialPackage.material_tasks[1],
          status: "等待",
          material_path: "",
          status_history: ["等待"],
        },
      ],
    };
    const secondPackage = {
      ...materialPackage,
      material_tasks: [
        {
          ...materialPackage.material_tasks[0],
          status: "已完成",
          material_path: "D:/短视频工厂/项目/示例/全部素材/shot-01.mp4",
          status_history: ["等待", "进行中", "已完成"],
        },
        {
          ...materialPackage.material_tasks[1],
          status: "待执行",
          material_path: "",
          status_history: ["等待", "待执行"],
        },
      ],
    };

    let packageRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        packageRequests += 1;
        if (packageRequests === 1) return jsonResponse(firstPackage);
        return jsonResponse(secondPackage);
      }
      if (url === "/api/pipeline/18/material-tasks/run-next") {
        return jsonResponse({
          pipeline_id: 18,
          message: "素材任务已完成",
          executed_task: {},
          tasks: [],
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialPackagePage pipelineId={18} />);
    await screen.findByRole("button", { name: "开始/继续素材获取" });
    await userEvent.click(screen.getByRole("button", { name: "开始/继续素材获取" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([url, init]) =>
            String(url) === "/api/pipeline/18/material-tasks/run-next" &&
            (init as RequestInit | undefined)?.method === "POST",
        ).length,
      ).toBe(1);
    });
    expect(await screen.findAllByText("待执行")).not.toHaveLength(0);
    await new Promise((resolve) => window.setTimeout(resolve, 50));
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) =>
          String(url) === "/api/pipeline/18/material-tasks/run-next" &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toHaveLength(1);
  });

  it("renders a video candidate from the extensionless preview route and shows a failure state", async () => {
    const videoPackage = JSON.parse(JSON.stringify(materialPackage)) as typeof materialPackage;
    videoPackage.material_tasks[0].candidate_pool!.candidates[0] = {
      ...videoPackage.material_tasks[0].candidate_pool!.candidates[0],
      preview_url: "/api/pipeline/18/material-tasks/shot-01/candidate-pool/cand-1/preview",
      preview_media_type: "video/mp4",
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      expect(String(input)).toBe("/api/pipeline/18/material-package");
      return jsonResponse(videoPackage);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<MaterialPackagePage pipelineId={18} />);
    const video = await waitFor(() => {
      const element = container.querySelector("video");
      expect(element).toBeTruthy();
      return element as HTMLVideoElement;
    });
    expect(video).toHaveAttribute("playsinline");
    expect(video).toHaveAttribute("controls");
    fireEvent.error(video);
    expect(container.querySelector('.material-candidate-preview [role="status"]')).toBeInTheDocument();
  });

  it("submits a multipart replacement and updates the candidate pool", async () => {
    const replacementPool = {
      ...materialPackage.material_tasks[0].candidate_pool!,
      revision: 1,
      candidates: materialPackage.material_tasks[0].candidate_pool!.candidates.map((candidate) =>
        candidate.candidate_id === "cand-1"
          ? {
              ...candidate,
              candidate_id: "cand-new",
              source: "uploaded",
              preview_url: "/api/pipeline/18/material-tasks/shot-01/candidate-pool/cand-new/preview",
            }
          : candidate,
      ),
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/pipeline/18/material-package") {
        return jsonResponse(materialPackage);
      }
      if (url.endsWith("/candidate-pool/cand-1/replace")) {
        expect(init?.method).toBe("POST");
        expect(init?.body).toBeInstanceOf(FormData);
        const form = init?.body as FormData;
        expect(form.get("replacement_candidate_id")).toBe("cand-new");
        expect(form.get("source")).toBe("uploaded");
        expect(form.get("rights")).toBe("Ian-owned");
        return jsonResponse(replacementPool);
      }
      throw new Error(`Unhandled request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<MaterialPackagePage pipelineId={18} />);
    await waitFor(() => {
      expect(container.querySelector('input[type="file"]')).toBeTruthy();
    });
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["fake-image"], "replacement.jpg", { type: "image/jpeg" });
    await userEvent.upload(fileInput, file);
    const textInputs = Array.from(
      container.querySelectorAll<HTMLInputElement>(
        ".material-candidate-replace-form input:not([type=file])",
      ),
    );
    await userEvent.type(textInputs[0], "uploaded");
    await userEvent.type(textInputs[1], "Ian-owned");
    await userEvent.type(textInputs[3], "framing changed");
    await userEvent.type(textInputs[4], "cand-new");
    const submit = container.querySelector(
      ".material-candidate-replace-form button[type=submit]",
    ) as HTMLButtonElement;
    await userEvent.click(submit);

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/candidate-pool/cand-1/replace"),
        ),
      ).toBe(true);
    });
    expect(await screen.findByText("cand-new")).toBeInTheDocument();
  });
});
