import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, LoaderCircle, RefreshCw } from "lucide-react";

import { getWeeklyReviewReport } from "./api";
import type { VideoReviewItem, WeeklyReviewReport } from "./types";
import "./styles.css";

function WeeklyReviewPage({
  onOpenTracking = () => undefined,
  pipelineId = null,
}: {
  onOpenTracking?: () => void;
  pipelineId?: number | null;
}) {
  const [report, setReport] = useState<WeeklyReviewReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadReport = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setReport(await getWeeklyReviewReport(pipelineId));
    } catch (loadError) {
      setError(toMessage(loadError, "复盘周报读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void loadReport();
  }, [loadReport]);

  return (
    <main className="app-shell review-shell">
      <header className="page-header">
        <div>
          <h1>复盘周报</h1>
        </div>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => void loadReport()}
          disabled={loading}
        >
          <RefreshCw size={16} aria-hidden="true" />
          刷新周报
        </button>
      </header>

      {error && (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      )}

      {loading && !report ? (
        <div className="empty-state">
          <LoaderCircle className="spin" size={22} aria-hidden="true" />
          正在读取复盘周报...
        </div>
      ) : report?.published_count === 0 ? (
        <div className="empty-state empty-state-stacked">
          <strong>本周还没有可复盘的发布数据。</strong>
          <span>先新增发布记录和数据快照；有证据后才会生成趋势和建议。</span>
          <button
            className="button button-primary"
            type="button"
            onClick={onOpenTracking}
          >
            去发布数据追踪
          </button>
        </div>
      ) : report ? (
        <>
          <section className="summary-strip">
            <Summary label="本周发布" value={report.published_count} />
            <Summary label="高收藏" value={report.high_favorite_videos.length} />
            <Summary label="有长尾" value={report.long_tail_videos.length} />
          </section>
          <p className="message message-info">{report.data_note}</p>

          <WeeklySection title="本周发布了哪些视频" items={report.published_videos} />
          <WeeklySection title="收藏率高，值得参考" items={report.high_favorite_videos} />
          <WeeklySection title="转发率高，有传播价值" items={report.high_share_videos} />
          <WeeklySection title="有长尾潜力" items={report.long_tail_videos} />
          <WeeklySection title="掉速或需要调整" items={report.stalled_videos} />

          <section className="workspace-section">
            <h3>下周建议做什么</h3>
            <ul className="review-reason-list">
              {report.next_week_do.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
          <section className="workspace-section">
            <h3>下周建议避免什么</h3>
            <ul className="review-reason-list">
              {report.next_week_avoid.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        </>
      ) : (
        <div className="empty-state empty-state-stacked">
          <strong>复盘周报没有读取成功。</strong>
          <span>确认后端服务正常后，可以重新读取本地规则周报。</span>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void loadReport()}
          >
            <RefreshCw size={16} aria-hidden="true" />
            重新读取周报
          </button>
        </div>
      )}
    </main>
  );
}

function WeeklySection({
  title,
  items,
}: {
  title: string;
  items: VideoReviewItem[];
}) {
  return (
    <section className="workspace-section">
      <h3>{title}</h3>
      {items.length ? (
        <div className="review-list compact-review-list">
          {items.map((item) => (
            <article className="review-row" key={`${title}-${item.video_post_id}`}>
              <div className="review-primary">
                <h2>{item.video_title}</h2>
                <p>{item.project_name} · {item.platform} · {item.judgement}</p>
              </div>
              <div className="review-rates">
                <span>收藏率 {item.favorite_rate}%</span>
                <span>转发率 {item.share_rate}%</span>
                <span>趋势 {item.growth_trend}</span>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <p className="workspace-empty-line">暂无数据。</p>
      )}
    </section>
  );
}

function Summary({ label, value }: { label: string; value: number }) {
  return (
    <div className="summary-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default WeeklyReviewPage;
