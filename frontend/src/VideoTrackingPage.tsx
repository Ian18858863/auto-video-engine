import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  Clock3,
  Edit3,
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  Save,
  TrendingUp,
  X,
} from "lucide-react";

import {
  addVideoSnapshot,
  createVideoPost,
  getVideoPostDetail,
  getVideoPosts,
  updateVideoPost,
} from "./api";
import type {
  VideoPostDetail,
  VideoPostItem,
  VideoPostListResponse,
  VideoPostPayload,
  VideoSnapshotPayload,
} from "./types";
import "./styles.css";

const emptyList: VideoPostListResponse = {
  items: [],
  pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
  filters: { platforms: [], projects: [], statuses: [] },
};

const defaultPost: VideoPostPayload = {
  pipeline_id: null,
  platform: "抖音",
  video_title: "",
  published_at: "",
  video_url: "",
  topic_type: "",
  video_format: "",
  hook_type: "",
  duration_seconds: 0,
  views: 0,
  likes: 0,
  comments: 0,
  favorites: 0,
  shares: 0,
  followers_gained: 0,
  completion_rate: 0,
  average_watch_seconds: 0,
  notes: "",
};

function VideoTrackingPage({
  onOpenReviews,
  initialPipelineId = null,
}: {
  onOpenReviews?: () => void;
  initialPipelineId?: number | null;
}) {
  const [data, setData] = useState(emptyList);
  const [platform, setPlatform] = useState("");
  const [pipelineId, setPipelineId] = useState(
    initialPipelineId === null ? "" : String(initialPipelineId),
  );
  const [status, setStatus] = useState("");
  const [sortBy, setSortBy] = useState<
    "published_at" | "views" | "favorites" | "shares"
  >("published_at");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editor, setEditor] = useState<VideoPostItem | "new" | null>(null);
  const [snapshotTarget, setSnapshotTarget] = useState<VideoPostItem | null>(null);
  const [historyTarget, setHistoryTarget] = useState<VideoPostDetail | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const loadPosts = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(
        await getVideoPosts({
          platform,
          pipelineId,
          status,
          sortBy,
          page: 1,
          pageSize: 20,
        }),
      );
    } catch (loadError) {
      setError(toMessage(loadError, "发布数据读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [pipelineId, platform, sortBy, status]);

  useEffect(() => {
    void loadPosts();
  }, [loadPosts]);

  useEffect(() => {
    const timer = window.setInterval(() => void loadPosts(), 30_000);
    return () => window.clearInterval(timer);
  }, [loadPosts]);

  async function saveRecord(payload: VideoPostPayload) {
    try {
      if (editor === "new") {
        await createVideoPost(payload);
        setNotice("发布记录已新增，并保存了第一条数据快照。");
      } else if (editor) {
        await updateVideoPost(editor.id, payload);
        setNotice("发布记录已更新。");
      }
      setEditor(null);
      await loadPosts();
    } catch (saveError) {
      setError(toMessage(saveError, "发布记录保存失败。"));
    }
  }

  async function saveSnapshot(payload: VideoSnapshotPayload) {
    if (!snapshotTarget) return;
    try {
      await addVideoSnapshot(snapshotTarget.id, payload);
      setNotice("最新数据已保存，历史快照已保留。");
      setSnapshotTarget(null);
      await loadPosts();
    } catch (saveError) {
      setError(toMessage(saveError, "数据快照保存失败。"));
    }
  }

  async function openHistory(item: VideoPostItem) {
    setHistoryLoading(true);
    setError("");
    try {
      setHistoryTarget(await getVideoPostDetail(item.id));
    } catch (historyError) {
      setError(toMessage(historyError, "历史数据读取失败。"));
    } finally {
      setHistoryLoading(false);
    }
  }

  return (
    <main className="app-shell tracking-shell">
      <header className="page-header">
        <div>
          <h1>发布数据追踪</h1>
        </div>
        <div className="tracking-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={onOpenReviews}
          >
            <TrendingUp size={16} aria-hidden="true" />
            数据复盘
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void loadPosts()}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新本地数据
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={() => setEditor("new")}
          >
            <Plus size={16} aria-hidden="true" />
            新增发布记录
          </button>
        </div>
      </header>

      <section className="summary-strip" aria-label="发布数据概况">
        <Summary label="发布记录" value={data.pagination.total} />
        <Summary
          label="有潜力"
          value={data.items.filter((item) => item.current_status === "有潜力").length}
          tone="good"
        />
        <Summary
          label="需要复盘"
          value={data.items.filter((item) => item.current_status === "需要复盘").length}
          tone="warning"
        />
      </section>

      <section className="filter-bar" aria-label="发布数据筛选">
        <Select
          label="按平台筛选"
          value={platform}
          options={data.filters.platforms.map((value) => ({ value, label: value }))}
          emptyLabel="全部平台"
          onChange={setPlatform}
        />
        <Select
          label="按项目筛选"
          value={pipelineId}
          options={data.filters.projects.map((project) => ({
            value: String(project.id),
            label: project.title,
          }))}
          emptyLabel="全部项目"
          onChange={setPipelineId}
        />
        <Select
          label="按状态筛选"
          value={status}
          options={data.filters.statuses.map((value) => ({ value, label: value }))}
          emptyLabel="全部状态"
          onChange={setStatus}
        />
        <label className="select-field">
          <span>排序方式</span>
          <select
            value={sortBy}
            onChange={(event) =>
              setSortBy(event.target.value as typeof sortBy)
            }
          >
            <option value="published_at">发布时间</option>
            <option value="views">播放量</option>
            <option value="favorites">收藏数</option>
            <option value="shares">转发数</option>
          </select>
        </label>
      </section>

      {notice && <p className="message message-success">{notice}</p>}
      {error && (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      )}

      <section className="tracking-list" aria-live="polite">
        {loading ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取发布数据...
          </div>
        ) : data.items.length === 0 ? (
          <div className="empty-state empty-state-stacked">
            <strong>还没有发布记录。</strong>
            <span>先手动新增一条视频数据，后续复盘和周报才有基础。</span>
            <button
              className="button button-primary"
              type="button"
              onClick={() => setEditor("new")}
            >
              <Plus size={16} aria-hidden="true" />
              新增发布记录
            </button>
          </div>
        ) : (
          data.items.map((item) => (
            <TrackingRow
              key={item.id}
              item={item}
              onEdit={() => setEditor(item)}
              onSnapshot={() => setSnapshotTarget(item)}
              onHistory={() => void openHistory(item)}
            />
          ))
        )}
      </section>

      {editor && (
        <RecordDialog
          item={editor === "new" ? null : editor}
          projects={data.filters.projects}
          onClose={() => setEditor(null)}
          onSave={saveRecord}
        />
      )}
      {snapshotTarget && (
        <SnapshotDialog
          item={snapshotTarget}
          onClose={() => setSnapshotTarget(null)}
          onSave={saveSnapshot}
        />
      )}
      {(historyLoading || historyTarget) && (
        <HistoryDialog
          detail={historyTarget}
          loading={historyLoading}
          onClose={() => setHistoryTarget(null)}
        />
      )}
    </main>
  );
}

