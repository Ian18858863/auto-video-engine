import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clapperboard,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Clock3,
  FolderOpen,
  LoaderCircle,
  Play,
  RefreshCw,
  X,
} from "lucide-react";

import {
  continuePipeline,
  getPipelineDetail,
  getPipelines,
} from "./api";
import type {
  PipelineDetail,
  PipelineListItem,
  PipelineListResponse,
} from "./types";
import "./styles.css";

const statusOptions = ["", "未开始", "进行中", "已完成", "失败", "需要人工确认"];
const emptyList: PipelineListResponse = {
  items: [],
  pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
};

function ProjectListPage({
  initialPipelineId = null,
  onOpenWorkspace,
  onOpenMaterials,
  onOpenHotspots,
  onOpenEditing,
  onOpenFinished,
}: {
  initialPipelineId?: number | null;
  onOpenWorkspace?: (pipelineId: number) => void;
  onOpenMaterials?: (pipelineId: number) => void;
  onOpenHotspots?: () => void;
  onOpenEditing?: (pipelineId: number) => void;
  onOpenFinished?: (pipelineId: number) => void;
}) {
  const [projects, setProjects] = useState(emptyList);
  const [status, setStatus] = useState("");
  const [failedOnly, setFailedOnly] = useState(false);
  const [needsConfirmationOnly, setNeedsConfirmationOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<PipelineDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [continuingId, setContinuingId] = useState<number | null>(null);
  const [notice, setNotice] = useState("");
  const openedInitialPipeline = useRef<number | null>(null);

  const loadProjects = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getPipelines({
        status,
        failedOnly,
        needsConfirmationOnly,
        page,
        pageSize: 20,
      });
      setProjects(result);
    } catch (loadError) {
      setError(toMessage(loadError, "项目列表读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [failedOnly, needsConfirmationOnly, page, status]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects, refreshKey]);

  useEffect(() => {
    if (
      initialPipelineId !== null &&
      openedInitialPipeline.current !== initialPipelineId
    ) {
      openedInitialPipeline.current = initialPipelineId;
      void openDetail(initialPipelineId);
    }
  }, [initialPipelineId]);

  const resetPage = () => setPage(1);

  async function openDetail(pipelineId: number) {
    setDetailLoading(true);
    setError("");
    try {
      setSelected(await getPipelineDetail(pipelineId));
    } catch (detailError) {
      setError(toMessage(detailError, "项目详情读取失败。"));
    } finally {
      setDetailLoading(false);
    }
  }

  async function handleContinue(project: PipelineListItem) {
    setContinuingId(project.pipeline_id);
    setNotice("");
    setError("");
    try {
      const result = await continuePipeline(project.pipeline_id);
      setNotice(`「${project.project_name}」已继续执行，当前状态：${result.status}`);
      if (selected?.pipeline_id === project.pipeline_id) {
        setSelected(result);
      }
      await loadProjects();
    } catch (continueError) {
      setError(toMessage(continueError, "继续执行失败。"));
    } finally {
      setContinuingId(null);
    }
  }

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>项目列表</h1>
        </div>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => setRefreshKey((value) => value + 1)}
          disabled={loading}
        >
          <RefreshCw size={16} aria-hidden="true" />
          刷新状态
        </button>
      </header>

      <section className="summary-strip" aria-label="项目概况">
        <SummaryItem label="当前结果" value={projects.pagination.total} />
        <SummaryItem
          label="需要关注"
          value={projects.items.filter((project) => project.needs_confirmation).length}
          tone="warning"
        />
        <SummaryItem
          label="执行失败"
          value={projects.items.filter((project) => project.has_failed).length}
          tone="danger"
        />
      </section>

      <section className="filter-bar" aria-label="项目筛选">
        <label className="select-field">
          <span>按状态筛选</span>
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              resetPage();
            }}
          >
            {statusOptions.map((option) => (
              <option key={option || "全部"} value={option}>
                {option || "全部状态"}
              </option>
            ))}
          </select>
        </label>

        <label className="check-field">
          <input
            type="checkbox"
            checked={failedOnly}
            onChange={(event) => {
              setFailedOnly(event.target.checked);
              resetPage();
            }}
          />
          只看失败项目
        </label>

        <label className="check-field">
          <input
            type="checkbox"
            checked={needsConfirmationOnly}
            onChange={(event) => {
              setNeedsConfirmationOnly(event.target.checked);
              resetPage();
            }}
          />
          只看需要人工确认
        </label>
      </section>

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

      <section className="project-section" aria-live="polite">
        {loading ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={24} aria-hidden="true" />
            正在读取项目...
          </div>
        ) : projects.items.length === 0 ? (
          <div className="empty-state empty-state-stacked">
            <strong>还没有项目。</strong>
            <span>请先从选题创建内容项目，或在任务里跑一次 V1 测试。</span>
            <button
              className="button button-primary"
              type="button"
              onClick={onOpenHotspots}
            >
              去热点池
            </button>
          </div>
        ) : (
          <div className="project-list">
            {projects.items.map((project) => (
              <ProjectRow
                key={project.pipeline_id}
                project={project}
                continuing={continuingId === project.pipeline_id}
                onOpen={() => void openDetail(project.pipeline_id)}
                onContinue={() => void handleContinue(project)}
                onOpenWorkspace={() => onOpenWorkspace?.(project.pipeline_id)}
                onOpenMaterials={() => onOpenMaterials?.(project.pipeline_id)}
                onOpenEditing={() => onOpenEditing?.(project.pipeline_id)}
                onOpenFinished={() => onOpenFinished?.(project.pipeline_id)}
              />
            ))}
          </div>
        )}
      </section>

      {projects.pagination.total_pages > 1 && (
        <nav className="pagination" aria-label="项目列表分页">
          <button
            className="icon-button"
            type="button"
            aria-label="上一页"
            disabled={page <= 1 || loading}
            onClick={() => setPage((value) => Math.max(1, value - 1))}
          >
            <ChevronLeft size={18} aria-hidden="true" />
          </button>
          <span>
            第 {page} / {projects.pagination.total_pages} 页
          </span>
          <button
            className="icon-button"
            type="button"
            aria-label="下一页"
            disabled={page >= projects.pagination.total_pages || loading}
            onClick={() => setPage((value) => value + 1)}
          >
            <ChevronRight size={18} aria-hidden="true" />
          </button>
        </nav>
      )}

      {(selected || detailLoading) && (
        <DetailDialog
          detail={selected}
          loading={detailLoading}
          onClose={() => setSelected(null)}
        />
      )}
    </main>
  );
}

