import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

const moduleLabels = ["任务", "选题", "脚本", "素材", "剪辑", "成片", "复盘", "设置"];
const demoProjectName = "示例：AI 搜索内容项目";
const goldenPublicationTitle = "黄金链手动发布记录";
const req008ActionProjectName = "REQ008-素材重试与跳过验收";
const req009StaleProjectName = "REQ009-锁定素材失效历史验收";
const req005RestoreProjectName = "REQ005-脚本历史恢复验收";

type PipelineListItem = {
  pipeline_id: number;
  hotspot_id: number;
  project_name: string;
  project_key?: string;
  project_dir: string;
  status: string;
  completion_reason_code: string;
  has_failed: boolean;
  failure_reason: string;
};

type PipelineListResponse = {
  items: PipelineListItem[];
};

type GoldenMaterialTask = {
  shot_id: string;
};

type GoldenMaterialPackage = {
  material_tasks: GoldenMaterialTask[];
};

type GoldenTimelineMediaRun = {
  binding: {
    timeline_spec: {
      version_id: string;
      source: string;
      artifacts: { artifact_id: string };
    };
  };
  render: {
    version_id: string;
    timeline_version_id: string;
    output_project_relative_path: string;
    artifact_sha256: string;
    visual_rebuild: {
      source_timeline_version_id: string | null;
      reused_segment_ids: string[];
      rebuilt_segment_ids: string[];
    };
    video_report: { is_valid: boolean };
  };
};

async function sha256File(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

function projectArtifactPath(projectDir: string, relativePath: string): string {
  return join(projectDir, ...relativePath.split("/"));
}

type Req008MaterialTask = GoldenMaterialTask & {
  status: string;
  status_history?: string[];
  reject_reason?: string;
  material_path?: string;
  needs_official_capture?: boolean;
  needs_pexels?: boolean;
  time_range?: string;
};

type Req008MaterialPackage = {
  material_tasks: Req008MaterialTask[];
};

type Req009MaterialPackage = {
  material_tasks: Array<{
    shot_id: string;
    candidate_pool?: {
      stale?: boolean;
      locked_candidate_id?: string | null;
      candidates: Array<{ candidate_id: string; status: string }>;
    };
  }>;
  material_selection_history: Array<{
    reason_code: string;
    locked_selections: Array<{ shot_id: string; candidate_id: string }>;
    entry_sha256: string;
  }>;
};

type ScriptWorkOrderBuildResult = {
  job_id: string;
  result_path: string;
};

type RuntimeCapabilityTruth = {
  id: string;
  status: string;
  installed: boolean | null;
  configured: boolean | null;
  verified: boolean;
  production_allowed: boolean;
  secret_configured?: boolean;
  declared_version?: string;
  last_verified_at: string | null;
};

function installFailureCapture(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const serverErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/") && response.status() >= 500) {
      serverErrors.push(`${response.status()} ${response.request().method()} ${response.url()}`);
    }
  });
  return { pageErrors, consoleErrors, serverErrors };
}

async function assertEightModulesStayVisible(page: Page) {
  for (const label of moduleLabels) {
    const moduleButton = page.getByRole("button", { name: label, exact: true });
    await expect(moduleButton).toBeVisible();
    const bounds = await moduleButton.evaluate((button) => {
      const rect = button.getBoundingClientRect();
      return {
        bottom: rect.bottom,
        height: window.innerHeight,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        width: window.innerWidth,
      };
    });
    expect(bounds.left, `${label} 模块左侧不应离开视口`).toBeGreaterThanOrEqual(-1);
    expect(bounds.right, `${label} 模块右侧不应离开视口`).toBeLessThanOrEqual(
      bounds.width + 1,
    );
    expect(bounds.top, `${label} 模块顶部不应离开视口`).toBeGreaterThanOrEqual(-1);
    expect(bounds.bottom, `${label} 模块底部不应离开视口`).toBeLessThanOrEqual(
      bounds.height + 1,
    );
  }
}

async function saveModuleEvidence(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({
    path: testInfo.outputPath(`${name}-viewport.png`),
  });
  await page.screenshot({
    path: testInfo.outputPath(`${name}-full.png`),
    fullPage: true,
  });
}

async function assertNoPageLevelHorizontalOverflow(page: Page, moduleName: string) {
  const widths = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(
    Math.max(widths.body, widths.document),
    `${moduleName} 不应让整页出现横向滚动`,
  ).toBeLessThanOrEqual(widths.viewport + 1);
}

async function assertButtonLabelsFit(page: Page, moduleName: string) {
  const overflowingButtons = await page.locator("button:visible").evaluateAll((buttons) =>
    buttons
      .filter((button) => button.scrollWidth > button.clientWidth + 1)
      .map((button) => (button.textContent || button.getAttribute("aria-label") || "").trim()),
  );
  expect.soft(overflowingButtons, `${moduleName} 的按钮文字不应溢出边界`).toEqual([]);
}

async function assertMaterialTaskTableFits(page: Page) {
  const table = page.locator(".material-task-table");
  await expect(table).toBeVisible();
  const width = await table.evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect.soft(width.scroll, "手机端素材任务不应依赖横向滚动").toBeLessThanOrEqual(
    width.client + 1,
  );
  const rowDisplay = await table
    .locator("tbody tr")
    .first()
    .evaluate((row) => getComputedStyle(row).display);
  expect.soft(rowDisplay, "手机端素材任务应折叠成可读卡片").toBe("grid");
}

function goldenMediaPaths(): string[] {
  const raw = process.env.GOLDEN_E2E_MEDIA_PATHS;
  if (!raw) throw new Error("GOLDEN_E2E_MEDIA_PATHS is required");
  const paths = JSON.parse(raw) as unknown;
  if (
    !Array.isArray(paths) ||
    paths.length !== 3 ||
    paths.some((path) => typeof path !== "string" || !/^[dD]:[\\/]/.test(path))
  ) {
    throw new Error("GOLDEN_E2E_MEDIA_PATHS must contain three absolute D: paths");
  }
  return paths;
}

function localScriptFactoryResult(pipelineId: number, jobId: string) {
  return {
    meta: {
      pipeline_id: pipelineId,
      job_id: jobId,
      duration_strategy: "按选题决定",
      target_duration_range: "12-18 秒",
      status: "ready_for_import",
      real_ai_called: false,
    },
    fact_check: {
      sources: [
        {
          title: "D 盘隔离黄金链事实来源",
          url: "https://example.com/golden-script-source",
          used_for: "验证脚本事实来源、不可变版本与下游绑定",
        },
      ],
      unresolved_facts: [],
    },
    voiceover:
      "换成人话说，AI 搜索真正改变的不是答案更快，而是人开始直接交付问题。普通人只要看三件事：来源是否可靠，过程能否复核，结果能不能继续行动。",
    storyboard: [
      {
        shot_id: "shot-01",
        start: "00:00",
        end: "00:06",
        voiceover: "换成人话说，AI 搜索真正改变的不是答案更快，而是人开始直接交付问题。",
        visual_intent: "竖屏搜索界面与问题卡片，突出从关键词到任务的变化。",
        material_requirements: {
          keywords: ["AI 搜索", "任务交付"],
          official_source_url: "https://example.com/golden-script-source",
        },
        semantic_beats: [
          {
            shot_id: "shot-01-proof",
            start: "00:00",
            end: "00:06",
            voiceover: "换成人话说，AI 搜索真正改变的不是答案更快，而是人开始直接交付问题。",
            semantic_role: "evidence",
            semantic_group: "opening-proof",
            viewer_takeaway: "看懂 AI 搜索正在从链接列表变成任务入口",
            visual_intent: "展示可核查的 AI 搜索结果页和任务卡片",
            subject: "AI 搜索结果页",
            action: "从链接列表切换到任务交付卡片",
            location: "竖屏产品界面",
            time: "当前版本",
            composition: "来源标题在上，任务卡片居中，出处固定在底部",
            visual_type: "产品证据画面",
            sound_notes: "任务卡片出现时使用一次轻提示音",
            source_url: "https://example.com/golden-script-source",
            asset_keywords: ["AI 搜索", "任务交付"],
            source_priority: ["自有素材", "信息动效", "备选素材"],
            forbidden_items: ["不得用普通办公画面冒充产品证据"],
            acceptance_conditions: ["画面必须同时显示来源和任务交付结果"],
          },
        ],
        source_priority: ["自有素材", "信息动效", "备选素材"],
        backup_visual: "使用问题卡片信息动效。",
        failure_fallback: "素材不可用时保留纯信息卡片。",
      },
      {
        shot_id: "shot-02",
        start: "00:06",
        end: "00:15",
        voiceover: "普通人只要看三件事：来源是否可靠，过程能否复核，结果能不能继续行动。",
        visual_intent: "三项验收清单逐条出现，形成明确的判断框架。",
        material_requirements: {
          keywords: ["来源可靠", "过程复核", "继续行动"],
        },
        source_priority: ["信息动效", "自有素材", "备选素材"],
        backup_visual: "使用三项判断清单。",
        failure_fallback: "素材不可用时保留纯信息卡片。",
      },
    ],
    subtitles: [
      {
        start: "00:00",
        end: "00:06",
        text: "AI 搜索真正改变的，是人开始直接交付问题。",
      },
      {
        start: "00:06",
        end: "00:15",
        text: "看来源、看过程、看结果能否继续行动。",
      },
    ],
    title_options: [{ title: "AI 搜索，正在把搜索框变成任务入口", type: "判断型" }],
    cover_copy: [{ text: "搜索框正在消失" }],
    publish_copy: {
      通用: "这条视频只拆 AI 搜索的产品变化，不提供投资建议。",
    },
    quality_self_check: {
      plain_language: true,
      judgement: "AI 搜索正在从返回链接转向交付可复核的任务结果。",
      collect_value: "高",
      risk_level: "低",
      risk_notes: [],
      banned_expression_check: [],
    },
    next_material_brief: [
      {
        shot_id: "shot-01",
        material_keywords: ["AI 搜索", "任务交付"],
        source_priority: ["自有素材", "信息动效", "备选素材"],
        matching_reason: "开头需要直观表现从关键词到任务的变化。",
      },
      {
        shot_id: "shot-02",
        material_keywords: ["验收清单", "来源", "复核"],
        source_priority: ["信息动效", "自有素材", "备选素材"],
        matching_reason: "结尾需要把判断框架变成可收藏清单。",
      },
    ],
  };
}

function restoredVersionFixture(
  pipelineId: number,
  jobId: string,
  revision: "第一版" | "第二版",
) {
  const result = localScriptFactoryResult(pipelineId, jobId);
  result.voiceover = `${revision}：${result.voiceover}`;
  result.storyboard[0].voiceover = `${revision}：${result.storyboard[0].voiceover}`;
  result.subtitles[0].text = `${revision}：${result.subtitles[0].text}`;
  result.title_options[0].title = `${revision} AI 搜索判断`;
  return result;
}

