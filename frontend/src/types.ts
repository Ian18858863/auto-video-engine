export interface PipelineListItem {
  pipeline_id: number;
  hotspot_id: number;
  topic_idea_id: number | null;
  script_asset_id: number | null;
  project_name: string;
  project_key: string;
  project_dir: string;
  legacy_project_dir: string | null;
  status: string;
  completion_reason_code: string;
  next_surface: "project" | "finished" | "none";
  current_step: string;
  completed_steps: number;
  total_steps: number;
  has_failed: boolean;
  failure_reason: string;
  needs_confirmation: boolean;
  created_at: string;
  updated_at: string;
  next_suggestion: string;
}

export interface PipelineListResponse {
  items: PipelineListItem[];
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
}

export interface PipelineStep {
  step_name: string;
  status: string;
  error_message: string;
}

export interface PipelineDetail {
  pipeline_id: number;
  hotspot_id: number;
  topic_idea_id: number | null;
  script_asset_id: number | null;
  project_name: string;
  project_key: string;
  project_dir: string;
  legacy_project_dir: string | null;
  status: string;
  completion_reason_code: string;
  next_surface: "project" | "finished" | "none";
  current_step: string;
  steps: PipelineStep[];
  generated_files: Record<string, string>;
  failure_reasons: Array<{ step_name: string; reason: string }>;
  needs_confirmation: boolean;
  next_suggestion: string;
}

export interface PipelineCompletionStatus {
  pipeline_id: number;
  status: string;
  is_complete: boolean;
  completion_reason_code: string;
  message: string;
  next_action: string;
  next_surface: "project" | "finished" | "none";
  current_step: string;
  needs_confirmation: boolean;
  evidence_present: boolean;
  quality_gate_state: "not_evaluated" | "blocked" | "passed";
  artifact_relative_path?: string | null;
  quality_report_relative_path?: string | null;
}

export interface ReleasePackageResult {
  pipeline_id: number;
  release_format: string;
  source_artifact_relative_path: string;
  artifact_relative_path: string;
  artifact_sha256: string;
  source_quality_report_relative_path: string;
  quality_report_relative_path: string;
  quality_report_sha256: string;
  release_manifest_relative_path: string;
  release_manifest_sha256: string;
  released_at: string;
  release_supported: boolean;
  external_publish_supported: boolean;
}

export interface QualityArtifactCandidate {
  artifact_relative_path: string;
  artifact_sha256: string;
  size_bytes: number;
  modified_at: string;
  timeline_version_id: string;
  timeline_input_hash: string;
  timeline_status: "current" | "historical";
  can_quality_check: boolean;
}

export interface QualityReportResult {
  pipeline_id: number;
  artifact_relative_path: string;
  artifact_sha256: string;
  quality_report_relative_path: string;
  quality_report_sha256: string;
  report: {
    release_blockers: string[];
    gates: Record<string, { status: "passed" | "failed"; evidence: string[] }>;
    [key: string]: unknown;
  };
  approval_required: boolean;
  publish_supported: boolean;
}

export interface QualityContractReview {
  contract_sha256: string;
  must_preserve_results: Record<
    string,
    { result: "passed" | "failed"; evidence: string }
  >;
  allowed_difference_results: Record<
    string,
    {
      result: "used" | "not_used";
      actual_difference: string;
      evidence: string;
    }
  >;
  planned_exceed_results: Record<
    string,
    {
      result: "exceeded" | "matched" | "not_met" | "not_comparable";
      difference: string;
      evidence: string;
    }
  >;
}

export interface QualityContractSnapshot {
  pipeline_id: number;
  quality_contract_relative_path: string;
  quality_contract_sha256: string;
  contract: {
    format: string;
    contract_id: string;
    pipeline_id: number;
    project_key: string;
    baseline: string;
    target: {
      platform: string;
      aspect_ratio: string;
      duration_seconds: number;
    };
    reference_pack: {
      status: "none_supplied" | "frozen";
      items: Array<{
        id: string;
        project_relative_path: string;
        sha256: string;
      }>;
    };
    must_preserve: Array<{
      id: string;
      label: string;
      criterion: string;
    }>;
    allowed_differences: Array<{
      id: string;
      label: string;
    }>;
    planned_exceed_dimensions: Array<{
      id: string;
      label: string;
      criterion: string;
      required_result: "exceeded";
    }>;
    created_at: string;
    source: string;
  };
}

export interface QualityApprovalResult {
  pipeline_id: number;
  evidence: Record<string, unknown>;
  decision: {
    is_complete: boolean;
    status: string;
    reason_code: string;
    message: string;
    next_action: string;
  };
  publish_supported: boolean;
}

