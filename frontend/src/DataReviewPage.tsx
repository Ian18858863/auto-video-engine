import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Clock3,
  Eye,
  LoaderCircle,
  RefreshCw,
  TrendingUp,
  X,
} from "lucide-react";

import { getVideoReviewDetail, getVideoReviews } from "./api";
import type {
  VideoReviewDetail,
  VideoReviewItem,
  VideoReviewListResponse,
} from "./types";
import "./styles.css";

const emptyList: VideoReviewListResponse = {
  items: [],
  pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
  filters: { platforms: [], projects: [], judgements: [] },
};

function DataReviewPage({
  onOpenTracking,
  initialPipelineId = null,
}: {
  onOpenTracking?: () => void;
  initialPipelineId?: number | null;
}) {
  const [data, setData] = useState(emptyList);
  const [platform, setPlatform] = useState("");
  const [pipelineId, setPipelineId] = useState(
    initialPipelineId === null ? "" : String(initialPipelineId),
  );
  const [publishedFrom, setPublishedFrom] = useState("");
  const [publishedTo, setPublishedTo] = useState("");
  const [judgement, setJudgement] = useState("");
  const [sortBy, setSortBy] = useState<
    | "published_at"
    | "favorite_rate"
    | "share_rate"
    | "follower_rate"
    | "view_velocity"
  >("published_at");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<VideoReviewDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadReviews = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(
        await getVideoReviews({
          platform,
          pipelineId,
          publishedFrom,
          publishedTo,
          judgement,
          sortBy,
          page: 1,
          pageSize: 20,
        }),
      );
    } catch (loadError) {
      setError(toMessage(loadError, "复盘数据读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [judgement, pipelineId, platform, publishedFrom, publishedTo, sortBy]);

  useEffect(() => {
    void loadReviews();
  }, [loadReviews]);

  async function openDetail(item: VideoReviewItem) {
    setDetailLoading(true);
    setError("");
    try {
      setDetail(await getVideoReviewDetail(item.video_post_id));
    } catch (detailError) {
      setError(toMessage(detailError, "详细复盘读取失败。"));
    } finally {
      setDetailLoading(false);
    }
  }

  return (
    <main className="app-shell review-shell">
      <header className="page-header">
        <div>
          <h1>数据复盘</h1>
        </div>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => void loadReviews()}
          disabled={loading}
        >
          <RefreshCw size={16} aria-hidden="true" />
          刷新复盘
        </button>
      </header>

      <section className="summary-strip" aria-label="复盘概况">
        <Summary label="视频数量" value={data.pagination.total} />
        <Summary
          label="值得继续"
          value={data.items.filter((item) => item.judgement === "值得继续做").length}
          tone="good"
        />
        <Summary
          label="需要调整"
          value={data.items.filter((item) => item.judgement === "需要调整").length}
          tone="warning"
        />
      </section>

      <section className="filter-bar review-filters" aria-label="复盘筛选">
        <Select
          label="按平台筛选"
          value={platform}
          emptyLabel="全部平台"
          options={data.filters.platforms.map((value) => ({ value, label: value }))}
          onChange={setPlatform}
        />
        <Select
          label="按项目筛选"
          value={pipelineId}
          emptyLabel="全部项目"
          options={data.filters.projects.map((project) => ({
            value: String(project.id),
            label: project.title,
          }))}
          onChange={setPipelineId}
        />
        <label className="select-field">
          <span>开始日期</span>
          <input
            aria-label="开始日期"
            type="date"
            value={publishedFrom}
            onChange={(event) => setPublishedFrom(event.target.value)}
          />
        </label>
        <label className="select-field">
          <span>结束日期</span>
          <input
            aria-label="结束日期"
            type="date"
            value={publishedTo}
            onChange={(event) => setPublishedTo(event.target.value)}
          />
        </label>
        <Select
          label="按判断筛选"
          value={judgement}
          emptyLabel="全部判断"
          options={data.filters.judgements.map((value) => ({ value, label: value }))}
          onChange={setJudgement}
        />
        <label className="select-field">
          <span>排序方式</span>
          <select
            aria-label="排序方式"
            value={sortBy}
            onChange={(event) => setSortBy(event.target.value as typeof sortBy)}
          >
            <option value="published_at">发布时间</option>
            <option value="favorite_rate">收藏率</option>
            <option value="share_rate">转发率</option>
            <option value="follower_rate">涨粉率</option>
            <option value="view_velocity">播放增长速度</option>
          </select>
        </label>
      </section>

      {error && (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      )}

      <section className="review-list" aria-live="polite">
        {loading ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取复盘数据...
          </div>
        ) : data.items.length === 0 ? (
          <div className="empty-state empty-state-stacked">
            <strong>当前没有可复盘的发布数据。</strong>
            <span>先在发布数据追踪里手动新增一条发布记录，再回来查看复盘。</span>
            <button
              className="button button-primary"
              type="button"
              onClick={onOpenTracking}
            >
              去发布数据追踪
            </button>
          </div>
        ) : (
          data.items.map((item) => (
            <ReviewRow
              key={item.video_post_id}
              item={item}
              onOpen={() => void openDetail(item)}
            />
          ))
        )}
      </section>

      {(detail || detailLoading) && (
        <ReviewDialog
          detail={detail}
          loading={detailLoading}
          onClose={() => setDetail(null)}
        />
      )}
    </main>
  );
}