async function createAcceptedScriptVersion(
  request: APIRequestContext,
  pipelineId: number,
  revision: "第一版" | "第二版",
) {
  const buildResponse = await request.post(
    `/api/pipeline/${pipelineId}/script-work-order/build`,
  );
  const buildText = await buildResponse.text();
  expect(buildResponse.ok(), buildText).toBe(true);
  const built = JSON.parse(buildText) as ScriptWorkOrderBuildResult;
  await writeFile(
    built.result_path,
    JSON.stringify(restoredVersionFixture(pipelineId, built.job_id, revision), null, 2),
    "utf8",
  );
  const importResponse = await request.put(
    `/api/pipeline/${pipelineId}/script-work-order/import`,
  );
  expect(importResponse.ok(), await importResponse.text()).toBe(true);
  const acceptResponse = await request.post(
    `/api/pipeline/${pipelineId}/script-work-order/accept`,
  );
  const acceptText = await acceptResponse.text();
  expect(acceptResponse.ok(), acceptText).toBe(true);
  expect((JSON.parse(acceptText) as { accepted: boolean }).accepted).toBe(true);
}

async function completeLocalScriptFactoryGolden(
  page: Page,
  request: APIRequestContext,
  testInfo: TestInfo,
  pipelineId: number,
) {
  await page.goto(`/scripts/${pipelineId}`);
  await expect(page.getByRole("heading", { name: "脚本", exact: true })).toBeVisible();

  const buildResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/script-work-order/build`,
  );
  await page.getByRole("button", { name: "生成 Codex 脚本任务包" }).click();
  const buildResponse = await buildResponsePromise;
  const buildText = await buildResponse.text();
  expect(buildResponse.ok(), buildText).toBe(true);
  const built = JSON.parse(buildText) as ScriptWorkOrderBuildResult;
  expect(built.job_id).toBeTruthy();
  expect(built.result_path).toMatch(/^[dD]:[\\/]/);
  await writeFile(
    built.result_path,
    JSON.stringify(localScriptFactoryResult(pipelineId, built.job_id), null, 2),
    "utf8",
  );

  await page.reload();
  const importButton = page.getByRole("button", { name: "从结果文件导入" });
  await expect(importButton).toBeEnabled();
  const importResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/script-work-order/import`,
  );
  await importButton.click();
  const importResponse = await importResponsePromise;
  expect(importResponse.ok(), await importResponse.text()).toBe(true);
  await expect(page.getByText("主体：AI 搜索结果页")).toBeVisible();
  await expect(page.getByText("动作：从链接列表切换到任务交付卡片")).toBeVisible();
  await expect(page.getByText("地点：竖屏产品界面")).toBeVisible();
  await expect(page.getByText("时间：当前版本")).toBeVisible();
  await expect(
    page.getByText("构图：来源标题在上，任务卡片居中，出处固定在底部"),
  ).toBeVisible();
  await expect(page.getByText("声音：任务卡片出现时使用一次轻提示音")).toBeVisible();
  await expect(page.getByText("不得用普通办公画面冒充产品证据")).toBeVisible();
  await expect(page.getByRole("button", { name: "验收并进入素材" })).toBeEnabled();

  const acceptResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/script-work-order/accept`,
    { timeout: 60_000 },
  );
  await page.getByRole("button", { name: "验收并进入素材" }).click();
  const acceptResponse = await acceptResponsePromise;
  const acceptText = await acceptResponse.text();
  expect(acceptResponse.ok(), acceptText).toBe(true);
  expect(JSON.parse(acceptText).accepted).toBe(true);

  const workspaceResponse = await request.get(`/api/pipeline/${pipelineId}/workspace`);
  const workspaceText = await workspaceResponse.text();
  expect(workspaceResponse.ok(), workspaceText).toBe(true);
  const workspace = JSON.parse(workspaceText) as {
    active_script_version_id: string;
    script_versions: Array<{
      status: string;
      version_id: string;
      schema_version: number;
      input_hash: string;
      content_hash: string;
      shot_plan_hash: string;
      snapshot_hash: string;
      integrity_status: string;
      approval_integrity_status: string;
      approval_event_hash: string;
      approval_evidence_hash: string;
    }>;
    script_factory: {
      script_version_id: string;
      script_version_hash: string;
      script_version_input_hash: string;
      script_version_snapshot_hash: string;
      script_version_shot_plan_hash: string;
      script_result_hash: string;
      approval_event_hash: string;
      approval_evidence_hash: string;
      approval_quality_hash: string;
      import_operation_id: string;
      approval_operation_id: string;
    };
    shot_plan: Array<Record<string, unknown>>;
    material_tasks: Array<Record<string, unknown>>;
  };
  expect(workspace.active_script_version_id).toBeTruthy();
  const activeVersion = workspace.script_versions.find(
    (version) => version.version_id === workspace.active_script_version_id,
  );
  expect(activeVersion, "脚本工厂结果必须成为当前不可变 ScriptVersion").toBeTruthy();
  expect(activeVersion).toEqual(
    expect.objectContaining({
      status: "approved",
      schema_version: 2,
      integrity_status: "full_envelope_verified",
      approval_integrity_status: "snapshot_hash_bound",
      approval_event_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      approval_evidence_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      shot_plan_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      snapshot_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
    }),
  );
  const detailResponse = await request.get(
    `/api/pipeline/${pipelineId}/script-versions/${workspace.active_script_version_id}`,
  );
  const detailText = await detailResponse.text();
  expect(detailResponse.ok(), detailText).toBe(true);
  const detail = JSON.parse(detailText) as {
    schema_version: number;
    integrity_status: string;
    approval_integrity_status: string;
    approval_event_hash: string;
    approval_evidence_hash: string;
    input_evidence: {
      kind: string;
      job_id: string;
      files: Record<string, string>;
      result_sha256: string;
    };
  };
  expect(detail.schema_version).toBe(2);
  expect(detail.integrity_status).toBe("full_envelope_verified");
  expect(detail.approval_integrity_status).toBe("snapshot_hash_bound");
  expect(detail.approval_event_hash).toMatch(/^[0-9a-f]{64}$/);
  expect(detail.approval_evidence_hash).toMatch(/^[0-9a-f]{64}$/);
  expect(detail.input_evidence).toEqual(
    expect.objectContaining({
      kind: "script_factory_work_order",
      job_id: built.job_id,
      result_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      files: {
        "script_factory_prompt.md": expect.stringMatching(/^[0-9a-f]{64}$/),
        "script_factory_context.json": expect.stringMatching(/^[0-9a-f]{64}$/),
        "script_factory_result_template.json": expect.stringMatching(/^[0-9a-f]{64}$/),
      },
    }),
  );
  expect(workspace.script_factory).toEqual(
    expect.objectContaining({
      script_version_id: workspace.active_script_version_id,
      script_version_hash: activeVersion?.content_hash,
      script_version_input_hash: activeVersion?.input_hash,
      script_version_snapshot_hash: activeVersion?.snapshot_hash,
      script_version_shot_plan_hash: activeVersion?.shot_plan_hash,
      script_result_hash: detail.input_evidence.result_sha256,
      approval_event_hash: detail.approval_event_hash,
      approval_evidence_hash: detail.approval_evidence_hash,
      approval_quality_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      import_operation_id: expect.stringMatching(/^[0-9a-f]{32}$/),
      approval_operation_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    }),
  );
  expect(JSON.stringify(detail.input_evidence)).not.toMatch(/[a-zA-Z]:[\\/]/);
  expect(workspace.shot_plan[0]).toEqual(
    expect.objectContaining({
      shot_id: "shot-01-proof",
      subject: "AI 搜索结果页",
      action: "从链接列表切换到任务交付卡片",
      location: "竖屏产品界面",
      time_context: "当前版本",
      composition: "来源标题在上，任务卡片居中，出处固定在底部",
      sound_notes: "任务卡片出现时使用一次轻提示音",
      forbidden_items: ["不得用普通办公画面冒充产品证据"],
      acceptance_conditions: ["画面必须同时显示来源和任务交付结果"],
    }),
  );
  expect(workspace.material_tasks[0]).toEqual(
    expect.objectContaining({
      shot_id: "shot-01-proof",
      subject: "AI 搜索结果页",
      action: "从链接列表切换到任务交付卡片",
      composition: "来源标题在上，任务卡片居中，出处固定在底部",
    }),
  );
  await page.goto(`/scripts/${pipelineId}`);
  await expect(page.locator("body")).toContainText("脚本已验收");
  await expect(page.getByText("v2 完整哈希已验证", { exact: true })).toBeVisible();
  await saveModuleEvidence(page, testInfo, "seed-script-factory-accepted");
}

async function installGoldenCandidatePools(
  request: APIRequestContext,
  pipelineId: number,
  projectDir: string,
): Promise<GoldenMaterialTask[]> {
  const materialResponse = await request.get(`/api/pipeline/${pipelineId}/material-package`);
  const materialText = await materialResponse.text();
  expect(materialResponse.ok(), materialText).toBe(true);
  const material = JSON.parse(materialText) as GoldenMaterialPackage;
  expect(material.material_tasks.length, "黄金项目必须有语义素材任务").toBeGreaterThan(0);
  expect(projectDir, "黄金项目必须已有正式项目目录").toMatch(/^[dD]:[\\/]/);
  const candidateDir = join(projectDir, "素材信息", "候选预览");
  await mkdir(candidateDir, { recursive: true });
  const mediaPaths = await Promise.all(
    goldenMediaPaths().map(async (sourcePath, index) => {
      const targetPath = join(candidateDir, `golden-${pipelineId}-${index + 1}.png`);
      await copyFile(sourcePath, targetPath);
      return targetPath;
    }),
  );

  for (const task of material.material_tasks) {
    const candidates = mediaPaths.map((previewPath, index) => ({
      candidate_id: `golden-${task.shot_id}-${String.fromCharCode(97 + index)}`,
      source: `D-only-owned-fixture-${index + 1}`,
      preview_path: previewPath,
      url: "",
      rights: "self-owned isolated acceptance fixture",
      evidence_kind: "self_owned",
      status: "candidate",
      reason: "D 盘隔离可解码媒体，只用于本地技术闭环验收",
    }));
    const response = await request.put(
      `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(task.shot_id)}/candidate-pool`,
      { data: { candidates } },
    );
    const body = await response.text();
    expect(response.ok(), body).toBe(true);
  }

  const savedResponse = await request.get(
    `/api/pipeline/${pipelineId}/material-package`,
  );
  const savedText = await savedResponse.text();
  expect(savedResponse.ok(), savedText).toBe(true);
  const saved = JSON.parse(savedText) as {
    material_tasks: Array<{
      candidate_pool?: {
        candidates: Array<{ candidate_id: string; preview_url: string }>;
      };
    }>;
  };
  for (const task of saved.material_tasks) {
    for (const candidate of task.candidate_pool?.candidates || []) {
      const previewResponse = await request.get(candidate.preview_url);
      expect(
        previewResponse.ok(),
        `候选 ${candidate.candidate_id} 必须能通过受控预览接口读取：${await previewResponse.text()}`,
      ).toBe(true);
      expect(previewResponse.headers()["content-type"]).toContain("image/png");
    }
  }
  return material.material_tasks;
}

async function runLocalRealDownstreamGolden(
  page: Page,
  request: APIRequestContext,
  testInfo: TestInfo,
  pipelineId: number,
  projectDir: string,
) {
  const tasks = await installGoldenCandidatePools(request, pipelineId, projectDir);
  const changedShot = tasks[0];
  expect(changedShot, "局部重建验收至少需要一个真实镜头").toBeTruthy();
  const initialCandidateId = `golden-${changedShot.shot_id}-a`;
  const replacementCandidateId = `golden-${changedShot.shot_id}-replacement`;

  const workspaceResponse = await request.get(`/api/pipeline/${pipelineId}/workspace`);
  const workspaceText = await workspaceResponse.text();
  expect(workspaceResponse.ok(), workspaceText).toBe(true);
  const workspace = JSON.parse(workspaceText) as {
    active_script_version_id: string;
    script_versions: Array<{ version_id: string }>;
  };
  expect(workspace.active_script_version_id).toBeTruthy();
  expect(
    workspace.script_versions.some(
      (version) => version.version_id === workspace.active_script_version_id,
    ),
    "素材与剪辑必须绑定一个真实不可变脚本版本",
  ).toBe(true);

  await page.goto(`/materials/${pipelineId}`);
  await expect(page.getByRole("heading", { name: "素材候选池" })).toBeVisible();
  await expect(page.getByText("主体：AI 搜索结果页")).toBeVisible();
  await expect(page.getByText("动作：从链接列表切换到任务交付卡片")).toBeVisible();
  await expect(page.getByText("地点：竖屏产品界面")).toBeVisible();
  await expect(page.getByText("时间：当前版本")).toBeVisible();
  await expect(
    page.getByText("构图：来源标题在上，任务卡片居中，出处固定在底部"),
  ).toBeVisible();
  await expect(page.getByText("声音：任务卡片出现时使用一次轻提示音")).toBeVisible();
  for (const [taskIndex, task] of tasks.entries()) {
    const candidateId = `golden-${task.shot_id}-a`;
    const card = page.locator(".material-candidate-card").filter({ hasText: task.shot_id });
    await expect(card).toBeVisible();
    const preview = card.getByRole("img", { name: `候选 ${candidateId} 预览` });
    await preview.scrollIntoViewIfNeeded();
    await expect(preview).toBeVisible();
    await expect
      .poll(
        () =>
          preview.evaluate(
            (node) =>
              (node as HTMLImageElement).complete &&
              (node as HTMLImageElement).naturalWidth > 0,
          ),
        { message: `候选 ${candidateId} 必须在浏览器中真实解码显示` },
      )
      .toBe(true);
    await expect(card).not.toContainText("预览加载失败");
    if (taskIndex === 0) {
      const replacedCandidateId = `golden-${task.shot_id}-b`;
      const replaceItem = card
        .locator(".material-candidate-item")
        .filter({ hasText: replacedCandidateId });
      await replaceItem.getByText("替换候选", { exact: true }).click();
      const replaceForm = replaceItem.locator(".material-candidate-replace-form");
      await replaceForm.locator('input[type="file"]').setInputFiles(goldenMediaPaths()[1]);
      await replaceForm.getByLabel("来源", { exact: true }).fill("D-only-owned-replacement");
      await replaceForm
        .getByLabel("权利说明", { exact: true })
        .fill("self-owned isolated acceptance fixture");
      const evidenceKindSelect = replaceForm.locator("select");
      await expect(evidenceKindSelect).toBeVisible({ timeout: 10_000 });
      await evidenceKindSelect.selectOption("self_owned");
      await replaceForm.getByLabel("替换理由", { exact: true }).fill("真实浏览器替换验收");
      await replaceForm
        .getByLabel("新候选 ID（可选）", { exact: true })
        .fill(replacementCandidateId);
      const replaceResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname.endsWith(
            `/candidate-pool/${encodeURIComponent(replacedCandidateId)}/replace`,
          ),
      );
      await replaceForm.getByRole("button", { name: "上传并替换", exact: true }).click();
      const replaceResponse = await replaceResponsePromise;
      expect(replaceResponse.ok(), await replaceResponse.text()).toBe(true);
      await expect(card).toContainText(replacementCandidateId);

      const rejectedCandidateId = `golden-${task.shot_id}-c`;
      const rejectItem = card
        .locator(".material-candidate-item")
        .filter({ hasText: rejectedCandidateId });
      await rejectItem
        .getByLabel(`候选 ${rejectedCandidateId} 操作理由`)
        .fill("画面语义与当前镜头不符");
      const rejectResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname.endsWith(
            `/candidate-pool/${encodeURIComponent(rejectedCandidateId)}/reject`,
          ),
      );
      await rejectItem
        .getByRole("button", { name: `拒绝候选 ${rejectedCandidateId}` })
        .click();
      const rejectResponse = await rejectResponsePromise;
      expect(rejectResponse.ok(), await rejectResponse.text()).toBe(true);
      await expect(rejectItem).toContainText("已拒绝");
    }
    await card.getByLabel(`候选 ${candidateId} 操作理由`).fill("本地黄金链选择");
    const lockResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith(
          `/candidate-pool/${encodeURIComponent(candidateId)}/lock`,
        ),
    );
    await card.getByRole("button", { name: `锁定候选 ${candidateId}` }).click();
    const lockResponse = await lockResponsePromise;
    expect(lockResponse.ok(), await lockResponse.text()).toBe(true);
    await expect(card).toContainText(`已锁定 ${candidateId}`);
  }
  await page.reload();
  await expect(page.getByRole("heading", { name: "素材候选池" })).toBeVisible();
  const persistedFirstCard = page
    .locator(".material-candidate-card")
    .filter({ hasText: tasks[0].shot_id });
  await expect(persistedFirstCard).toContainText(`已锁定 golden-${tasks[0].shot_id}-a`);
  await expect(persistedFirstCard).toContainText(`golden-${tasks[0].shot_id}-replacement`);
  await expect(persistedFirstCard).toContainText("已拒绝");
  await saveModuleEvidence(page, testInfo, "seed-material-preview-replace-reject-lock-persisted");

  await page.goto(`/editing/${pipelineId}`);
  const initialRebuildResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/editing-export/rebuild`,
  );
  await page
    .getByRole("button", { name: /生成(变更时间线|成片方案)/ })
    .first()
    .click();
  const initialRebuildResponse = await initialRebuildResponsePromise;
  expect(initialRebuildResponse.ok(), await initialRebuildResponse.text()).toBe(true);
  await expect(page.getByLabel("TimelineSpec 状态")).toContainText("当前版本");

  const initialRenderResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/timeline-media/render`,
    { timeout: 300_000 },
  );
  await page.getByRole("button", { name: "生成现代媒体草稿", exact: true }).click();
  const initialRenderResponse = await initialRenderResponsePromise;
  const initialRenderText = await initialRenderResponse.text();
  expect(initialRenderResponse.ok(), initialRenderText).toBe(true);
  const initialRender = JSON.parse(initialRenderText) as GoldenTimelineMediaRun;
  expect(initialRender.binding.timeline_spec.source).toBe("audio_first_timing");
  expect(initialRender.binding.timeline_spec.artifacts.artifact_id).toBeTruthy();
  expect(initialRender.render.output_project_relative_path).toMatch(/^成片\/versions\//);
  expect(initialRender.render.artifact_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(initialRender.render.visual_rebuild.source_timeline_version_id).toBeNull();
  expect(initialRender.render.visual_rebuild.reused_segment_ids).toEqual([]);
  expect(initialRender.render.visual_rebuild.rebuilt_segment_ids).toEqual(
    tasks.map((task) => task.shot_id),
  );
  expect(initialRender.render.video_report.is_valid).toBe(true);
  await expect(page.getByRole("status")).toContainText("现代媒体草稿已生成", {
    timeout: 30_000,
  });
  await expect(page.getByLabel("TimelineSpec 状态")).toContainText("TimelineSpec · 当前");
  const initialArtifactPath = projectArtifactPath(
    projectDir,
    initialRender.render.output_project_relative_path,
  );
  expect(await sha256File(initialArtifactPath)).toBe(initialRender.render.artifact_sha256);
  await saveModuleEvidence(page, testInfo, "seed-downstream-editing-v1-rendered");

  await page.goto(`/materials/${pipelineId}`);
  const changedCard = page
    .locator(".material-candidate-card")
    .filter({ hasText: changedShot.shot_id });
  const replacementItem = changedCard
    .locator(".material-candidate-item")
    .filter({ hasText: replacementCandidateId });
  await replacementItem
    .getByLabel(`候选 ${replacementCandidateId} 操作理由`)
    .fill("仅替换这个镜头，验证局部重建");
  const reselectResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith(
        `/candidate-pool/${encodeURIComponent(replacementCandidateId)}/reselect`,
      ),
  );
  page.once("dialog", (dialog) => void dialog.accept());
  await replacementItem
    .getByRole("button", { name: `改选为候选 ${replacementCandidateId}` })
    .click();
  const reselectResponse = await reselectResponsePromise;
  const reselectText = await reselectResponse.text();
  expect(reselectResponse.ok(), reselectText).toBe(true);
  expect(JSON.parse(reselectText).selection_event).toEqual(
    expect.objectContaining({
      event: "candidate_reselected",
      shot_id: changedShot.shot_id,
      previous_candidate_id: initialCandidateId,
      selected_candidate_id: replacementCandidateId,
    }),
  );
  await expect(changedCard).toContainText(`已锁定 ${replacementCandidateId}`);
  await saveModuleEvidence(page, testInfo, "seed-material-one-shot-reselected");

  await page.goto(`/editing/${pipelineId}`);
  const timelineStatus = page.getByLabel("TimelineSpec 状态");
  await expect(timelineStatus).toContainText("TimelineSpec · 已过期");
  await expect(timelineStatus).toContainText(`待生成新时间线：${changedShot.shot_id}`);
  const partialRebuildResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/editing-export/rebuild`,
  );
  await page.getByRole("button", { name: "生成变更时间线", exact: true }).click();
  const partialRebuildResponse = await partialRebuildResponsePromise;
  const partialRebuildText = await partialRebuildResponse.text();
  expect(partialRebuildResponse.ok(), partialRebuildText).toBe(true);
  const partialRebuild = JSON.parse(partialRebuildText) as {
    timeline_sync: {
      current_version_id: string;
      affected_segment_ids: string[];
      created_new_version: boolean;
    };
    timeline_spec: {
      parent_version_id: string;
      change_set: { change_base_version_id: string; affected_segment_ids: string[] };
    };
  };
  expect(partialRebuild.timeline_sync.created_new_version).toBe(true);
  expect(partialRebuild.timeline_sync.affected_segment_ids).toEqual([changedShot.shot_id]);
  expect(partialRebuild.timeline_spec.parent_version_id).toBe(
    initialRender.binding.timeline_spec.version_id,
  );
  expect(partialRebuild.timeline_spec.change_set.affected_segment_ids).toEqual([
    changedShot.shot_id,
  ]);

  const partialRenderResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/timeline-media/render`,
    { timeout: 300_000 },
  );
  await page.getByRole("button", { name: "生成现代媒体草稿", exact: true }).click();
  const partialRenderResponse = await partialRenderResponsePromise;
  const partialRenderText = await partialRenderResponse.text();
  expect(partialRenderResponse.ok(), partialRenderText).toBe(true);
  const partialRender = JSON.parse(partialRenderText) as GoldenTimelineMediaRun;
  expect(partialRender.render.output_project_relative_path).not.toBe(
    initialRender.render.output_project_relative_path,
  );
  expect(partialRender.render.artifact_sha256).not.toBe(initialRender.render.artifact_sha256);
  expect(partialRender.render.visual_rebuild.source_timeline_version_id).toBe(
    initialRender.binding.timeline_spec.version_id,
  );
  expect(partialRender.render.visual_rebuild.rebuilt_segment_ids).toEqual([
    changedShot.shot_id,
  ]);
  expect(partialRender.render.visual_rebuild.reused_segment_ids.sort()).toEqual(
    tasks
      .map((task) => task.shot_id)
      .filter((shotId) => shotId !== changedShot.shot_id)
      .sort(),
  );
  expect(await sha256File(initialArtifactPath)).toBe(initialRender.render.artifact_sha256);
  expect(
    await sha256File(
      projectArtifactPath(projectDir, partialRender.render.output_project_relative_path),
    ),
  ).toBe(partialRender.render.artifact_sha256);
  await expect(page.getByLabel("TimelineSpec 状态")).toContainText("TimelineSpec · 当前");
  await expect(page.getByLabel("现代媒体渲染")).toContainText(
    `本次复用 ${Math.max(tasks.length - 1, 0)} 段，重建 1 段。`,
    { timeout: 30_000 },
  );
  await saveModuleEvidence(page, testInfo, "seed-downstream-editing-v2-partial-rebuild");

  await page.goto(`/finished/${pipelineId}`);
  const contractResponse = await request.get(
    `/api/pipeline/${pipelineId}/quality-contract`,
  );
  const contractText = await contractResponse.text();
  expect(contractResponse.ok(), contractText).toBe(true);
  const qualityContract = JSON.parse(contractText) as {
    quality_contract_relative_path: string;
    quality_contract_sha256: string;
    contract: {
      target: { platform: string; aspect_ratio: string; duration_seconds: number };
      reference_pack: { status: "none_supplied" | "frozen" };
      must_preserve: Array<{ id: string; label: string }>;
      allowed_differences: Array<{ id: string; label: string }>;
      planned_exceed_dimensions: Array<{ id: string; label: string }>;
    };
  };
  expect(qualityContract.quality_contract_relative_path).toBe(
    "质量合同/quality-contract-v1.json",
  );
  expect(qualityContract.quality_contract_sha256).toMatch(/^[0-9a-f]{64}$/);
  const qualityContractPanel = page.getByLabel("只读质量合同");
  await expect(qualityContractPanel).toContainText("发布平台");
  await expect(qualityContractPanel).toContainText("抖音");
  await expect(qualityContractPanel).toContainText("画面比例");
  await expect(qualityContractPanel).toContainText("9:16");
  await expect(qualityContractPanel).toContainText("目标时长");
  await expect(qualityContractPanel).toContainText("60 秒");
  await expect(
    page.getByLabel(`质量合同 SHA-256：${qualityContract.quality_contract_sha256}`),
  ).toHaveText(
    `${qualityContract.quality_contract_sha256.slice(0, 12)}…${qualityContract.quality_contract_sha256.slice(-8)}`,
  );
  const candidatesResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/quality-report/candidates`,
  );
  await page.getByRole("button", { name: "加载可验收成片" }).click();
  const candidatesResponse = await candidatesResponsePromise;
  expect(candidatesResponse.ok(), await candidatesResponse.text()).toBe(true);

  for (const item of qualityContract.contract.must_preserve) {
    await page.getByRole("checkbox", { name: `保持项：${item.label}` }).check();
    await page
      .getByLabel(`保持项证据：${item.label}`)
      .fill(`黄金链逐项复核通过：${item.label}`);
  }
  for (const [index, item] of qualityContract.contract.allowed_differences.entries()) {
    if (index === 0) {
      await page.getByLabel(`允许差异结果：${item.label}`).selectOption("used");
      await page
        .getByLabel(`实际差异：${item.label}`)
        .fill("旁白实测时长驱动镜头重新定时");
    }
    await page
      .getByLabel(`允许差异证据：${item.label}`)
      .fill(index === 0 ? "TimelineSpec audio_first_timing 版本记录" : "本片未使用此允许差异");
  }
  for (const item of qualityContract.contract.planned_exceed_dimensions) {
    await page.getByLabel(`超越结果：${item.label}`).selectOption("exceeded");
    await page.getByLabel(`实际变化：${item.label}`).fill(`已超越统一 R1 基线：${item.label}`);
    await page.getByLabel(`超越证据：${item.label}`).fill(`黄金链逐项对照记录：${item.label}`);
  }
  await page.getByLabel("我确认画面与脚本叙事一致").check();
  await page.getByLabel("我确认事实来源与素材权利已检查").check();

  const player = page.locator("video.finished-video-player");
  await expect(player).toBeVisible();
  await player.evaluate(async (node) => {
    const video = node as HTMLVideoElement;
    video.muted = true;
    video.playbackRate = 4;
    await video.play();
  });
  await expect(page.getByText("已完整播放当前版本。")).toBeVisible({
    timeout: 90_000,
  });

  const reportResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/pipeline/${pipelineId}/quality-report`,
    { timeout: 60_000 },
  );
  await page.getByRole("button", { name: "生成质量报告" }).click();
  const reportResponse = await reportResponsePromise;
  const reportText = await reportResponse.text();
  expect(reportResponse.ok(), reportText).toBe(true);
  const qualityReport = JSON.parse(reportText) as {
    report: { gates: { quality_contract: { evidence: string[] } } };
  };
  const contractEvidence = qualityReport.report.gates.quality_contract.evidence;
  expect(contractEvidence).toContain(
    `quality_contract_sha256:${qualityContract.quality_contract_sha256}`,
  );
  expect(
    contractEvidence.filter((item) => item.startsWith("must_preserve_review:")),
  ).toHaveLength(qualityContract.contract.must_preserve.length);
  expect(
    contractEvidence.filter((item) => item.startsWith("allowed_difference_review:")),
  ).toHaveLength(qualityContract.contract.allowed_differences.length);
  expect(
    contractEvidence.filter((item) => item.startsWith("planned_exceed_review:")),
  ).toHaveLength(qualityContract.contract.planned_exceed_dimensions.length);
  await expect(page.locator("body")).toContainText("五门闸已通过");

  const approvalResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/pipeline/${pipelineId}/quality-report/approve`,
  );
  await page.getByRole("button", { name: "Ian 批准成片" }).click();
  const approvalResponse = await approvalResponsePromise;
  const approvalText = await approvalResponse.text();
  expect(approvalResponse.ok(), approvalText).toBe(true);
  expect(JSON.parse(approvalText).evidence.evidence_version).toBe(2);
  await expect(page.getByRole("status")).toContainText("Ian 批准已写入");

  const releaseResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/pipeline/${pipelineId}/release-package`,
  );
  await page.getByRole("button", { name: "发布成片" }).click();
  const releaseResponse = await releaseResponsePromise;
  const releaseText = await releaseResponse.text();
  expect(releaseResponse.ok(), releaseText).toBe(true);
  expect(JSON.parse(releaseText).release_supported).toBe(true);
  await expect(
    page.getByRole("status").filter({ hasText: "正式发布包已生成" }),
  ).toContainText("正式发布包已生成");
  await expect(page.locator("body")).toContainText("正式发布包：");
  await saveModuleEvidence(page, testInfo, "seed-downstream-finished-release");

  await page.getByRole("button", { name: "记录手动发布" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/review\\?pipeline_id=${pipelineId}$`),
  );
  await expect(page.getByRole("heading", { name: "发布数据追踪" })).toBeVisible();
  await page.getByRole("button", { name: "新增发布记录" }).first().click();
  const dialog = page.getByRole("dialog", { name: "新增发布记录" });
  await dialog.getByLabel("关联项目").selectOption(String(pipelineId));
  await dialog.getByLabel("视频标题").fill(goldenPublicationTitle);
  await dialog.getByLabel("发布时间").fill("2026-07-19T00:00");
  await dialog
    .getByLabel("视频链接")
    .fill(`https://example.test/golden-manual-publication/${pipelineId}`);
  await dialog.getByLabel("视频形式").fill("本地黄金链竖屏成片");
  await dialog.getByLabel("视频时长（秒）").fill("15");
  await dialog.getByLabel("播放量").fill("120");
  await dialog.getByLabel("点赞数").fill("12");
  await dialog.getByLabel("评论数").fill("3");
  await dialog.getByLabel("收藏数").fill("8");
  await dialog.getByLabel("转发数").fill("5");
  await dialog.getByLabel("涨粉数").fill("2");
  await dialog.getByLabel("完播率（%）").fill("80");
  await dialog.getByLabel("平均观看时长（秒）").fill("12");
  await dialog
    .getByLabel("备注")
    .fill("正式发布包已生成；外部平台发布仍由 Ian 手动完成，本条仅记录发布结果。");
  const createPostResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/video-posts",
  );
  await dialog.getByRole("button", { name: "保存发布记录" }).click();
  const createPostResponse = await createPostResponsePromise;
  const createPostText = await createPostResponse.text();
  expect(createPostResponse.ok(), createPostText).toBe(true);
  expect(JSON.parse(createPostText).pipeline_id).toBe(pipelineId);
  await expect(page.getByText(goldenPublicationTitle)).toBeVisible();

  await page.reload();
  await expect(page.getByText(goldenPublicationTitle)).toBeVisible();
  await page.getByRole("tab", { name: "数据复盘" }).click();
  await expect(page.getByRole("heading", { name: "数据复盘", exact: true })).toBeVisible();
  await expect(page.getByText(goldenPublicationTitle)).toBeVisible();
  await saveModuleEvidence(page, testInfo, "seed-review-publication-readback");
}