export interface HotspotItem {
  id: number;
  original_title: string;
  original_url: string;
  source_platform: string;
  source_type: string;
  title: string;
  url: string;
  published_at: string | null;
  fetched_at: string;
  summary: string;
  metrics: Record<string, number | string>;
  tags: string[];
  category: string;
  data_origin: "real" | "manual" | "sample" | string;
  origin_note: string;
  is_filtered: boolean;
  filter_reason: string;
  hot_score: number;
  topic_score: number;
  save_score: number;
  risk_score: number;
  freshness_score: number;
  duplicate_reason: string;
  source_credibility: "高" | "中" | "低" | string;
  source_credibility_reason: string;
  has_project: boolean;
}

export interface HotspotListResponse {
  items: HotspotItem[];
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
  filters: {
    categories: string[];
    sources: string[];
    category_counts?: Record<string, number>;
    source_counts?: Record<string, number>;
  };
}

export interface ManualHotspotPayload {
  title: string;
  source_platform: string;
  url: string;
  summary: string;
  category: string;
  tags: string[];
  published_at: string | null;
  notes: string;
}

export interface ManualHotspotImportResult {
  created: boolean;
  message: string;
  item: HotspotItem;
}

export interface ManualV1RunPayload {
  title: string;
  summary: string;
  source_platform: string;
  url: string;
  category: string;
  tags: string[];
  notes: string;
}

export interface ManualV1RunResult {
  message: string;
  pipeline_id: number;
  hotspot_id: number;
  topic_idea_id: number | null;
  script_asset_id: number | null;
  project_name: string;
  project_key: string;
  project_dir: string;
  legacy_project_dir: string | null;
  provider: string;
  model: string;
  quality_check: Partial<ContentQualityResult> & { id: number };
  repair_attempts: number;
  exports: Record<string, string>;
  links: Record<string, number | null>;
  real_ai_called: boolean;
  provider_notice: string;
}

export interface HotspotFetchResult {
  source_count: number;
  fetched_count: number;
  saved_count: number;
  duplicate_count: number;
  filtered_count: number;
  archived_expired_count?: number;
  preserved_expired_count?: number;
  failed_sources: string[];
  rate_limited_sources: string[];
  message: string;
}

export interface SourceRunStatus {
  source_name: string;
  status: "成功" | "失败" | "跳过" | "限流" | string;
  last_run_at: string | null;
  items_fetched: number;
  success_count: number;
  failure_reason: string;
  is_rate_limited: boolean;
}

export interface SourceStatusResponse {
  items: SourceRunStatus[];
  source_count: number;
  archived_expired_count?: number;
  daily_refresh_date?: string | null;
  daily_refresh_completed_at?: string | null;
  manual_import_is_fallback: boolean;
  real_ai_called: boolean;
}

export interface AutoHotspotRunResult extends HotspotFetchResult {
  skipped_count: number;
  source_statuses: SourceRunStatus[];
  generated_hotspot_count: number;
  generated_topic_idea_count: number;
  topic_generation: Array<{
    hotspot_id: number;
    title: string;
    generated_count: number;
    video_type: string;
    recommendation_bucket: string;
    error: string;
  }>;
  provider: string;
  real_ai_called: boolean;
  manual_import_is_fallback: boolean;
  ran_at: string | null;
  next_due_at: string | null;
}

export interface BrowserHotspotRunResult {
  source_count: number;
  fetched_count: number;
  saved_count: number;
  duplicate_count: number;
  csv_written_count: number;
  failed_sources: string[];
  needs_login_sources: string[];
  skipped_sources: string[];
  csv_path: string;
  message: string;
  safety_note: string;
}

export interface BrowserHotspotLoginResult {
  message: string;
  pid: number;
  sources: Array<{ name: string; url: string }>;
  profile_dir: string;
  real_collection_started: boolean;
  note: string;
}

export interface BuildProjectResult {
  pipeline_id: number;
  hotspot_id: number;
  project_name: string;
  project_key: string;
  status: string;
  provider: string;
}

export interface TopicIdeaItem {
  id: number;
  hot_item_id: number;
  hotspot_title: string;
  title: string;
  core_view: string;
  recommended_format: string;
  why_watch: string;
  why_save: string;
  comment_trigger: string;
  risk_note: string;
  recommended_duration: string;
  hook: string;
  video_type:
    | "collection_event"
    | "single_deep_dive_candidate"
    | "single_deep_dive"
    | "multi_hotspot_collection"
    | string;
  generation_source: string;
  recommendation_score: number;
  recommendation_bucket: "today" | "backup" | string;
  source_hotspot_ids: number[];
  source_hotspot_titles: string[];
  source_hotspot_urls: string[];
  source_published_at?: string[];
  source_fetched_at?: string[];
  source_summaries?: string[];
  source_platforms?: string[];
  source_credibility: "高" | "中" | "低" | string;
  source_credibility_reason: string;
  score_breakdown?: Record<string, number>;
  score_bonus_reasons?: string[];
  fits_collection?: boolean;
  can_single_deep_dive?: boolean;
  is_example: boolean;
  is_selected: boolean;
  has_script: boolean;
  script_asset_id: number | null;
  pipeline_id: number | null;
}

