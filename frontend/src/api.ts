import type {
  AccountStyleLibrary,
  AutoHotspotRunResult,
  BrowserHotspotRunResult,
  BrowserHotspotLoginResult,
  BuildProjectResult,
  ContentQualityResult,
  PipelineCompletionStatus,
  QualityContractReview,
  QualityContractSnapshot,
  QualityApprovalResult,
  QualityArtifactCandidate,
  QualityReportResult,
  ReleasePackageResult,
  RuntimeSettingsStatus,
  CopywritingSampleItem,
  CopywritingSampleListResponse,
  CopywritingSamplePayload,
  ExportRebuildResult,
  FinishedVideoListResponse,
  HotspotFetchResult,
  HotspotItem,
  HotspotListResponse,
  ManualHotspotImportResult,
  ManualHotspotPayload,
  MaterialPackageData,
  MaterialTaskRunNextResult,
  MaterialTaskStatusResult,
  EditingExportPackage,
  EndToEndVideoResult,
  ManualV1RunPayload,
  ManualV1RunResult,
  MaterialCandidateInput,
  MaterialCandidatePool,
  MaterialRightsReviewInput,
  PipelineVoiceoverResult,
  AudioSubtitleArtifact,
  TimelineMediaRunResult,
  PipelineDetail,
  PipelineListResponse,
  ScriptRewriteResult,
  ScriptVersionDetail,
  ScriptVersionRestoreRequest,
  ScriptVersionRestoreResponse,
  ScriptWorkOrderResult,
  ScriptWorkspaceData,
  ScriptWorkspaceUpdatePayload,
  SourceStatusResponse,
  TodayTasksResponse,
  TopicIdeaGenerateResult,
  TopicFilterSettings,
  TopicIdeaItem,
  TopicIdeaListResponse,
  VideoPostDetail,
  VideoPostListResponse,
  VideoPostPayload,
  VideoSnapshotPayload,
  VideoReviewDetail,
  VideoReviewListResponse,
  WeeklyReviewReport,
} from "./types";

export async function getRuntimeSettingsStatus(): Promise<RuntimeSettingsStatus> {
  return requestJson<RuntimeSettingsStatus>("/api/settings/runtime-status", {
    headers: jsonHeaders,
  });
}

export interface PipelineFilters {
  status: string;
  failedOnly: boolean;
  needsConfirmationOnly: boolean;
  page: number;
  pageSize: number;
}

export interface HotspotFilters {
  category: string;
  sourcePlatform: string;
  unfilteredOnly: boolean;
  sortBy: "hot_score" | "topic_score" | "save_score";
  page: number;
  pageSize: number;
}

export interface TopicIdeaFilters {
  hotspotId: string;
  recommendationBucket?: string;
  page: number;
  pageSize: number;
}

export interface VideoPostFilters {
  platform: string;
  pipelineId: string;
  status: string;
  sortBy: "published_at" | "views" | "favorites" | "shares";
  page: number;
  pageSize: number;
}

export interface VideoReviewFilters {
  platform: string;
  pipelineId: string;
  publishedFrom: string;
  publishedTo: string;
  judgement: string;
  sortBy:
    | "published_at"
    | "favorite_rate"
    | "share_rate"
    | "follower_rate"
    | "view_velocity";
  page: number;
  pageSize: number;
}

export interface ManualContentQualityPayload {
  text: string;
  sourceTitle: string;
  sourceType?: string;
  pipelineId?: number | null;
  scriptAssetId?: number | null;
}

export interface CopywritingSampleFilters {
  sampleType: string;
  scene: string;
  contentDirection: string;
  tag: string;
  keyword: string;
  enabledOnly: boolean;
  keyReferenceOnly: boolean;
  page: number;
  pageSize: number;
}

const jsonHeaders = {
  Accept: "application/json",
};

export async function getPipelines(
  filters: PipelineFilters,
): Promise<PipelineListResponse> {
  const params = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (filters.status) {
    params.set("status", filters.status);
  }
  if (filters.failedOnly) {
    params.set("failed_only", "true");
  }
  if (filters.needsConfirmationOnly) {
    params.set("needs_confirmation_only", "true");
  }
  return requestJson<PipelineListResponse>(`/api/pipeline?${params.toString()}`, {
    headers: jsonHeaders,
  });
}

