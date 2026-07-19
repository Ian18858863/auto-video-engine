import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Download,
  Eye,
  Film,
  FolderOpen,
  LoaderCircle,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";

import {
  approvePipelineQualityReport,
  createPipelineReleasePackage,
  createPipelineQualityReport,
  deleteFinishedVideo,
  exportPipelineFinishedVideo,
  exportFinishedVideo,
  getPipelineCompletionStatus,
  getPipelineQualityArtifactCandidates,
  getPipelineQualityContract,
  getFinishedVideos,
  openFinishedVideoFolder,
} from "./api";
import type {
  FinishedVideoItem,
  PipelineCompletionStatus,
  QualityArtifactCandidate,
  QualityContractReview,
  QualityContractSnapshot,
  QualityReportResult,
} from "./types";

function FinishedVideoPage({
  pipelineId,
  onOpenToday,
  onOpenEditing,
  onOpenTracking,
}: {
  pipelineId: number | null;
  onOpenToday?: () => void;
  onOpenEditing?: (pipelineId: number) => void;
  onOpenTracking?: () => void;
}) {
  const [videos, setVideos] = useState<FinishedVideoItem[]>([]);
  const [completionStatus, setCompletionStatus] =
    useState<PipelineCompletionStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [completionLoading, setCompletionLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [releaseResult, setReleaseResult] = useState<{
    artifact_relative_path: string;
    release_manifest_relative_path: string;
  } | null>(null);
  const [qualityCandidates, setQualityCandidates] = useState<
    QualityArtifactCandidate[]
  >([]);
  const [selectedQualityArtifact, setSelectedQualityArtifact] = useState("");
  const [qualityReport, setQualityReport] = useState<QualityReportResult | null>(
    null,
  );
  const [qualityLoading, setQualityLoading] = useState(false);
  const [qualityGenerating, setQualityGenerating] = useState(false);
  const [qualityApproving, setQualityApproving] = useState(false);
  const [qualityPlayed, setQualityPlayed] = useState(false);
  const [qualityContract, setQualityContract] =
    useState<QualityContractSnapshot | null>(null);
  const [qualityContractReview, setQualityContractReview] =
    useState<QualityContractReview | null>(null);
  const [qualityContractLoading, setQualityContractLoading] = useState(false);
  const [qualityContractError, setQualityContractError] = useState("");
  const [manualAttestations, setManualAttestations] = useState({
    visual_narration: false,
    facts_rights: false,
  });
  const [activePreviewId, setActivePreviewId] = useState("");
  const [busyVideoId, setBusyVideoId] = useState("");
  const [error, setError] = useState("");
  const [completionError, setCompletionError] = useState("");
  const [notice, setNotice] = useState("");

  const loadVideos = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getFinishedVideos(pipelineId);
      setVideos(result.items);
      setActivePreviewId((current) => current || result.items[0]?.["成片ID"] || "");
    } catch (loadError) {
      setError(toMessage(loadError, "成片列表读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [pipelineId]);

  const loadCompletionStatus = useCallback(async () => {
    if (pipelineId === null) {
      setCompletionStatus(null);
      setCompletionError("");
      return;
    }
    setCompletionLoading(true);
    setCompletionError("");
    try {
      setCompletionStatus(await getPipelineCompletionStatus(pipelineId));
    } catch (loadError) {
      setCompletionStatus(null);
      setCompletionError(toMessage(loadError, "成片验收状态读取失败。"));
    } finally {
      setCompletionLoading(false);
    }
  }, [pipelineId]);

  const loadQualityContract = useCallback(async () => {
    if (pipelineId === null) {
      setQualityContract(null);
      setQualityContractReview(null);
      setQualityContractError("");
      return;
    }
    setQualityContractLoading(true);
    setQualityContractError("");
    try {
      const snapshot = await getPipelineQualityContract(pipelineId);
      setQualityContract(snapshot);
      setQualityContractReview(createEmptyContractReview(snapshot));
    } catch (loadError) {
      setQualityContract(null);
      setQualityContractReview(null);
      setQualityContractError(toMessage(loadError, "质量合同读取失败。"));
    } finally {
      setQualityContractLoading(false);
    }
  }, [pipelineId]);

  function invalidateQualityReview() {
    setQualityReport(null);
    setQualityPlayed(false);
    setReleaseResult(null);
  }

  function updateContractReview(
    update: (current: QualityContractReview) => QualityContractReview,
  ) {
    invalidateQualityReview();
    setQualityContractReview((current) => (current ? update(current) : current));
  }

  async function handleLoadQualityCandidates() {
    if (pipelineId === null) return;
    setQualityLoading(true);
    setError("");
    try {
      const result = await getPipelineQualityArtifactCandidates(pipelineId);
      setQualityCandidates(result.items);
      const currentCandidate = result.items.find((item) => item.can_quality_check);
      setSelectedQualityArtifact((current) => {
        const selected = result.items.find(
          (item) => item.artifact_relative_path === current && item.can_quality_check,
        );
        return selected?.artifact_relative_path || currentCandidate?.artifact_relative_path || "";
      });
      if (result.items.length === 0) {
        setError("当前项目还没有现代剪辑渲染成片，请先在剪辑模块完成一次真实渲染。");
      } else if (!currentCandidate) {
        setError("当前只有历史成片，请先回到剪辑模块渲染最新时间线。");
      }
    } catch (candidateError) {
      setError(toMessage(candidateError, "可验收成片读取失败。"));
    } finally {
      setQualityLoading(false);
    }
  }

  async function handleGenerateQualityReport() {
    if (pipelineId === null || !selectedQualityArtifact) {
      setError("请先加载并选择一个项目内渲染成片。");
      return;
    }
    const selectedCandidate = qualityCandidates.find(
      (item) => item.artifact_relative_path === selectedQualityArtifact,
    );
    if (!selectedCandidate?.can_quality_check) {
      setError("历史成片不能作为当前版本验收，请先渲染最新时间线。");
      return;
    }
    if (!qualityContractReview) {
      setError("质量合同尚未加载，不能生成质量报告。");
      return;
    }
    if (!isQualityContractReviewReady(qualityContractReview)) {
      setError("请为质量合同每一项填写结果和证据；使用允许差异时还要写明实际差异。");
      return;
    }
    setQualityGenerating(true);
    setError("");
    setNotice("");
    const evidenceText: Record<keyof typeof manualAttestations, string> = {
      visual_narration: "Ian 已完成视频播放并确认画面与脚本一致",
      facts_rights: "Ian 已检查事实来源与素材权利",
    };
    try {
      const result = await createPipelineQualityReport(
        pipelineId,
        selectedQualityArtifact,
        {
          visual_narration: manualGate(
            "visual_narration",
            manualAttestations.visual_narration,
            evidenceText.visual_narration,
          ),
          facts_rights: manualGate(
            "facts_rights",
            manualAttestations.facts_rights,
            evidenceText.facts_rights,
          ),
        },
        qualityContractReview,
      );
      setQualityReport(result);
      setNotice(
        result.report.release_blockers.length === 0
          ? "质量报告已通过五门闸，播放结束后可由 Ian 批准。"
          : "质量报告已生成，但仍有人工闸未通过。",
      );
    } catch (reportError) {
      setError(toMessage(reportError, "质量报告生成失败。"));
    } finally {
      setQualityGenerating(false);
    }
  }

  async function handleApproveQualityReport() {
    if (
      pipelineId === null ||
      !selectedQualityArtifact ||
      !qualityReport ||
      !qualityPlayed ||
      Object.values(manualAttestations).some((passed) => !passed)
    ) {
      setError("请先完整播放成片，完成两项人工确认和质量合同逐项复核，再由 Ian 批准。 ");
      return;
    }
    setQualityApproving(true);
    setError("");
    setNotice("");
    try {
      await approvePipelineQualityReport(pipelineId, {
        artifactRelativePath: selectedQualityArtifact,
        qualityReportRelativePath: qualityReport.quality_report_relative_path,
        approvedBy: "Ian",
      });
      setNotice("Ian 批准已写入，正式发布按钮已按验收结果刷新。 ");
      await loadCompletionStatus();
    } catch (approvalError) {
      setError(toMessage(approvalError, "Ian 批准失败，未进入正式发布。"));
    } finally {
      setQualityApproving(false);
    }
  }

  useEffect(() => {
    setQualityCandidates([]);
    setSelectedQualityArtifact("");
    setQualityReport(null);
    setQualityPlayed(false);
    setQualityContract(null);
    setQualityContractReview(null);
    setManualAttestations({
      visual_narration: false,
      facts_rights: false,
    });
    setReleaseResult(null);
    void loadVideos();
    void loadCompletionStatus();
    void loadQualityContract();
  }, [loadCompletionStatus, loadQualityContract, loadVideos]);

  async function handleGenerateVideo() {
    if (pipelineId === null) {
      setError("请先从任务或剪辑选择一个项目，再生成成片。");
      return;
    }
    setGenerating(true);
    setError("");
    setNotice("");
    try {
      const result = await exportPipelineFinishedVideo(pipelineId);
      if (!result["成功"]) {
        setError(result["提示"] || "成片没有生成成功，请查看后端提示。");
        return;
      }
      setNotice("项目成片已生成。");
      await Promise.all([loadVideos(), loadCompletionStatus()]);
    } catch (generateError) {
      setError(toMessage(generateError, "成片生成失败。"));
    } finally {
      setGenerating(false);
    }
  }

  async function handleReleaseVideo() {
    if (pipelineId === null) {
      setError("请先选择一个项目，再发布成片。");
      return;
    }
    if (!completionStatus?.is_complete) {
      setError("正式发布已锁定：请先完成五门验收并由 Ian 批准。");
      return;
    }
    setReleasing(true);
    setError("");
    setNotice("");
    try {
      const result = await createPipelineReleasePackage(pipelineId);
      setReleaseResult(result);
      setNotice("正式发布包已生成；外部平台发布仍需单独执行。");
      await loadCompletionStatus();
    } catch (releaseError) {
      setError(toMessage(releaseError, "正式发布包生成失败。"));
    } finally {
      setReleasing(false);
    }
  }

  async function handleOpenFolder(video: FinishedVideoItem) {
    await runVideoAction(video, async () => {
      await openFinishedVideoFolder(video["成片ID"], pipelineId);
      setNotice("已尝试打开成片所在文件夹。");
    });
  }

  async function handleExport(video: FinishedVideoItem) {
    await runVideoAction(video, async () => {
      const result = await exportFinishedVideo(video["成片ID"], pipelineId);
      const exportPath =
        typeof result["导出路径"] === "string" ? result["导出路径"] : "";
      setNotice(exportPath ? `已导出到：${exportPath}` : "成片已导出。");
    });
  }

  async function handleDelete(video: FinishedVideoItem) {
    await runVideoAction(video, async () => {
      await deleteFinishedVideo(video["成片ID"], pipelineId);
      setVideos((current) =>
        current.filter((item) => item["成片ID"] !== video["成片ID"]),
      );
      if (activePreviewId === video["成片ID"]) {
        setActivePreviewId("");
      }
      setNotice("这条成片和生成目录已删除。");
    });
  }

  async function runVideoAction(
    video: FinishedVideoItem,
    action: () => Promise<void>,
  ) {
    setBusyVideoId(video["成片ID"]);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (actionError) {
      setError(toMessage(actionError, "成片操作失败。"));
    } finally {
      setBusyVideoId("");
    }
  }

  function handleModify() {
    if (pipelineId !== null) {
      onOpenEditing?.(pipelineId);
      return;
    }
    setNotice("这条成片暂未绑定项目，请从剪辑页打开对应项目修改。");
  }

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>成片</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void loadVideos()}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void handleGenerateVideo()}
            disabled={generating}
          >
            {generating ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Film size={16} aria-hidden="true" />
            )}
            生成成片
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleReleaseVideo()}
            disabled={releasing || !completionStatus?.is_complete}
            title={
              completionStatus?.is_complete
                ? "生成项目内正式发布包"
                : "需先通过五门验收并由 Ian 批准"
            }
          >
            {releasing ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={16} aria-hidden="true" />
            )}
            发布成片
          </button>
          {completionStatus?.is_complete && (
            <button
              className="button button-secondary"
              type="button"
              onClick={onOpenTracking}
            >
              <BarChart3 size={16} aria-hidden="true" />
              记录手动发布
            </button>
          )}
        </div>
      </header>

      {pipelineId !== null && (
        <p className="muted-text">
          “生成成片”保留旧兼容草稿链；“发布成片”只有在五门验收通过并完成 Ian 批准后才会解锁。
        </p>
      )}

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

      {pipelineId !== null && (
        <section className="finished-video-panel" aria-label="成片验收状态">
          <div className="finished-video-title-row">
            <h2>成片验收</h2>
            {completionStatus && (
              <span
                className={`status-badge ${completionStatusClass(completionStatus)}`}
              >
                {completionStatus.status}
              </span>
            )}
          </div>
          {completionLoading ? (
            <p className="muted-text">正在读取五门质量闸状态...</p>
          ) : completionStatus ? (
            <>
              <p>{completionStatus.message}</p>
              <small>{qualityGateLabel(completionStatus.quality_gate_state)}</small>
              {completionStatus.artifact_relative_path && (
                <code className="finished-video-path">
                  验收成片：{completionStatus.artifact_relative_path}
                </code>
              )}
              {completionStatus.quality_report_relative_path && (
                <code className="finished-video-path">
                  质量报告：{completionStatus.quality_report_relative_path}
                </code>
              )}
              {!completionStatus.is_complete && (
                <small>{completionStatus.next_action}</small>
              )}
            </>
          ) : (
            <p className="message message-error" role="alert">
              {completionError || "成片验收状态暂不可用。"}
            </p>
          )}
          <div className="quality-review-workspace" aria-label="人工验收工具">
            <div className="finished-video-title-row">
              <h3>人工验收</h3>
              <button
                className="button button-secondary"
                type="button"
                onClick={() => void handleLoadQualityCandidates()}
                disabled={qualityLoading}
              >
                {qualityLoading ? (
                  <LoaderCircle className="spin" size={15} aria-hidden="true" />
                ) : (
                  <RefreshCw size={15} aria-hidden="true" />
                )}
                加载可验收成片
              </button>
            </div>
            {qualityContractLoading ? (
              <p className="muted-text">正在读取只读质量合同...</p>
            ) : qualityContract && qualityContractReview ? (
              <section className="quality-contract-panel" aria-label="只读质量合同">
                <div className="quality-contract-heading">
                  <div>
                    <h4>只读质量合同</h4>
                    <p className="muted-text">
                      标准已随项目冻结。验收时逐项记录结果和证据，不能临时改标准。
                    </p>
                  </div>
                  <span className="status-badge status-muted">只读</span>
                </div>
                <dl className="quality-contract-target">
                  <div>
                    <dt>发布平台</dt>
                    <dd>{qualityContract.contract.target.platform}</dd>
                  </div>
                  <div>
                    <dt>画面比例</dt>
                    <dd>{qualityContract.contract.target.aspect_ratio}</dd>
                  </div>
                  <div>
                    <dt>目标时长</dt>
                    <dd>{qualityContract.contract.target.duration_seconds} 秒</dd>
                  </div>
                </dl>
                <div className="quality-contract-meta">
                  <div>
                    <span>合同文件</span>
                    <code className="finished-video-path">
                      {qualityContract.quality_contract_relative_path}
                    </code>
                  </div>
                  <div>
                    <span>版本指纹</span>
                    <code
                      className="quality-contract-hash"
                      aria-label={`质量合同 SHA-256：${qualityContract.quality_contract_sha256}`}
                      title={qualityContract.quality_contract_sha256}
                    >
                      {qualityContract.quality_contract_sha256.slice(0, 12)}…
                      {qualityContract.quality_contract_sha256.slice(-8)}
                    </code>
                  </div>
                </div>
                <div className="quality-contract-reference">
                  <strong>参考资料</strong>
                  {qualityContract.contract.reference_pack.status === "none_supplied" ? (
                    <p className="muted-text">未提供参考资料，按统一 R1 基线验收。</p>
                  ) : (
                    qualityContract.contract.reference_pack.items.map((item) => (
                      <code className="finished-video-path" key={item.id}>
                        {item.project_relative_path}
                      </code>
                    ))
                  )}
                </div>

                <fieldset className="quality-contract-group">
                  <legend>
                    <span>必须保持</span>
                    <small>全部确认通过，成片才可进入批准。</small>
                  </legend>
                  {qualityContract.contract.must_preserve.map((item) => {
                    const result = qualityContractReview.must_preserve_results[item.id];
                    return (
                      <div className="quality-contract-item" key={item.id}>
                        <div className="quality-contract-item-copy">
                          <label className="quality-contract-check">
                            <input
                              aria-label={`保持项：${item.label}`}
                              type="checkbox"
                              checked={result.result === "passed"}
                              onChange={(event) =>
                                updateContractReview((current) => ({
                                  ...current,
                                  must_preserve_results: {
                                    ...current.must_preserve_results,
                                    [item.id]: {
                                      ...current.must_preserve_results[item.id],
                                      result: event.target.checked ? "passed" : "failed",
                                    },
                                  },
                                }))
                              }
                            />
                            <strong>{item.label}</strong>
                            <small>{result.result === "passed" ? "已确认" : "待确认"}</small>
                          </label>
                          <p className="muted-text">{item.criterion}</p>
                        </div>
                        <label className="quality-contract-control">
                          <span>保持项证据：{item.label}</span>
                          <input
                            aria-label={`保持项证据：${item.label}`}
                            value={result.evidence}
                            onChange={(event) =>
                              updateContractReview((current) => ({
                                ...current,
                                must_preserve_results: {
                                  ...current.must_preserve_results,
                                  [item.id]: {
                                    ...current.must_preserve_results[item.id],
                                    evidence: event.target.value,
                                  },
                                },
                              }))
                            }
                          />
                        </label>
                      </div>
                    );
                  })}
                </fieldset>

                <fieldset className="quality-contract-group">
                  <legend>
                    <span>允许差异</span>
                    <small>如实记录是否使用；使用时说明实际差异。</small>
                  </legend>
                  {qualityContract.contract.allowed_differences.map((item) => {
                    const result =
                      qualityContractReview.allowed_difference_results[item.id];
                    return (
                      <div className="quality-contract-item" key={item.id}>
                        <div className="quality-contract-item-copy">
                          <strong>{item.label}</strong>
                        </div>
                        <div className="quality-contract-controls">
                          <label className="quality-contract-control">
                            <span>允许差异结果：{item.label}</span>
                            <select
                              aria-label={`允许差异结果：${item.label}`}
                              value={result.result}
                              onChange={(event) =>
                                updateContractReview((current) => ({
                                  ...current,
                                  allowed_difference_results: {
                                    ...current.allowed_difference_results,
                                    [item.id]: {
                                      ...current.allowed_difference_results[item.id],
                                      result: event.target.value as "used" | "not_used",
                                      actual_difference:
                                        event.target.value === "used"
                                          ? current.allowed_difference_results[item.id]
                                              .actual_difference
                                          : "",
                                    },
                                  },
                                }))
                              }
                            >
                              <option value="not_used">未使用</option>
                              <option value="used">已使用</option>
                            </select>
                          </label>
                          {result.result === "used" && (
                            <label className="quality-contract-control">
                              <span>实际差异：{item.label}</span>
                              <input
                                aria-label={`实际差异：${item.label}`}
                                value={result.actual_difference}
                                onChange={(event) =>
                                  updateContractReview((current) => ({
                                    ...current,
                                    allowed_difference_results: {
                                      ...current.allowed_difference_results,
                                      [item.id]: {
                                        ...current.allowed_difference_results[item.id],
                                        actual_difference: event.target.value,
                                      },
                                    },
                                  }))
                                }
                              />
                            </label>
                          )}
                          <label className="quality-contract-control">
                            <span>允许差异证据：{item.label}</span>
                            <input
                              aria-label={`允许差异证据：${item.label}`}
                              value={result.evidence}
                              onChange={(event) =>
                                updateContractReview((current) => ({
                                  ...current,
                                  allowed_difference_results: {
                                    ...current.allowed_difference_results,
                                    [item.id]: {
                                      ...current.allowed_difference_results[item.id],
                                      evidence: event.target.value,
                                    },
                                  },
                                }))
                              }
                            />
                          </label>
                        </div>
                      </div>
                    );
                  })}
                </fieldset>

                <fieldset className="quality-contract-group">
                  <legend>
                    <span>计划超越</span>
                    <small>每项都必须达到“已超越”，并留下比较依据。</small>
                  </legend>
                  {qualityContract.contract.planned_exceed_dimensions.map((item) => {
                    const result = qualityContractReview.planned_exceed_results[item.id];
                    return (
                      <div className="quality-contract-item" key={item.id}>
                        <div className="quality-contract-item-copy">
                          <strong>{item.label}</strong>
                          <p className="muted-text">{item.criterion}</p>
                        </div>
                        <div className="quality-contract-controls">
                          <label className="quality-contract-control">
                            <span>超越结果：{item.label}</span>
                            <select
                              aria-label={`超越结果：${item.label}`}
                              value={result.result}
                              onChange={(event) =>
                                updateContractReview((current) => ({
                                  ...current,
                                  planned_exceed_results: {
                                    ...current.planned_exceed_results,
                                    [item.id]: {
                                      ...current.planned_exceed_results[item.id],
                                      result: event.target.value as
                                        | "exceeded"
                                        | "matched"
                                        | "not_met"
                                        | "not_comparable",
                                    },
                                  },
                                }))
                              }
                            >
                              <option value="exceeded">已超越</option>
                              <option value="matched">持平</option>
                              <option value="not_met">未达到</option>
                              <option value="not_comparable">不可比较</option>
                            </select>
                          </label>
                          <label className="quality-contract-control">
                            <span>实际变化：{item.label}</span>
                            <input
                              aria-label={`实际变化：${item.label}`}
                              value={result.difference}
                              onChange={(event) =>
                                updateContractReview((current) => ({
                                  ...current,
                                  planned_exceed_results: {
                                    ...current.planned_exceed_results,
                                    [item.id]: {
                                      ...current.planned_exceed_results[item.id],
                                      difference: event.target.value,
                                    },
                                  },
                                }))
                              }
                            />
                          </label>
                          <label className="quality-contract-control">
                            <span>超越证据：{item.label}</span>
                            <input
                              aria-label={`超越证据：${item.label}`}
                              value={result.evidence}
                              onChange={(event) =>
                                updateContractReview((current) => ({
                                  ...current,
                                  planned_exceed_results: {
                                    ...current.planned_exceed_results,
                                    [item.id]: {
                                      ...current.planned_exceed_results[item.id],
                                      evidence: event.target.value,
                                    },
                                  },
                                }))
                              }
                            />
                          </label>
                        </div>
                      </div>
                    );
                  })}
                </fieldset>
              </section>
            ) : (
              <p className="message message-error" role="alert">
                {qualityContractError || "质量合同暂不可用。"}
              </p>
            )}
            {qualityCandidates.length > 0 && (
              <>
                <label className="select-field">
                  <span>选择项目内渲染版本</span>
                  <select
                    value={selectedQualityArtifact}
                    onChange={(event) => {
                      setSelectedQualityArtifact(event.target.value);
                      invalidateQualityReview();
                      setManualAttestations({
                        visual_narration: false,
                        facts_rights: false,
                      });
                      if (qualityContract) {
                        setQualityContractReview(createEmptyContractReview(qualityContract));
                      }
                    }}
                  >
                    <option value="" disabled>
                      请选择当前时间线成片
                    </option>
                    {qualityCandidates.map((candidate) => (
                      <option
                        key={candidate.artifact_relative_path}
                        value={candidate.artifact_relative_path}
                        disabled={!candidate.can_quality_check}
                      >
                        {candidate.artifact_relative_path} · {formatDate(candidate.modified_at)} ·{
                          candidate.can_quality_check ? "当前版本" : "历史版本（不可验收）"
                        }
                      </option>
                    ))}
                  </select>
                </label>
                {selectedQualityArtifact && pipelineId !== null && (
                  <video
                    key={selectedQualityArtifact}
                    className="finished-video-player"
                    src={`/api/pipeline/${pipelineId}/quality-report/preview?artifact_relative_path=${encodeURIComponent(selectedQualityArtifact)}`}
                    controls
                    onEnded={() => setQualityPlayed(true)}
                  >
                    当前浏览器不能播放项目内渲染成片。
                  </video>
                )}
                <small>
                  {qualityPlayed
                    ? "已完整播放当前版本。"
                    : "请完整播放当前版本；播放结束才算完成视觉验收。"}
                </small>
                <div className="copy-checkbox">
                  <label>
                    <input
                      type="checkbox"
                      checked={manualAttestations.visual_narration}
                      onChange={(event) => {
                        invalidateQualityReview();
                        setManualAttestations((current) => ({
                          ...current,
                          visual_narration: event.target.checked,
                        }));
                      }}
                    />
                    我确认画面与脚本叙事一致
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={manualAttestations.facts_rights}
                      onChange={(event) => {
                        invalidateQualityReview();
                        setManualAttestations((current) => ({
                          ...current,
                          facts_rights: event.target.checked,
                        }));
                      }}
                    />
                    我确认事实来源与素材权利已检查
                  </label>
                </div>
                <div className="finished-video-actions">
                  <button
                    className="button button-secondary"
                    type="button"
                    onClick={() => void handleGenerateQualityReport()}
                    disabled={qualityGenerating}
                  >
                    {qualityGenerating ? (
                      <LoaderCircle className="spin" size={15} aria-hidden="true" />
                    ) : (
                      <CheckCircle2 size={15} aria-hidden="true" />
                    )}
                    生成质量报告
                  </button>
                  <button
                    className="button button-primary"
                    type="button"
                    onClick={() => void handleApproveQualityReport()}
                    disabled={
                      qualityApproving ||
                      !qualityReport ||
                      !qualityPlayed ||
                      Object.values(manualAttestations).some((passed) => !passed) ||
                      qualityReport.report.release_blockers.length > 0
                    }
                  >
                    {qualityApproving ? (
                      <LoaderCircle className="spin" size={15} aria-hidden="true" />
                    ) : (
                      <CheckCircle2 size={15} aria-hidden="true" />
                    )}
                    Ian 批准成片
                  </button>
                </div>
                {qualityReport && (
                  <small>
                    质量报告：{qualityReport.quality_report_relative_path}；
                    {qualityReport.report.release_blockers.length > 0
                      ? `待处理：${qualityReport.report.release_blockers.join("、")}`
                      : "五门闸已通过"}
                  </small>
                )}
              </>
            )}
          </div>
          {releaseResult && (
            <p className="message message-success" role="status">
              正式发布包：{releaseResult.artifact_relative_path}（清单：
              {releaseResult.release_manifest_relative_path}）
            </p>
          )}
        </section>
      )}

      {loading ? (
        <div className="empty-state">
          <LoaderCircle className="spin" size={24} aria-hidden="true" />
          正在读取成片...
        </div>
      ) : videos.length === 0 ? (
        <div className="empty-state empty-state-stacked">
          <strong>还没有成片。</strong>
          <span>先从任务确认选题，系统会自动生成后续成片任务。</span>
          <button className="button button-primary" type="button" onClick={onOpenToday}>
            去任务
          </button>
        </div>
      ) : (
        <section className="finished-video-panel" aria-label="成片列表">
          <h2>成片列表</h2>
          <div className="finished-video-list">
            {videos.map((video) => {
              const isPreviewing = activePreviewId === video["成片ID"];
              const busy = busyVideoId === video["成片ID"];
              return (
                <article className="finished-video-card" key={video["成片ID"]}>
                  <div>
                    <div className="finished-video-title-row">
                      <strong>{video["标题"]}</strong>
                      <span className="status-badge status-good">
                        {video["状态"]}
                      </span>
                    </div>
                    <small>
                      <span>{`${video["画面数量"]} 个画面`}</span>
                      <span>{` · ${formatDate(video["生成时间"])}`}</span>
                    </small>
                    <code className="finished-video-path">
                      {video["最终成片路径"]}
                    </code>
                    {isPreviewing && (
                      <video
                        className="finished-video-player"
                        src={video["预览地址"]}
                        controls
                      >
                        当前浏览器不能播放这个视频，请打开文件夹查看。
                      </video>
                    )}
                  </div>
                  <div className="finished-video-actions">
                    <button
                      className="button button-primary"
                      type="button"
                      onClick={() =>
                        setActivePreviewId(isPreviewing ? "" : video["成片ID"])
                      }
                    >
                      <Eye size={15} aria-hidden="true" />
                      预览
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void handleOpenFolder(video)}
                      disabled={busy}
                    >
                      <FolderOpen size={15} aria-hidden="true" />
                      打开文件夹
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void handleExport(video)}
                      disabled={busy}
                    >
                      <Download size={15} aria-hidden="true" />
                      导出
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={handleModify}
                    >
                      <SlidersHorizontal size={15} aria-hidden="true" />
                      修改
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void handleDelete(video)}
                      disabled={busy}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                      删除
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}
    </main>
  );
}