export interface TopicFilterWeights {
  impact: number;
  account_fit: number;
  freshness_heat: number;
  cross_source_resonance: number;
  ordinary_relevance: number;
}

export interface TopicCategoryBonusRule {
  id: string;
  label: string;
  category: string;
  score: number;
  enabled: boolean;
}

export interface TopicKeywordBonusRule {
  id: string;
  label: string;
  keyword: string;
  score: number;
  enabled: boolean;
}

export interface TopicFilterSettings {
  weights: TopicFilterWeights;
  current_period_limit: number;
  backup_pool_limit: number;
  source_limit: number;
  minimum_score: number;
  freshness_window_hours: number;
  bonus_rules: {
    categories: TopicCategoryBonusRule[];
    keywords: TopicKeywordBonusRule[];
  };
  last_saved_at: string;
  is_active: boolean;
  using_default: boolean;
}

export interface TopicIdeaListResponse {
  items: TopicIdeaItem[];
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
  filters: {
    hotspots: Array<{ id: number; title: string }>;
  };
}

export interface TopicIdeaGenerateResult {
  pipeline_id: number;
  topic_idea_id: number;
  script_asset_id: number;
  project_name: string;
  project_key: string;
  status: string;
  provider: string;
}

export interface ScriptVersionSummary {
  version_id: string;
  version_number: number;
  input_hash: string;
  content_hash: string;
  schema_version?: number;
  shot_plan_hash?: string;
  snapshot_hash?: string;
  integrity_status?: string;
  approval_integrity_status?: string;
  approval_event_hash?: string;
  approval_evidence_hash?: string;
  status: string;
  source: string;
  created_at: string;
  approved_at?: string;
  invalidated_at?: string;
  invalidated_reason?: string;
  shot_count: number;
}

export interface ScriptVersionDetail {
  version_id: string;
  version_number: number;
  input_hash: string;
  content_hash: string;
  schema_version?: number;
  shot_plan_hash?: string;
  snapshot_hash?: string;
  integrity_status?: string;
  approval_integrity_status?: string;
  approval_event_hash?: string;
  approval_evidence_hash?: string;
  input_evidence?: Record<string, unknown>;
  status: string;
  source: string;
  created_at: string;
  approved_at: string;
  invalidated_at: string;
  invalidated_reason: string;
  payload: Record<string, unknown>;
  fact_sources: Array<Record<string, unknown>>;
  shot_plan: Array<Record<string, unknown>>;
}

export interface ScriptVersionRestoreRequest {
  operation_id: string;
  source_content_hash: string;
  source_snapshot_hash: string;
  expected_current_version_id: string;
  expected_current_content_hash: string;
  expected_current_snapshot_hash: string;
  expected_script_state_sha256: string;
}

export interface ScriptVersionRestoreResponse {
  operation: {
    operation_id: string;
    source_version_id: string;
    restored_version_id: string;
    restored_version_number: number;
    status: "draft";
    idempotent: boolean;
  };
  workspace: ScriptWorkspaceData;
}