async function assertDownstreamSurvivedRestart(
  page: Page,
  request: APIRequestContext,
  testInfo: TestInfo,
  pipelineId: number,
) {
  const workspaceResponse = await request.get(`/api/pipeline/${pipelineId}/workspace`);
  const workspaceText = await workspaceResponse.text();
  expect(workspaceResponse.ok(), workspaceText).toBe(true);
  const workspace = JSON.parse(workspaceText) as {
    active_script_version_id: string;
    script_versions: Array<{
      version_id: string;
      status: string;
      integrity_status: string;
      approval_integrity_status: string;
      approval_evidence_hash: string;
      shot_plan_hash: string;
      snapshot_hash: string;
    }>;
    script_factory: {
      import_operation_id: string;
      approval_operation_id: string;
    };
  };
  const activeVersion = workspace.script_versions.find(
    (version) => version.version_id === workspace.active_script_version_id,
  );
  expect(activeVersion).toEqual(
    expect.objectContaining({
      status: "approved",
      integrity_status: "full_envelope_verified",
      approval_integrity_status: "snapshot_hash_bound",
      approval_evidence_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      shot_plan_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      snapshot_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
    }),
  );
  expect(workspace.script_factory).toEqual(
    expect.objectContaining({
      import_operation_id: expect.stringMatching(/^[0-9a-f]{32}$/),
      approval_operation_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    }),
  );
  await page.goto(`/scripts/${pipelineId}`);
  await expect(page.getByText("v2 完整哈希已验证", { exact: true })).toBeVisible();

  const completionResponse = await request.get(
    `/api/pipeline/${pipelineId}/completion-status`,
  );
  const completionText = await completionResponse.text();
  expect(completionResponse.ok(), completionText).toBe(true);
  expect(JSON.parse(completionText).is_complete).toBe(true);

  const contractResponse = await request.get(
    `/api/pipeline/${pipelineId}/quality-contract`,
  );
  const contractText = await contractResponse.text();
  expect(contractResponse.ok(), contractText).toBe(true);
  const contract = JSON.parse(contractText) as {
    quality_contract_relative_path: string;
    quality_contract_sha256: string;
    contract: { format: string };
  };
  expect(contract.quality_contract_relative_path).toBe(
    "质量合同/quality-contract-v1.json",
  );
  expect(contract.quality_contract_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(contract.contract.format).toBe("auto-video-engine-quality-contract-v1");

  const releaseResponse = await request.post(`/api/pipeline/${pipelineId}/release-package`);
  const releaseText = await releaseResponse.text();
  expect(releaseResponse.ok(), releaseText).toBe(true);
  expect(JSON.parse(releaseText).release_supported).toBe(true);

  await page.goto(`/finished/${pipelineId}`);
  const qualityContractPanel = page.getByLabel("只读质量合同");
  await expect(qualityContractPanel).toContainText("发布平台");
  await expect(qualityContractPanel).toContainText("抖音");
  await expect(qualityContractPanel).toContainText("画面比例");
  await expect(qualityContractPanel).toContainText("9:16");
  await expect(qualityContractPanel).toContainText("目标时长");
  await expect(qualityContractPanel).toContainText("60 秒");
  await expect(
    page.getByLabel(`质量合同 SHA-256：${contract.quality_contract_sha256}`),
  ).toHaveText(
    `${contract.quality_contract_sha256.slice(0, 12)}…${contract.quality_contract_sha256.slice(-8)}`,
  );
  await expect(page.locator("body")).toContainText("验收成片：");
  await expect(page.locator("body")).toContainText("质量报告：");
  await saveModuleEvidence(page, testInfo, "restart-downstream-finished-persisted");

  const postResponse = await request.get(
    `/api/video-posts?sort_by=published_at&page=1&page_size=20&pipeline_id=${pipelineId}`,
  );
  const postText = await postResponse.text();
  expect(postResponse.ok(), postText).toBe(true);
  expect(JSON.parse(postText).items).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        pipeline_id: pipelineId,
        video_title: goldenPublicationTitle,
      }),
    ]),
  );
  const reviewResponse = await request.get(
    `/api/reviews?sort_by=published_at&page=1&page_size=20&pipeline_id=${pipelineId}`,
  );
  const reviewText = await reviewResponse.text();
  expect(reviewResponse.ok(), reviewText).toBe(true);
  expect(JSON.parse(reviewText).items).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        pipeline_id: pipelineId,
        video_title: goldenPublicationTitle,
      }),
    ]),
  );

  await page.goto(`/review?pipeline_id=${pipelineId}`);
  await expect(page.getByText(goldenPublicationTitle)).toBeVisible();
  await page.getByRole("tab", { name: "数据复盘" }).click();
  await expect(page.getByText(goldenPublicationTitle)).toBeVisible();
  await saveModuleEvidence(page, testInfo, "restart-review-publication-persisted");

  const weeklyResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/reviews/weekly/report" &&
      url.searchParams.get("pipeline_id") === String(pipelineId)
    );
  });
  await page.getByRole("tab", { name: "周报建议", exact: true }).click();
  const weeklyResponse = await weeklyResponsePromise;
  const weeklyText = await weeklyResponse.text();
  expect(weeklyResponse.ok(), weeklyText).toBe(true);
  const weeklyPayload = JSON.parse(weeklyText) as {
    published_count: number;
    published_videos: Array<{ pipeline_id: number; video_title: string }>;
  };
  expect(weeklyPayload.published_count).toBeGreaterThan(0);
  expect(weeklyPayload.published_videos.every(
    (item) => item.pipeline_id === pipelineId,
  )).toBe(true);
  expect(weeklyPayload.published_videos).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ video_title: goldenPublicationTitle }),
    ]),
  );
  await expect(page.getByText(goldenPublicationTitle).first()).toBeVisible();
  if (testInfo.project.name.includes("mobile")) {
    const weeklyRow = page.locator(".compact-review-list .review-row").first();
    const primaryBox = await weeklyRow.locator(".review-primary").boundingBox();
    const ratesBox = await weeklyRow.locator(".review-rates").boundingBox();
    expect(primaryBox).not.toBeNull();
    expect(ratesBox).not.toBeNull();
    expect(ratesBox!.y).toBeGreaterThanOrEqual(primaryBox!.y + primaryBox!.height);
    await expect(weeklyRow.locator(".review-primary h2")).toHaveCSS(
      "white-space",
      "normal",
    );
  }
  await saveModuleEvidence(page, testInfo, "restart-weekly-project-scoped");
}