function TrackingRow({
  item,
  onEdit,
  onSnapshot,
  onHistory,
}: {
  item: VideoPostItem;
  onEdit: () => void;
  onSnapshot: () => void;
  onHistory: () => void;
}) {
  return (
    <article className="tracking-row">
      <div className="tracking-primary">
        <div className="project-title-line">
          <h2>{item.video_title}</h2>
          <span className={`status-badge ${statusClass(item.current_status)}`}>
            {item.current_status}
          </span>
        </div>
        <p className="tracking-context">
          {item.project_name} · {item.platform} · {item.video_format || "未填写形式"} ·{" "}
          {item.duration_seconds || 0} 秒
        </p>
        <p className="tracking-time">
          发布：{formatDate(item.published_at)} · 当前阶段：{item.current_stage} ·
          最新更新：{formatDate(item.latest_updated_at)}
        </p>
        {item.notes && <p className="tracking-notes">{item.notes}</p>}
      </div>

      <div className="metric-grid">
        <Metric label="播放" value={formatNumber(item.views)} />
        <Metric label="点赞" value={formatNumber(item.likes)} />
        <Metric label="评论" value={formatNumber(item.comments)} />
        <Metric label="收藏" value={formatNumber(item.favorites)} />
        <Metric label="转发" value={formatNumber(item.shares)} />
        <Metric label="涨粉" value={formatNumber(item.followers_gained)} />
      </div>

      <div className="tracking-rates">
        <span>收藏率 <strong>{item.favorite_rate}%</strong></span>
        <span>转发率 <strong>{item.share_rate}%</strong></span>
        <span>完播率 <strong>{item.completion_rate}%</strong></span>
        <span>平均观看 <strong>{item.average_watch_seconds} 秒</strong></span>
        <span>播放增速 <strong>{formatNumber(item.view_velocity)}/小时</strong></span>
        <span>历史快照 <strong>{item.snapshot_count} 条</strong></span>
      </div>

      <div className="tracking-actions">
        <button
          className="icon-button"
          type="button"
          aria-label={`编辑 ${item.video_title}`}
          title="编辑发布信息"
          onClick={onEdit}
        >
          <Edit3 size={17} aria-hidden="true" />
        </button>
        <button
          className="button button-primary"
          type="button"
          aria-label={`更新 ${item.video_title} 的数据`}
          onClick={onSnapshot}
        >
          <BarChart3 size={16} aria-hidden="true" />
          更新数据
        </button>
        <button
          className="button button-secondary"
          type="button"
          aria-label={`查看 ${item.video_title} 的历史`}
          onClick={onHistory}
        >
          <History size={16} aria-hidden="true" />
          历史
        </button>
      </div>
    </article>
  );
}