export interface ScriptWorkspaceData {
  pipeline_id: number;
  status: string;
  completion_reason_code: string;
  next_surface: "project" | "finished" | "none";
  current_step: string;
  needs_confirmation: boolean;
  next_suggestion: string;
  provider: string;
  project_name: string;
  project_key: string;
  project_dir: string;
  legacy_project_dir: string | null;
  hotspot: {
    id: number | null;
    title: string;
    summary: string;
  };
  topic: {
    id: number | null;
    title: string;
    core_view: string;
    recommended_format: string;
    recommended_duration: string;
    risk_note: string;
    hook: string;
  };
  script: {
    id: number | null;
    title: string;
    voiceover: string;
    storyboard: Array<Record<string, unknown>>;
    subtitles: Array<Record<string, unknown>>;
    title_options: Array<Record<string, unknown>>;
    cover_copy: Array<Record<string, unknown>>;
    publish_copy: Record<string, unknown>;
    use_avatar: boolean;
  };
  material_tasks: Array<{
    material_task_id?: string;
    shot_id: string;
    time_range: string;
    voiceover: string;
    recommended_visual: string;
    material_type: string;
    jianying_keywords: string[];
    asset_keywords?: string[];
    source_priority?: string[];
    needs_official_capture?: boolean;
    needs_pexels?: boolean;
    needs_info_motion?: boolean;
    needs_backup_asset?: boolean;
    needs_codex_graphic: boolean;
    needs_avatar: boolean;
    status: string;
    material_path: string;
    source_type?: string;
    reject_reason?: string;
    notes?: string;
    status_history?: string[];
    reason_code?: string;
    execution_id?: string;
    execution_state?: string;
    active_attempt_id?: string;
    publication_phase?: string;
    retry_available?: boolean;
    recovery_action_available?: boolean;
    beat_id?: string;
    beat_type?: string;
    viewer_takeaway?: string;
    visual_intent?: string;
    evidence_kind?: string;
    source_refs?: Array<{ url: string; kind: string }>;
    acceptance_conditions?: string[];
    forbidden_items?: string[];
    director_version?: string;
    subject?: string;
    action?: string;
    location?: string;
    time_context?: string;
    composition?: string;
    visual_type?: string;
    sound_notes?: string;
    candidate_pool?: MaterialCandidatePool;
  }>;
  graphic_tasks: Array<{
    shot_id: string;
    time_range: string;
    visual_form: string;
    core_message: string;
    keywords: string[];
    status: string;
    save_path: string;
    codex_skill_executed: boolean;
  }>;
  avatar_tasks: Array<{
    shot_id: string;
    required: boolean;
    position: string;
    state: string;
    avoid_subtitle: boolean;
    avoid_chart: boolean;
    lip_sync: boolean;
    material_path: string;
    generated: boolean;
    enabled?: boolean;
    disabled_reason?: string;
  }>;
  exports: Record<string, { generated: boolean; path: string; exists?: boolean }>;
  script_factory: ScriptFactoryState;
  script_versions?: ScriptVersionSummary[];
  shot_plan?: Array<{
    shot_id: string;
    beat_id: string;
    beat_type: string;
    viewer_takeaway: string;
    evidence_kind: string;
    visual_type: string;
    visual_intent: string;
    start: string;
    end: string;
    duration_seconds: number;
    subtitle_load: number;
    voiceover: string;
    source_refs: Array<{ url: string; kind: string }>;
    source_priority: string[];
    forbidden_items: string[];
    acceptance_conditions: string[];
    subject: string;
    action: string;
    location: string;
    time_context: string;
    composition: string;
    sound_notes: string;
  }>;
  active_script_version_id?: string;
  script_state_sha256?: string;
  material_selection_history?: Array<{
    event: string;
    reason_code: string;
    reason: string;
    stale_at: string;
    source_script_version_id: string;
    target_script_version_id: string;
    locked_selections: Array<{
      shot_id: string;
      candidate_id: string;
      reason: string;
      source: string;
      media_sha256: string;
    }>;
    entry_sha256: string;
  }>;
}

export interface ScriptFactoryState {
  status: string;
  job_id: string;
  job_dir: string;
  file_path: string;
  prompt_path: string;
  context_path: string;
  template_path: string;
  result_path: string;
  result_exists: boolean;
  real_ai_called: boolean;
  failure_reason: string;
  next_suggestion: string;
  accepted: boolean;
  dispatch_supported: boolean;
  thread_id: string;
  manual_prompt: string;
  script_version_id?: string;
  script_version_hash?: string;
  script_version_input_hash?: string;
  script_version_snapshot_hash?: string;
  script_version_shot_plan_hash?: string;
  script_result_hash?: string;
  approval_event_hash?: string;
  approval_evidence_hash?: string;
  approval_quality_hash?: string;
  import_operation_id?: string;
  draft_operation_id?: string;
  restore_operation_id?: string;
  approval_operation_id?: string;
  restored_from_version_id?: string;
  restored_from_content_hash?: string;
  restored_from_snapshot_hash?: string;
  ready_for_review: boolean;
}

export interface ScriptWorkspaceUpdatePayload {
  voiceover: string;
  storyboard: Array<Record<string, unknown>>;
  subtitles: Array<Record<string, unknown>>;
  title_options: Array<Record<string, unknown>>;
  cover_copy: Array<Record<string, unknown>>;
  publish_copy: Record<string, unknown>;
  use_avatar?: boolean;
}

export interface ScriptWorkOrderImportPayload {
  voiceover: string;
  storyboard: Array<Record<string, unknown>>;
  subtitles: Array<Record<string, unknown>>;
  animation_prompts?: Array<Record<string, unknown>>;
  title_options: Array<Record<string, unknown>>;
  cover_copy: Array<Record<string, unknown>>;
  publish_copy: Record<string, unknown>;
  raw_result?: string;
}

