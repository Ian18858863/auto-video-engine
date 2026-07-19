import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Ban,
  ExternalLink,
  LockKeyhole,
  SkipForward,
  LoaderCircle,
  Play,
  RefreshCw,
  Upload,
} from "lucide-react";

import {
  ApiError,
  getMaterialPackage,
  lockMaterialCandidate,
  reselectMaterialCandidate,
  replaceMaterialCandidate,
  rejectMaterialCandidate,
  reviewMaterialCandidateRights,
  reconcileAndRetryMaterialTask,
  runNextMaterialTask,
  skipMaterialTask,
} from "./api";
import type {
  MaterialMediaValidation,
  MaterialPackageData,
  MaterialRightsReviewFlag,
  MaterialRightsReviewInput,
  MaterialRightsReviewStatus,
  MaterialThirdPartyClearance,
} from "./types";
import "./styles.css";

const RUNNABLE_MATERIAL_STATUSES = new Set(["待执行"]);
const ACTIVE_MATERIAL_STATUSES = new Set([
  "进行中",
  "官方页面录屏中",
  "Pexels下载中",
  "Pixabay下载中",
  "Coverr下载中",
  "信息动效生成中",
]);

function MaterialPackagePage({
  pipelineId,
  onOpenProjects,
}: {
  pipelineId: number | null;
  onOpenProjects?: () => void;
}) {
  const [data, setData] = useState<MaterialPackageData | null>(null);
  const [loading, setLoading] = useState(false);
  const [runningTask, setRunningTask] = useState(false);
  const [error, setError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [candidateAction, setCandidateAction] = useState("");
  const packageReadVersion = useRef(0);

  const loadPackage = useCallback(async () => {
    if (pipelineId === null) {
      packageReadVersion.current += 1;
      setData(null);
      setLoading(false);
      return;
    }
    const readVersion = ++packageReadVersion.current;
    setLoading(true);
    setError("");
    try {
      const nextData = await getMaterialPackage(pipelineId);
      if (readVersion === packageReadVersion.current) {
        setData(nextData);
      }
    } catch (loadError) {
      if (readVersion === packageReadVersion.current) {
        setError(toMessage(loadError, "素材读取失败。"));
      }
    } finally {
      if (readVersion === packageReadVersion.current) {
        setLoading(false);
      }
    }
  }, [pipelineId]);

  useEffect(() => {
    void loadPackage();
  }, [loadPackage]);

  useEffect(() => {
    if (!runningTask || pipelineId === null) return undefined;
    const timer = window.setInterval(() => {
      void loadPackage();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadPackage, pipelineId, runningTask]);

  const runNextTask = useCallback(async () => {
    if (pipelineId === null) return;
    setRunningTask(true);
    setError("");
    setActionMessage("");
    try {
      const result = await runNextMaterialTask(pipelineId);
      setActionMessage(result.message || "素材任务已更新。");
      await loadPackage();
    } catch (runError) {
      const message = toMessage(runError, "素材任务执行失败。");
      if (requiresStateReadback(runError)) {
        await loadPackage();
      }
      setError(message);
    } finally {
      setRunningTask(false);
    }
  }, [loadPackage, pipelineId]);

  const reconcileTask = useCallback(async (shotId: string) => {
    if (pipelineId === null) return;
    setRunningTask(true);
    setError("");
    setActionMessage("");
    try {
      const result = await reconcileAndRetryMaterialTask(pipelineId, shotId);
      setActionMessage(result.message || "上次执行状态已核查。");
      await loadPackage();
    } catch (reconcileError) {
      const message = toMessage(reconcileError, "上次执行状态核查失败。");
      if (requiresStateReadback(reconcileError)) {
        await loadPackage();
      }
      setError(message);
    } finally {
      setRunningTask(false);
    }
  }, [loadPackage, pipelineId]);

  const skipTask = useCallback(async (shotId: string) => {
    if (pipelineId === null) return;
    setRunningTask(true);
    setError("");
    setActionMessage("");
    try {
      const result = await skipMaterialTask(pipelineId, shotId);
      setActionMessage(result.message || "已跳过这条素材任务。");
      await loadPackage();
    } catch (skipError) {
      const message = toMessage(skipError, "素材任务跳过失败。");
      if (requiresStateReadback(skipError)) {
        await loadPackage();
      }
      setError(message);
    } finally {
      setRunningTask(false);
    }
  }, [loadPackage, pipelineId]);

  const updateCandidatePool = useCallback(
    (shotId: string, candidatePool: NonNullable<MaterialPackageData["material_tasks"][number]["candidate_pool"]>) => {
      // A mutation response is authoritative.  Ignore any slower package read
      // that began before it so stale data cannot visually undo a durable
      // lock, rejection, rights review, replacement, or reselection.
      packageReadVersion.current += 1;
      setLoading(false);
      setData((current) =>
        current
          ? {
              ...current,
              material_tasks: current.material_tasks.map((task) =>
                task.shot_id === shotId ? { ...task, candidate_pool: candidatePool } : task,
              ),
            }
          : current,
      );
    },
    [],
  );

  const lockCandidate = useCallback(
    async (shotId: string, candidateId: string, reason: string) => {
      if (pipelineId === null) return;
      setCandidateAction(`${shotId}:${candidateId}:lock`);
      setError("");
      setActionMessage("");
      try {
        const candidatePool = await lockMaterialCandidate(
          pipelineId,
          shotId,
          candidateId,
          reason,
        );
        updateCandidatePool(shotId, candidatePool);
        setActionMessage(`已锁定候选 ${candidateId}，重跑不会覆盖它。`);
      } catch (candidateError) {
        if (requiresStateReadback(candidateError)) {
          await loadPackage();
        }
        setError(toMessage(candidateError, "锁定候选失败。"));
      } finally {
        setCandidateAction("");
      }
    },
    [loadPackage, pipelineId, updateCandidatePool],
  );

  const rejectCandidate = useCallback(
    async (shotId: string, candidateId: string, reason: string) => {
      if (pipelineId === null) return;
      setCandidateAction(`${shotId}:${candidateId}:reject`);
      setError("");
      setActionMessage("");
      try {
        const candidatePool = await rejectMaterialCandidate(
          pipelineId,
          shotId,
          candidateId,
          reason,
        );
        updateCandidatePool(shotId, candidatePool);
        setActionMessage(`已拒绝候选 ${candidateId}，原因已保存。`);
      } catch (candidateError) {
        if (requiresStateReadback(candidateError)) {
          await loadPackage();
        }
        setError(toMessage(candidateError, "拒绝候选失败。"));
      } finally {
        setCandidateAction("");
      }
    },
    [loadPackage, pipelineId, updateCandidatePool],
  );

  const reselectCandidate = useCallback(
    async (
      shotId: string,
      candidateId: string,
      reason: string,
      expectedRevision: number,
    ) => {
      if (pipelineId === null) return;
      setCandidateAction(`${shotId}:${candidateId}:reselect`);
      setError("");
      setActionMessage("");
      try {
        const candidatePool = await reselectMaterialCandidate(
          pipelineId,
          shotId,
          candidateId,
          reason,
          expectedRevision,
        );
        updateCandidatePool(shotId, candidatePool);
        setActionMessage(
          `已改选 ${shotId} 的镜头素材；请到剪辑页重建，新旧版本都会保留。`,
        );
      } catch (candidateError) {
        if (requiresStateReadback(candidateError)) {
          await loadPackage();
        }
        setError(toMessage(candidateError, "改选镜头素材失败。"));
      } finally {
        setCandidateAction("");
      }
    },
    [loadPackage, pipelineId, updateCandidatePool],
  );

  const reviewCandidateRights = useCallback(
    async (
      shotId: string,
      candidateId: string,
      payload: MaterialRightsReviewInput,
    ) => {
      if (pipelineId === null) return;
      setCandidateAction(`${shotId}:${candidateId}:rights-review`);
      setError("");
      setActionMessage("");
      try {
        const candidatePool = await reviewMaterialCandidateRights(
          pipelineId,
          shotId,
          candidateId,
          payload,
        );
        updateCandidatePool(shotId, candidatePool);
        setActionMessage(`候选 ${candidateId} 的本次用途清权结果已保存。`);
      } catch (candidateError) {
        if (requiresStateReadback(candidateError)) {
          await loadPackage();
        }
        setError(toMessage(candidateError, "素材清权结果保存失败。"));
      } finally {
        setCandidateAction("");
      }
    },
    [loadPackage, pipelineId, updateCandidatePool],
  );

  const replaceCandidate = useCallback(
    async (
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
    ) => {
      if (pipelineId === null) return false;
      setCandidateAction(`${shotId}:${candidateId}:replace`);
      setError("");
      setActionMessage("");
      try {
        const candidatePool = await replaceMaterialCandidate(
          pipelineId,
          shotId,
          candidateId,
          payload,
        );
        updateCandidatePool(shotId, candidatePool);
        setActionMessage("候选已替换并保存；锁定的候选不会被覆盖。 ");
        return true;
      } catch (candidateError) {
        if (requiresStateReadback(candidateError)) {
          await loadPackage();
        }
        setError(toMessage(candidateError, "替换候选失败。"));
        return false;
      } finally {
        setCandidateAction("");
      }
    },
    [loadPackage, pipelineId, updateCandidatePool],
  );

  const matchedMaterialTasks =
    data?.material_tasks.filter(
      (task) =>
        Boolean(task.material_path) &&
        !["发布收口中", "被阻止", "失败", "已拒绝", "已跳过"].includes(
          materialStatus(task),
        ),
    ) || [];
  const hasRunnableTask =
    data?.material_tasks.some((task) => isRunnableMaterialTask(task)) || false;
  const activeTask = data?.material_tasks.find((task) =>
    ACTIVE_MATERIAL_STATUSES.has(materialStatus(task)),
  );
  const publishingTask = data?.material_tasks.find(
    (task) => materialStatus(task) === "发布收口中",
  );
  const recoveryTask = data?.material_tasks.find(
    (task) =>
      task.recovery_action_available === true ||
      materialStatus(task) === "被阻止",
  );
  const primaryActionLabel = runningTask
    ? "处理中"
    : publishingTask
      ? "正在发布"
      : activeTask
        ? "正在获取"
        : "开始/继续素材获取";

  return (
    <main className="app-shell workspace-shell">
      <header className="page-header">
        <div>
          <h1>素材</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void loadPackage()}
            disabled={loading || runningTask || pipelineId === null}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void runNextTask()}
            disabled={runningTask || pipelineId === null || !hasRunnableTask}
          >
            {runningTask ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Play size={16} aria-hidden="true" />
            )}
            {primaryActionLabel}
          </button>
          {recoveryTask && (
            <button
              className="button button-secondary"
              type="button"
              onClick={() => void reconcileTask(recoveryTask.shot_id)}
              disabled={
                runningTask ||
                (materialStatus(recoveryTask) === "被阻止" &&
                  recoveryTask.retry_available !== true)
              }
            >
              <RefreshCw size={16} aria-hidden="true" />
              {recoveryActionLabel(recoveryTask)}
            </button>
          )}
        </div>
      </header>

      {error && (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      )}

      {actionMessage && (
        <p className="message message-success">
          {actionMessage}
        </p>
      )}

      {pipelineId === null ? (
        <div className="empty-state empty-state-stacked">
          <strong>还没有打开项目。</strong>
          <span>请先从项目列表打开一个已生成素材任务的项目。</span>
          <button
            className="button button-primary"
            type="button"
            onClick={onOpenProjects}
          >
            去项目列表
          </button>
        </div>
      ) : loading && !data ? (
        <div className="empty-state">
          <LoaderCircle className="spin" size={22} aria-hidden="true" />
          正在读取素材...
        </div>
      ) : data ? (
        <>
          <section className="workspace-section">
            <h3>素材任务</h3>
            {data.material_tasks.length === 0 ? (
              <p className="workspace-empty-line">素材任务尚未生成。</p>
            ) : (
              <div className="workspace-table-wrap material-task-table-wrap">
                <table className="workspace-table material-task-table">
                  <thead>
                    <tr>
                      <th>时间段</th>
                      <th>推荐画面</th>
                      <th>匹配方式</th>
                      <th>素材关键词</th>
                      <th>状态</th>
                      <th>结果/原因</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.material_tasks.map((task) => (
                      <tr key={task.shot_id}>
                        <td data-label="时间段">{task.time_range}</td>
                        <td className="material-task-cell-wide" data-label="推荐画面">
                          {task.recommended_visual}
                        </td>
                        <td className="material-task-cell-wide" data-label="匹配方式">
                          {materialStrategy(task)}
                        </td>
                        <td className="material-task-cell-wide" data-label="素材关键词">
                          {materialKeywords(task).join("、") || "无"}
                        </td>
                        <td data-label="状态">
                          <span
                            className={`status-badge ${materialStatusClass(task)}`}
                          >
                            {materialStatus(task)}
                          </span>
                        </td>
                        <td
                          className="material-reason-cell material-task-cell-wide"
                          data-label="结果/原因"
                        >
                          <div>{materialReason(task) || "等待执行"}</div>
                          {executionMetadata(task).map((entry) => (
                            <small key={entry} className="material-execution-meta">
                              {entry}
                            </small>
                          ))}
                        </td>
                        <td className="material-task-action material-task-cell-wide" data-label="操作">
                          <button
                            className="button button-secondary button-compact"
                            type="button"
                            onClick={() => void skipTask(task.shot_id)}
                            disabled={runningTask || !canSkipTask(task)}
                          >
                            <SkipForward size={15} aria-hidden="true" />
                            跳过
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <SemanticMaterialConstraints tasks={data.material_tasks} />

          <MaterialCandidatePools
            tasks={data.material_tasks}
            pendingAction={candidateAction}
            onLock={lockCandidate}
            onReselect={reselectCandidate}
            onReject={rejectCandidate}
            onReviewRights={reviewCandidateRights}
            onReplace={replaceCandidate}
          />

          <MaterialSelectionHistory
            history={data.material_selection_history ?? []}
          />

          <section className="workspace-section">
            <h3>素材预览</h3>
            {matchedMaterialTasks.length === 0 ? (
              <p className="workspace-empty-line">暂无已匹配素材，等待官方页面录屏、Pexels 或信息动效任务完成。</p>
            ) : (
              <div className="material-preview-grid">
                {matchedMaterialTasks.map((task) => (
                  <article className="material-preview-card" key={task.shot_id}>
                    <MatchedMaterialPreview task={task} />
                    <div className="material-preview-body">
                      <strong>{task.time_range || task.shot_id}</strong>
                      <p>{task.recommended_visual || "等待画面要求"}</p>
                      <div className="task-tags">
                        {materialKeywords(task).length ? (
                          materialKeywords(task).map((keyword) => (
                            <span key={keyword}>{keyword}</span>
                          ))
                        ) : (
                          <span>暂无关键词</span>
                        )}
                      </div>
                      <small>
                        {task.material_path
                          ? "已绑定项目素材；浏览器预览以候选池为准"
                          : "尚未绑定本地素材"}
                      </small>
                    </div>
                    <span className="status-badge status-complete">已完成</span>
                  </article>
                ))}
              </div>
            )}
          </section>

          <section className="workspace-section">
            <h3>图文补充任务</h3>
            {data.graphic_tasks.length === 0 ? (
              <p className="workspace-empty-line">图文补充任务尚未生成。</p>
            ) : (
              <div className="workspace-task-list">
                {data.graphic_tasks.map((task) => (
                  <article className="workspace-task-row" key={task.shot_id}>
                    <div>
                      <span>{task.time_range}</span>
                      <strong>{task.visual_form}</strong>
                      <p>{task.core_message}</p>
                    </div>
                    <div className="task-tags">
                      {task.keywords.map((keyword) => (
                        <span key={keyword}>{keyword}</span>
                      ))}
                    </div>
                    <div>
                      <strong>{task.status}</strong>
                      <p>{task.save_path || "尚无保存目录"}</p>
                      <small>
                        {task.codex_skill_executed
                          ? "已生成图文"
                          : "等待素材缺口确认"}
                      </small>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          <section className="workspace-section">
            <h3>数字人字段</h3>
            {!data.script.use_avatar ? (
              <p className="workspace-empty-line">本期未添加数字人，按配音和字幕推进。</p>
            ) : data.avatar_tasks.length === 0 ? (
              <p className="workspace-empty-line">数字人字段尚未生成。</p>
            ) : (
              <div className="workspace-task-list">
                {data.avatar_tasks.map((task) => (
                  <article className="workspace-task-row avatar-task" key={task.shot_id}>
                    <div>
                      <span>{task.shot_id}</span>
                      <strong>{task.position}</strong>
                      <p>{task.state}</p>
                    </div>
                    <div className="task-tags">
                      <span>{task.required ? "需要出镜" : "无需出镜"}</span>
                      <span>{task.avoid_subtitle ? "避开字幕" : "无需避让字幕"}</span>
                      <span>{task.avoid_chart ? "避开图表" : "无需避让图表"}</span>
                      <span>{task.lip_sync ? "口型同步" : "无需口型同步"}</span>
                    </div>
                    <div>
                      <strong>{task.generated ? "已有素材" : "素材未生成"}</strong>
                      <p>{task.material_path || "尚无数字人素材路径"}</p>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </>
      ) : (
        <div className="empty-state empty-state-stacked">
          <strong>素材没有读取成功。</strong>
          <span>确认后端服务正常后，可以重新读取当前项目素材。</span>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void loadPackage()}
          >
            <RefreshCw size={16} aria-hidden="true" />
            重新读取素材
          </button>
        </div>
      )}
    </main>
  );
}

function SemanticMaterialConstraints({
  tasks,
}: {
  tasks: MaterialTask[];
}) {
  const semanticTasks = tasks.filter(
    (task) =>
      task.beat_id ||
      task.beat_type ||
      task.viewer_takeaway ||
      task.acceptance_conditions?.length ||
      task.source_refs?.length,
  );

  return (
    <section className="workspace-section semantic-material-section">
      <div className="section-heading-row">
        <div>
          <h3>语义素材约束</h3>
          <p className="section-subtitle">
            素材任务必须服务于脚本分镜的观众任务，不合格画面不能因为“看起来像”而通过。
          </p>
        </div>
        <span className="section-kicker">Shot Director → 素材</span>
      </div>
      {semanticTasks.length === 0 ? (
        <p className="workspace-empty-line">当前项目还没有已批准的语义分镜版本。</p>
      ) : (
        <div className="semantic-material-grid">
          {semanticTasks.map((task) => (
            <article className="semantic-material-card" key={task.shot_id}>
              <div className="semantic-material-card-header">
                <strong>{task.shot_id}</strong>
                <span>{task.beat_type || "未分类"}</span>
              </div>
              <p className="semantic-material-takeaway">
                {task.viewer_takeaway || task.visual_intent || "未记录观众任务"}
              </p>
              {materialSemanticDetails(task).length ? (
                <div className="semantic-shot-detail-grid">
                  {materialSemanticDetails(task).map((detail) => (
                    <span key={detail}>{detail}</span>
                  ))}
                </div>
              ) : null}
              {task.evidence_kind && (
                <span className="task-tag task-tag-evidence">
                  {task.evidence_kind === "direct_evidence" ? "直接证据" : task.evidence_kind}
                </span>
              )}
              {task.source_refs?.length ? (
                <p className="semantic-material-meta">
                  来源：{task.source_refs.map((ref) => ref.url).join("、")}
                </p>
              ) : null}
              {task.acceptance_conditions?.length ? (
                <div className="semantic-material-rules">
                  <span>通过条件</span>
                  <ul>
                    {task.acceptance_conditions.map((condition) => (
                      <li key={condition}>{condition}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {task.forbidden_items?.length ? (
                <div className="semantic-material-rules semantic-material-rules-warning">
                  <span>禁止替代</span>
                  <ul>
                    {task.forbidden_items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function materialSemanticDetails(task: MaterialTask): string[] {
  return [
    task.subject ? `主体：${task.subject}` : "",
    task.action ? `动作：${task.action}` : "",
    task.location ? `地点：${task.location}` : "",
    task.time_context ? `时间：${task.time_context}` : "",
    task.composition ? `构图：${task.composition}` : "",
    task.sound_notes ? `声音：${task.sound_notes}` : "",
  ].filter(Boolean);
}

type MaterialTaskView = MaterialPackageData["material_tasks"][number];

function MaterialSelectionHistory({
  history,
}: {
  history: NonNullable<MaterialPackageData["material_selection_history"]>;
}) {
  if (history.length === 0) return null;
  return (
    <section className="workspace-section material-selection-history">
      <h3>历史素材锁定</h3>
      <p className="section-subtitle">
        上游变化不会抹掉旧选择；这里仅展示审计摘要，不能作为当前素材继续使用。
      </p>
      <div className="material-candidate-list">
        {history.map((entry) => (
          <article className="material-candidate-card" key={entry.entry_sha256}>
            <strong>{entry.reason || "上游变化使旧素材锁定失效"}</strong>
            <p>{entry.stale_at ? `失效时间：${entry.stale_at}` : "失效时间未记录"}</p>
            {entry.locked_selections.length ? (
              <ul>
                {entry.locked_selections.map((selection) => (
                  <li key={`${selection.shot_id}:${selection.candidate_id}`}>
                    {selection.shot_id} · {selection.candidate_id}
                    {selection.reason ? ` · ${selection.reason}` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              <p>本次历史没有已锁定候选。</p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}

function MatchedMaterialPreview({ task }: { task: MaterialTaskView }) {
  const candidate = task.candidate_pool?.candidates.find(
    (item) =>
      item.candidate_id === task.candidate_pool?.locked_candidate_id ||
      item.status === "locked",
  );
  const previewUrl = safePreviewPath(candidate?.preview_url);
  if (!previewUrl) {
    return (
      <div className="material-preview-thumb" role="status">
        <span>匹配结果暂无浏览器预览</span>
      </div>
    );
  }
  return (
    <div className="material-preview-thumb">
      {isVideoPreview(previewUrl, candidate?.preview_media_type) ? (
        <video
          src={previewUrl}
          controls
          playsInline
          preload="metadata"
          aria-label={`${task.shot_id} 匹配素材预览`}
        />
      ) : (
        <img src={previewUrl} alt={`${task.shot_id} 匹配素材预览`} />
      )}
    </div>
  );
}

function MaterialCandidatePools({
  tasks,
  pendingAction,
  onLock,
  onReselect,
  onReject,
  onReviewRights,
  onReplace,
}: {
  tasks: MaterialTaskView[];
  pendingAction: string;
  onLock: (shotId: string, candidateId: string, reason: string) => Promise<void>;
  onReselect: (
    shotId: string,
    candidateId: string,
    reason: string,
    expectedRevision: number,
  ) => Promise<void>;
  onReject: (shotId: string, candidateId: string, reason: string) => Promise<void>;
  onReviewRights: (
    shotId: string,
    candidateId: string,
    payload: MaterialRightsReviewInput,
  ) => Promise<void>;
  onReplace: (
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
  ) => Promise<boolean>;
}) {
  const candidateTasks = tasks.filter((task) => task.candidate_pool);

  return (
    <section className="workspace-section material-candidate-section">
      <div className="section-heading-row">
        <div>
          <h3>素材候选池</h3>
          <p className="section-subtitle">
            每个普通镜头先比较真实候选，再由 Ian 锁定；图库素材完成本次用途清权后才能导出。
          </p>
        </div>
        <span className="section-kicker">3–5 个候选</span>
      </div>
      {candidateTasks.length === 0 ? (
        <p className="workspace-empty-line">
          当前还没有通过合同校验的候选池；素材搜索完成后再进入人工比较。
        </p>
      ) : (
        <div className="material-candidate-list">
          {candidateTasks.map((task) => (
            <MaterialCandidateCard
              key={task.shot_id}
              task={task}
              pendingAction={pendingAction}
              onLock={onLock}
              onReselect={onReselect}
              onReject={onReject}
              onReviewRights={onReviewRights}
              onReplace={onReplace}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function MaterialCandidateCard({
  task,
  pendingAction,
  onLock,
  onReselect,
  onReject,
  onReviewRights,
  onReplace,
}: {
  task: MaterialTaskView;
  pendingAction: string;
  onLock: (shotId: string, candidateId: string, reason: string) => Promise<void>;
  onReselect: (
    shotId: string,
    candidateId: string,
    reason: string,
    expectedRevision: number,
  ) => Promise<void>;
  onReject: (shotId: string, candidateId: string, reason: string) => Promise<void>;
  onReviewRights: (
    shotId: string,
    candidateId: string,
    payload: MaterialRightsReviewInput,
  ) => Promise<void>;
  onReplace: (
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
  ) => Promise<boolean>;
}) {
  const pool = task.candidate_pool;
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [replacements, setReplacements] = useState<Record<string, ReplacementDraft>>({});
  const [previewErrors, setPreviewErrors] = useState<Record<string, boolean>>({});
  const [rightsDrafts, setRightsDrafts] = useState<
    Record<string, RightsReviewDraft>
  >({});
  const replacementFor = (candidateId: string): ReplacementDraft =>
    replacements[candidateId] || EMPTY_REPLACEMENT_DRAFT;
  const updateReplacement = (
    candidateId: string,
    patch: Partial<ReplacementDraft>,
  ) => {
    setReplacements((current) => ({
      ...current,
      [candidateId]: { ...replacementFor(candidateId), ...patch },
    }));
  };
  const clearReplacement = (candidateId: string) => {
    setReplacements((current) => {
      const next = { ...current };
      delete next[candidateId];
      return next;
    });
  };
  const rightsDraftFor = (candidateId: string): RightsReviewDraft =>
    rightsDrafts[candidateId] || EMPTY_RIGHTS_REVIEW_DRAFT;
  const updateRightsDraft = (
    candidateId: string,
    patch: Partial<RightsReviewDraft>,
  ) => {
    setRightsDrafts((current) => ({
      ...current,
      [candidateId]: { ...rightsDraftFor(candidateId), ...patch },
    }));
  };
  if (!pool) return null;
  const stale = pool.stale === true;

  return (
    <article className="material-candidate-card">
      <header className="material-candidate-header">
        <div>
          <strong>{task.shot_id}</strong>
          <span>{pool.candidates.length} 个跨来源候选 · 第 {pool.revision} 版</span>
        </div>
        {stale ? (
          <span className="status-badge status-warning">历史锁定已过期</span>
        ) : pool.locked_candidate_id ? (
          <span className="status-badge status-complete">
            已锁定 {pool.locked_candidate_id}
          </span>
        ) : (
          <span className="status-badge status-warning">待人工选择</span>
        )}
      </header>
      {stale ? (
        <p className="message message-warning" role="status">
          {pool.stale_reason || "上游脚本已经变化；旧锁定仅保留为历史，需重新生成并确认素材。"}
        </p>
      ) : pool.locked_candidate_id ? (
        <p className="message message-warning">
          改选后，现有剪辑输入会变旧；请到剪辑页重建新版本，旧素材记录仍会保留。
        </p>
      ) : null}
      <div className="material-candidate-grid">
        {pool.candidates.map((candidate) => {
          const reason = reasons[candidate.candidate_id] || "";
          const replacement = replacementFor(candidate.candidate_id);
          const busy = pendingAction.startsWith(`${task.shot_id}:${candidate.candidate_id}:`);
          const locked = candidate.status === "locked";
          const rejected = candidate.status === "rejected";
          const reselecting =
            !stale && Boolean(pool.locked_candidate_id) && !locked && !rejected;
          const previewUrl = stale
            ? ""
            : safePreviewPath(candidate.preview_url || candidate.preview_path);
          const previewFailed = previewErrors[candidate.candidate_id] === true;
          return (
            <article
              className={`material-candidate-item candidate-${candidate.status}`}
              key={candidate.candidate_id}
            >
              <div className="material-candidate-preview">
                {stale ? (
                  <span>历史预览已停用</span>
                ) : previewFailed ? (
                  <span role="status">预览加载失败，请重新上传预览。</span>
                ) : previewUrl ? (
                  isVideoPreview(previewUrl, candidate.preview_media_type) ? (
                    <video
                      src={previewUrl}
                      controls
                      playsInline
                      preload="metadata"
                      aria-label={`候选 ${candidate.candidate_id} 视频预览`}
                      onError={() =>
                        setPreviewErrors((current) => ({
                          ...current,
                          [candidate.candidate_id]: true,
                        }))
                      }
                    />
                  ) : (
                    <img
                      src={previewUrl}
                      alt={`候选 ${candidate.candidate_id} 预览`}
                      loading="lazy"
                      onError={() =>
                        setPreviewErrors((current) => ({
                          ...current,
                          [candidate.candidate_id]: true,
                        }))
                      }
                    />
                  )
                ) : (
                  <span>没有预览</span>
                )}
              </div>
              <div className="material-candidate-body">
                {!locked && !stale && (
                  <details className="material-candidate-replace">
                    <summary>
                      <Upload size={14} aria-hidden="true" />
                      替换候选
                    </summary>
                    <form
                      className="material-candidate-replace-form"
                      onSubmit={async (event) => {
                        event.preventDefault();
                        if (!replacement.file) return;
                        const replaced = await onReplace(task.shot_id, candidate.candidate_id, {
                          preview: replacement.file,
                          replacementCandidateId: replacement.candidateId || undefined,
                          source: replacement.source,
                          url: replacement.url,
                          rights: replacement.rights,
                          evidenceKind: replacement.evidenceKind,
                          reason: replacement.reason,
                        });
                        if (!replaced) return;
                        clearReplacement(candidate.candidate_id);
                        setPreviewErrors((current) => ({
                          ...current,
                          [candidate.candidate_id]: false,
                        }));
                      }}
                    >
                      <label>
                        预览文件
                        <input
                          type="file"
                          accept="image/*,video/mp4,video/webm,video/quicktime"
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              file: event.target.files?.[0] || null,
                            })
                          }
                        />
                      </label>
                      <label>
                        来源
                        <input
                          value={replacement.source}
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              source: event.target.value,
                            })
                          }
                          required
                        />
                      </label>
                      <label>
                        权利说明
                        <input
                          value={replacement.rights}
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              rights: event.target.value,
                            })
                          }
                          required
                        />
                      </label>
                      <label>
                        证据类型
                        <select
                          value={replacement.evidenceKind}
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              evidenceKind: event.target.value,
                            })
                          }
                        >
                          <option value="atmosphere">氛围</option>
                          <option value="direct_evidence">直接证据</option>
                          <option value="explanation">解释</option>
                          <option value="self_owned">自有</option>
                          <option value="licensed">已授权</option>
                          <option value="programmatic">程序生成</option>
                        </select>
                      </label>
                      <label>
                        来源链接（可选）
                        <input
                          type="url"
                          value={replacement.url}
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              url: event.target.value,
                            })
                          }
                        />
                      </label>
                      <label>
                        替换理由
                        <input
                          value={replacement.reason}
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              reason: event.target.value,
                            })
                          }
                          required
                        />
                      </label>
                      <label>
                        新候选 ID（可选）
                        <input
                          value={replacement.candidateId}
                          onChange={(event) =>
                            updateReplacement(candidate.candidate_id, {
                              candidateId: event.target.value,
                            })
                          }
                        />
                      </label>
                      <button
                        className="button button-secondary button-compact"
                        type="submit"
                        disabled={
                          busy ||
                          !replacement.file ||
                          !replacement.source.trim() ||
                          !replacement.rights.trim() ||
                          !replacement.reason.trim()
                        }
                      >
                        上传并替换
                      </button>
                    </form>
                  </details>
                )}
                <div className="material-candidate-title">
                  <strong>{candidate.candidate_id}</strong>
                  <span>{candidate.source}</span>
                </div>
                <div className="candidate-media-verification">
                  {candidate.media_validation ? (
                    <>
                      <span className="status-badge status-complete">媒体已验证</span>
                      <small>{mediaValidationSummary(candidate.media_validation)}</small>
                    </>
                  ) : (
                    <small>旧候选，锁定前会复核</small>
                  )}
                </div>
                {candidate.rights_evidence && (
                  <div className="candidate-rights-evidence">
                    <div className="candidate-rights-heading">
                      <span>来源与许可已留证</span>
                      <span
                        className={`status-badge ${rightsReviewClass(candidate.rights_evidence.rights_review.status)}`}
                      >
                        {rightsReviewLabel(candidate.rights_evidence.rights_review.status)}
                      </span>
                    </div>
                    <small>
                      {candidate.rights_evidence.license_snapshot.license_name} ·{" "}
                      {candidate.rights_evidence.contributor.name}
                    </small>
                    <details>
                      <summary>许可与署名要求</summary>
                      <p>{candidate.rights_evidence.attribution.api_display_requirement}</p>
                      <a
                        href={candidate.rights_evidence.license_snapshot.license_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <ExternalLink size={13} aria-hidden="true" />
                        查看许可原文
                      </a>
                    </details>
                    <details className="candidate-rights-review">
                      <summary>核对本次使用权利</summary>
                      <div className="candidate-rights-review-form">
                        <label>
                          本次用途
                          <input
                            value={rightsDraftFor(candidate.candidate_id).intendedUse}
                            onChange={(event) =>
                              updateRightsDraft(candidate.candidate_id, {
                                intendedUse: event.target.value,
                              })
                            }
                          />
                        </label>
                        <RightsFlagSelect
                          label="可识别人像"
                          value={rightsDraftFor(candidate.candidate_id).identifiablePeople}
                          onChange={(value) =>
                            updateRightsDraft(candidate.candidate_id, {
                              identifiablePeople: value,
                            })
                          }
                        />
                        <RightsFlagSelect
                          label="品牌或 Logo"
                          value={rightsDraftFor(candidate.candidate_id).brandsOrLogos}
                          onChange={(value) =>
                            updateRightsDraft(candidate.candidate_id, {
                              brandsOrLogos: value,
                            })
                          }
                        />
                        <RightsFlagSelect
                          label="场地或建筑"
                          value={rightsDraftFor(candidate.candidate_id).propertyOrArchitecture}
                          onChange={(value) =>
                            updateRightsDraft(candidate.candidate_id, {
                              propertyOrArchitecture: value,
                            })
                          }
                        />
                        <RightsFlagSelect
                          label="音乐或第三方音频"
                          value={rightsDraftFor(candidate.candidate_id).musicOrThirdPartyAudio}
                          onChange={(value) =>
                            updateRightsDraft(candidate.candidate_id, {
                              musicOrThirdPartyAudio: value,
                            })
                          }
                        />
                        <label>
                          第三方许可
                          <select
                            value={rightsDraftFor(candidate.candidate_id).thirdPartyClearance}
                            onChange={(event) =>
                              updateRightsDraft(candidate.candidate_id, {
                                thirdPartyClearance: event.target
                                  .value as MaterialThirdPartyClearance,
                              })
                            }
                          >
                            <option value="unknown">尚未判断</option>
                            <option value="not_required">不需要额外许可</option>
                            <option value="obtained">已取得额外许可</option>
                            <option value="required">仍需取得许可</option>
                          </select>
                        </label>
                        <label className="candidate-rights-notes">
                          限制备注
                          <textarea
                            value={rightsDraftFor(candidate.candidate_id).restrictionNotes}
                            onChange={(event) =>
                              updateRightsDraft(candidate.candidate_id, {
                                restrictionNotes: event.target.value,
                              })
                            }
                          />
                        </label>
                        <div className="candidate-rights-review-actions">
                          <button
                            className="button button-secondary button-compact"
                            type="button"
                            disabled={
                              stale ||
                              busy ||
                              !rightsDraftFor(candidate.candidate_id).intendedUse.trim()
                            }
                            onClick={() =>
                              void onReviewRights(
                                task.shot_id,
                                candidate.candidate_id,
                                rightsReviewPayload(
                                  rightsDraftFor(candidate.candidate_id),
                                  pool.revision,
                                  "needs_review",
                                ),
                              )
                            }
                          >
                            保存为待核查
                          </button>
                          <button
                            className="button button-primary button-compact"
                            type="button"
                            disabled={
                              stale ||
                              busy ||
                              !canClearRights(rightsDraftFor(candidate.candidate_id))
                            }
                            onClick={() =>
                              void onReviewRights(
                                task.shot_id,
                                candidate.candidate_id,
                                rightsReviewPayload(
                                  rightsDraftFor(candidate.candidate_id),
                                  pool.revision,
                                  "cleared_for_intended_use",
                                ),
                              )
                            }
                          >
                            确认本次用途已清权
                          </button>
                        </div>
                      </div>
                    </details>
                  </div>
                )}
                <p>{candidate.reason || "未填写匹配理由"}</p>
                <small>授权：{candidate.rights}</small>
                <small>类型：{candidate.evidence_kind}</small>
                {candidate.url && (
                  <a href={candidate.url} target="_blank" rel="noreferrer">
                    <ExternalLink size={13} aria-hidden="true" />
                    打开来源页
                  </a>
                )}
                <span className="candidate-status-text">
                  {stale
                    ? locked
                      ? "历史锁定（已过期）"
                      : "历史候选（已过期）"
                    : locked
                      ? "已锁定"
                      : rejected
                        ? "已拒绝"
                        : "可选"}
                </span>
                {!stale && !locked && !rejected && (
                  <input
                    aria-label={`候选 ${candidate.candidate_id} 操作理由`}
                    value={reason}
                    placeholder="填写选择或拒绝理由"
                    onChange={(event) =>
                      setReasons((current) => ({
                        ...current,
                        [candidate.candidate_id]: event.target.value,
                      }))
                    }
                  />
                )}
                <div className="material-candidate-actions">
                  <button
                    className="button button-primary button-compact"
                    type="button"
                    aria-label={`${reselecting ? "改选为候选" : "锁定候选"} ${candidate.candidate_id}`}
                    disabled={stale || busy || locked || rejected || !reason.trim()}
                    onClick={() => {
                      if (!reselecting) {
                        void onLock(task.shot_id, candidate.candidate_id, reason.trim());
                        return;
                      }
                      const confirmed = window.confirm(
                        `确认把 ${task.shot_id} 改选为 ${candidate.candidate_id}？现有剪辑输入会变旧，需到剪辑页重建；旧素材记录仍会保留。`,
                      );
                      if (!confirmed) return;
                      void onReselect(
                        task.shot_id,
                        candidate.candidate_id,
                        reason.trim(),
                        pool.revision,
                      );
                    }}
                  >
                    <LockKeyhole size={14} aria-hidden="true" />
                    {locked ? "已锁定" : reselecting ? "改选" : "锁定"}
                  </button>
                  <button
                    className="button button-secondary button-compact"
                    type="button"
                    aria-label={`拒绝候选 ${candidate.candidate_id}`}
                    disabled={stale || busy || locked || rejected || !reason.trim()}
                    onClick={() => void onReject(task.shot_id, candidate.candidate_id, reason.trim())}
                  >
                    <Ban size={14} aria-hidden="true" />
                    {rejected ? "已拒绝" : "拒绝"}
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </article>
  );
}

type ReplacementDraft = {
  candidateId: string;
  file: File | null;
  source: string;
  url: string;
  rights: string;
  evidenceKind: string;
  reason: string;
};

type RightsReviewDraft = {
  intendedUse: string;
  identifiablePeople: MaterialRightsReviewFlag;
  brandsOrLogos: MaterialRightsReviewFlag;
  propertyOrArchitecture: MaterialRightsReviewFlag;
  musicOrThirdPartyAudio: MaterialRightsReviewFlag;
  thirdPartyClearance: MaterialThirdPartyClearance;
  restrictionNotes: string;
};

const EMPTY_RIGHTS_REVIEW_DRAFT: RightsReviewDraft = {
  intendedUse: "当前项目短视频",
  identifiablePeople: "unknown",
  brandsOrLogos: "unknown",
  propertyOrArchitecture: "unknown",
  musicOrThirdPartyAudio: "unknown",
  thirdPartyClearance: "unknown",
  restrictionNotes: "",
};

function RightsFlagSelect({
  label,
  value,
  onChange,
}: {
  label: string;
  value: MaterialRightsReviewFlag;
  onChange: (value: MaterialRightsReviewFlag) => void;
}) {
  return (
    <label>
      {label}
      <select
        value={value}
        onChange={(event) =>
          onChange(event.target.value as MaterialRightsReviewFlag)
        }
      >
        <option value="unknown">尚未判断</option>
        <option value="no">没有</option>
        <option value="yes">有</option>
      </select>
    </label>
  );
}

function canClearRights(draft: RightsReviewDraft): boolean {
  const flags = [
    draft.identifiablePeople,
    draft.brandsOrLogos,
    draft.propertyOrArchitecture,
    draft.musicOrThirdPartyAudio,
  ];
  if (!draft.intendedUse.trim() || flags.includes("unknown")) return false;
  if (
    draft.thirdPartyClearance === "unknown" ||
    draft.thirdPartyClearance === "required"
  ) {
    return false;
  }
  return !flags.includes("yes") || draft.thirdPartyClearance === "obtained";
}

function rightsReviewPayload(
  draft: RightsReviewDraft,
  expectedRevision: number,
  status: MaterialRightsReviewInput["status"],
): MaterialRightsReviewInput {
  return {
    expected_revision: expectedRevision,
    status,
    intended_use: draft.intendedUse.trim(),
    identifiable_people: draft.identifiablePeople,
    brands_or_logos: draft.brandsOrLogos,
    property_or_architecture: draft.propertyOrArchitecture,
    music_or_third_party_audio: draft.musicOrThirdPartyAudio,
    third_party_clearance: draft.thirdPartyClearance,
    restriction_notes: draft.restrictionNotes
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean),
  };
}

const EMPTY_REPLACEMENT_DRAFT: ReplacementDraft = {
  candidateId: "",
  file: null,
  source: "",
  url: "",
  rights: "",
  evidenceKind: "atmosphere",
  reason: "",
};

function mediaValidationSummary(report: MaterialMediaValidation): string {
  const labels: Record<MaterialMediaValidation["mime_type"], string> = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WebP",
    "video/mp4": "MP4",
    "video/quicktime": "MOV",
    "video/webm": "WebM",
  };
  const duration =
    report.duration_seconds === null
      ? ""
      : ` · ${formatDuration(report.duration_seconds)}`;
  return `${labels[report.mime_type]} · ${report.width}×${report.height} · ${formatBytes(report.size_bytes)}${duration}`;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(value: number): string {
  return `${value.toFixed(value < 10 ? 1 : 0)} 秒`;
}

function rightsReviewLabel(status: MaterialRightsReviewStatus): string {
  return {
    unreviewed: "待人工清权",
    needs_review: "需要进一步核查",
    cleared_for_intended_use: "本次用途已清权",
    rejected: "权利审查未通过",
  }[status];
}

function rightsReviewClass(status: MaterialRightsReviewStatus): string {
  if (status === "cleared_for_intended_use") return "status-complete";
  if (status === "rejected") return "status-danger";
  return "status-warning";
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

type MaterialTask = MaterialPackageData["material_tasks"][number];

function materialStatus(task: MaterialTask): string {
  const persisted = task.status || "等待";
  if (
    [
      "发布收口中",
      "被阻止",
      "失败",
      "已拒绝",
      "已跳过",
      ...ACTIVE_MATERIAL_STATUSES,
    ].includes(persisted)
  ) {
    return persisted;
  }
  if (task.execution_state === "blocked") return "被阻止";
  if (
    task.execution_state === "publishing" ||
    task.publication_phase === "db_committed"
  ) {
    return "发布收口中";
  }
  if (task.material_path || persisted === "已完成") return "已完成";
  return persisted;
}

function materialStatusClass(task: MaterialTask): string {
  const status = materialStatus(task);
  if (status === "已完成") return "status-complete";
  if (status === "失败" || status === "已拒绝" || status === "被阻止") return "status-failed";
  if (status === "已跳过") return "status-warning";
  if (["待执行", "等待", "未开始", "待搜索"].includes(status)) return "status-warning";
  return "status-active";
}

function canSkipTask(task: MaterialTask): boolean {
  const status = materialStatus(task);
  return ![
    "已完成",
    "已跳过",
    "被阻止",
    "发布收口中",
    ...ACTIVE_MATERIAL_STATUSES,
  ].includes(status);
}

function isRunnableMaterialTask(task: MaterialTask): boolean {
  return !task.material_path && RUNNABLE_MATERIAL_STATUSES.has(materialStatus(task));
}

function materialReason(task: MaterialTask): string {
  if (task.reject_reason) return task.reject_reason;
  if (task.notes) return task.notes;
  if (task.material_path) return task.material_path;
  if (materialStatus(task) === "被阻止") return "需先确认旧进程已停止";
  return "";
}

function executionMetadata(task: MaterialTask): string[] {
  const entries: string[] = [];
  if (task.execution_state) entries.push(`执行状态：${task.execution_state}`);
  if (task.execution_id) entries.push(`执行 ID：${task.execution_id}`);
  if (task.active_attempt_id) entries.push(`尝试 ID：${task.active_attempt_id}`);
  if (task.publication_phase && task.publication_phase !== "none") {
    entries.push(`发布阶段：${task.publication_phase}`);
  }
  if (materialStatus(task) === "被阻止") {
    entries.push(task.retry_available === true ? "恢复证据可重试" : "恢复证据不足，需人工处理");
  } else if (task.recovery_action_available === true) {
    entries.push("可检查上次执行状态");
  }
  return entries;
}

function materialStrategy(task: MaterialTask): string {
  if (task.material_path) return task.source_type || task.material_type || "已匹配素材";
  const sources = [
    task.needs_official_capture ? "官方页面录屏" : "",
    task.needs_pexels ? "Pexels" : "",
    task.needs_pexels ? "Pixabay" : "",
    "备选素材",
    task.needs_info_motion ? "信息动效" : "",
  ].filter(Boolean);
  return sources.join(" / ") || "等待素材任务";
}

function recoveryActionLabel(task: MaterialTask): string {
  const status = materialStatus(task);
  if (status === "发布收口中") return "检查发布状态";
  if (status === "被阻止") {
    return task.retry_available === true ? "重新检查并重试" : "需要人工处理";
  }
  return "检查上次执行状态";
}

function materialKeywords(task: MaterialTask): string[] {
  return task.asset_keywords?.length ? task.asset_keywords : task.jianying_keywords;
}

function requiresStateReadback(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 409 || error.status === 503);
}

function safePreviewPath(value: string | undefined): string {
  // The API's canonical value is preview_url.  Keep the leading-slash fallback
  // only for older local fixtures; never place a D:/ or C:/ filesystem path in
  // a browser src attribute.
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "";
  if (value.includes("\\") || value.includes(":") || /\s/.test(value)) return "";
  return value;
}

function isVideoPreview(value: string, mediaType?: string): boolean {
  return (
    mediaType?.toLowerCase().startsWith("video/") === true ||
    /\.(?:mp4|m4v|mov|webm)(?:$|[?#])/i.test(value)
  );
}

export default MaterialPackagePage;