export async function getPipelineDetail(
  pipelineId: number,
): Promise<PipelineDetail> {
  return requestJson<PipelineDetail>(`/api/pipeline/${pipelineId}`, {
    headers: jsonHeaders,
  });
}

export async function getPipelineCompletionStatus(
  pipelineId: number,
): Promise<PipelineCompletionStatus> {
  return requestJson<PipelineCompletionStatus>(
    `/api/pipeline/${pipelineId}/completion-status`,
    { headers: jsonHeaders },
  );
}

export async function createPipelineReleasePackage(
  pipelineId: number,
): Promise<ReleasePackageResult> {
  return requestJson<ReleasePackageResult>(
    `/api/pipeline/${pipelineId}/release-package`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function getPipelineQualityArtifactCandidates(
  pipelineId: number,
): Promise<{ pipeline_id: number; items: QualityArtifactCandidate[] }> {
  return requestJson<{ pipeline_id: number; items: QualityArtifactCandidate[] }>(
    `/api/pipeline/${pipelineId}/quality-report/candidates`,
    { headers: jsonHeaders },
  );
}

export async function getPipelineQualityContract(
  pipelineId: number,
): Promise<QualityContractSnapshot> {
  return requestJson<QualityContractSnapshot>(
    `/api/pipeline/${pipelineId}/quality-contract`,
    { headers: jsonHeaders },
  );
}

export type QualityManualGatePayload = {
  status: "passed" | "failed";
  evidence: string[];
};

export async function createPipelineQualityReport(
  pipelineId: number,
  artifactRelativePath: string,
  gates: Record<"visual_narration" | "facts_rights", QualityManualGatePayload>,
  qualityContractReview: QualityContractReview,
): Promise<QualityReportResult> {
  return requestJson<QualityReportResult>(
    `/api/pipeline/${pipelineId}/quality-report`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        artifact_relative_path: artifactRelativePath,
        gates,
        quality_contract_review: qualityContractReview,
      }),
    },
  );
}