export interface ScriptWorkOrderResult {
  pipeline_id: number;
  script_asset_id: number | null;
  status: string;
  job_id: string;
  job_dir: string;
  file_path: string;
  prompt_path: string;
  context_path: string;
  template_path: string;
  result_path: string;
  result_exists?: boolean;
  real_ai_called: boolean;
  failure_reason: string;
  next_suggestion: string;
  quality_check: ContentQualityResult | null;
  accepted: boolean;
  dispatch_supported: boolean;
  thread_id: string;
  manual_prompt: string;
}

export interface EditingExportPackage {
  pipeline_id: number;
  status: string;
  project_name: string;
  project_key: string;
  project_dir: string;
  legacy_project_dir: string | null;
  timeline: Array<{
    segment_id: string;
    start: string;
    end: string;
    voiceover: string;
    visual_type: string;
    visual_direction: string;
    material_keywords: string[];
    material_sources: string[];
    material_status: string;
    material_path: string;
    needs_graphic: boolean;
    graphic_task: Record<string, unknown>;
    avatar_task: Record<string, unknown>;
    subtitle_strategy: string;
    edit_instruction: string;
  }>;
  versions: Array<{
    version_id: string;
    name: string;
    status: string;
    estimated_duration_seconds: number;
    strategy: string;
    best_for: string;
    template?: string;
    requires_avatar_reconfirmation?: boolean;
    avatar_note?: string;
  }>;
  recommended_template?: {
    version_id: string;
    name: string;
    status: string;
    estimated_duration_seconds: number;
    strategy: string;
    best_for: string;
    template?: string;
    requires_avatar_reconfirmation?: boolean;
    avatar_note?: string;
  };
  export_task: {
    pipeline_id: number;
    status: string;
    need_final_confirmation: boolean;
    version_count: number;
    timeline_version_id?: string;
    timeline_version_number?: number;
    material_ready_count: number;
    timeline_segment_count: number;
    real_jianying_automation: boolean;
    real_avatar_generation: boolean;
    real_publish_executed: boolean;
  };
  generated_files: Record<string, string>;
  timeline_sync: {
    status: "current" | "stale" | "unsaved";
    current_version_id: string;
    previous_version_id: string;
    desired_input_hash: string;
    affected_segment_ids: string[];
    created_new_version: boolean;
  };
  timeline_spec?: {
    status?: string;
    version_id?: string;
    version_number?: number;
    input_hash?: string;
    parent_version_id?: string | null;
    change_set?: {
      affected_segment_ids?: string[];
      change_kinds?: string[];
    };
    tracks?: Array<Record<string, unknown>>;
    segments?: Array<Record<string, unknown>>;
    settings?: Record<string, unknown>;
  };
  subtitle_preview: string;
  note: string;
}

export interface EndToEndVideoMatch {
  "镜头": string;
  "时间段": string;
  "口播": string;
  "字幕": string;
  "画面说明": string;
  "素材关键词": string[];
  "素材状态": string;
  "素材类型": string;
  "素材路径": string;
  "匹配原因": string;
}

export interface EndToEndVideoResult {
  "成功": boolean;
  "提示"?: string;
  "选题"?: string;
  "简略脚本"?: string[];
  "画面数量"?: number;
  "素材匹配": EndToEndVideoMatch[];
  "导出文件": Record<string, string>;
  "剪辑参数"?: Record<string, string>;
  "提醒"?: string[];
}

export interface FinishedVideoItem {
  "成片ID": string;
  pipeline_id?: number | null;
  "标题": string;
  "生成时间": string;
  "画面数量": number;
  "状态": string;
  "成片目录": string;
  "最终成片路径": string;
  "预览地址": string;
  "提醒": string[];
}

export interface FinishedVideoListResponse {
  pipeline_id?: number;
  items: FinishedVideoItem[];
  note: string;
}

export interface RuntimePathStatus {
  key: string;
  path: string;
  drive: string;
  is_system_drive: boolean;
  exists: boolean;
  parent_exists: boolean;
  status: "ready" | "missing" | "blocked" | string;
  reason?: string;
}

export interface RuntimeCapabilityTruth {
  id: string;
  name: string;
  status: string;
  installed: boolean | null;
  configured: boolean | null;
  verified: boolean;
  production_allowed: boolean;
  secret_configured?: boolean;
  declared_version?: string;
  last_verified_at: string | null;
  purpose: string;
  applicable_route?: string;
  affected_modules: string[];
  fallback: string;
  cost_class?: string;
  reason: string;
}