function RecordDialog({
  item,
  projects,
  onClose,
  onSave,
}: {
  item: VideoPostItem | null;
  projects: Array<{ id: number; title: string }>;
  onClose: () => void;
  onSave: (payload: VideoPostPayload) => Promise<void>;
}) {
  const [form, setForm] = useState<VideoPostPayload>(
    item
      ? {
          pipeline_id: item.pipeline_id,
          platform: item.platform,
          video_title: item.video_title,
          published_at: toLocalInput(item.published_at),
          video_url: item.video_url,
          topic_type: item.topic_type,
          video_format: item.video_format,
          hook_type: item.hook_type,
          duration_seconds: item.duration_seconds,
          views: item.views,
          likes: item.likes,
          comments: item.comments,
          favorites: item.favorites,
          shares: item.shares,
          followers_gained: item.followers_gained,
          completion_rate: item.completion_rate,
          average_watch_seconds: item.average_watch_seconds,
          notes: item.notes,
        }
      : { ...defaultPost },
  );
  const [saving, setSaving] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await onSave({
        ...form,
        published_at: new Date(form.published_at).toISOString(),
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog title={item ? "编辑发布记录" : "新增发布记录"} onClose={onClose}>
      <form className="tracking-form" onSubmit={(event) => void submit(event)}>
        <label>
          <span>关联项目</span>
          <select
            aria-label="关联项目"
            value={form.pipeline_id ?? ""}
            onChange={(event) =>
              setForm({
                ...form,
                pipeline_id: event.target.value ? Number(event.target.value) : null,
              })
            }
          >
            <option value="">暂不关联</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.title}
              </option>
            ))}
          </select>
        </label>
        <TextField
          label="视频标题"
          value={form.video_title}
          required
          onChange={(value) => setForm({ ...form, video_title: value })}
        />
        <label>
          <span>发布平台</span>
          <select
            aria-label="发布平台"
            value={form.platform}
            onChange={(event) => setForm({ ...form, platform: event.target.value })}
          >
            {["抖音", "小红书", "B站", "YouTube Shorts", "TikTok", "其他"].map(
              (value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ),
            )}
          </select>
        </label>
        <label>
          <span>发布时间</span>
          <input
            aria-label="发布时间"
            type="datetime-local"
            required
            value={form.published_at}
            onChange={(event) =>
              setForm({ ...form, published_at: event.target.value })
            }
          />
        </label>
        <TextField
          label="视频链接"
          value={form.video_url}
          onChange={(value) => setForm({ ...form, video_url: value })}
        />
        <TextField
          label="视频形式"
          value={form.video_format}
          onChange={(value) => setForm({ ...form, video_format: value })}
        />
        <NumberField
          label="视频时长（秒）"
          value={form.duration_seconds}
          onChange={(value) => setForm({ ...form, duration_seconds: value })}
        />
        {!item && (
          <MetricFields
            value={form}
            onChange={(changes) => setForm({ ...form, ...changes })}
          />
        )}
        <TextField
          label="备注"
          value={form.notes}
          wide
          onChange={(value) => setForm({ ...form, notes: value })}
        />
        <div className="dialog-actions tracking-form-actions">
          <button className="button button-secondary" type="button" onClick={onClose}>
            取消
          </button>
          <button className="button button-primary" type="submit" disabled={saving}>
            <Save size={16} aria-hidden="true" />
            {item ? "保存修改" : "保存发布记录"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function SnapshotDialog({
  item,
  onClose,
  onSave,
}: {
  item: VideoPostItem;
  onClose: () => void;
  onSave: (payload: VideoSnapshotPayload) => Promise<void>;
}) {
  const [form, setForm] = useState<VideoSnapshotPayload>({
    views: item.views,
    likes: item.likes,
    comments: item.comments,
    favorites: item.favorites,
    shares: item.shares,
    followers_gained: item.followers_gained,
    completion_rate: item.completion_rate,
    average_watch_seconds: item.average_watch_seconds,
    notes: "",
  });
  const [saving, setSaving] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await onSave(form);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog title="手动更新最新数据" onClose={onClose}>
      <form className="tracking-form" onSubmit={(event) => void submit(event)}>
        <MetricFields
          value={form}
          onChange={(changes) => setForm({ ...form, ...changes })}
        />
        <TextField
          label="本次备注"
          value={form.notes}
          wide
          onChange={(value) => setForm({ ...form, notes: value })}
        />
        <div className="dialog-actions tracking-form-actions">
          <button className="button button-secondary" type="button" onClick={onClose}>
            取消
          </button>
          <button className="button button-primary" type="submit" disabled={saving}>
            <Save size={16} aria-hidden="true" />
            保存数据快照
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function HistoryDialog({
  detail,
  loading,
  onClose,
}: {
  detail: VideoPostDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog title={detail?.video_title || "数据变化历史"} onClose={onClose}>
      {loading || !detail ? (
        <div className="empty-state">
          <LoaderCircle className="spin" size={22} aria-hidden="true" />
          正在读取历史...
        </div>
      ) : (
        <div className="snapshot-history">
          {detail.snapshots.map((snapshot) => (
            <div className="snapshot-row" key={snapshot.id}>
              <div>
                <Clock3 size={16} aria-hidden="true" />
                <strong>{formatElapsed(snapshot.elapsed_minutes)}</strong>
                <time>{formatDate(snapshot.captured_at)}</time>
              </div>
              <div className="snapshot-metrics">
                <span>播放 {formatNumber(snapshot.views)}</span>
                <span>收藏 {formatNumber(snapshot.favorites)}</span>
                <span>转发 {formatNumber(snapshot.shares)}</span>
                <span>完播 {snapshot.completion_rate}%</span>
              </div>
              <p>备注：{snapshot.notes || "本次未填写"}</p>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}

function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="detail-dialog tracking-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <h2>{title}</h2>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭"
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}

function MetricFields({
  value,
  onChange,
}: {
  value: VideoSnapshotPayload | VideoPostPayload;
  onChange: (changes: Partial<VideoSnapshotPayload>) => void;
}) {
  return (
    <>
      <NumberField label="播放量" value={value.views} onChange={(views) => onChange({ views })} />
      <NumberField label="点赞数" value={value.likes} onChange={(likes) => onChange({ likes })} />
      <NumberField label="评论数" value={value.comments} onChange={(comments) => onChange({ comments })} />
      <NumberField label="收藏数" value={value.favorites} onChange={(favorites) => onChange({ favorites })} />
      <NumberField label="转发数" value={value.shares} onChange={(shares) => onChange({ shares })} />
      <NumberField label="涨粉数" value={value.followers_gained} onChange={(followers_gained) => onChange({ followers_gained })} />
      <NumberField label="完播率（%）" value={value.completion_rate} step="0.1" onChange={(completion_rate) => onChange({ completion_rate })} />
      <NumberField label="平均观看时长（秒）" value={value.average_watch_seconds} step="0.1" onChange={(average_watch_seconds) => onChange({ average_watch_seconds })} />
    </>
  );
}

function TextField({
  label,
  value,
  onChange,
  required = false,
  wide = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  wide?: boolean;
}) {
  return (
    <label className={wide ? "form-wide" : ""}>
      <span>{label}</span>
      <input
        aria-label={label}
        value={value}
        required={required}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function NumberField({
  label,
  value,
  onChange,
  step = "1",
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  step?: string;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        aria-label={label}
        type="number"
        min="0"
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function Select({
  label,
  value,
  options,
  emptyLabel,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  emptyLabel: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="select-field">
      <span>{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{emptyLabel}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Summary({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: number;
  tone?: "normal" | "good" | "warning";
}) {
  return (
    <div className={`summary-item summary-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function statusClass(status: string): string {
  if (status === "有潜力" || status === "加速") return "status-complete";
  if (status === "掉速" || status === "需要复盘") return "status-failed";
  if (status === "观察中") return "status-warning";
  return "status-active";
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);
}

function formatDate(value: string | null): string {
  if (!value) return "未填写";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function toLocalInput(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function formatElapsed(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} 小时`;
  return `${Math.round(minutes / 1440)} 天`;
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default VideoTrackingPage;