function formatDate(value: string): string {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function toMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function qualityGateLabel(state: PipelineCompletionStatus["quality_gate_state"]): string {
  if (state === "passed") return "五门质量闸已通过。";
  if (state === "blocked") return "已提交验收证据，但仍有阻断项。";
  return "尚未提交五门验收证据";
}

function completionStatusClass(status: PipelineCompletionStatus): string {
  if (status.is_complete) return "status-complete";
  if (status.status === "失败") return "status-failed";
  return "status-warning";
}

function manualGate(
  name: "visual_narration" | "facts_rights",
  passed: boolean,
  passedEvidence: string,
): { status: "passed" | "failed"; evidence: string[] } {
  return {
    status: passed ? "passed" : "failed",
    evidence: [passed ? passedEvidence : `manual_review_required:${name}`],
  };
}

function createEmptyContractReview(
  snapshot: QualityContractSnapshot,
): QualityContractReview {
  return {
    contract_sha256: snapshot.quality_contract_sha256,
    must_preserve_results: Object.fromEntries(
      snapshot.contract.must_preserve.map((item) => [
        item.id,
        { result: "failed" as const, evidence: "" },
      ]),
    ),
    allowed_difference_results: Object.fromEntries(
      snapshot.contract.allowed_differences.map((item) => [
        item.id,
        {
          result: "not_used" as const,
          actual_difference: "",
          evidence: "",
        },
      ]),
    ),
    planned_exceed_results: Object.fromEntries(
      snapshot.contract.planned_exceed_dimensions.map((item) => [
        item.id,
        {
          result: "not_comparable" as const,
          difference: "",
          evidence: "",
        },
      ]),
    ),
  };
}


function isQualityContractReviewReady(review: QualityContractReview): boolean {
  const hasText = (value: string) => value.trim().length > 0;
  return (
    Object.values(review.must_preserve_results).every((item) => hasText(item.evidence)) &&
    Object.values(review.allowed_difference_results).every(
      (item) =>
        hasText(item.evidence) &&
        (item.result === "not_used" || hasText(item.actual_difference)),
    ) &&
    Object.values(review.planned_exceed_results).every(
      (item) => hasText(item.difference) && hasText(item.evidence),
    )
  );
}

export default FinishedVideoPage;