export interface RuntimeSettingsStatus {
  provider: {
    name: string;
    status: string;
    production_allowed: boolean;
    verified: boolean;
    reason: string;
    secret_configured: boolean;
  };
  providers: RuntimeCapabilityTruth[];
  skills: RuntimeCapabilityTruth[];
  paths: RuntimePathStatus[];
  capabilities: Record<
    string,
    { status: string; production_allowed: boolean; reason: string }
  >;
  storage_policy: {
    system_drive: string;
    payload_drive_required: string;
    runtime_paths_are_resolved: boolean;
  };
}

export interface ScriptRewriteResult {
  pipeline_id: number;
  script_asset_id: number;
  part: string;
  part_label: string;
  used_account_style: boolean;
  used_copywriting_samples: boolean;
  real_ai_called: boolean;
  message: string;
}

export interface PipelineVoiceoverResult {
  pipeline_id: number;
  script_asset_id: number;
  success: boolean;
  provider: string;
  audio_path: string;
  subtitle_path: string;
  subtitle_preview: string;
  message: string;
  real_tts_api_called: boolean;
  huoshan_tts_ready: boolean;
}

export interface AudioSubtitleArtifact {
  artifact_id: string;
  audio: {
    project_relative_path: string;
    sha256: string;
    duration_seconds: number;
    mime: string;
  };
  subtitles: {
    project_relative_path: string;
    sha256: string;
    mime: string;
  };
  manifest_project_relative_path: string;
  provider: string;
  created_at: string;
}

export interface TimelineMediaRunResult {
  binding: {
    pipeline_id: number;
    artifact_id: string;
    timeline_spec: Record<string, unknown>;
    artifact: AudioSubtitleArtifact;
    publish_supported: boolean;
  };
  render: {
    version_id: string;
    timeline_version_id: string;
    timeline_input_hash: string;
    mode: string;
    output_project_relative_path: string;
    report_project_relative_path: string;
    visual_render_manifest_project_relative_path: string;
    artifact_sha256: string;
    visual_rebuild: {
      source_timeline_version_id: string | null;
      reused_segment_ids: string[];
      rebuilt_segment_ids: string[];
    };
    video_report: Record<string, unknown>;
    media_report: Record<string, unknown>;
  };
  publish_supported: boolean;
}

export type MaterialPackageData = ScriptWorkspaceData & {
  package_note: string;
};

export type MaterialCandidateStatus = "candidate" | "locked" | "rejected";

export interface MaterialMediaValidation {
  status: "verified";
  media_kind: "image" | "video";
  mime_type: "image/jpeg" | "image/png" | "image/webp" | "video/mp4" | "video/quicktime" | "video/webm";
  sha256: string;
  size_bytes: number;
  width: number;
  height: number;
  duration_seconds: number | null;
  codec: string;
  container: string;
  decode_verified: true;
}

export type MaterialRightsReviewStatus =
  | "unreviewed"
  | "needs_review"
  | "cleared_for_intended_use"
  | "rejected";

export type MaterialRightsReviewFlag = "unknown" | "yes" | "no";
export type MaterialThirdPartyClearance =
  | "unknown"
  | "required"
  | "not_required"
  | "obtained";

export interface MaterialRightsReviewInput {
  expected_revision: number;
  status: "needs_review" | "cleared_for_intended_use" | "rejected";
  intended_use: string;
  identifiable_people: MaterialRightsReviewFlag;
  brands_or_logos: MaterialRightsReviewFlag;
  property_or_architecture: MaterialRightsReviewFlag;
  music_or_third_party_audio: MaterialRightsReviewFlag;
  third_party_clearance: MaterialThirdPartyClearance;
  restriction_notes: string[];
}

export interface MaterialRightsEvidence {
  schema_version: 1;
  provider: "Pexels" | "Pixabay";
  provider_asset_id: string;
  media_type: "video" | "photo" | "illustration" | "vector";
  asset_page_url: string;
  contributor: {
    provider_user_id: string;
    name: string;
    profile_url: string;
  };
  license_snapshot: {
    license_name: string;
    license_url: string;
    terms_url: string;
    observed_at: string;
    terms_version_or_last_updated: string;
  };
  attribution: {
    license_required: false;
    api_display_requirement: string;
    recommended_credit: string;
  };
  rights_review: {
    status: MaterialRightsReviewStatus;
    intended_use: string;
    identifiable_people: MaterialRightsReviewFlag;
    brands_or_logos: MaterialRightsReviewFlag;
    property_or_architecture: MaterialRightsReviewFlag;
    music_or_third_party_audio: MaterialRightsReviewFlag;
    third_party_clearance: MaterialThirdPartyClearance;
    restriction_notes: string[];
    reviewed_by: string;
    reviewed_at: string;
  };
  source_record: {
    search_query: string;
    record_checked_at: string;
  };
}