test("空数据库复盘不虚构趋势、因果或方向建议", async ({ page, request }, testInfo) => {
  test.skip(process.env.GOLDEN_E2E_PHASE !== "empty", "只在示例数据写入前验证空数据库");
  const failures = installFailureCapture(page);

  const listResponse = await request.get(
    "/api/reviews?sort_by=published_at&page=1&page_size=20",
  );
  expect(listResponse.ok(), await listResponse.text()).toBe(true);
  const listPayload = await listResponse.json();
  expect(listPayload.items).toEqual([]);
  expect(listPayload.pagination.total).toBe(0);

  const weeklyResponse = await request.get("/api/reviews/weekly/report");
  expect(weeklyResponse.ok(), await weeklyResponse.text()).toBe(true);
  const weeklyPayload = await weeklyResponse.json();
  expect(weeklyPayload).toMatchObject({
    published_count: 0,
    data_note: "最近 7 天没有发布数据，无法判断趋势、因果或下周方向。",
    next_week_do: [],
    next_week_avoid: [],
    local_rules_only: true,
    real_ai_called: false,
  });
  for (const key of [
    "published_videos",
    "high_favorite_videos",
    "high_share_videos",
    "long_tail_videos",
    "stalled_videos",
    "continue_topics",
    "pause_topics",
  ]) {
    expect(weeklyPayload[key], `${key} 在空库中必须为空`).toEqual([]);
  }

  await page.goto("/review");
  await expect(page.getByRole("heading", { name: "发布数据追踪", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "数据复盘", exact: true }).click();
  await expect(page.getByText("当前没有可复盘的发布数据。")).toBeVisible();
  await expect(page.locator(".review-row")).toHaveCount(0);
  await page.getByRole("button", { name: "去发布数据追踪" }).click();
  await expect(page.getByRole("heading", { name: "发布数据追踪", exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "周报建议", exact: true }).click();
  await expect(page.getByRole("heading", { name: "复盘周报", exact: true })).toBeVisible();
  await expect(page.getByText("本周还没有可复盘的发布数据。")).toBeVisible();
  await expect(
    page.getByText("先新增发布记录和数据快照；有证据后才会生成趋势和建议。"),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "有长尾潜力" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "下周建议做什么" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "下周建议避免什么" })).toHaveCount(0);
  await assertEightModulesStayVisible(page);
  await assertNoPageLevelHorizontalOverflow(page, "empty-review");
  await assertButtonLabelsFit(page, "empty-review");
  await saveModuleEvidence(page, testInfo, "empty-review-no-invention");
  await page.getByRole("button", { name: "去发布数据追踪" }).click();
  await expect(page.getByRole("heading", { name: "发布数据追踪", exact: true })).toBeVisible();

  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});

