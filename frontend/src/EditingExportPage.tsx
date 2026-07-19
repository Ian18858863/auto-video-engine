import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Film,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";

import {
  buildPipelineVoiceoverArtifact,
  getLatestEndToEndVideoTest,
  getEditingExportPackage,
  getPipelineQualityArtifactCandidates,
  rebuildEditingExportPackage,
  renderPipelineTimelineMedia,
  runEndToEndVideoTest,
} from "./api";
import EndToEndVideoResultPanel from "./EndToEndVideoResultPanel";
import type {
  EditingExportPackage,
  EndToEndVideoResult,
  QualityArtifactCandidate,
  TimelineMediaRunResult,
} from "./types";

function EditingExportPage({
  pipelineId,
  onOpenToday,
  onOpenMaterials,
}: {
  pipelineId: number | null;
  onOpenToday?: () => void;
  onOpenMaterials?: (pipelineId: number) => void;
}) {
  const [packageData, setPackageData] = useState<EditingExportPackage | null>(null);
  const [loading, setLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [testExportRunning, setTestExportRunning] = useState(false);
  const [testExportResult, setTestExportResult] =
    useState<EndToEndVideoResult | null>(null);
  const [mediaRun, setMediaRun] = useState<TimelineMediaRunResult | null>(null);
  const [persistedMediaArtifact, setPersistedMediaArtifact] =
    useState<QualityArtifactCandidate | null>(null);
  const [mediaRendering, setMediaRendering] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const loadPackage = useCallback(async () => {
    if (pipelineId === null) return;
    setLoading(true);
    setError("");
    try {
      const loaded = await getEditingExportPackage(pipelineId);
      setPackageData(loaded);
      try {
        const candidates = await getPipelineQualityArtifactCandidates(pipelineId);
        setPersistedMediaArtifact(
          loaded.timeline_sync.status === "current"
            ? candidates.items.find(
                (item) =>
                  item.can_quality_check &&
                  item.timeline_version_id === loaded.timeline_sync.current_version_id,
              ) || null
            : null,
        );
      } catch {
        setPersistedMediaArtifact(null);
      }
    } catch (loadError) {
      setPersistedMediaArtifact(null);
      if (isEditingNotReadyError(loadError)) {
        setPackageData(null);
      } else {
        setError(toMessage(loadError, "剪辑任务读取失败。"));
      }
    } finally {
      setLoading(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void loadPackage();
  }, [loadPackage]);

  useEffect(() => {
    setMediaRun(null);
    setPersistedMediaArtifact(null);
  }, [pipelineId]);

  useEffect(() => {
    let active = true;
    getLatestEndToEndVideoTest()
      .then((result) => {
        if (active && result["导出文件"] && Object.keys(result["导出文件"]).length > 0) {
          setTestExportResult(result);
        }
      })
      .catch(() => {
        // 没有最近成片时保持页面干净，不打扰主流程。
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleRebuild() {
    if (pipelineId === null) return;
    setRebuilding(true);
    setError("");
    setNotice("");
    try {
      const result = await rebuildEditingExportPackage(pipelineId);
      if (
        result.export_task.real_jianying_automation ||
        result.export_task.real_avatar_generation ||
        result.export_task.real_publish_executed
      ) {
        throw new Error("安全校验失败：当前不允许真实剪辑、数字人或自动发布。");
      }
      setPackageData(result);
      if (result.timeline_sync.created_new_version) {
        setMediaRun(null);
        setPersistedMediaArtifact(null);
        setNotice(
          `已生成新时间线 ${result.timeline_sync.current_version_id}；受影响镜头：${formatAffectedSegments(
            result.timeline_sync.affected_segment_ids,
          )}。`,
        );
      } else {
        setNotice(
          `时间线已是当前版本 ${result.timeline_sync.current_version_id}，无需创建重复版本。`,
        );
      }
    } catch (rebuildError) {
      if (isEditingNotReadyError(rebuildError)) {
        setPackageData(null);
      } else {
        setError(toMessage(rebuildError, "重新生成剪辑方案失败。"));
      }
    } finally {
      setRebuilding(false);
    }
  }

  async function handleRunTestExport() {
    setTestExportRunning(true);
    setError("");
    setNotice("");
    try {
      const result = await runEndToEndVideoTest();
      setTestExportResult(result);
      if (result["成功"]) {
        setNotice("一分钟测试成片已生成，可以在下方查看 MP4 路径。");
      } else {
        setError(result["提示"] || "测试成片没有生成成功，请看下方提醒。");
      }
    } catch (exportError) {
      setError(toMessage(exportError, "一分钟测试成片生成失败。"));
    } finally {
      setTestExportRunning(false);
    }
  }

  async function handleRenderModernMedia() {
    if (pipelineId === null) return;
    if (!packageData || packageData.timeline_sync.status !== "current") {
      setError("镜头选择已变化，请先生成变更时间线，再渲染现代媒体草稿。");
      return;
    }
    setMediaRendering(true);
    setError("");
    setNotice("");
    try {
      const artifact = await buildPipelineVoiceoverArtifact(pipelineId);
      const run = await renderPipelineTimelineMedia(
        pipelineId,
        artifact.artifact_id,
        packageData?.timeline_spec?.version_id,
      );
      setMediaRun(run);
      await loadPackage();
      setNotice(
        `现代媒体草稿已生成：${run.render.output_project_relative_path}。它仍需在成片页播放、质检和 Ian 批准。`,
      );
    } catch (renderError) {
      setError(toMessage(renderError, "现代媒体草稿生成失败。"));
    } finally {
      setMediaRendering(false);
    }
  }

  const recommendedTemplate =
    packageData?.recommended_template || packageData?.versions[0] || null;
  const timelineNeedsRebuild =
    packageData !== null && packageData.timeline_sync.status !== "current";

  if (pipelineId === null) {
    return (
      <main className="app-shell">
        <header className="page-header">
          <div>
            <h1>剪辑</h1>
          </div>
          <div className="page-header-actions">
            <button
              className="button button-primary"
              type="button"
              onClick={() => void handleRunTestExport()}
              disabled={testExportRunning}
            >
              {testExportRunning ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : (
                <Film size={16} aria-hidden="true" />
              )}
              生成一分钟测试成片
            </button>
          </div>
        </header>
        {notice && (
          <p className="message message-success" role="status">
            <CheckCircle2 size={17} aria-hidden="true" />
            {notice}
          </p>
        )}
        {error && (
          <p className="message message-error" role="alert">
            <AlertTriangle size={17} aria-hidden="true" />
            {error}
          </p>
        )}
        <div className="empty-state empty-state-stacked">
          <strong>还没有选中的项目。</strong>
          <span>先从任务确认选题，系统会自动生成后续剪辑任务。</span>
          <button className="button button-primary" type="button" onClick={onOpenToday}>
            去任务
          </button>
        </div>
        <EndToEndVideoResultPanel result={testExportResult} />
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>剪辑</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void loadPackage()}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleRunTestExport()}
            disabled={testExportRunning}
          >
            {testExportRunning ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Film size={16} aria-hidden="true" />
            )}
            生成一分钟测试成片
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void handleRebuild()}
            disabled={rebuilding}
          >
            {rebuilding ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Film size={16} aria-hidden="true" />
            )}
            {timelineNeedsRebuild ? "生成变更时间线" : "生成成片方案"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleRenderModernMedia()}
            disabled={mediaRendering || rebuilding || timelineNeedsRebuild}
            title={
              timelineNeedsRebuild
                ? "请先生成变更时间线"
                : "生成 D 盘现代 TimelineSpec 音频/字幕媒体草稿，不会发布"
            }
          >
            {mediaRendering ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Film size={16} aria-hidden="true" />
            )}
            生成现代媒体草稿
          </button>
        </div>
      </header>

      {notice && (
        <p className="message message-success" role="status">
          <CheckCircle2 size={17} aria-hidden="true" />
          {notice}
        </p>
      )}
      {error && (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      )}

      {loading ? (
        <div className="empty-state">
          <LoaderCircle className="spin" size={24} aria-hidden="true" />
          正在读取剪辑任务...
        </div>
      ) : packageData === null ? (
        <div className="empty-state empty-state-stacked">
          <strong>剪辑方案还没生成。</strong>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void handleRebuild()}
          >
            生成成片方案
          </button>
        </div>
      ) : (
        <>
          <section className="summary-strip" aria-label="剪辑概况">
            <SummaryItem label="时间轴段落" value={packageData.timeline.length} />
            <SummaryItem
              label="推荐模板"
              value={packageData.export_task.version_count || 1}
            />
            <SummaryItem
              label="已就绪素材"
              value={packageData.export_task.material_ready_count}
              tone="good"
            />
            <SummaryItem
              label="时间轴版本"
              value={
                packageData.timeline_spec?.version_number
                  ? `v${packageData.timeline_spec.version_number}`
                  : "未保存"
              }
            />
          </section>

          <section className="editing-status-panel">
            <div>
              <span>导出状态</span>
              <strong>{packageData.status}</strong>
              <p>{packageData.note}</p>
            </div>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => onOpenMaterials?.(pipelineId)}
            >
              打开素材
            </button>
          </section>

          {packageData.timeline_spec && (
            <section className="editing-status-panel" aria-label="TimelineSpec 状态">
              <div>
                <span>
                  TimelineSpec · {timelineSyncLabel(packageData.timeline_sync.status)}
                </span>
                <strong>
                  {packageData.timeline_spec.version_id ||
                    packageData.timeline_spec.status ||
                    "未保存"}
                </strong>
                {timelineNeedsRebuild ? (
                  <p>
                    待生成新时间线：
                    {formatAffectedSegments(packageData.timeline_sync.affected_segment_ids)}
                  </p>
                ) : (
                  <p>
                    当前版本：
                    {packageData.timeline_sync.current_version_id ||
                      packageData.timeline_spec.version_id ||
                      "未保存"}
                  </p>
                )}
              </div>
            </section>
          )}

          <section className="editing-status-panel" aria-label="现代媒体渲染">
            <div>
              <span>现代媒体渲染</span>
              <strong>
                {timelineNeedsRebuild
                  ? "等待新时间线"
                  : mediaRun
                    ? "已生成草稿"
                    : persistedMediaArtifact
                      ? "已存在当前草稿"
                    : "尚未生成"}
              </strong>
              <p>
                这条路径会生成独立的音频/字幕版本并渲染 D 盘媒体草稿；不覆盖旧导出，
                也不代表已通过五门质量或允许外部发布。
              </p>
              {mediaRun && (
                <>
                  <small>
                    {`本次复用 ${mediaRun.render.visual_rebuild.reused_segment_ids.length} 段，重建 ${mediaRun.render.visual_rebuild.rebuilt_segment_ids.length} 段。`}
                  </small>
                  <small>
                    成片：{mediaRun.render.output_project_relative_path}｜技术报告：
                    {mediaRun.render.report_project_relative_path}
                  </small>
                </>
              )}
              {!mediaRun && persistedMediaArtifact && (
                <small>
                  当前成片：{persistedMediaArtifact.artifact_relative_path}｜时间线：
                  {persistedMediaArtifact.timeline_version_id}
                </small>
              )}
            </div>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => void handleRenderModernMedia()}
              disabled={mediaRendering || rebuilding || timelineNeedsRebuild}
            >
              {mediaRendering ? "正在渲染…" : "生成/刷新现代草稿"}
            </button>
          </section>

          <section className="editing-grid" aria-label="智能推荐模板">
            {recommendedTemplate && (
              <article
                className="editing-version-card editing-template-card"
                key={recommendedTemplate.version_id}
              >
                <div>
                  <strong>{recommendedTemplate.name}</strong>
                  <span className="status-badge status-warning">
                    {recommendedTemplate.status}
                  </span>
                </div>
                <p>{recommendedTemplate.strategy}</p>
                <small>
                  约 {recommendedTemplate.estimated_duration_seconds} 秒｜
                  {recommendedTemplate.best_for}
                </small>
                {recommendedTemplate.avatar_note && (
                  <small>{recommendedTemplate.avatar_note}</small>
                )}
              </article>
            )}
          </section>

          <section className="timeline-table" aria-label="剪辑时间轴" id="editing-timeline">
            <h2>时间轴结构</h2>
            {packageData.timeline.map((segment) => (
              <article className="timeline-row" key={segment.segment_id}>
                <span>{segment.start}-{segment.end}</span>
                <div>
                  <strong>{segment.visual_type || "待定画面"}</strong>
                  <p>{segment.voiceover}</p>
                  <small>{segment.edit_instruction}</small>
                </div>
              </article>
            ))}
          </section>

          <section className="export-file-list" aria-label="导出文件">
            <h2>导出文件</h2>
            {Object.entries(packageData.generated_files).length === 0 ? (
              <div className="empty-state empty-state-stacked">
                <strong>还没有写出文件。</strong>
                <button
                  className="button button-primary"
                  type="button"
                  onClick={() => void handleRebuild()}
                >
                  写出导出文件
                </button>
              </div>
            ) : (
              Object.entries(packageData.generated_files).map(([key, path]) => (
                <div className="export-file-row" key={key}>
                  <Download size={16} aria-hidden="true" />
                  <span>{exportFileLabel(key)}</span>
                  <code>{path}</code>
                </div>
              ))
            )}
          </section>

          <EndToEndVideoResultPanel result={testExportResult} />
        </>
      )}
    </main>
  );
}

function exportFileLabel(key: string): string {
  const labels: Record<string, string> = {
    script_md: "口播稿文件",
    shotlist_csv: "分镜表",
    edit_manifest_json: "剪辑清单",
    timeline_json: "时间轴文件",
    rough_cut_plan_json: "粗剪方案",
    subtitles_srt: "字幕文件",
    edit_notes_md: "剪辑说明",
  };
  return labels[key] || key;
}

function formatAffectedSegments(segmentIds: string[]): string {
  return segmentIds.length > 0 ? segmentIds.join("、") : "全局配置";
}

function timelineSyncLabel(status: EditingExportPackage["timeline_sync"]["status"]): string {
  if (status === "current") return "当前";
  if (status === "stale") return "已过期";
  return "未保存";
}

function SummaryItem({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: number | string;
  tone?: "normal" | "good";
}) {
  return (
    <div className={`summary-item summary-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function toMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function isEditingNotReadyError(error: unknown): boolean {
  return error instanceof Error && error.message.includes("请求失败（500");
}

export default EditingExportPage;
