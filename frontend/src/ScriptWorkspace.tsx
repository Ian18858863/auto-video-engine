import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ClipboardCheck,
  FileText,
  LoaderCircle,
  Mic2,
  Plus,
  RefreshCw,
  Send,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";

import {
  acceptScriptWorkOrder,
  ApiError,
  buildScriptWorkOrder,
  checkPipelineContentQuality,
  continuePipeline,
  dispatchScriptWorkOrder,
  generatePipelineVoiceover,
  getScriptVersionDetail,
  getScriptWorkspace,
  importScriptWorkOrderResult,
  rewriteScriptPart,
  restoreScriptVersionAsDraft,
  updateScriptPresentationOptions,
  updateScriptWorkspace,
} from "./api";
import type {
  ContentQualityResult,
  ScriptFactoryState,
  ScriptVersionDetail,
  ScriptVersionSummary,
  ScriptWorkOrderResult,
  ScriptWorkspaceData,
  ScriptWorkspaceUpdatePayload,
} from "./types";
import "./styles.css";

type VoiceoverFlowState = "" | "checking" | "advancing";
type ScriptWorkOrderAction =
  | ""
  | "building"
  | "dispatching"
  | "importing"
  | "accepting";

interface ScriptWorkspaceProps {
  pipelineId: number | null;
  onOpenContentQuality?: (pipelineId: number) => void;
  onOpenMaterials?: (pipelineId: number) => void;
}