export interface MaterialCandidate {
  candidate_id: string;
  source: string;
  preview_path?: string;
  preview_url?: string;
  preview_media_type?: string;
  url: string;
  rights: string;
  evidence_kind: string;
  status: MaterialCandidateStatus;
  reason: string;
  media_validation?: MaterialMediaValidation;
  rights_evidence?: MaterialRightsEvidence;
}

export interface MaterialCandidateInput
  extends Omit<MaterialCandidate, "status" | "reason"> {
  preview_path: string;
  status?: MaterialCandidateStatus;
  reason?: string;
}

export interface MaterialCandidatePool {
  shot_id: string;
  revision: number;
  locked_candidate_id: string | null;
  candidates: MaterialCandidate[];
  stale?: boolean;
  stale_reason_code?: "upstream_script_changed";
  stale_reason?: string;
  stale_at?: string;
  upstream_script_version_id?: string;
  upstream_script_version_hash?: string;
  target_script_version_id?: string;
  target_script_version_hash?: string;
  target_material_input_sha256?: string;
  selection_event?: {
    event: "candidate_locked" | "candidate_reselected";
    shot_id: string;
    previous_candidate_id: string | null;
    selected_candidate_id: string;
    revision_from: number;
    revision_to: number;
    reason: string;
    actor?: string;
    created_at: string;
  };
  rights_review_event?: {
    event: "candidate_rights_reviewed";
    shot_id: string;
    candidate_id: string;
    status_from: MaterialRightsReviewStatus;
    status_to: MaterialRightsReviewStatus;
    revision_from: number;
    revision_to: number;
    actor: "Ian";
    created_at: string;
  };
}

export interface ExportRebuildResult {
  export_type: string;
  path: string;
  message: string;
}

export interface MaterialTaskStatusResult {
  pipeline_id: number;
  shot_id: string;
  status: string;
}

export interface MaterialTaskRunNextResult {
  pipeline_id: number;
  message: string;
  executed_task: Record<string, unknown> | null;
  tasks: Array<Record<string, unknown>>;
}

export interface VideoSnapshotItem {
  id: number;
  captured_at: string;
  elapsed_minutes: number;
  views: number;
  likes: number;
  comments: number;
  favorites: number;
  shares: number;
  followers_gained: number;
  completion_rate: number;
  average_watch_seconds: number;
  notes: string;
}

export interface VideoPostItem {
  id: number;
  pipeline_id: number | null;
  project_name: string;
  platform: string;
  video_title: string;
  published_at: string | null;
  video_url: string;
  topic_type: string;
  video_format: string;
  hook_type: string;
  duration_seconds: number;
  views: number;
  likes: number;
  comments: number;
  favorites: number;
  shares: number;
  followers_gained: number;
  completion_rate: number;
  average_watch_seconds: number;
  latest_updated_at: string;
  current_stage: string;
  current_status: string;
  notes: string;
  snapshot_count: number;
  favorite_rate: number;
  share_rate: number;
  view_velocity: number;
}

export interface VideoPostDetail extends VideoPostItem {
  snapshots: VideoSnapshotItem[];
}

export interface VideoPostListResponse {
  items: VideoPostItem[];
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
  filters: {
    platforms: string[];
    projects: Array<{ id: number; title: string }>;
    statuses: string[];
  };
}

export interface VideoPostPayload {
  pipeline_id: number | null;
  platform: string;
  video_title: string;
  published_at: string;
  video_url: string;
  topic_type: string;
  video_format: string;
  hook_type: string;
  duration_seconds: number;
  views: number;
  likes: number;
  comments: number;
  favorites: number;
  shares: number;
  followers_gained: number;
  completion_rate: number;
  average_watch_seconds: number;
  notes: string;
}

export interface VideoSnapshotPayload {
  views: number;
  likes: number;
  comments: number;
  favorites: number;
  shares: number;
  followers_gained: number;
  completion_rate: number;
  average_watch_seconds: number;
  notes: string;
}

export interface VideoReviewItem {
  video_post_id: number;
  pipeline_id: number | null;
  project_name: string;
  video_title: string;
  platform: string;
  published_at: string | null;
  views: number;
  likes: number;
  comments: number;
  favorites: number;
  shares: number;
  followers_gained: number;
  completion_rate: number;
  average_watch_seconds: number;
  favorite_rate: number;
  share_rate: number;
  follower_rate: number;
  view_velocity: number;
  growth_trend: string;
  judgement: string;
  reasons: string[];
  next_suggestion: string;
  snapshot_count: number;
}