export async function approvePipelineQualityReport(
  pipelineId: number,
  payload: {
    artifactRelativePath: string;
    qualityReportRelativePath: string;
    approvedBy: string;
    approvedAt?: string;
  },
): Promise<QualityApprovalResult> {
  return requestJson<QualityApprovalResult>(
    `/api/pipeline/${pipelineId}/quality-report/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        artifact_relative_path: payload.artifactRelativePath,
        quality_report_relative_path: payload.qualityReportRelativePath,
        approved_by: payload.approvedBy,
        ...(payload.approvedAt ? { approved_at: payload.approvedAt } : {}),
      }),
    },
  );
}

export async function continuePipeline(
  pipelineId: number,
): Promise<PipelineDetail> {
  return requestJson<PipelineDetail>(`/api/pipeline/${pipelineId}/continue`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force: false }),
  });
}

export async function getScriptWorkspace(
  pipelineId: number,
): Promise<ScriptWorkspaceData> {
  return requestJson<ScriptWorkspaceData>(
    `/api/pipeline/${pipelineId}/workspace`,
    { headers: jsonHeaders },
  );
}

export async function getScriptVersionDetail(
  pipelineId: number,
  versionId: string,
): Promise<ScriptVersionDetail> {
  return requestJson<ScriptVersionDetail>(
    `/api/pipeline/${pipelineId}/script-versions/${encodeURIComponent(versionId)}`,
    { headers: jsonHeaders },
  );
}

export async function restoreScriptVersionAsDraft(
  pipelineId: number,
  versionId: string,
  payload: ScriptVersionRestoreRequest,
): Promise<ScriptVersionRestoreResponse> {
  return requestJson<ScriptVersionRestoreResponse>(
    `/api/pipeline/${pipelineId}/script-versions/${encodeURIComponent(versionId)}/restore`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function updateScriptWorkspace(
  pipelineId: number,
  payload: ScriptWorkspaceUpdatePayload,
): Promise<ScriptWorkspaceData> {
  return requestJson<ScriptWorkspaceData>(
    `/api/pipeline/${pipelineId}/workspace/script`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function updateScriptPresentationOptions(
  pipelineId: number,
  payload: { use_avatar: boolean },
): Promise<ScriptWorkspaceData> {
  return requestJson<ScriptWorkspaceData>(
    `/api/pipeline/${pipelineId}/workspace/presentation`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function buildScriptWorkOrder(
  pipelineId: number,
): Promise<ScriptWorkOrderResult> {
  return requestJson<ScriptWorkOrderResult>(
    `/api/pipeline/${pipelineId}/script-work-order/build`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function dispatchScriptWorkOrder(
  pipelineId: number,
): Promise<ScriptWorkOrderResult> {
  return requestJson<ScriptWorkOrderResult>(
    `/api/pipeline/${pipelineId}/script-work-order/dispatch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function importScriptWorkOrderResult(
  pipelineId: number,
): Promise<ScriptWorkOrderResult> {
  return requestJson<ScriptWorkOrderResult>(
    `/api/pipeline/${pipelineId}/script-work-order/import`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function acceptScriptWorkOrder(
  pipelineId: number,
): Promise<ScriptWorkOrderResult> {
  return requestJson<ScriptWorkOrderResult>(
    `/api/pipeline/${pipelineId}/script-work-order/accept`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function getHotspots(
  filters: HotspotFilters,
): Promise<HotspotListResponse> {
  const params = new URLSearchParams({
    sort_by: filters.sortBy,
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (filters.category) {
    params.set("category", filters.category);
  }
  if (filters.sourcePlatform) {
    params.set("source_platform", filters.sourcePlatform);
  }
  if (filters.unfilteredOnly) {
    params.set("unfiltered_only", "true");
  }
  return requestJson<HotspotListResponse>(`/api/hotspots?${params.toString()}`, {
    headers: jsonHeaders,
  });
}

export async function getHotspotDetail(hotspotId: number): Promise<HotspotItem> {
  return requestJson<HotspotItem>(`/api/hotspots/${hotspotId}`, {
    headers: jsonHeaders,
  });
}

export async function importManualHotspot(
  payload: ManualHotspotPayload,
): Promise<ManualHotspotImportResult> {
  return requestJson<ManualHotspotImportResult>("/api/hotspots/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function fetchHotspotSources(): Promise<HotspotFetchResult> {
  return requestJson<HotspotFetchResult>("/api/hotspots/fetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
}

export async function getAutomationSourceStatus(): Promise<SourceStatusResponse> {
  return requestJson<SourceStatusResponse>("/api/automation/sources/status", {
    headers: jsonHeaders,
  });
}

export async function runAutoHotspotIntake(
  payload: {
    source_names?: string[];
    force?: boolean;
    generate_topics?: boolean;
    max_hotspots_for_topics?: number;
  } = {},
): Promise<AutoHotspotRunResult> {
  return requestJson<AutoHotspotRunResult>("/api/automation/hotspots/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function runBrowserHotspotIntake(
  payload: {
    source_names?: string[];
    force?: boolean;
  } = {},
): Promise<BrowserHotspotRunResult> {
  return requestJson<BrowserHotspotRunResult>("/api/automation/browser-hotspots/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function openBrowserHotspotLoginBrowser(): Promise<BrowserHotspotLoginResult> {
  return requestJson<BrowserHotspotLoginResult>(
    "/api/automation/browser-hotspots/login-browser",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function runManualV1Test(
  payload: ManualV1RunPayload,
): Promise<ManualV1RunResult> {
  return requestJson<ManualV1RunResult>("/api/pipeline/manual-v1-test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function buildProjectFromHotspot(
  hotspot: HotspotItem,
): Promise<BuildProjectResult> {
  return requestJson<BuildProjectResult>(
    `/api/pipeline/hotspots/${hotspot.id}/build-project`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_name: hotspot.title }),
    },
  );
}

export async function rewriteScriptPart(
  pipelineId: number,
  part: string,
): Promise<ScriptRewriteResult> {
  return requestJson<ScriptRewriteResult>(`/api/pipeline/${pipelineId}/rewrite`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ part }),
  });
}

export async function generatePipelineVoiceover(
  pipelineId: number,
  provider?: string,
): Promise<PipelineVoiceoverResult> {
  return requestJson<PipelineVoiceoverResult>(
    `/api/pipeline/${pipelineId}/voiceover/generate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider || null }),
    },
  );
}

export async function buildPipelineVoiceoverArtifact(
  pipelineId: number,
): Promise<AudioSubtitleArtifact> {
  return requestJson<AudioSubtitleArtifact>(
    `/api/pipeline/${pipelineId}/voiceover/artifact`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function renderPipelineTimelineMedia(
  pipelineId: number,
  artifactId: string,
  timelineVersionId?: string,
): Promise<TimelineMediaRunResult> {
  return requestJson<TimelineMediaRunResult>(
    `/api/pipeline/${pipelineId}/timeline-media/render`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        artifact_id: artifactId,
        ...(timelineVersionId ? { timeline_version_id: timelineVersionId } : {}),
      }),
    },
  );
}

export async function getMaterialPackage(
  pipelineId: number,
): Promise<MaterialPackageData> {
  return requestJson<MaterialPackageData>(
    `/api/pipeline/${pipelineId}/material-package`,
    { headers: jsonHeaders },
  );
}

export async function getEditingExportPackage(
  pipelineId: number,
): Promise<EditingExportPackage> {
  return requestJson<EditingExportPackage>(
    `/api/pipeline/${pipelineId}/editing-export`,
    { headers: jsonHeaders },
  );
}

export async function rebuildEditingExportPackage(
  pipelineId: number,
): Promise<EditingExportPackage> {
  return requestJson<EditingExportPackage>(
    `/api/pipeline/${pipelineId}/editing-export/rebuild`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function runEndToEndVideoTest(
  topic = "AI 搜索正在改变普通人获取信息的方式",
): Promise<EndToEndVideoResult> {
  return requestJson<EndToEndVideoResult>("/api/editing-export/end-to-end-test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic }),
  });
}

export async function exportPipelineFinishedVideo(
  pipelineId: number,
): Promise<EndToEndVideoResult> {
  return requestJson<EndToEndVideoResult>(
    `/api/pipeline/${pipelineId}/finished-video/export`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function getLatestEndToEndVideoTest(): Promise<EndToEndVideoResult> {
  return requestJson<EndToEndVideoResult>(
    "/api/editing-export/end-to-end-test/latest",
    { headers: jsonHeaders },
  );
}

export async function getFinishedVideos(
  pipelineId?: number | null,
): Promise<FinishedVideoListResponse> {
  const path =
    pipelineId == null
      ? "/api/editing-export/finished-videos"
      : `/api/pipeline/${pipelineId}/finished-videos`;
  return requestJson<FinishedVideoListResponse>(
    path,
    { headers: jsonHeaders },
  );
}

export async function openFinishedVideoFolder(
  videoId: string,
  pipelineId?: number | null,
): Promise<Record<string, unknown>> {
  const query = pipelineId == null ? "" : `?pipeline_id=${pipelineId}`;
  return requestJson<Record<string, unknown>>(
    `/api/editing-export/finished-videos/${encodeURIComponent(videoId)}/open-folder${query}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function exportFinishedVideo(
  videoId: string,
  pipelineId?: number | null,
): Promise<Record<string, unknown>> {
  const query = pipelineId == null ? "" : `?pipeline_id=${pipelineId}`;
  return requestJson<Record<string, unknown>>(
    `/api/editing-export/finished-videos/${encodeURIComponent(videoId)}/export${query}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function deleteFinishedVideo(
  videoId: string,
  pipelineId?: number | null,
): Promise<Record<string, unknown>> {
  const query = pipelineId == null ? "" : `?pipeline_id=${pipelineId}`;
  return requestJson<Record<string, unknown>>(
    `/api/editing-export/finished-videos/${encodeURIComponent(videoId)}${query}`,
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function rebuildExportFile(
  pipelineId: number,
  exportType: string,
): Promise<ExportRebuildResult> {
  return requestJson<ExportRebuildResult>(
    `/api/pipeline/${pipelineId}/exports/rebuild`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ export_type: exportType }),
    },
  );
}

export async function updateMaterialTaskStatus(
  pipelineId: number,
  shotId: string,
  status: string,
): Promise<MaterialTaskStatusResult> {
  return requestJson<MaterialTaskStatusResult>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/status`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
}

export async function runNextMaterialTask(
  pipelineId: number,
): Promise<MaterialTaskRunNextResult> {
  return requestJson<MaterialTaskRunNextResult>(
    `/api/pipeline/${pipelineId}/material-tasks/run-next`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function reconcileAndRetryMaterialTask(
  pipelineId: number,
  shotId: string,
): Promise<MaterialTaskRunNextResult> {
  return requestJson<MaterialTaskRunNextResult>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/reconcile-and-retry`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function skipMaterialTask(
  pipelineId: number,
  shotId: string,
): Promise<MaterialTaskRunNextResult> {
  return requestJson<MaterialTaskRunNextResult>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/skip`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function saveMaterialCandidatePool(
  pipelineId: number,
  shotId: string,
  candidates: MaterialCandidateInput[],
): Promise<MaterialCandidatePool> {
  return requestJson<MaterialCandidatePool>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/candidate-pool`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidates }),
    },
  );
}

export async function lockMaterialCandidate(
  pipelineId: number,
  shotId: string,
  candidateId: string,
  reason: string,
): Promise<MaterialCandidatePool> {
  return requestJson<MaterialCandidatePool>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/candidate-pool/${encodeURIComponent(candidateId)}/lock`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}

export async function reselectMaterialCandidate(
  pipelineId: number,
  shotId: string,
  candidateId: string,
  reason: string,
  expectedRevision: number,
): Promise<MaterialCandidatePool> {
  return requestJson<MaterialCandidatePool>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/candidate-pool/${encodeURIComponent(candidateId)}/reselect`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, expected_revision: expectedRevision }),
    },
  );
}

export async function rejectMaterialCandidate(
  pipelineId: number,
  shotId: string,
  candidateId: string,
  reason: string,
): Promise<MaterialCandidatePool> {
  return requestJson<MaterialCandidatePool>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/candidate-pool/${encodeURIComponent(candidateId)}/reject`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}

export async function reviewMaterialCandidateRights(
  pipelineId: number,
  shotId: string,
  candidateId: string,
  payload: MaterialRightsReviewInput,
): Promise<MaterialCandidatePool> {
  return requestJson<MaterialCandidatePool>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/candidate-pool/${encodeURIComponent(candidateId)}/rights-review`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function replaceMaterialCandidate(
  pipelineId: number,
  shotId: string,
  candidateId: string,
  payload: {
    preview: File;
    replacementCandidateId?: string;
    source: string;
    url?: string;
    rights: string;
    evidenceKind: string;
    reason: string;
  },
): Promise<MaterialCandidatePool> {
  const form = new FormData();
  form.append("preview", payload.preview);
  form.append("replacement_candidate_id", payload.replacementCandidateId || "");
  form.append("source", payload.source);
  form.append("url", payload.url || "");
  form.append("rights", payload.rights);
  form.append("evidence_kind", payload.evidenceKind);
  form.append("reason", payload.reason);
  return requestJson<MaterialCandidatePool>(
    `/api/pipeline/${pipelineId}/material-tasks/${encodeURIComponent(shotId)}/candidate-pool/${encodeURIComponent(candidateId)}/replace`,
    {
      method: "POST",
      body: form,
    },
  );
}

export async function getTopicIdeas(
  filters: TopicIdeaFilters,
): Promise<TopicIdeaListResponse> {
  const params = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (filters.hotspotId) {
    params.set("hotspot_id", filters.hotspotId);
  }
  if (filters.recommendationBucket) {
    params.set("recommendation_bucket", filters.recommendationBucket);
  }
  return requestJson<TopicIdeaListResponse>(
    `/api/topic-ideas?${params.toString()}`,
    { headers: jsonHeaders },
  );
}

export async function getTopicFilterSettings(): Promise<TopicFilterSettings> {
  return requestJson<TopicFilterSettings>("/api/topic-filter-settings", {
    headers: jsonHeaders,
  });
}

export async function saveTopicFilterSettings(
  payload: TopicFilterSettings,
): Promise<TopicFilterSettings> {
  return requestJson<TopicFilterSettings>("/api/topic-filter-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateTopicIdeaRecommendation(
  topicIdeaId: number,
  bucket: "today" | "backup",
): Promise<TopicIdeaItem> {
  return requestJson<TopicIdeaItem>(
    `/api/topic-ideas/${topicIdeaId}/recommendation`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bucket }),
    },
  );
}

export async function getTopicIdeaDetail(
  topicIdeaId: number,
): Promise<TopicIdeaItem> {
  return requestJson<TopicIdeaItem>(`/api/topic-ideas/${topicIdeaId}`, {
    headers: jsonHeaders,
  });
}

export async function selectTopicIdea(
  topicIdeaId: number,
): Promise<TopicIdeaItem> {
  return requestJson<TopicIdeaItem>(`/api/topic-ideas/${topicIdeaId}/select`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}

export async function setTopicEventSelection(
  topicIdeaId: number,
  selected: boolean,
): Promise<TopicIdeaItem> {
  return requestJson<TopicIdeaItem>(
    `/api/topic-ideas/${topicIdeaId}/event-selection`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected }),
    },
  );
}

export async function composeTopicIdeaCollection(
  topicIdeaIds: number[],
): Promise<TopicIdeaItem> {
  return requestJson<TopicIdeaItem>("/api/topic-ideas/compose-collection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic_idea_ids: topicIdeaIds }),
  });
}

export async function generateTopicIdeaScript(
  topicIdeaId: number,
): Promise<TopicIdeaGenerateResult> {
  return requestJson<TopicIdeaGenerateResult>(
    `/api/topic-ideas/${topicIdeaId}/generate-script`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function getVideoPosts(
  filters: VideoPostFilters,
): Promise<VideoPostListResponse> {
  const params = new URLSearchParams({
    sort_by: filters.sortBy,
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (filters.platform) {
    params.set("platform", filters.platform);
  }
  if (filters.pipelineId) {
    params.set("pipeline_id", filters.pipelineId);
  }
  if (filters.status) {
    params.set("status", filters.status);
  }
  return requestJson<VideoPostListResponse>(
    `/api/video-posts?${params.toString()}`,
    { headers: jsonHeaders },
  );
}

export async function getVideoPostDetail(
  postId: number,
): Promise<VideoPostDetail> {
  return requestJson<VideoPostDetail>(`/api/video-posts/${postId}`, {
    headers: jsonHeaders,
  });
}

export async function createVideoPost(
  payload: VideoPostPayload,
): Promise<VideoPostDetail> {
  return requestJson<VideoPostDetail>("/api/video-posts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateVideoPost(
  postId: number,
  payload: Partial<VideoPostPayload>,
): Promise<VideoPostDetail> {
  return requestJson<VideoPostDetail>(`/api/video-posts/${postId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function addVideoSnapshot(
  postId: number,
  payload: VideoSnapshotPayload,
): Promise<VideoPostDetail> {
  return requestJson<VideoPostDetail>(`/api/video-posts/${postId}/snapshots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function getVideoReviews(
  filters: VideoReviewFilters,
): Promise<VideoReviewListResponse> {
  const params = new URLSearchParams({
    sort_by: filters.sortBy,
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (filters.platform) params.set("platform", filters.platform);
  if (filters.pipelineId) params.set("pipeline_id", filters.pipelineId);
  if (filters.publishedFrom) params.set("published_from", filters.publishedFrom);
  if (filters.publishedTo) params.set("published_to", filters.publishedTo);
  if (filters.judgement) params.set("judgement", filters.judgement);
  return requestJson<VideoReviewListResponse>(
    `/api/reviews?${params.toString()}`,
    { headers: jsonHeaders },
  );
}

export async function getVideoReviewDetail(
  videoPostId: number,
): Promise<VideoReviewDetail> {
  return requestJson<VideoReviewDetail>(`/api/reviews/${videoPostId}`, {
    headers: jsonHeaders,
  });
}

export async function getWeeklyReviewReport(
  pipelineId: number | null = null,
): Promise<WeeklyReviewReport> {
  const params = new URLSearchParams();
  if (pipelineId !== null) params.set("pipeline_id", String(pipelineId));
  const query = params.toString();
  return requestJson<WeeklyReviewReport>(`/api/reviews/weekly/report${query ? `?${query}` : ""}`, {
    headers: jsonHeaders,
  });
}

export async function getTodayTasks(): Promise<TodayTasksResponse> {
  return requestJson<TodayTasksResponse>("/api/today/tasks", {
    headers: jsonHeaders,
  });
}

export async function completeTodayTask(
  taskId: string,
): Promise<{ task_id: string; handled_task_ids: string[] }> {
  return requestJson<{ task_id: string; handled_task_ids: string[] }>(
    `/api/today/tasks/${encodeURIComponent(taskId)}/complete`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function seedDemoData(): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>("/api/demo-data/seed", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}

export async function clearDemoData(): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>("/api/demo-data/clear", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });
}

export async function getAccountStyle(): Promise<AccountStyleLibrary> {
  return requestJson<AccountStyleLibrary>("/api/account-style", {
    headers: jsonHeaders,
  });
}

export async function saveAccountStyle(
  payload: AccountStyleLibrary,
): Promise<AccountStyleLibrary> {
  return requestJson<AccountStyleLibrary>("/api/account-style", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function resetAccountStyle(): Promise<AccountStyleLibrary> {
  return requestJson<AccountStyleLibrary>("/api/account-style/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}

export async function checkManualContentQuality(
  payload: ManualContentQualityPayload,
): Promise<ContentQualityResult> {
  return requestJson<ContentQualityResult>("/api/content-quality/manual/check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text: payload.text,
      source_title: payload.sourceTitle,
      source_type: payload.sourceType || "手动粘贴",
      pipeline_id: payload.pipelineId ?? null,
      script_asset_id: payload.scriptAssetId ?? null,
    }),
  });
}

export async function checkPipelineContentQuality(
  pipelineId: number,
): Promise<ContentQualityResult> {
  return requestJson<ContentQualityResult>(
    `/api/content-quality/pipeline/${pipelineId}/check`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export async function getCopywritingSamples(
  filters: CopywritingSampleFilters,
): Promise<CopywritingSampleListResponse> {
  const params = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (filters.sampleType) params.set("sample_type", filters.sampleType);
  if (filters.scene) params.set("scene", filters.scene);
  if (filters.contentDirection) params.set("content_direction", filters.contentDirection);
  if (filters.tag) params.set("tag", filters.tag);
  if (filters.keyword) params.set("keyword", filters.keyword);
  if (filters.enabledOnly) params.set("enabled_only", "true");
  if (filters.keyReferenceOnly) params.set("key_reference_only", "true");
  return requestJson<CopywritingSampleListResponse>(
    `/api/script-engine?${params.toString()}`,
    { headers: jsonHeaders },
  );
}

export async function getCopywritingSampleDetail(
  sampleId: number,
): Promise<CopywritingSampleItem> {
  return requestJson<CopywritingSampleItem>(`/api/script-engine/${sampleId}`, {
    headers: jsonHeaders,
  });
}

export async function createCopywritingSample(
  payload: CopywritingSamplePayload,
): Promise<CopywritingSampleItem> {
  return requestJson<CopywritingSampleItem>("/api/script-engine", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateCopywritingSample(
  sampleId: number,
  payload: CopywritingSamplePayload,
): Promise<CopywritingSampleItem> {
  return requestJson<CopywritingSampleItem>(`/api/script-engine/${sampleId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteCopywritingSample(
  sampleId: number,
): Promise<{ deleted: boolean; id: number }> {
  return requestJson<{ deleted: boolean; id: number }>(
    `/api/script-engine/${sampleId}`,
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    },
  );
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function requestJson<T>(url: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new Error(
      "无法连接后端服务。请先双击“启动全部.cmd”，再刷新页面。",
    );
  }
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

async function readErrorMessage(response: Response): Promise<string> {
  if (response.status === 502) {
    return "后端服务连接失败（502）。请先双击“启动全部.cmd”，或确认后端 127.0.0.1:8000 已启动。";
  }
  try {
    const data = (await response.json()) as { detail?: string };
    return data.detail || `请求失败（${response.status}）`;
  } catch {
    try {
      const text = await response.text();
      if (text.trim()) {
        return `请求失败（${response.status}）：${text.trim().slice(0, 120)}`;
      }
    } catch {
      // Ignore body parsing errors and fall back to a short status message.
    }
    return `请求失败（${response.status}）。请检查后端窗口里的错误信息。`;
  }
}