function ScriptWorkspace({
  pipelineId,
  onOpenContentQuality,
  onOpenMaterials,
}: ScriptWorkspaceProps) {
  const [workspace, setWorkspace] = useState<ScriptWorkspaceData | null>(null);
  const [latestScriptPayload, setLatestScriptPayload] =
    useState<ScriptWorkspaceUpdatePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [savingDraft, setSavingDraft] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [rewritingPart, setRewritingPart] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [voiceoverConfirmed, setVoiceoverConfirmed] = useState(false);
  const [voiceoverFlowState, setVoiceoverFlowState] =
    useState<VoiceoverFlowState>("");
  const [qualityIssue, setQualityIssue] = useState<ContentQualityResult | null>(null);
  const [generatingAudio, setGeneratingAudio] = useState(false);
  const [workOrderAction, setWorkOrderAction] =
    useState<ScriptWorkOrderAction>("");
  const [workOrderResult, setWorkOrderResult] =
    useState<ScriptWorkOrderResult | null>(null);
  const [restoringVersionId, setRestoringVersionId] = useState("");
  const autoBuildPipelineRef = useRef<number | null>(null);
  const restoreOperationIdsRef = useRef<Map<string, string>>(new Map());

  const loadWorkspace = useCallback(async () => {
    if (pipelineId === null) {
      setWorkspace(null);
      setWorkOrderResult(null);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const data = await getScriptWorkspace(pipelineId);
      setWorkspace(data);
      setWorkOrderResult(scriptFactoryToWorkOrderResult(data.script_factory));
      setLatestScriptPayload(null);
      setVoiceoverConfirmed(false);
      setQualityIssue(null);
    } catch (loadError) {
      setError(toMessage(loadError, "脚本工作台读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace, refreshKey]);

  useEffect(() => {
    if (
      pipelineId === null ||
      !workspace ||
      workspace.script_factory.status !== "not_built" ||
      autoBuildPipelineRef.current === pipelineId ||
      workOrderAction
    ) {
      return;
    }
    autoBuildPipelineRef.current = pipelineId;
    void handleBuildScriptWorkOrder();
  }, [pipelineId, workspace, workOrderAction]);

  async function handleRewrite(part: string) {
    if (pipelineId === null) return;
    setRewritingPart(part);
    setNotice("");
    setError("");
    try {
      const result = await rewriteScriptPart(pipelineId, part);
      setNotice(`${result.part_label}已重写：${result.message}`);
      setRefreshKey((value) => value + 1);
    } catch (rewriteError) {
      setError(toMessage(rewriteError, "局部重写失败。"));
    } finally {
      setRewritingPart("");
    }
  }

  async function handleSaveScriptDraft(payload: ScriptWorkspaceUpdatePayload) {
    if (pipelineId === null) return;
    setSavingDraft(true);
    setNotice("");
    setError("");
    try {
      const updated = await updateScriptWorkspace(pipelineId, payload);
      setWorkspace(updated);
      setWorkOrderResult(scriptFactoryToWorkOrderResult(updated.script_factory));
      setVoiceoverConfirmed(false);
      setQualityIssue(null);
      setNotice("脚本修改已保存，后续素材任务和剪辑清单会基于最新脚本重跑。");
    } catch (saveError) {
      setError(toMessage(saveError, "脚本修改保存失败。"));
      throw saveError;
    } finally {
      setSavingDraft(false);
    }
  }

  async function handleConfirmVoiceover() {
    if (pipelineId === null) return;
    setNotice("");
    setError("");
    setQualityIssue(null);
    setVoiceoverFlowState("checking");
    try {
      if (latestScriptPayload) {
        const updated = await updateScriptWorkspace(pipelineId, latestScriptPayload);
        setWorkspace(updated);
      }
      const checked = await checkPipelineContentQuality(pipelineId);
      if (!canAutoContinueAfterQuality(checked)) {
        setQualityIssue(checked);
        setVoiceoverConfirmed(false);
        setError(`质检发现需要修改：${checked.max_issue}`);
        onOpenContentQuality?.(pipelineId);
        return;
      }
      setVoiceoverFlowState("advancing");
      await continuePipeline(pipelineId);
      setVoiceoverConfirmed(true);
      setNotice("脚本质检通过，已自动进入素材处理素材任务。");
      onOpenMaterials?.(pipelineId);
    } catch (confirmError) {
      setError(toMessage(confirmError, "脚本确认后的自动质检或推进失败。"));
    } finally {
      setVoiceoverFlowState("");
    }
  }

  async function handleToggleAvatar(useAvatar: boolean) {
    if (pipelineId === null) return;
    setNotice("");
    setError("");
    try {
      const updated = await updateScriptPresentationOptions(pipelineId, {
        use_avatar: useAvatar,
      });
      setWorkspace(updated);
      setNotice(useAvatar ? "本期已启用数字人字段。" : "本期已改为配音+字幕模式，不把数字人作为必需任务。");
    } catch (toggleError) {
      setError(toMessage(toggleError, "数字人开关保存失败。"));
    }
  }

  async function handleGenerateAudio() {
    if (pipelineId === null) return;
    setGeneratingAudio(true);
    setNotice("");
    setError("");
    try {
      const result = await generatePipelineVoiceover(pipelineId);
      if (result.success) {
        setNotice(`配音和字幕已生成：${result.audio_path}`);
      } else {
        setError(result.message);
      }
      setRefreshKey((value) => value + 1);
    } catch (audioError) {
      setError(toMessage(audioError, "配音和字幕生成失败。"));
    } finally {
      setGeneratingAudio(false);
    }
  }

  async function handleBuildScriptWorkOrder() {
    if (pipelineId === null) return;
    setWorkOrderAction("building");
    setNotice("");
    setError("");
    try {
      const result = await buildScriptWorkOrder(pipelineId);
      setWorkOrderResult(result);
      setNotice(`Codex 脚本任务包已生成：${result.prompt_path || result.file_path}`);
      await loadWorkspace();
    } catch (buildError) {
      setError(toMessage(buildError, "Codex 脚本任务包生成失败。"));
    } finally {
      setWorkOrderAction("");
    }
  }

  async function handleDispatchScriptWorkOrder() {
    if (pipelineId === null) return;
    setWorkOrderAction("dispatching");
    setNotice("");
    setError("");
    try {
      const result = await dispatchScriptWorkOrder(pipelineId);
      setWorkOrderResult(result);
      if (result.failure_reason) {
        setError(result.failure_reason);
      }
      setNotice(result.next_suggestion || "请按手动方案新建 Codex 对话。");
      await loadWorkspace();
    } catch (dispatchError) {
      setError(toMessage(dispatchError, "Codex 线程派发失败。"));
    } finally {
      setWorkOrderAction("");
    }
  }

  async function handleImportScriptWorkOrder() {
    if (pipelineId === null) return;
    setWorkOrderAction("importing");
    setNotice("");
    setError("");
    try {
      const result = await importScriptWorkOrderResult(pipelineId);
      setWorkOrderResult(result);
      setQualityIssue(null);
      setNotice(`脚本结果已导入：${result.result_path}`);
      await loadWorkspace();
    } catch (importError) {
      setError(toMessage(importError, "脚本结果导入失败。"));
    } finally {
      setWorkOrderAction("");
    }
  }

  async function handleAcceptScriptWorkOrder() {
    if (pipelineId === null) return;
    setWorkOrderAction("accepting");
    setNotice("");
    setError("");
    try {
      const result = await acceptScriptWorkOrder(pipelineId);
      setWorkOrderResult(result);
      if (!result.accepted) {
        setQualityIssue(result.quality_check);
        setError(result.failure_reason || "脚本验收未通过。");
        onOpenContentQuality?.(pipelineId);
        return;
      }
      setQualityIssue(null);
      setNotice(result.next_suggestion || "脚本已验收通过，已进入素材流程。");
      await loadWorkspace();
      onOpenMaterials?.(pipelineId);
    } catch (acceptError) {
      setError(toMessage(acceptError, "脚本验收失败。"));
    } finally {
      setWorkOrderAction("");
    }
  }

  async function handleRestoreScriptVersion(source: ScriptVersionSummary) {
    if (pipelineId === null || !workspace) return;
    const current = (workspace.script_versions ?? []).find(
      (version) => version.version_id === workspace.active_script_version_id,
    );
    if (
      !current ||
      !isSha256(source.content_hash) ||
      !isOptionalSha256(source.snapshot_hash) ||
      !isSha256(current.content_hash) ||
      !isOptionalSha256(current.snapshot_hash) ||
      !isSha256(workspace.script_state_sha256)
    ) {
      setError("脚本版本证据不完整，不能安全恢复。请刷新后再试。");
      return;
    }
    const operationKey = `${pipelineId}:${source.version_id}`;
    const operationId =
      restoreOperationIdsRef.current.get(operationKey) ?? createOperationId();
    restoreOperationIdsRef.current.set(operationKey, operationId);
    setRestoringVersionId(source.version_id);
    setNotice("");
    setError("");
    try {
      const result = await restoreScriptVersionAsDraft(
        pipelineId,
        source.version_id,
        {
          operation_id: operationId,
          source_content_hash: source.content_hash,
          source_snapshot_hash: source.snapshot_hash ?? "",
          expected_current_version_id: current.version_id,
          expected_current_content_hash: current.content_hash,
          expected_current_snapshot_hash: current.snapshot_hash ?? "",
          expected_script_state_sha256: workspace.script_state_sha256,
        },
      );
      restoreOperationIdsRef.current.delete(operationKey);
      setWorkspace(result.workspace);
      setWorkOrderResult(
        scriptFactoryToWorkOrderResult(result.workspace.script_factory),
      );
      setLatestScriptPayload(null);
      setVoiceoverConfirmed(false);
      setQualityIssue(null);
      setNotice(
        `已从版本 ${source.version_number} 创建版本 ${result.operation.restored_version_number} 草稿。旧版本仍保留；重新生成任务包并验收后才能进入素材。`,
      );
    } catch (restoreError) {
      if (restoreError instanceof ApiError && restoreError.status === 409) {
        restoreOperationIdsRef.current.delete(operationKey);
        await loadWorkspace();
        setError("项目已变化，已刷新脚本状态；请重新选择要恢复的版本。");
      } else if (restoreError instanceof ApiError && restoreError.status === 503) {
        setError(
          "恢复结果正在核对。请保留当前页面后重试；系统会沿用同一个 operation_id，不会重复创建版本。",
        );
      } else {
        setError(toMessage(restoreError, "历史脚本恢复失败。"));
      }
      throw restoreError;
    } finally {
      setRestoringVersionId("");
    }
  }

  return (
    <main className="app-shell workspace-shell">
      <header className="page-header workspace-header">
        <div>
          <h1>脚本</h1>
          {workspace && (
            <p
              className="workspace-project-context"
              aria-label={`当前项目：${workspace.project_name}`}
            >
              {workspace.project_name}
            </p>
          )}
        </div>
        <div className="workspace-header-actions">
          <button
            className="button button-secondary"
            type="button"
            aria-label="刷新当前项目状态"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading || pipelineId === null}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新
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

      {pipelineId === null ? (
        <div className="empty-state empty-state-stacked workspace-empty">
          <strong>还没有打开脚本项目。</strong>
          <span>请先从任务或选题里打开一个已经生成脚本的项目。</span>
        </div>
      ) : loading && !workspace ? (
        <div className="empty-state workspace-empty">
          <LoaderCircle className="spin" size={24} aria-hidden="true" />
          正在读取脚本内容...
        </div>
      ) : workspace ? (
        <>
          {(() => {
            const activeWorkOrder =
              workOrderResult ?? scriptFactoryToWorkOrderResult(workspace.script_factory);
            const canReviewScript = scriptFactoryCanReview(activeWorkOrder);
            return (
              <>
           <CodexWorkOrderPanel
            action={workOrderAction}
            result={activeWorkOrder}
            canAccept={
              Boolean(workspace.script.voiceover) &&
              activeWorkOrder?.status === "imported"
            }
            onBuild={() => void handleBuildScriptWorkOrder()}
            onDispatch={() => void handleDispatchScriptWorkOrder()}
            onImport={() => void handleImportScriptWorkOrder()}
             onAccept={() => void handleAcceptScriptWorkOrder()}
           />
           {(workspace.script_versions?.length ?? 0) >= 2 ? (
             <ScriptVersionComparePanel
               key={workspace.script_versions?.map((version) => version.version_id).join(":")}
               pipelineId={workspace.pipeline_id}
               versions={workspace.script_versions ?? []}
               activeVersionId={workspace.active_script_version_id ?? ""}
               restoringVersionId={restoringVersionId}
               onRestore={handleRestoreScriptVersion}
             />
           ) : null}
           {canReviewScript ? (
            <>
          <VoiceoverDecisionPanel
            workspace={workspace}
            confirmed={voiceoverConfirmed}
            flowState={voiceoverFlowState}
            qualityIssue={qualityIssue}
            onConfirm={() => void handleConfirmVoiceover()}
            onToggleAvatar={(useAvatar) => void handleToggleAvatar(useAvatar)}
            onGenerateAudio={() => void handleGenerateAudio()}
            onRevise={() => {
              setNotice("可先在下方修改字幕、分镜、标题和发布文案，再确认脚本。");
            }}
           onOpenQuality={() =>
               pipelineId !== null && onOpenContentQuality?.(pipelineId)
             }
             generatingAudio={generatingAudio}
           />
           <ShotDirectorPanel workspace={workspace} />
           <section className="workspace-content">
            <ScriptPanel
              workspace={workspace}
              onSave={handleSaveScriptDraft}
              saving={savingDraft}
              onRewrite={handleRewrite}
              rewritingPart={rewritingPart}
              onDraftChange={setLatestScriptPayload}
            />
          </section>
            </>
          ) : (
            <ScriptFactoryWaitingPanel result={activeWorkOrder} />
          )}
              </>
            );
          })()}
        </>
      ) : (
        <div className="empty-state empty-state-stacked workspace-empty">
          <strong>脚本工作台没有读取成功。</strong>
          <span>确认后端服务正常后，可以重新读取当前项目。</span>
          <button
            className="button button-primary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
          >
            <RefreshCw size={16} aria-hidden="true" />
            重新读取
          </button>
        </div>
      )}
    </main>
  );
}

function CodexWorkOrderPanel({
  action,
  result,
  canAccept,
  onBuild,
  onDispatch,
  onImport,
  onAccept,
}: {
  action: ScriptWorkOrderAction;
  result: ScriptWorkOrderResult | null;
  canAccept: boolean;
  onBuild: () => void;
  onDispatch: () => void;
  onImport: () => void;
  onAccept: () => void;
}) {
  const busy = Boolean(action);
  const hasJob = Boolean(result?.job_dir || result?.file_path);
  const hasResultFile = Boolean(result?.result_exists);
  const finished = result?.status === "imported" || result?.status === "accepted";
  const needsFreshWorkOrder = result?.status === "draft_edited";
  const canDispatch = hasJob && !finished && !needsFreshWorkOrder;
  const canImport = Boolean(
    (hasResultFile || result?.status === "result_found") &&
      !finished &&
      !needsFreshWorkOrder,
  );
  const displayPath =
    canImport || result?.status === "imported" || result?.status === "accepted"
      ? result?.result_path || result?.prompt_path || result?.file_path || ""
      : result?.prompt_path || result?.file_path || result?.result_path || "";
  return (
    <section className="codex-work-order-panel" aria-label="Codex 脚本任务包">
      <div className="codex-work-order-header">
        <div>
          <h2>Codex 脚本任务包</h2>
          <span className="status-pill status-pill-manual">Manual / Agent 协助</span>
        </div>
        <div className="codex-work-order-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={onBuild}
            disabled={busy}
          >
            {action === "building" ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <FileText size={16} aria-hidden="true" />
            )}
            {action === "building" ? "生成中" : "生成 Codex 脚本任务包"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={onDispatch}
            disabled={busy || !canDispatch}
          >
            {action === "dispatching" ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Send size={16} aria-hidden="true" />
            )}
            {action === "dispatching" ? "派发中" : "派发 Codex 线程"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={onImport}
            disabled={busy || !canImport}
          >
            {action === "importing" ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Upload size={16} aria-hidden="true" />
            )}
            {action === "importing" ? "导入中" : "从结果文件导入"}
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={onAccept}
            disabled={busy || !canAccept}
          >
            {action === "accepting" ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <ShieldCheck size={16} aria-hidden="true" />
            )}
            {action === "accepting" ? "验收中" : "验收并进入素材"}
          </button>
        </div>
      </div>
      {result ? (
        <div className="codex-work-order-result" role="status">
          <strong>{workOrderStatusLabel(result.status)}</strong>
          <code>{displayPath}</code>
          {result.real_ai_called ? (
            <span className="status-pill status-pill-blocked">Real AI</span>
          ) : (
            <span className="status-pill">No API</span>
          )}
          {result.job_dir ? <code>{result.job_dir}</code> : null}
          {result.result_path && !hasResultFile && result.status !== "imported" ? (
            <span className="codex-work-order-failure">
              <AlertTriangle size={14} aria-hidden="true" />
              结果文件还没生成，Codex 没有输出完成。
            </span>
          ) : null}
          {needsFreshWorkOrder ? (
            <span className="codex-work-order-failure">
              <AlertTriangle size={14} aria-hidden="true" />
              当前是手动修改草稿；旧任务包不能验收，请重新生成任务包。
            </span>
          ) : null}
          {result.next_suggestion ? (
            <span className="codex-work-order-next">
              <ArrowRight size={14} aria-hidden="true" />
              {result.next_suggestion}
            </span>
          ) : null}
          {result.failure_reason ? (
            <span className="codex-work-order-failure">
              <AlertTriangle size={14} aria-hidden="true" />
              {result.failure_reason}
            </span>
          ) : null}
        </div>
      ) : null}
      {result?.manual_prompt ? (
        <textarea
          className="codex-work-order-import"
          aria-label="手动新建 Codex 对话内容"
          value={result.manual_prompt}
          rows={5}
          readOnly
        />
      ) : null}
    </section>
  );
}

function ScriptFactoryWaitingPanel({
  result,
}: {
  result: ScriptWorkOrderResult | null;
}) {
  const rows = scriptFactoryStatusRows(result);
  return (
    <section className="script-factory-waiting-panel" aria-label="脚本工厂真实状态">
      <div>
        <h2>等待 Codex 脚本结果</h2>
        <strong>{scriptFactoryPrimaryStatus(result)}</strong>
      </div>
      <div className="script-factory-status-grid">
        {rows.map((row) => (
          <div className="script-factory-status-item" key={row.label}>
            <span>{row.label}</span>
            <strong>{row.value}</strong>
          </div>
        ))}
      </div>
      {result?.result_path ? (
        <code className="script-factory-result-path">{result.result_path}</code>
      ) : null}
    </section>
  );
}

function VoiceoverDecisionPanel({
  workspace,
  confirmed,
  flowState,
  qualityIssue,
  onConfirm,
  onToggleAvatar,
  onGenerateAudio,
  onRevise,
  onOpenQuality,
  generatingAudio,
}: {
  workspace: ScriptWorkspaceData;
  confirmed: boolean;
  flowState: VoiceoverFlowState;
  qualityIssue: ContentQualityResult | null;
  onConfirm: () => void;
  onToggleAvatar: (useAvatar: boolean) => void;
  onGenerateAudio: () => void;
  onRevise: () => void;
  onOpenQuality: () => void;
  generatingAudio: boolean;
}) {
  const useAvatar = workspace.script.use_avatar;
  const summaryItems = [
    {
      label: "分镜",
      value: `${scriptSegmentCount(workspace)} 段`,
    },
    {
      label: "素材关键词",
      value: `${materialKeywordCount(workspace)} 个`,
    },
    {
      label: "数字人字段",
      value: useAvatar
        ? `${requiredAvatarCount(workspace)} / ${workspace.avatar_tasks.length} 段`
        : "未启用",
    },
    {
      label: "剪辑节奏",
      value: editingRhythmText(workspace),
    },
  ];
  return (
    <section className="voiceover-decision-panel" aria-label="脚本确认">
      <div>
        <h2>脚本确认</h2>
        <label className="avatar-option-toggle">
          <input
            type="checkbox"
            checked={useAvatar}
            onChange={(event) => onToggleAvatar(event.target.checked)}
          />
          <span>添加数字人</span>
        </label>
      </div>
      <div className="voiceover-summary-grid" aria-label="脚本联动摘要">
        {summaryItems.map((item) => (
          <div
            className="voiceover-summary-card"
            key={item.label}
          >
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </div>
        ))}
      </div>
      <div className="voiceover-actions">
        <button
          className="button button-primary"
          type="button"
          onClick={onConfirm}
          disabled={confirmed || !workspace.script.voiceover || Boolean(flowState)}
        >
          {flowState ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <ClipboardCheck size={16} aria-hidden="true" />
          )}
          {voiceoverConfirmLabel(confirmed, flowState)}
        </button>
        <button className="button button-secondary" type="button" onClick={onRevise}>
          需要修改
        </button>
        <button
          className="button button-secondary"
          type="button"
          onClick={onGenerateAudio}
          disabled={generatingAudio || !workspace.script.voiceover}
        >
          {generatingAudio ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <Mic2 size={16} aria-hidden="true" />
          )}
          生成配音字幕
        </button>
      </div>
      {qualityIssue ? (
        <div className="voiceover-quality-alert" role="status">
          <strong>质检未通过</strong>
          <span>{qualityIssue.max_issue}</span>
          <button className="button button-secondary" type="button" onClick={onOpenQuality}>
            查看质检
          </button>
        </div>
      ) : null}
    </section>
  );
}