export interface VideoReviewDetail extends VideoReviewItem {
  snapshots: VideoSnapshotItem[];
}

export interface VideoReviewListResponse {
  items: VideoReviewItem[];
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
  filters: {
    platforms: string[];
    projects: Array<{ id: number; title: string }>;
    judgements: string[];
  };
}

export interface WeeklyReviewReport {
  period: { start: string; end: string };
  published_count: number;
  data_note: string;
  published_videos: VideoReviewItem[];
  high_favorite_videos: VideoReviewItem[];
  high_share_videos: VideoReviewItem[];
  long_tail_videos: VideoReviewItem[];
  stalled_videos: VideoReviewItem[];
  continue_topics: VideoReviewItem[];
  pause_topics: VideoReviewItem[];
  next_week_do: string[];
  next_week_avoid: string[];
  local_rules_only: boolean;
  real_ai_called: boolean;
}

export interface TodayTaskItem {
  task_id: string;
  title: string;
  subtitle: string;
  reason: string;
  next_action: string;
  target_type:
    | "hotspot"
    | "topic"
    | "topic_pool"
    | "project"
    | "workspace"
    | "materials"
    | "editing"
    | "finished"
    | "tracking"
    | "review";
  target_id: number | null;
  priority: number;
  meta: Record<string, unknown>;
  is_done: boolean;
}

export interface TodayTasksResponse {
  date: string;
  hotspots: TodayTaskItem[];
  topic_ideas: TodayTaskItem[];
  collectible_ideas: TodayTaskItem[];
  projects: TodayTaskItem[];
  confirmations: TodayTaskItem[];
  publish_reminders: TodayTaskItem[];
  suggestions: TodayTaskItem[];
  decision_items: TodayTaskItem[];
  running_items: TodayTaskItem[];
  finished_items: TodayTaskItem[];
  handled_task_ids: string[];
}

export interface StyleListItem {
  id: string;
  label: string;
  enabled: boolean;
  category?: string;
  reason?: string;
}

export interface StructurePreferenceItem {
  id: string;
  name: string;
  framework: string;
  enabled: boolean;
}

export interface SceneTemplateItem {
  id: string;
  scene: string;
  template: string;
  enabled: boolean;
}

export interface StyleValidationRules {
  professional_terms_require_explanation: boolean;
  professional_terms: string[];
  plain_language_examples: Array<{
    term: string;
    plain_explanation: string;
  }>;
  default_common_knowledge_phrases: string[];
  checks: string[];
}

export interface AccountStyleLibrary {
  account_positioning: string;
  target_audiences: StyleListItem[];
  content_directions: StyleListItem[];
  expression_styles: StyleListItem[];
  banned_expressions: StyleListItem[];
  structure_preferences: StructurePreferenceItem[];
  opening_templates: SceneTemplateItem[];
  ending_templates: SceneTemplateItem[];
  validation_rules: StyleValidationRules;
  last_saved_at: string;
  is_active: boolean;
  using_default: boolean;
}

export interface ContentQualityResult {
  id: number;
  pipeline_id: number | null;
  script_asset_id: number | null;
  source_type: string;
  source_title: string;
  checked_text: string;
  created_at: string;
  total_score: number;
  publish_suggestion: "可以发布" | "建议修改" | "不建议发布";
  max_issue: string;
  issues: string[];
  suggestions: string[];
  matched_banned_expressions: string[];
  professional_terms_need_explanation: string[];
  risk_level: "低" | "中" | "高";
  collect_value: "低" | "中" | "高";
  series_worthy: boolean;
  style_source: string;
  checked_dimensions: string[];
  local_rules_only: boolean;
  real_ai_called: boolean;
}

export interface CopywritingSampleItem {
  id: number;
  title: string;
  sample_type: string;
  sample_text: string;
  scene: string;
  content_direction: string;
  structure_type: string;
  why_reference: string;
  reusable_points: string;
  avoid_points: string;
  tags: string[];
  enabled: boolean;
  is_key_reference: boolean;
  created_at: string;
  updated_at: string;
  notes: string;
}

export interface CopywritingSamplePayload {
  title: string;
  sample_type: string;
  sample_text: string;
  scene: string;
  content_direction: string;
  structure_type: string;
  why_reference: string;
  reusable_points: string;
  avoid_points: string;
  tags: string[];
  enabled: boolean;
  is_key_reference: boolean;
  notes: string;
}

export interface CopywritingSampleListResponse {
  items: CopywritingSampleItem[];
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
  filters: {
    sample_types: string[];
    scenes: string[];
    content_directions: string[];
    tags: string[];
  };
  reference_rule: string;
  style_priority: string;
  real_ai_called: boolean;
}