test("设置页用真实后端区分 Skills 与 providers 的安装、配置和验证状态", async ({
  page,
  request,
}, testInfo) => {
  test.skip(process.env.GOLDEN_E2E_PHASE !== "empty", "能力真相在无业务数据阶段验收");
  const failures = installFailureCapture(page);

  const response = await request.get("/api/settings/runtime-status");
  const body = await response.text();
  expect(response.ok(), body).toBe(true);
  expect(body).not.toContain("golden-test-disabled");
  const payload = JSON.parse(body) as {
    provider: {
      name: string;
      status: string;
      verified: boolean;
      production_allowed: boolean;
      secret_configured: boolean;
    };
    providers: RuntimeCapabilityTruth[];
    skills: RuntimeCapabilityTruth[];
  };
  expect(payload.provider).toMatchObject({
    name: "fake",
    status: "test_only",
    verified: false,
    production_allowed: false,
    secret_configured: false,
  });

  const providers = new Map(payload.providers.map((item) => [item.id, item]));
  const skills = new Map(payload.skills.map((item) => [item.id, item]));
  expect(providers.get("script_ai")).toMatchObject({
    status: "test_only",
    installed: true,
    configured: false,
    verified: false,
    production_allowed: false,
  });
  expect(providers.get("pexels")).toMatchObject({
    status: "blocked_missing_config",
    installed: true,
    configured: false,
    verified: false,
    production_allowed: false,
    secret_configured: false,
  });
  expect(skills.get("hyperframes")).toMatchObject({
    status: "installed_not_integrated",
    installed: true,
    configured: false,
    verified: false,
    production_allowed: false,
    declared_version: "1.2.3-golden",
    last_verified_at: null,
  });
  expect(skills.get("video-use")).toMatchObject({
    status: "not_installed",
    installed: false,
    configured: false,
    verified: false,
    production_allowed: false,
  });
  for (const capability of [...payload.providers, ...payload.skills]) {
    expect(capability.production_allowed).toBe(false);
  }

  await page.goto("/settings");
  await page.getByRole("tab", { name: "AI 配置", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Providers 能力状态" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Skills 能力状态" })).toBeVisible();

  const aiCard = page.locator(".capability-truth-card").filter({
    has: page.getByRole("heading", { name: "脚本 AI（fake）", exact: true }),
  });
  await expect(aiCard.getByText("仅测试", { exact: true })).toBeVisible();
  await expect(aiCard.getByText("已内置", { exact: true })).toBeVisible();
  await expect(aiCard.getByText("无生产级 fallback", { exact: true })).toBeVisible();
  await expect(aiCard.getByText("当前阻断", { exact: true })).toBeVisible();

  const pexelsCard = page.locator(".capability-truth-card").filter({
    has: page.getByRole("heading", { name: "Pexels", exact: true }),
  });
  await expect(pexelsCard.getByText("缺少配置", { exact: true })).toBeVisible();
  await expect(pexelsCard.getByText("Key 未配置", { exact: true })).toBeVisible();

  const hyperframesCard = page
    .locator(".capability-truth-card")
    .filter({ has: page.getByRole("heading", { name: "HyperFrames", exact: true }) });
  await expect(hyperframesCard.getByText("已安装，未接入", { exact: true })).toBeVisible();
  await expect(hyperframesCard.getByText("版本 1.2.3-golden", { exact: true })).toBeVisible();
  const videoUseCard = page
    .locator(".capability-truth-card")
    .filter({ has: page.getByRole("heading", { name: "video-use", exact: true }) });
  await expect(videoUseCard.getByText("未安装", { exact: true }).first()).toBeVisible();

  expect(await page.locator("body").innerText()).not.toContain("golden-test-disabled");
  await assertEightModulesStayVisible(page);
  await assertNoPageLevelHorizontalOverflow(page, "req018-capability-truth");
  await assertButtonLabelsFit(page, "req018-capability-truth");
  await saveModuleEvidence(page, testInfo, "empty-req018-capability-truth");

  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});