function ReviewRow({
  item,
  onOpen,
}: {
  item: VideoReviewItem;
  onOpen: () => void;
}) {
  return (
    <article className="review-row">
      <div className="review-primary">
        <div className="project-title-line">
          <h2>{item.video_title}</h2>
          <span className={`status-badge ${judgementClass(item.judgement)}`}>
            {item.judgement}
          </span>
        </div>
        <p>{item.project_name} · {item.platform} · {formatDate(item.published_at)}</p>
        <p className="review-suggestion">{item.next_suggestion}</p>
      </div>

      <div className="review-metrics">
        <Metric label="播放" value={formatNumber(item.views)} />
        <Metric label="点赞" value={formatNumber(item.likes)} />
        <Metric label="评论" value={formatNumber(item.comments)} />
        <Metric label="收藏" value={formatNumber(item.favorites)} />
        <Metric label="转发" value={formatNumber(item.shares)} />
        <Metric label="涨粉" value={formatNumber(item.followers_gained)} />
      </div>

      <div className="review-rates">
        <Rate label="收藏率" value={`${item.favorite_rate}%`} />
        <Rate label="转发率" value={`${item.share_rate}%`} />
        <Rate label="涨粉率" value={`${item.follower_rate}%`} />
        <Rate label="完播率" value={`${item.completion_rate}%`} />
        <Rate label="平均观看" value={`${item.average_watch_seconds} 秒`} />
        <Rate label="播放增速" value={`${formatNumber(item.view_velocity)}/小时`} />
      </div>

      <div className="review-trend">
        <TrendingUp size={18} aria-hidden="true" />
        <span>播放增长趋势</span>
        <strong>{item.growth_trend}</strong>
        <small>{item.snapshot_count} 条数据快照</small>
      </div>

      <button
        className="button button-secondary"
        type="button"
        aria-label={`查看 ${item.video_title} 的详细复盘`}
        onClick={onOpen}
      >
        <Eye size={16} aria-hidden="true" />
        详细复盘
      </button>
    </article>
  );
}

function ReviewDialog({
  detail,
  loading,
  onClose,
}: {
  detail: VideoReviewDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="detail-dialog review-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="详细复盘"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div>
            <p className="dialog-label">详细复盘</p>
            <h2>{detail?.video_title || "正在读取..."}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭详细复盘"
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        {loading || !detail ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取详细复盘...
          </div>
        ) : (
          <div className="review-detail-content">
            <section className="review-conclusion">
              <span className={`status-badge ${judgementClass(detail.judgement)}`}>
                {detail.judgement}
              </span>
              <p>{detail.next_suggestion}</p>
            </section>

            <section>
              <h3>判断依据</h3>
              <ul className="review-reason-list">
                {detail.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </section>

            <section>
              <h3>数据快照变化</h3>
              <div className="review-snapshots">
                {detail.snapshots.map((snapshot) => (
                  <div className="review-snapshot-row" key={snapshot.id}>
                    <div>
                      <Clock3 size={15} aria-hidden="true" />
                      <strong>{formatElapsed(snapshot.elapsed_minutes)}</strong>
                      <time>{formatDate(snapshot.captured_at)}</time>
                    </div>
                    <span>播放 {formatNumber(snapshot.views)}</span>
                    <span>收藏 {formatNumber(snapshot.favorites)}</span>
                    <span>转发 {formatNumber(snapshot.shares)}</span>
                    <span>完播 {snapshot.completion_rate}%</span>
                    <p>{snapshot.notes || "未填写备注"}</p>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}
      </section>
    </div>
  );
}

function Select({
  label,
  value,
  emptyLabel,
  options,
  onChange,
}: {
  label: string;
  value: string;
  emptyLabel: string;
  options: Array<{ value: string; label: string }>;
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

function Rate({ label, value }: { label: string; value: string }) {
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

function judgementClass(judgement: string): string {
  if (judgement === "值得继续做" || judgement === "有潜力") {
    return "status-complete";
  }
  if (judgement === "需要调整") return "status-warning";
  if (judgement === "不建议继续") return "status-failed";
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
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatElapsed(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} 小时`;
  return `${Math.round(minutes / 1440)} 天`;
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default DataReviewPage;