function ScriptVersionComparePanel({
  pipelineId,
  versions,
  activeVersionId,
  restoringVersionId,
  onRestore,
}: {
  pipelineId: number;
  versions: ScriptVersionSummary[];
  activeVersionId: string;
  restoringVersionId: string;
  onRestore: (version: ScriptVersionSummary) => Promise<void>;
}) {
  const selectableVersions = [...versions].sort(
    (left, right) => right.version_number - left.version_number,
  );
  const [leftVersionId, setLeftVersionId] = useState(
    selectableVersions[0]?.version_id ?? "",
  );
  const [rightVersionId, setRightVersionId] = useState(
    selectableVersions[1]?.version_id ?? "",
  );
  const [details, setDetails] = useState<
    [ScriptVersionDetail, ScriptVersionDetail] | null
  >(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [confirmingVersionId, setConfirmingVersionId] = useState("");
  const confirmingVersion = versions.find(
    (version) => version.version_id === confirmingVersionId,
  );
  const nextVersionNumber =
    Math.max(...versions.map((version) => version.version_number), 0) + 1;

  async function loadComparison() {
    if (!leftVersionId || !rightVersionId || leftVersionId === rightVersionId) return;
    setLoading(true);
    setError("");
    try {
      const result = await Promise.all([
        getScriptVersionDetail(pipelineId, leftVersionId),
        getScriptVersionDetail(pipelineId, rightVersionId),
      ]);
      setDetails(result);
    } catch (loadError) {
      setDetails(null);
      setError(toMessage(loadError, "脚本版本对比读取失败。"));
    } finally {
      setLoading(false);
    }
  }

  async function confirmRestore() {
    if (!confirmingVersion) return;
    try {
      await onRestore(confirmingVersion);
      setConfirmingVersionId("");
    } catch {
      // The page-level alert keeps the actionable server error visible.
    }
  }

  return (
    <section className="workspace-section" aria-label="脚本版本只读对比">
      <div className="workspace-section-header">
        <div>
          <h2>脚本版本对比</h2>
          <p className="workspace-section-note">
            历史版本保持只读；恢复会复制成新草稿，不覆盖旧版本，也不会自动批准。
          </p>
        </div>
      </div>
      <div className="codex-work-order-actions">
        <label>
          对比版本 A
          <select
            aria-label="对比版本 A"
            value={leftVersionId}
            onChange={(event) => {
              setLeftVersionId(event.target.value);
              setDetails(null);
            }}
          >
            {selectableVersions.map((version) => (
              <option key={version.version_id} value={version.version_id}>
                版本 {version.version_number} · {version.status}
              </option>
            ))}
          </select>
        </label>
        <label>
          对比版本 B
          <select
            aria-label="对比版本 B"
            value={rightVersionId}
            onChange={(event) => {
              setRightVersionId(event.target.value);
              setDetails(null);
            }}
          >
            {selectableVersions.map((version) => (
              <option key={version.version_id} value={version.version_id}>
                版本 {version.version_number} · {version.status}
              </option>
            ))}
          </select>
        </label>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => void loadComparison()}
          disabled={loading || !leftVersionId || leftVersionId === rightVersionId}
        >
          {loading ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : null}
          {loading ? "正在加载" : "加载版本对比"}
        </button>
      </div>
      {leftVersionId === rightVersionId ? (
        <p className="empty-state-inline">请选择两个不同版本。</p>
      ) : null}
      {error ? (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      ) : null}
      {details ? (
        <>
          <ScriptVersionDifferenceSummary left={details[0]} right={details[1]} />
          <div className="voiceover-summary-grid script-version-compare-grid" aria-label="脚本版本对比结果">
            {details.map((detail) => (
              <ScriptVersionDetailCard
                key={detail.version_id}
                detail={detail}
                active={detail.version_id === activeVersionId}
                restoring={restoringVersionId === detail.version_id}
                onRequestRestore={() => setConfirmingVersionId(detail.version_id)}
              />
            ))}
          </div>
        </>
      ) : null}
      {confirmingVersion ? (
        <div
          className="script-version-restore-confirmation"
          role="alertdialog"
          aria-label={`确认恢复脚本版本 ${confirmingVersion.version_number}`}
        >
          <h3>确认恢复脚本版本 {confirmingVersion.version_number}</h3>
          <p>
            系统会创建新的版本 {nextVersionNumber} 草稿；当前批准会失效，素材和剪辑结果需要重新核对。
          </p>
          <p>历史版本不会被修改，也不会自动批准或进入素材。</p>
          <div className="codex-work-order-actions">
            <button
              className="button button-secondary"
              type="button"
              onClick={() => setConfirmingVersionId("")}
              disabled={Boolean(restoringVersionId)}
            >
              取消
            </button>
            <button
              className="button button-primary"
              type="button"
              onClick={() => void confirmRestore()}
              disabled={Boolean(restoringVersionId)}
            >
              {restoringVersionId ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : null}
              {restoringVersionId ? "正在恢复" : "确认恢复为新草稿"}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function ScriptVersionDetailCard({
  detail,
  active,
  restoring,
  onRequestRestore,
}: {
  detail: ScriptVersionDetail;
  active: boolean;
  restoring: boolean;
  onRequestRestore: () => void;
}) {
  const voiceover =
    typeof detail.payload.voiceover === "string" && detail.payload.voiceover.trim()
      ? detail.payload.voiceover
      : "暂无口播全文";
  return (
    <article className="voiceover-summary-card script-version-compare-card">
      <h3>版本 {detail.version_number}</h3>
      <p>状态：{scriptVersionStatusLabel(detail.status)}</p>
      <p>来源：{detail.source || "未记录"}</p>
      <p>创建时间：{detail.created_at || "未记录"}</p>
      <p>版本完整性：{scriptVersionIntegrityLabel(detail.integrity_status)}</p>
      {detail.status === "approved" ? (
        <>
          <p>批准绑定：{scriptApprovalIntegrityLabel(detail.approval_integrity_status)}</p>
          <p>
            验收证据：
            {detail.approval_evidence_hash
              ? "已绑定脚本结果与质量检查哈希"
              : "未绑定，不能用于生产"}
          </p>
        </>
      ) : null}
      {detail.approved_at ? <p>批准时间：{detail.approved_at}</p> : null}
      {detail.invalidated_at ? <p>失效时间：{detail.invalidated_at}</p> : null}
      {detail.invalidated_reason ? <p>失效原因：{detail.invalidated_reason}</p> : null}
      <p className="script-version-voiceover">{voiceover}</p>
      <p>事实来源：{detail.fact_sources.length} 条</p>
      <p>分镜：{detail.shot_plan.length} 个</p>
      {active ? (
        <span className="status-pill">当前脚本</span>
      ) : (
        <button
          className="button button-secondary"
          type="button"
          onClick={onRequestRestore}
          disabled={restoring}
          aria-label={`恢复版本 ${detail.version_number} 为新草稿`}
        >
          {restoring ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : null}
          {restoring
            ? "正在恢复"
            : `恢复版本 ${detail.version_number} 为新草稿`}
        </button>
      )}
    </article>
  );
}

function ScriptVersionDifferenceSummary({
  left,
  right,
}: {
  left: ScriptVersionDetail;
  right: ScriptVersionDetail;
}) {
  const comparisons = [
    ["口播", left.payload.voiceover, right.payload.voiceover],
    ["分镜脚本", left.payload.storyboard, right.payload.storyboard],
    ["字幕", left.payload.subtitles, right.payload.subtitles],
    ["动画提示", left.payload.animation_prompts, right.payload.animation_prompts],
    ["标题", left.payload.title_options, right.payload.title_options],
    ["封面文案", left.payload.cover_copy, right.payload.cover_copy],
    ["发布文案", left.payload.publish_copy, right.payload.publish_copy],
    ["事实来源", left.fact_sources, right.fact_sources],
    ["ShotPlan", left.shot_plan, right.shot_plan],
  ] as const;
  const changed = comparisons.filter(
    ([, leftValue, rightValue]) =>
      canonicalJson(leftValue) !== canonicalJson(rightValue),
  );
  return (
    <div className="script-version-difference-summary" aria-label="版本字段差异摘要">
      <strong>{changed.length ? `${changed.length} 项有变化` : "字段内容一致"}</strong>
      <span>
        {changed.length
          ? changed.map(([label]) => label).join("、")
          : "口播、分镜、字幕、文案、事实来源和 ShotPlan 均一致"}
      </span>
    </div>
  );
}

function scriptVersionStatusLabel(status: string): string {
  if (status === "approved") return "已批准";
  if (status === "draft") return "草稿";
  if (status === "invalidated") return "已失效";
  return status || "未知";
}

function scriptVersionIntegrityLabel(status?: string): string {
  if (status === "full_envelope_verified") return "v2 完整哈希已验证";
  if (status === "legacy_payload_only") {
    return "旧版仅校验脚本内容，ShotPlan 不可用于生产";
  }
  return "未知，不能用于生产";
}

function scriptApprovalIntegrityLabel(status?: string): string {
  if (status === "snapshot_hash_bound") return "已绑定完整快照哈希";
  if (status === "legacy_version_id_only") {
    return "旧批准未绑定完整快照，不可用于生产";
  }
  return "未验证，不能用于生产";
}

function ShotDirectorPanel({ workspace }: { workspace: ScriptWorkspaceData }) {
  const plan = workspace.shot_plan ?? [];
  const versions = workspace.script_versions ?? [];
  const activeVersion = workspace.active_script_version_id;
  const activeVersionSummary = versions.find(
    (version) => version.version_id === activeVersion,
  );

  return (
    <section className="workspace-section shot-director-panel" aria-label="Shot Director 语义分镜">
      <div className="workspace-section-header">
        <div>
          <h2>Shot Director 语义分镜</h2>
          <p className="workspace-section-note">
            按观众要理解的语义组织画面；不会按标点机械切镜头。
          </p>
        </div>
        <div className="shot-director-version-meta">
          <span>{versions.length ? `版本 ${versions.length}` : "预览模式"}</span>
          {activeVersion ? <code>当前版本 {activeVersion}</code> : null}
          {activeVersionSummary ? (
            <span>{scriptVersionIntegrityLabel(activeVersionSummary.integrity_status)}</span>
          ) : null}
        </div>
      </div>
      {plan.length ? (
        <div className="shot-director-list">
          {plan.map((shot) => (
            <article className="shot-director-card" key={shot.beat_id || shot.shot_id}>
              <div className="shot-director-card-header">
                <strong>{shot.beat_id || shot.shot_id}</strong>
                <span className="status-badge status-neutral">
                  {shot.beat_type} · {shotEvidenceLabel(shot.evidence_kind)}
                </span>
                <small>
                  {shot.start}–{shot.end} · {shot.visual_type || "待定画面"}
                </small>
              </div>
              <p>{shot.viewer_takeaway || "尚未填写观众任务"}</p>
              <div className="shot-director-details">
                <span>画面：{shot.visual_intent || "待补充"}</span>
                <span>口播：{shot.voiceover || "待补充"}</span>
              </div>
              {shotSemanticDetails(shot).length ? (
                <div className="semantic-shot-detail-grid">
                  {shotSemanticDetails(shot).map((detail) => (
                    <span key={detail}>{detail}</span>
                  ))}
                </div>
              ) : null}
              {shot.source_refs.length ? (
                <small className="shot-director-source">
                  来源已记录：{shot.source_refs.map((source) => source.url).join("、")}
                </small>
              ) : null}
              {shot.acceptance_conditions.length ? (
                <div className="shot-director-rule-block">
                  <strong>通过条件</strong>
                  <ul className="shot-director-conditions">
                    {shot.acceptance_conditions.map((condition) => (
                      <li key={condition}>{condition}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {shot.forbidden_items.length ? (
                <div className="shot-director-rule-block shot-director-rule-warning">
                  <strong>禁止项</strong>
                  <ul className="shot-director-conditions">
                    {shot.forbidden_items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      ) : (
        <p className="empty-state-inline">导入真实脚本结果后，这里会出现可审核的语义分镜。</p>
      )}
    </section>
  );
}

type ShotPlanView = NonNullable<ScriptWorkspaceData["shot_plan"]>[number];

function shotSemanticDetails(shot: ShotPlanView): string[] {
  return [
    shot.subject ? `主体：${shot.subject}` : "",
    shot.action ? `动作：${shot.action}` : "",
    shot.location ? `地点：${shot.location}` : "",
    shot.time_context ? `时间：${shot.time_context}` : "",
    shot.composition ? `构图：${shot.composition}` : "",
    shot.sound_notes ? `声音：${shot.sound_notes}` : "",
  ].filter(Boolean);
}

function shotEvidenceLabel(value: string): string {
  if (value === "direct_evidence") return "直接证据";
  if (value === "explanation") return "解释画面";
  if (value === "atmosphere") return "气氛画面";
  return value || "待分类";
}

function ScriptPanel({
  workspace,
  onSave,
  saving,
  onRewrite,
  rewritingPart,
  onDraftChange,
}: {
  workspace: ScriptWorkspaceData;
  onSave: (payload: ScriptWorkspaceUpdatePayload) => Promise<void>;
  saving: boolean;
  onRewrite: (part: string) => Promise<void>;
  rewritingPart: string;
  onDraftChange: (payload: ScriptWorkspaceUpdatePayload) => void;
}) {
  const [draft, setDraft] = useState<ScriptDraft>(() => scriptToDraft(workspace));
  const [draftError, setDraftError] = useState("");

  useEffect(() => {
    setDraft(scriptToDraft(workspace));
    setDraftError("");
  }, [workspace]);

  useEffect(() => {
    onDraftChange(draftToPayload(draft));
  }, [draft, onDraftChange]);

  async function handleSaveDraft() {
    setDraftError("");
    try {
      await onSave(draftToPayload(draft));
    } catch (saveError) {
      setDraftError(toMessage(saveError, "脚本修改保存失败。"));
    }
  }

  function updateSegment(index: number, patch: Partial<ScriptSegmentDraft>) {
    setDraft((current) => ({
      ...current,
      segments: current.segments.map((segment, segmentIndex) =>
        segmentIndex === index ? { ...segment, ...patch } : segment,
      ),
    }));
  }

  function addSegment() {
    setDraft((current) => ({
      ...current,
      segments: [
        ...current.segments,
        {
          start: "",
          end: "",
          visual: "",
          voiceover: "",
        },
      ],
    }));
  }

  function removeSegment(index: number) {
    setDraft((current) => {
      if (current.segments.length <= 1) return current;
      return {
        ...current,
        segments: current.segments.filter((_, segmentIndex) => segmentIndex !== index),
      };
    });
  }

  return (
    <div className="script-panel">
      <WorkspaceSection
        title="字幕 / 脚本分镜"
        actions={
          <>
            <RewriteButton
              part="voiceover"
              label="重写口播"
              rewritingPart={rewritingPart}
              onRewrite={onRewrite}
            />
            <RewriteButton
              part="storyboard"
              label="重写分镜"
              rewritingPart={rewritingPart}
              onRewrite={onRewrite}
            />
            <button
              className="button button-secondary compact-button"
              type="button"
              onClick={addSegment}
            >
              <Plus size={15} aria-hidden="true" />
              添加一段
            </button>
          </>
        }
      >
        <div className="script-segment-grid">
          <div className="script-segment-head" aria-hidden="true">
            <span>时间</span>
            <span>画面</span>
            <span>字幕（可配音）</span>
          </div>
          {draft.segments.map((segment, index) => (
            <div className="script-segment-row" key={`segment-${index}`}>
              <div className="segment-time-fields">
                <div className="segment-time-range">
                  <input
                    aria-label={`编辑第${index + 1}段开始时间`}
                    value={segment.start}
                    onChange={(event) =>
                      updateSegment(index, { start: event.target.value })
                    }
                    placeholder="开始"
                  />
                  <span aria-hidden="true">-</span>
                  <input
                    aria-label={`编辑第${index + 1}段结束时间`}
                    value={segment.end}
                    onChange={(event) =>
                      updateSegment(index, { end: event.target.value })
                    }
                    placeholder="结束"
                  />
                </div>
                {draft.segments.length > 1 ? (
                  <button
                    className="button button-secondary segment-remove-button"
                    type="button"
                    onClick={() => removeSegment(index)}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                    删除
                  </button>
                ) : null}
              </div>
              <AutoGrowTextarea
                className="segment-visual-editor"
                aria-label={`编辑第${index + 1}段画面`}
                value={segment.visual}
                onChange={(value) => updateSegment(index, { visual: value })}
                placeholder="画面说明"
              />
              <AutoGrowTextarea
                className="segment-voiceover-editor"
                aria-label={`编辑第${index + 1}段字幕口播`}
                value={segment.voiceover}
                onChange={(value) => updateSegment(index, { voiceover: value })}
                placeholder="字幕（可配音）"
              />
            </div>
          ))}
        </div>
      </WorkspaceSection>
      <div className="script-options-grid">
        <WorkspaceSection
          title="标题"
          actions={
            <RewriteButton
              part="title"
              label="重写标题"
              rewritingPart={rewritingPart}
              onRewrite={onRewrite}
            />
          }
        >
          <input
            className="workspace-editor single-line-editor"
            aria-label="编辑标题"
            value={draft.titleText}
            onChange={(event) =>
              setDraft((value) => ({
                ...value,
                titleText: singleLineText(event.target.value),
              }))
            }
            placeholder="一句话标题"
          />
        </WorkspaceSection>
        <WorkspaceSection
          title="发布文案"
          actions={
            <RewriteButton
              part="publish_copy"
              label="重写文案"
              rewritingPart={rewritingPart}
              onRewrite={onRewrite}
            />
          }
        >
          <input
            className="workspace-editor single-line-editor"
            aria-label="编辑发布文案"
            value={draft.publishCopyText}
            onChange={(event) =>
              setDraft((value) => ({
                ...value,
                publishCopyText: singleLineText(event.target.value),
              }))
            }
            placeholder="一句话发布文案"
          />
        </WorkspaceSection>
      </div>
      {draftError ? (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {draftError}
        </p>
      ) : null}
      <div className="script-save-bar">
        <button
          className="button button-primary"
          type="button"
          onClick={() => void handleSaveDraft()}
          disabled={saving}
        >
          {saving ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : null}
          保存脚本修改
        </button>
        <span>保存后，素材任务、字幕文件和剪辑清单会按最新脚本重新衔接。</span>
      </div>
    </div>
  );
}

type RewritePart = "voiceover" | "storyboard" | "title" | "publish_copy";

interface ScriptSegmentDraft {
  start: string;
  end: string;
  visual: string;
  voiceover: string;
}

interface ScriptDraft {
  segments: ScriptSegmentDraft[];
  titleText: string;
  coverCopy: Array<Record<string, unknown>>;
  publishCopyText: string;
  useAvatar: boolean;
}

function canAutoContinueAfterQuality(result: ContentQualityResult): boolean {
  return result.publish_suggestion === "可以发布" && result.risk_level !== "高";
}

function voiceoverConfirmLabel(
  confirmed: boolean,
  flowState: VoiceoverFlowState,
): string {
  if (flowState === "checking") return "自动质检中";
  if (flowState === "advancing") return "继续生成中";
  return confirmed ? "已确认" : "确认脚本";
}

function workOrderStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    not_built: "任务包未生成",
    built: "任务包已生成",
    manual_dispatch_required: "需要手动新建 Codex 对话",
    dispatched: "已派发",
    result_found: "已发现结果文件",
    imported: "脚本结果已导入",
    draft_edited: "手动修改草稿",
    accepted: "脚本已验收",
    blocked: "脚本被阻止",
    failed: "脚本工厂失败",
  };
  return labels[status] || status;
}

function scriptFactoryToWorkOrderResult(
  state?: ScriptFactoryState,
): ScriptWorkOrderResult | null {
  if (!state || (state.status === "not_built" && !state.job_dir)) {
    return null;
  }
  return {
    pipeline_id: 0,
    script_asset_id: null,
    status: state.status,
    job_id: state.job_id,
    job_dir: state.job_dir,
    file_path: state.file_path,
    prompt_path: state.prompt_path,
    context_path: state.context_path,
    template_path: state.template_path,
    result_path: state.result_path,
    result_exists: state.result_exists,
    real_ai_called: state.real_ai_called,
    failure_reason: state.failure_reason,
    next_suggestion: state.next_suggestion,
    quality_check: null,
    accepted: state.accepted,
    dispatch_supported: state.dispatch_supported,
    thread_id: state.thread_id,
    manual_prompt: state.manual_prompt,
  };
}

function scriptFactoryCanReview(result: ScriptWorkOrderResult | null): boolean {
  return Boolean(
    result && ["imported", "draft_edited", "accepted", "blocked"].includes(result.status),
  );
}

function scriptFactoryPrimaryStatus(result: ScriptWorkOrderResult | null): string {
  if (!result) return "正在生成任务包，暂时没有脚本结果。";
  if (result.status === "built") return "任务包已生成，但还没有派发给 Codex。";
  if (result.status === "manual_dispatch_required") return "没有在跑，需要手动或 Agent 新建 Codex 对话。";
  if (result.status === "dispatched") return "已派发，正在等待结果文件。";
  if (result.status === "result_found") return "结果文件已出现，可以导入。";
  if (result.status === "draft_edited") {
    return "当前是手动修改草稿；旧任务包不能验收，请重新生成任务包。";
  }
  if (result.status === "failed") return "脚本工厂失败，需要先处理失败原因。";
  return "还没有可审阅的 Codex 脚本结果。";
}

function scriptFactoryStatusRows(result: ScriptWorkOrderResult | null) {
  return [
    {
      label: "任务包",
      value: result?.job_dir ? "已生成" : "未生成",
    },
    {
      label: "Codex 派发",
      value:
        result?.status === "dispatched"
          ? "已派发"
          : result?.status === "manual_dispatch_required"
            ? "需要手动/Agent"
            : "未派发",
    },
    {
      label: "是否在跑",
      value: result?.status === "dispatched" ? "等待中" : "没有在跑",
    },
    {
      label: "结果文件",
      value: result?.result_exists ? "已生成" : "未生成",
    },
  ];
}

function scriptSegmentCount(workspace: ScriptWorkspaceData): number {
  return Math.max(
    workspace.script.storyboard.length,
    workspace.script.subtitles.length,
    workspace.script.voiceover.split(/\r?\n/).filter((line) => line.trim()).length,
  );
}

function materialKeywordCount(workspace: ScriptWorkspaceData): number {
  return new Set(
    workspace.material_tasks.flatMap((task) =>
      (task.asset_keywords?.length ? task.asset_keywords : task.jianying_keywords)
        .map((keyword) => keyword.trim())
        .filter(Boolean),
    ),
  ).size;
}

function requiredAvatarCount(workspace: ScriptWorkspaceData): number {
  return workspace.avatar_tasks.filter((task) => task.required).length;
}

function editingRhythmText(workspace: ScriptWorkspaceData): string {
  const segmentCount = scriptSegmentCount(workspace);
  const durationSeconds = workspace.script.storyboard.reduce((total, shot) => {
    const start = parseTimestamp(value(shot.start));
    const end = parseTimestamp(value(shot.end));
    return end > start ? total + (end - start) : total;
  }, 0);
  if (durationSeconds > 0) {
    return `${segmentCount} 段 / ${formatDuration(durationSeconds)}`;
  }
  if (workspace.topic.recommended_duration) {
    return `${segmentCount} 段 / ${workspace.topic.recommended_duration}`;
  }
  return `${segmentCount} 段`;
}

function parseTimestamp(text: string): number {
  const parts = text
    .trim()
    .split(":")
    .map((part) => Number(part));
  if (!parts.length || parts.some((part) => Number.isNaN(part))) return 0;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts.slice(-3).reduce((total, part) => total * 60 + part, 0);
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes}分${rest}秒` : `${minutes}分`;
}

function scriptToDraft(workspace: ScriptWorkspaceData): ScriptDraft {
  const storyboard = workspace.script.storyboard || [];
  const subtitles = workspace.script.subtitles || [];
  const maxCount = Math.max(storyboard.length, subtitles.length);
  const segments =
    maxCount > 0
      ? Array.from({ length: maxCount }, (_, index) => {
          const shot = storyboard[index] || {};
          const subtitle = subtitles[index] || {};
          return {
            start: value(shot.start) || value(subtitle.start),
            end: value(shot.end) || value(subtitle.end),
            visual: value(shot.recommended_visual),
            voiceover: value(shot.voiceover) || value(subtitle.text),
          };
        })
      : [
          {
            start: "",
            end: "",
            visual: "",
            voiceover: workspace.script.voiceover || "",
          },
        ];
  return {
    segments,
    titleText: firstNonEmpty(
      workspace.script.title_options.map((item) => value(item.title)),
      workspace.script.title,
    ),
    coverCopy: workspace.script.cover_copy,
    publishCopyText: firstNonEmpty(Object.values(workspace.script.publish_copy).map(value)),
    useAvatar: workspace.script.use_avatar,
  };
}

function draftToPayload(draft: ScriptDraft): ScriptWorkspaceUpdatePayload {
  const activeSegments = draft.segments.filter(
    (segment) =>
      segment.voiceover.trim() ||
      segment.visual.trim() ||
      segment.start.trim() ||
      segment.end.trim(),
  );
  return {
    voiceover: activeSegments
      .map((segment) => segment.voiceover.trim())
      .filter(Boolean)
      .join("\n"),
    storyboard: activeSegments.map((segment, index) => ({
      shot_id: `shot-${String(index + 1).padStart(2, "0")}`,
      start: segment.start.trim(),
      end: segment.end.trim(),
      voiceover: segment.voiceover.trim(),
      recommended_visual: segment.visual.trim(),
    })),
    subtitles: activeSegments.map((segment) => ({
      start: segment.start.trim(),
      end: segment.end.trim(),
      text: segment.voiceover.trim(),
    })),
    title_options: singleLineText(draft.titleText)
      ? [{ title: singleLineText(draft.titleText), type: "手动编辑" }]
      : [],
    cover_copy: draft.coverCopy,
    publish_copy: singleLineText(draft.publishCopyText)
      ? { 通用: singleLineText(draft.publishCopyText) }
      : {},
    use_avatar: draft.useAvatar,
  };
}

function RewriteButton({
  part,
  label,
  rewritingPart,
  onRewrite,
}: {
  part: RewritePart;
  label: string;
  rewritingPart: string;
  onRewrite: (part: string) => Promise<void>;
}) {
  return (
    <button
      className="button button-secondary compact-button"
      type="button"
      onClick={() => void onRewrite(part)}
      disabled={Boolean(rewritingPart)}
    >
      {rewritingPart === part ? (
        <LoaderCircle className="spin" size={15} aria-hidden="true" />
      ) : null}
      {label}
    </button>
  );
}

function AutoGrowTextarea({
  className,
  "aria-label": ariaLabel,
  value,
  placeholder,
  onChange,
}: {
  className: string;
  "aria-label": string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [value]);

  return (
    <textarea
      ref={textareaRef}
      className={className}
      aria-label={ariaLabel}
      value={value}
      rows={1}
      onChange={(event) => {
        onChange(event.target.value);
        resizeTextarea(event.currentTarget);
      }}
      placeholder={placeholder}
    />
  );
}

function WorkspaceSection({
  title,
  actions,
  children,
}: {
  title: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="workspace-section">
      <div className="workspace-section-header">
        <h3>{title}</h3>
        {actions ? <div className="workspace-section-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

function value(input: unknown): string {
  if (typeof input === "string" || typeof input === "number") {
    return String(input);
  }
  return "";
}

function firstNonEmpty(values: string[], fallback = ""): string {
  return values.map(singleLineText).find(Boolean) || singleLineText(fallback);
}

function singleLineText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function resizeTextarea(textarea: HTMLTextAreaElement | null) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function createOperationId(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function isSha256(value: string | undefined): value is string {
  return /^[0-9a-f]{64}$/.test(value ?? "");
}

function isOptionalSha256(value: string | undefined): boolean {
  return !value || isSha256(value);
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return item;
    return Object.fromEntries(
      Object.entries(item as Record<string, unknown>).sort(([left], [right]) =>
        left.localeCompare(right),
      ),
    );
  });
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default ScriptWorkspace;