test("八模块通过真实测试后端读取同一项目并在重启后保持", async ({ page, request }, testInfo) => {
  test.setTimeout(420_000);
  const phase = process.env.GOLDEN_E2E_PHASE;
  test.skip(phase === "empty", "空数据库阶段不写示例项目");
  const failures = installFailureCapture(page);

  if (phase === "seed") {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "账号风格库" })).toBeVisible();
    await page.getByRole("tab", { name: "系统检查", exact: true }).click();
    await expect(page.getByRole("heading", { name: "系统检查", exact: true })).toBeVisible();
    const seedResponsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/demo-data/seed" &&
        response.request().method() === "POST",
      { timeout: 60_000 },
    );
    await page.getByRole("button", { name: "生成示例数据" }).click();
    const seedResponse = await seedResponsePromise;
    expect(seedResponse.ok(), await seedResponse.text()).toBe(true);
    await expect(page.getByRole("status").filter({ hasText: /示例数据/ })).toContainText(
      /示例数据已(创建|存在)|已准备/,
      { timeout: 10_000 },
    );
  }

  const listResponse = await request.get("/api/pipeline?page=1&page_size=100");
  expect(listResponse.ok(), await listResponse.text()).toBe(true);
  const pipelineList = (await listResponse.json()) as PipelineListResponse;
  const project = pipelineList.items.find((item) => item.project_name === demoProjectName);
  expect(project, `${phase} 阶段必须读回真实 SQLite 中的示例项目`).toBeTruthy();
  expect(project!.has_failed, project!.failure_reason || "示例项目不应执行失败").toBe(false);
  const pipelineId = project!.pipeline_id;

  const modules = [
    { name: "01-task", path: "/tasks", heading: "任务" },
    { name: "02-topic", path: "/topics", heading: "选题" },
    { name: "03-script", path: `/scripts/${pipelineId}`, heading: "脚本" },
    { name: "04-material", path: `/materials/${pipelineId}`, heading: "素材" },
    { name: "05-editing", path: `/editing/${pipelineId}`, heading: "剪辑" },
    { name: "06-finished", path: `/finished/${pipelineId}`, heading: "成片" },
    {
      name: "07-review",
      path: `/review?pipeline_id=${pipelineId}`,
      heading: "发布数据追踪",
    },
    { name: "08-settings", path: "/settings", heading: "账号风格库" },
  ];

  for (const module of modules) {
    await page.goto(module.path);
    await expect(page.getByRole("heading", { name: module.heading, exact: true })).toBeVisible();
    await page.waitForLoadState("networkidle");
    await assertEightModulesStayVisible(page);
    await assertNoPageLevelHorizontalOverflow(page, module.name);
    await assertButtonLabelsFit(page, module.name);
    if (module.name === "04-material" && testInfo.project.name === "mobile-375x812-edge") {
      await assertMaterialTaskTableFits(page);
    }
    if (phase === "restart" && module.name === "03-script") {
      await expect(page.getByText("主体：AI 搜索结果页")).toBeVisible();
      await expect(page.getByText("动作：从链接列表切换到任务交付卡片")).toBeVisible();
      await expect(page.getByText("地点：竖屏产品界面")).toBeVisible();
      await expect(page.getByText("时间：当前版本")).toBeVisible();
      await expect(
        page.getByText("构图：来源标题在上，任务卡片居中，出处固定在底部"),
      ).toBeVisible();
      await expect(page.getByText("声音：任务卡片出现时使用一次轻提示音")).toBeVisible();
    }
    if (phase === "restart" && module.name === "04-material") {
      await expect(page.getByText("主体：AI 搜索结果页")).toBeVisible();
      await expect(page.getByText("不得用普通办公画面冒充产品证据")).toBeVisible();
      await expect(page.getByText("画面必须同时显示来源和任务交付结果")).toBeVisible();
      const persistedCard = page
        .locator(".material-candidate-card")
        .filter({ hasText: "shot-01-proof" });
      await expect(persistedCard).toContainText(
        "已锁定 golden-shot-01-proof-replacement",
      );
      await expect(persistedCard).toContainText("golden-shot-01-proof-a");
      await expect(persistedCard).toContainText("golden-shot-01-proof-replacement");
      await expect(persistedCard).toContainText("已拒绝");
    }
    await expect(page.locator("body")).not.toContainText("无法连接后端服务");
    await expect(page.locator("body")).not.toContainText("Internal Server Error");
    const overlay = await page.locator(
      "[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay",
    ).count();
    expect(overlay, `${module.name} 不应出现框架错误遮罩`).toBe(0);
    await saveModuleEvidence(page, testInfo, `${phase}-${module.name}`);
  }

  await page.goto(`/scripts/${pipelineId}`);
  await page.reload();
  await expect(page.getByRole("heading", { name: "脚本", exact: true })).toBeVisible();
  await expect(page.locator("body")).toContainText(demoProjectName);

  if (phase === "seed") {
    await completeLocalScriptFactoryGolden(page, request, testInfo, pipelineId);
    await runLocalRealDownstreamGolden(
      page,
      request,
      testInfo,
      pipelineId,
      project!.project_dir,
    );
  } else {
    await assertDownstreamSurvivedRestart(page, request, testInfo, pipelineId);
  }

  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});

test("脚本历史版本恢复为新草稿，并在后端重启和手机端保持", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(300_000);
  const phase = process.env.GOLDEN_E2E_PHASE;
  test.skip(phase === "empty", "历史恢复在示例项目准备后验收");
  const failures = installFailureCapture(page);

  let listResponse = await request.get("/api/pipeline?page=1&page_size=100");
  expect(listResponse.ok(), await listResponse.text()).toBe(true);
  let projects = (await listResponse.json()) as PipelineListResponse;
  let restoreProject = projects.items.find(
    (item) => item.project_name === req005RestoreProjectName,
  );

  if (phase === "seed") {
    expect(restoreProject, "REQ-005 独立项目在 seed 前不应存在").toBeUndefined();
    const demoProject = projects.items.find(
      (item) => item.project_name === demoProjectName,
    );
    expect(demoProject, "必须先由主黄金链生成示例项目").toBeTruthy();
    const buildProjectResponse = await request.post(
      `/api/pipeline/hotspots/${demoProject!.hotspot_id}/build-project`,
      {
        data: {
          project_name: req005RestoreProjectName,
          stop_after: "创建中文项目目录",
        },
        timeout: 120_000,
      },
    );
    expect(buildProjectResponse.ok(), await buildProjectResponse.text()).toBe(true);
    listResponse = await request.get("/api/pipeline?page=1&page_size=100");
    expect(listResponse.ok(), await listResponse.text()).toBe(true);
    projects = (await listResponse.json()) as PipelineListResponse;
    restoreProject = projects.items.find(
      (item) => item.project_name === req005RestoreProjectName,
    );
    expect(restoreProject).toBeTruthy();
    await createAcceptedScriptVersion(request, restoreProject!.pipeline_id, "第一版");
    await createAcceptedScriptVersion(request, restoreProject!.pipeline_id, "第二版");

    await page.goto(`/scripts/${restoreProject!.pipeline_id}`);
    await expect(page.getByRole("heading", { name: "脚本", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "脚本版本对比" })).toBeVisible();
    await page.getByRole("button", { name: "加载版本对比" }).click();
    await expect(
      page.locator(".script-version-voiceover").filter({ hasText: /第一版：换成人话说/ }),
    ).toBeVisible();
    await expect(
      page.locator(".script-version-voiceover").filter({ hasText: /第二版：换成人话说/ }),
    ).toBeVisible();
    await expect(page.getByLabel("版本字段差异摘要")).toContainText("有变化");
    await page.getByRole("button", { name: "恢复版本 1 为新草稿" }).click();
    const confirmation = page.getByRole("alertdialog", {
      name: "确认恢复脚本版本 1",
    });
    await expect(confirmation).toContainText("创建新的版本 3 草稿");
    await expect(confirmation).toContainText("当前批准会失效");
    await expect(confirmation).toContainText("历史版本不会被修改");
    await page.screenshot({
      path: testInfo.outputPath("seed-req005-restore-confirmation.png"),
      fullPage: true,
    });
    const restoreResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith("/restore"),
    );
    await confirmation.getByRole("button", { name: "确认恢复为新草稿" }).click();
    const restoreResponse = await restoreResponsePromise;
    const restoreText = await restoreResponse.text();
    expect(restoreResponse.ok(), restoreText).toBe(true);
    const restored = JSON.parse(restoreText) as {
      operation: {
        operation_id: string;
        restored_version_id: string;
        restored_version_number: number;
      };
    };
    expect(restored.operation.operation_id).toMatch(/^[0-9a-f]{32}$/);
    expect(restored.operation.restored_version_number).toBe(3);
    await expect(
      page.getByText(
        "已从版本 1 创建版本 3 草稿。旧版本仍保留；重新生成任务包并验收后才能进入素材。",
      ),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "验收并进入素材" })).toBeDisabled();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
  }

  expect(restoreProject, `${phase} 阶段必须读到 REQ-005 独立项目`).toBeTruthy();
  const workspaceResponse = await request.get(
    `/api/pipeline/${restoreProject!.pipeline_id}/workspace`,
  );
  const workspaceText = await workspaceResponse.text();
  expect(workspaceResponse.ok(), workspaceText).toBe(true);
  const workspace = JSON.parse(workspaceText) as {
    active_script_version_id: string;
    script_state_sha256: string;
    script_versions: Array<{
      version_id: string;
      version_number: number;
      status: string;
    }>;
    script_factory: {
      status: string;
      accepted: boolean;
      restore_operation_id: string;
      restored_from_version_id: string;
    };
  };
  expect(workspace.script_state_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(workspace.script_versions).toHaveLength(3);
  expect(workspace.script_versions.find((item) => item.version_number === 1)?.status).toBe(
    "invalidated",
  );
  expect(workspace.script_versions.find((item) => item.version_number === 2)?.status).toBe(
    "invalidated",
  );
  const restoredVersion = workspace.script_versions.find(
    (item) => item.version_number === 3,
  );
  expect(restoredVersion).toEqual(
    expect.objectContaining({
      version_id: workspace.active_script_version_id,
      status: "draft",
    }),
  );
  expect(workspace.script_factory).toEqual(
    expect.objectContaining({
      status: "draft_edited",
      accepted: false,
      restore_operation_id: expect.stringMatching(/^[0-9a-f]{32}$/),
      restored_from_version_id: expect.stringMatching(/-0001-/),
    }),
  );
  const restoredDetailResponse = await request.get(
    `/api/pipeline/${restoreProject!.pipeline_id}/script-versions/${workspace.active_script_version_id}`,
  );
  expect(restoredDetailResponse.ok(), await restoredDetailResponse.text()).toBe(true);
  const restoredDetail = (await restoredDetailResponse.json()) as {
    input_evidence: {
      restore_lineage: {
        operation_id: string;
        source_version_id: string;
        fallback_fields: string[];
      };
    };
  };
  expect(restoredDetail.input_evidence.restore_lineage).toEqual(
    expect.objectContaining({
      operation_id: workspace.script_factory.restore_operation_id,
      source_version_id: workspace.script_factory.restored_from_version_id,
      fallback_fields: expect.arrayContaining(["use_avatar"]),
    }),
  );
  const operationFiles = await readdir(
    join(restoreProject!.project_dir, "脚本版本", "operations"),
  );
  expect(
    operationFiles.some(
      (name) =>
        name ===
        `operation-${workspace.script_factory.restore_operation_id}-intent.json`,
    ),
  ).toBe(true);
  expect(
    operationFiles.some(
      (name) =>
        name.startsWith(
          `operation-${workspace.script_factory.restore_operation_id}-outcome-`,
        ) && name.endsWith(".json"),
    ),
  ).toBe(true);

  if (phase === "restart") {
    await page.goto(`/scripts/${restoreProject!.pipeline_id}`);
    await expect(page.getByRole("heading", { name: "脚本版本对比" })).toBeVisible();
    await page.getByRole("button", { name: "加载版本对比" }).click();
    await expect(page.getByLabel("版本字段差异摘要")).toBeVisible();
    await expect(page.getByRole("button", { name: "验收并进入素材" })).toBeDisabled();
    if (testInfo.project.name === "mobile-375x812-edge") {
      const columns = await page.locator(".script-version-compare-grid").evaluate(
        (element) => getComputedStyle(element).gridTemplateColumns.split(" ").length,
      );
      expect(columns).toBe(1);
    }
  }
  await assertEightModulesStayVisible(page);
  await assertNoPageLevelHorizontalOverflow(page, "req005-script-restore");
  await assertButtonLabelsFit(page, "req005-script-restore");
  await saveModuleEvidence(page, testInfo, `${phase}-req005-script-restore`);
  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});