interface ProjectRowProps {
  project: PipelineListItem;
  continuing: boolean;
  onOpen: () => void;
  onContinue: () => void;
  onOpenWorkspace: () => void;
  onOpenMaterials: () => void;
  onOpenEditing: () => void;
  onOpenFinished: () => void;
}

function ProjectRow({
  project,
  continuing,
  onOpen,
  onContinue,
  onOpenWorkspace,
  onOpenMaterials,
  onOpenEditing,
  onOpenFinished,
}: ProjectRowProps) {
  const progress =
    project.total_steps > 0
      ? Math.round((project.completed_steps / project.total_steps) * 100)
      : 0;
  const needsFinalReview = project.next_surface === "finished";

  return (
    <article
      className={`project-row ${project.has_failed ? "project-row-failed" : ""}`}
    >
      <div className="project-primary">
        <div className="project-title-line">
          <button
            className="project-title"
            type="button"
            aria-label={`查看 ${project.project_name}`}
            onClick={onOpen}
          >
            {project.project_name}
          </button>
          <StatusBadge status={project.status} />
          {project.has_failed && <span className="risk-label">执行失败</span>}
        </div>
        <p className="current-step">
          当前步骤：<strong>{project.current_step}</strong>
        </p>
        <div className="progress-line">
          <div className="progress-track" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </div>
          <span>
            {project.completed_steps} / {project.total_steps} 步
          </span>
        </div>
      </div>

      <div className="project-details">
        {project.has_failed && (
          <p className="failure-reason">
            <AlertTriangle size={15} aria-hidden="true" />
            {project.failure_reason || "有步骤执行失败"}
          </p>
        )}
        {!project.has_failed &&
          project.needs_confirmation &&
          Boolean(project.failure_reason) && (
            <p className="confirmation-reason">
              <AlertTriangle size={15} aria-hidden="true" />
              {project.failure_reason}
            </p>
          )}
        {project.needs_confirmation && (
          <p className="confirmation-note">
            <CircleDot size={15} aria-hidden="true" />
            需要人工确认
          </p>
        )}
        <p className="next-suggestion">{project.next_suggestion}</p>
        <p className="project-path" title={project.project_dir}>
          <FolderOpen size={15} aria-hidden="true" />
          {project.project_dir || "尚未创建项目目录"}
        </p>
      </div>

      <div className="project-meta">
        <time dateTime={project.created_at}>
          创建：{formatDate(project.created_at)}
        </time>
        <time dateTime={project.updated_at}>
          更新：{formatDate(project.updated_at)}
        </time>
      </div>

      <div className="project-actions">
        {project.script_asset_id !== null && (
          <button
            className="button button-primary"
            type="button"
            aria-label={`打开 ${project.project_name} 的脚本工作台`}
            onClick={onOpenWorkspace}
          >
            打开工作台
          </button>
        )}
        {project.project_dir && (
          <button
            className="button button-secondary"
            type="button"
            aria-label={`打开 ${project.project_name} 的素材`}
            onClick={onOpenMaterials}
          >
            素材
          </button>
        )}
        {project.project_dir && (
          <button
            className="button button-secondary"
            type="button"
            aria-label={`打开 ${project.project_name} 的成片导出`}
            onClick={onOpenEditing}
          >
            成片导出
          </button>
        )}
        {needsFinalReview ? (
          <button
            className="button button-primary"
            type="button"
            aria-label={`去成片验收 ${project.project_name}`}
            onClick={onOpenFinished}
          >
            <Clapperboard size={16} aria-hidden="true" />
            去成片验收
          </button>
        ) : project.status !== "已完成" ? (
          <button
            className="button button-primary"
            type="button"
            aria-label={`继续 ${project.project_name}`}
            onClick={onContinue}
            disabled={continuing}
          >
            {continuing ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Play size={16} aria-hidden="true" />
            )}
            {continuing ? "执行中" : "继续执行"}
          </button>
        ) : null}
        <button className="button button-secondary" type="button" onClick={onOpen}>
          查看详情
        </button>
      </div>
    </article>
  );
}