test("素材恢复只重试目标镜头，另一镜头可跳过并在重启后保持", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(360_000);
  const phase = process.env.GOLDEN_E2E_PHASE;
  test.skip(phase === "empty", "素材动作矩阵在示例数据写入后验收");
  const failures = installFailureCapture(page);

  const listResponse = await request.get("/api/pipeline?page=1&page_size=100");
  const listText = await listResponse.text();
  expect(listResponse.ok(), listText).toBe(true);
  let pipelineList = JSON.parse(listText) as PipelineListResponse;
  let actionProject = pipelineList.items.find(
    (item) => item.project_name === req008ActionProjectName,
  );

  if (phase === "seed") {
    expect(actionProject, "独立素材动作项目在 seed 前不应已存在").toBeUndefined();
    const demoProject = pipelineList.items.find((item) => item.project_name === demoProjectName);
    expect(demoProject, "必须先由主黄金链生成真实示例项目").toBeTruthy();
    const buildResponse = await request.post(
      `/api/pipeline/hotspots/${demoProject!.hotspot_id}/build-project`,
      {
        data: {
          project_name: req008ActionProjectName,
          stop_after: "创建中文项目目录",
        },
        timeout: 120_000,
      },
    );
    const buildText = await buildResponse.text();
    expect(buildResponse.ok(), buildText).toBe(true);
    expect((JSON.parse(buildText) as { pipeline_id: number }).pipeline_id).toBeGreaterThan(0);

    const refreshedListResponse = await request.get("/api/pipeline?page=1&page_size=100");
    const refreshedListText = await refreshedListResponse.text();
    expect(refreshedListResponse.ok(), refreshedListText).toBe(true);
    pipelineList = JSON.parse(refreshedListText) as PipelineListResponse;
    actionProject = pipelineList.items.find(
      (item) => item.project_name === req008ActionProjectName,
    );
  }

  expect(actionProject, `${phase} 阶段必须读到独立素材动作项目`).toBeTruthy();
  const pipelineId = actionProject!.pipeline_id;
  const materialResponse = await request.get(
    `/api/pipeline/${pipelineId}/material-package`,
  );
  const materialText = await materialResponse.text();
  expect(materialResponse.ok(), materialText).toBe(true);
  const material = JSON.parse(materialText) as Req008MaterialPackage;

  if (phase === "seed") {
    expect(material.material_tasks.length, "素材动作矩阵至少需要两个镜头").toBeGreaterThanOrEqual(2);
    const skipTarget = material.material_tasks[0];
    const retryTarget =
      material.material_tasks.find(
        (task, index) =>
          index > 0 && task.needs_official_capture !== true && task.needs_pexels !== true,
      ) || material.material_tasks[1];
    expect(retryTarget.shot_id).not.toBe(skipTarget.shot_id);
    const recoveryMarker = "进行中";

    const markResponse = await request.post(
      `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(retryTarget.shot_id)}/status`,
      { data: { status: recoveryMarker } },
    );
    const markText = await markResponse.text();
    expect(markResponse.ok(), markText).toBe(true);

    await page.goto(`/materials/${pipelineId}`);
    await expect(page.getByRole("heading", { name: "素材", exact: true })).toBeVisible();
    await expect(page.getByText(recoveryMarker, { exact: true })).toBeVisible();
    const recoveryButton = page.getByRole("button", {
      name: "检查上次执行状态",
      exact: true,
    });
    await expect(recoveryButton).toBeVisible();
    const retryPath = `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(retryTarget.shot_id)}/reconcile-and-retry`;
    const retryResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === retryPath,
      { timeout: 300_000 },
    );
    await recoveryButton.click();
    const retryResponse = await retryResponsePromise;
    const retryText = await retryResponse.text();
    expect(retryResponse.ok(), retryText).toBe(true);
    const retryResult = JSON.parse(retryText) as {
      executed_task: Req008MaterialTask;
      tasks: Req008MaterialTask[];
    };
    expect(
      retryResult.executed_task.shot_id,
      "恢复第二条素材时不得误执行队列中更靠前的第一条",
    ).toBe(retryTarget.shot_id);
    expect(
      retryResult.tasks.find((task) => task.shot_id === skipTarget.shot_id)?.status,
      "目标重试完成前，第一条待执行素材不得被抢跑",
    ).toBe(skipTarget.status);

    const firstRow = page.locator(".material-task-table tbody tr").first();
    const skipButton = firstRow.getByRole("button", { name: "跳过", exact: true });
    await expect(skipButton).toBeEnabled({ timeout: 30_000 });
    const skipPath = `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(skipTarget.shot_id)}/skip`;
    const skipResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === skipPath,
    );
    await skipButton.focus();
    await page.keyboard.press("Enter");
    const skipResponse = await skipResponsePromise;
    const skipText = await skipResponse.text();
    expect(skipResponse.ok(), skipText).toBe(true);
    const skipResult = JSON.parse(skipText) as { executed_task: Req008MaterialTask };
    expect(skipResult.executed_task.shot_id).toBe(skipTarget.shot_id);
    expect(skipResult.executed_task.status).toBe("已跳过");

    const readbackResponse = await request.get(
      `/api/pipeline/${pipelineId}/material-package`,
    );
    const readbackText = await readbackResponse.text();
    expect(readbackResponse.ok(), readbackText).toBe(true);
    const readback = JSON.parse(readbackText) as Req008MaterialPackage;
    const skipped = readback.material_tasks.find(
      (task) => task.shot_id === skipTarget.shot_id,
    );
    const retried = readback.material_tasks.find(
      (task) => task.shot_id === retryTarget.shot_id,
    );
    expect(skipped?.status).toBe("已跳过");
    expect(skipped?.reject_reason).toContain("手动跳过");
    expect(retried?.status).not.toBe(recoveryMarker);
    expect(retried?.status_history).toContain(recoveryMarker);
    expect(retried?.material_path || retried?.status).toBeTruthy();

    await page.reload();
    await expect(page.locator(".material-task-table tbody tr").first()).toContainText("已跳过");
    await expect(page.locator("body")).not.toContainText(recoveryMarker);
    await saveModuleEvidence(page, testInfo, "seed-req008-retry-skip-readback");
  } else {
    const skipped = material.material_tasks.find((task) => task.status === "已跳过");
    const retried = material.material_tasks.find((task) =>
      task.status_history?.includes("进行中"),
    );
    expect(skipped, "重启后必须保留已跳过素材").toBeTruthy();
    expect(skipped?.reject_reason).toContain("手动跳过");
    expect(retried, "重启后必须保留目标镜头的恢复历史").toBeTruthy();
    expect(retried?.shot_id).not.toBe(skipped?.shot_id);
    expect(retried?.status).not.toBe("进行中");

    await page.goto(`/materials/${pipelineId}`);
    await expect(page.getByRole("heading", { name: "素材", exact: true })).toBeVisible();
    await expect(page.locator(".material-task-table")).toContainText("已跳过");
    await expect(page.locator("body")).toContainText("手动跳过");
    await expect(page.locator("body")).not.toContainText("进行中");
    if (testInfo.project.name === "mobile-375x812-edge") {
      await assertMaterialTaskTableFits(page);
      const mobileSkipTarget = material.material_tasks.find(
        (task) =>
          !task.material_path && ["待执行", "等待", "未开始"].includes(task.status),
      );
      expect(mobileSkipTarget, "手机端必须有一条可实际操作的待执行素材").toBeTruthy();
      expect(mobileSkipTarget?.time_range).toBeTruthy();
      const mobileTargetRow = page
        .locator(".material-task-table tbody tr")
        .filter({ hasText: mobileSkipTarget!.time_range! })
        .first();
      const mobileSkipButton = mobileTargetRow.getByRole("button", {
        name: "跳过",
        exact: true,
      });
      await expect(mobileSkipButton).toBeEnabled();
      const mobileSkipPath = `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(mobileSkipTarget!.shot_id)}/skip`;
      const mobileSkipResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname === mobileSkipPath,
      );
      await mobileSkipButton.click();
      const mobileSkipResponse = await mobileSkipResponsePromise;
      const mobileSkipText = await mobileSkipResponse.text();
      expect(mobileSkipResponse.ok(), mobileSkipText).toBe(true);
      expect(
        (JSON.parse(mobileSkipText) as { executed_task: Req008MaterialTask }).executed_task
          .shot_id,
      ).toBe(mobileSkipTarget!.shot_id);
      await page.reload();
      await expect(
        page
          .locator(".material-task-table tbody tr")
          .filter({ hasText: mobileSkipTarget!.time_range! })
          .first(),
      ).toContainText("已跳过");
      const mobileReadbackResponse = await request.get(
        `/api/pipeline/${pipelineId}/material-package`,
      );
      const mobileReadbackText = await mobileReadbackResponse.text();
      expect(mobileReadbackResponse.ok(), mobileReadbackText).toBe(true);
      const mobileReadback = JSON.parse(mobileReadbackText) as Req008MaterialPackage;
      expect(
        mobileReadback.material_tasks.find(
          (task) => task.shot_id === mobileSkipTarget!.shot_id,
        )?.status,
      ).toBe("已跳过");
    }
    await saveModuleEvidence(page, testInfo, "restart-req008-retry-skip-persisted");
  }

  await assertEightModulesStayVisible(page);
  await assertNoPageLevelHorizontalOverflow(page, "req008-retry-skip");
  await assertButtonLabelsFit(page, "req008-retry-skip");
  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});

test("上游脚本变化后锁定素材变为可见历史，并在后端重启后保持", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(240_000);
  const phase = process.env.GOLDEN_E2E_PHASE;
  test.skip(phase === "empty", "REQ-009 在示例数据写入后验收");
  const failures = installFailureCapture(page);

  const listResponse = await request.get("/api/pipeline?page=1&page_size=100");
  expect(listResponse.ok(), await listResponse.text()).toBe(true);
  let pipelineList = (await listResponse.json()) as PipelineListResponse;
  let staleProject = pipelineList.items.find(
    (item) => item.project_name === req009StaleProjectName,
  );

  if (phase === "seed") {
    expect(staleProject, "REQ-009 独立项目在 seed 前不应存在").toBeUndefined();
    const demoProject = pipelineList.items.find(
      (item) => item.project_name === demoProjectName,
    );
    expect(demoProject, "必须先由主黄金链生成示例项目").toBeTruthy();
    const buildResponse = await request.post(
      `/api/pipeline/hotspots/${demoProject!.hotspot_id}/build-project`,
      {
        data: {
          project_name: req009StaleProjectName,
          stop_after: "创建中文项目目录",
        },
        timeout: 120_000,
      },
    );
    expect(buildResponse.ok(), await buildResponse.text()).toBe(true);

    const refreshed = await request.get("/api/pipeline?page=1&page_size=100");
    expect(refreshed.ok(), await refreshed.text()).toBe(true);
    pipelineList = (await refreshed.json()) as PipelineListResponse;
    staleProject = pipelineList.items.find(
      (item) => item.project_name === req009StaleProjectName,
    );
    expect(staleProject).toBeTruthy();

    const tasks = await installGoldenCandidatePools(
      request,
      staleProject!.pipeline_id,
      staleProject!.project_dir,
    );
    const target = tasks[0];
    const candidateId = `golden-${target.shot_id}-a`;
    const lockResponse = await request.post(
      `/api/pipeline/${staleProject!.pipeline_id}/material-tasks/${encodeURIComponent(target.shot_id)}/candidate-pool/${encodeURIComponent(candidateId)}/lock`,
      { data: { reason: "REQ-009 重启前锁定" } },
    );
    expect(lockResponse.ok(), await lockResponse.text()).toBe(true);

    const workspaceResponse = await request.get(
      `/api/pipeline/${staleProject!.pipeline_id}/workspace`,
    );
    expect(workspaceResponse.ok(), await workspaceResponse.text()).toBe(true);
    const workspace = (await workspaceResponse.json()) as {
      script: {
        voiceover: string;
        storyboard: Array<Record<string, unknown>>;
        subtitles: Array<Record<string, unknown>>;
        title_options: Array<Record<string, unknown>>;
        cover_copy: Array<Record<string, unknown>>;
        publish_copy: Record<string, unknown>;
        use_avatar: boolean;
      };
    };
    const editResponse = await request.put(
      `/api/pipeline/${staleProject!.pipeline_id}/workspace/script`,
      {
        data: {
          ...workspace.script,
          voiceover: `${workspace.script.voiceover}\nREQ-009 上游脚本变化。`,
        },
      },
    );
    expect(editResponse.ok(), await editResponse.text()).toBe(true);

    const staleMutation = await request.post(
      `/api/pipeline/${staleProject!.pipeline_id}/material-tasks/${encodeURIComponent(target.shot_id)}/status`,
      { data: { status: "需要人工确认" } },
    );
    expect(staleMutation.status()).toBe(409);
  }

  expect(staleProject, `${phase} 阶段必须读到 REQ-009 独立项目`).toBeTruthy();
  const pipelineId = staleProject!.pipeline_id;
  const materialResponse = await request.get(
    `/api/pipeline/${pipelineId}/material-package`,
  );
  const materialText = await materialResponse.text();
  expect(materialResponse.ok(), materialText).toBe(true);
  const material = JSON.parse(materialText) as Req009MaterialPackage;
  const staleTask = material.material_tasks.find(
    (task) => task.candidate_pool?.stale === true,
  );
  expect(staleTask, "锁定镜头必须明确标记为历史失效").toBeTruthy();
  const lockedCandidate = staleTask?.candidate_pool?.candidates.find(
    (candidate) => candidate.status === "locked",
  );
  expect(lockedCandidate, "旧锁定候选不能被上游修改抹掉").toBeTruthy();
  expect(material.material_selection_history).toHaveLength(1);
  expect(material.material_selection_history[0]).toEqual(
    expect.objectContaining({
      reason_code: "upstream_script_changed",
      entry_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    }),
  );
  expect(material.material_selection_history[0].locked_selections).toContainEqual(
    expect.objectContaining({
      shot_id: staleTask!.shot_id,
      candidate_id: lockedCandidate!.candidate_id,
    }),
  );

  await page.goto(`/materials/${pipelineId}`);
  await expect(page.getByRole("heading", { name: "素材", exact: true })).toBeVisible();
  await expect(page.getByText("历史锁定已过期").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "历史素材锁定" })).toBeVisible();
  await expect(
    page.getByText(`${staleTask!.shot_id} · ${lockedCandidate!.candidate_id}`, {
      exact: false,
    }),
  ).toBeVisible();
  await assertEightModulesStayVisible(page);
  await assertNoPageLevelHorizontalOverflow(page, "req009-stale-history");
  await assertButtonLabelsFit(page, "req009-stale-history");
  await saveModuleEvidence(page, testInfo, `${phase}-req009-stale-history`);

  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});

test("任务页同时显示运行、失败、待批准和失效批准的真实状态", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(60_000);
  test.skip(process.env.GOLDEN_E2E_PHASE !== "restart", "状态矩阵在重启前写入真实 SQLite");
  const failures = installFailureCapture(page);
  const listResponse = await request.get("/api/pipeline?page=1&page_size=100");
  expect(listResponse.ok(), await listResponse.text()).toBe(true);
  const pipelineList = (await listResponse.json()) as PipelineListResponse;
  const expected = [
    ["REQ003-运行中项目", "进行中", "step_running"],
    ["REQ003-失败阻断项目", "失败", "step_failed"],
    ["REQ003-待成片批准项目", "需要人工确认", "approval_missing"],
    ["REQ003-批准后成片变更项目", "需要人工确认", "artifact_hash_mismatch"],
    [demoProjectName, "已完成", "approved_artifact_verified"],
  ] as const;
  const projects = new Map(
    pipelineList.items.map((item) => [item.project_name, item] as const),
  );
  for (const [name, status, reasonCode] of expected) {
    const project = projects.get(name);
    expect(project, `${name} 必须存在于真实 SQLite`).toBeTruthy();
    expect(project!.status).toBe(status);
    expect(project!.completion_reason_code).toBe(reasonCode);
  }

  const todayResponse = await request.get("/api/today/tasks");
  expect(todayResponse.ok(), await todayResponse.text()).toBe(true);
  const today = (await todayResponse.json()) as {
    confirmations: Array<{ task_id: string; target_id: number; meta: Record<string, unknown> }>;
    running_items: Array<{ target_id: number; meta: Record<string, unknown> }>;
    finished_items: Array<{ target_id: number; meta: Record<string, unknown> }>;
    handled_task_ids: string[];
  };
  const running = projects.get("REQ003-运行中项目")!;
  const failed = projects.get("REQ003-失败阻断项目")!;
  const approval = projects.get("REQ003-待成片批准项目")!;
  const stale = projects.get("REQ003-批准后成片变更项目")!;
  const verified = projects.get(demoProjectName)!;
  expect(today.running_items.some((item) => item.target_id === running.pipeline_id)).toBe(true);
  for (const [project, reasonCode] of [
    [failed, "step_failed"],
    [approval, "approval_missing"],
    [stale, "artifact_hash_mismatch"],
  ] as const) {
    expect(
      today.confirmations.some(
        (item) =>
          item.target_id === project.pipeline_id &&
          item.meta.completion_reason_code === reasonCode,
      ),
      `${project.project_name} 必须进入需要处理清单`,
    ).toBe(true);
  }
  expect(today.finished_items.some((item) => item.target_id === verified.pipeline_id)).toBe(true);
  expect(today.finished_items.some((item) => item.target_id === stale.pipeline_id)).toBe(false);

  const blockedTaskId = `confirm_project_${failed.pipeline_id}`;
  const completeResponse = await request.post(
    `/api/today/tasks/${encodeURIComponent(blockedTaskId)}/complete`,
  );
  expect(completeResponse.status()).toBe(409);
  expect(await completeResponse.text()).toContain("解决实际阻断");
  const afterBlockedAttempt = await request.get("/api/today/tasks");
  expect(afterBlockedAttempt.ok(), await afterBlockedAttempt.text()).toBe(true);
  expect((await afterBlockedAttempt.json()).handled_task_ids).not.toContain(blockedTaskId);

  await page.goto("/tasks");
  await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "需要处理", exact: true })).toBeVisible();
  await expect(page.getByText("REQ003-运行中项目", { exact: false }).first()).toBeVisible();
  for (const name of [
    "REQ003-失败阻断项目",
    "REQ003-待成片批准项目",
    "REQ003-批准后成片变更项目",
  ]) {
    const card = page.locator(".today-card-decision").filter({ hasText: name }).first();
    await expect(card).toBeVisible();
    await expect(card.getByRole("button", { name: `完成 ${name}` })).toHaveCount(0);
  }
  await expect(
    page.locator(".today-card-decision").filter({ hasText: "REQ-003 注入的素材步骤失败。" }).first(),
  ).toBeVisible();
  await expect(
    page.locator(".today-card-decision").filter({ hasText: "当前成片文件已发生变化。" }).first(),
  ).toBeVisible();
  await assertEightModulesStayVisible(page);
  await assertNoPageLevelHorizontalOverflow(page, "req003-status-matrix");
  await assertButtonLabelsFit(page, "req003-status-matrix");
  await saveModuleEvidence(page, testInfo, "restart-req003-status-matrix");

  await page.getByRole("tab", { name: "项目列表", exact: true }).click();
  await expect(page.getByRole("heading", { name: "项目列表", exact: true })).toBeVisible();
  const staleRow = page.locator(".project-row").filter({
    hasText: "REQ003-批准后成片变更项目",
  });
  await expect(staleRow).toBeVisible();
  await expect(staleRow).toContainText("当前成片文件已发生变化。");
  await expect(
    staleRow.getByRole("button", { name: "去成片验收 REQ003-批准后成片变更项目" }),
  ).toBeVisible();
  await assertNoPageLevelHorizontalOverflow(page, "req003-project-list-stale");
  await assertButtonLabelsFit(page, "req003-project-list-stale");
  await saveModuleEvidence(page, testInfo, "restart-req003-project-list-stale");

  expect(failures.pageErrors).toEqual([]);
  expect(failures.consoleErrors).toEqual([]);
  expect(failures.serverErrors).toEqual([]);
});