function StatusBadge({ status }: { status: string }) {
  const className =
    status === "已完成"
      ? "status-complete"
      : status === "失败"
        ? "status-failed"
        : status === "需要人工确认"
          ? "status-warning"
          : "status-active";
  return <span className={`status-badge ${className}`}>{status}</span>;
}

function SummaryItem({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: number;
  tone?: "normal" | "warning" | "danger";
}) {
  return (
    <div className={`summary-item summary-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DetailDialog({
  detail,
  loading,
  onClose,
}: {
  detail: PipelineDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="detail-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div>
            <p className="dialog-label">项目详情</p>
            <h2 id="detail-title">{detail?.project_name || "正在读取..."}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭详情"
            onClick={onClose}
          >
            <X size={19} aria-hidden="true" />
          </button>
        </header>

        {loading || !detail ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取详情...
          </div>
        ) : (
          <>
            <div className="detail-summary">
              <StatusBadge status={detail.status} />
              <span>{detail.next_suggestion}</span>
            </div>
            <div className="step-list">
              {detail.steps.map((step) => (
                <div className="step-row" key={step.step_name}>
                  <StepIcon status={step.status} />
                  <div>
                    <strong>{step.step_name}</strong>
                    {step.error_message && <p>{step.error_message}</p>}
                  </div>
                  <span>{step.status}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function StepIcon({ status }: { status: string }) {
  if (status === "已完成") {
    return <CheckCircle2 className="step-complete" size={18} aria-hidden="true" />;
  }
  if (status === "失败") {
    return <AlertTriangle className="step-failed" size={18} aria-hidden="true" />;
  }
  return <Clock3 className="step-pending" size={18} aria-hidden="true" />;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || "未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default ProjectListPage;
