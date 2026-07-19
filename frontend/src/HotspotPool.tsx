import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  AlertTriangle,
  ArrowUpRight,
  Bookmark,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleSlash2,
  Flame,
  FolderPlus,
  Lightbulb,
  LoaderCircle,
  Plus,
  RefreshCw,
  Sparkles,
  X,
} from "lucide-react";

import {
  buildProjectFromHotspot,
  fetchHotspotSources,
  getHotspotDetail,
  getHotspots,
  importManualHotspot,
} from "./api";
import type { HotspotItem, HotspotListResponse } from "./types";

const emptyList: HotspotListResponse = {
  items: [],
  pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
  filters: { categories: [], sources: [] },
};

type ScoreSort = "hot_score" | "topic_score" | "save_score";

function HotspotPool({
  initialHotspotId = null,
  onOpenTopics,
}: {
  initialHotspotId?: number | null;
  onOpenTopics?: () => void;
}) {
  const [hotspots, setHotspots] = useState(emptyList);
  const [category, setCategory] = useState("");
  const [sourcePlatform, setSourcePlatform] = useState("");
  const [unfilteredOnly, setUnfilteredOnly] = useState(false);
  const [sortBy, setSortBy] = useState<ScoreSort>("topic_score");
  const [page, setPage] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<HotspotItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [buildingId, setBuildingId] = useState<number | null>(null);
  const [fetchingSources, setFetchingSources] = useState(false);
  const [showImportForm, setShowImportForm] = useState(false);
  const [manualDraft, setManualDraft] = useState({
    title: "",
    source_platform: "手动导入",
    url: "",
    summary: "",
    category: "AI",
    tags: "",
    notes: "",
  });
  const openedInitialHotspot = useRef<number | null>(null);

  const loadHotspots = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setHotspots(
        await getHotspots({
          category,
          sourcePlatform,
          unfilteredOnly,
          sortBy,
          page,
          pageSize: 20,
        }),
      );
    } catch (loadError) {
      setError(toMessage(loadError, "热点池读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [category, page, sortBy, sourcePlatform, unfilteredOnly]);

  useEffect(() => {
    void loadHotspots();
  }, [loadHotspots, refreshKey]);

  useEffect(() => {
    if (
      initialHotspotId !== null &&
      openedInitialHotspot.current !== initialHotspotId
    ) {
      openedInitialHotspot.current = initialHotspotId;
      void openDetail(initialHotspotId);
    }
  }, [initialHotspotId]);

  async function openDetail(hotspotId: number) {
    setDetailLoading(true);
    setError("");
    try {
      setSelected(await getHotspotDetail(hotspotId));
    } catch (detailError) {
      setError(toMessage(detailError, "热点详情读取失败。"));
    } finally {
      setDetailLoading(false);
    }
  }

  async function handleBuildProject(hotspot: HotspotItem) {
    setBuildingId(hotspot.id);
    setNotice("");
    setError("");
    try {
      const result = await buildProjectFromHotspot(hotspot);
      if (result.provider !== "fake") {
        throw new Error("安全校验失败：当前流程不是 fake provider。");
      }
      setNotice(
        `项目已创建：${result.project_name}，继续使用 fake provider，未调用真实 AI。`,
      );
      setHotspots((current) => ({
        ...current,
        items: current.items.map((item) =>
          item.id === hotspot.id ? { ...item, has_project: true } : item,
        ),
      }));
      if (selected?.id === hotspot.id) {
        setSelected({ ...selected, has_project: true });
      }
    } catch (buildError) {
      setError(toMessage(buildError, "创建内容项目失败。"));
    } finally {
      setBuildingId(null);
    }
  }

  async function handleFetchSources() {
    setFetchingSources(true);
    setNotice("");
    setError("");
    try {
      const result = await fetchHotspotSources();
      setNotice(
        `公开来源抓取完成：新增 ${result.saved_count} 条，重复 ${result.duplicate_count} 条，失败来源 ${result.failed_sources.length} 个。`,
      );
      setRefreshKey((value) => value + 1);
    } catch (fetchError) {
      setError(toMessage(fetchError, "公开来源抓取失败。"));
    } finally {
      setFetchingSources(false);
    }
  }

  async function handleManualImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice("");
    setError("");
    try {
      const result = await importManualHotspot({
        title: manualDraft.title,
        source_platform: manualDraft.source_platform || "手动导入",
        url: manualDraft.url,
        summary: manualDraft.summary,
        category: manualDraft.category || "其他",
        tags: manualDraft.tags
          .split(/[,，\s]+/)
          .map((tag) => tag.trim())
          .filter(Boolean)
          .slice(0, 5),
        published_at: null,
        notes: manualDraft.notes,
      });
      setNotice(result.message);
      setManualDraft({
        title: "",
        source_platform: "手动导入",
        url: "",
        summary: "",
        category: "AI",
        tags: "",
        notes: "",
      });
      setShowImportForm(false);
      setRefreshKey((value) => value + 1);
    } catch (importError) {
      setError(toMessage(importError, "手动导入热点失败。"));
    }
  }

  const resetPage = () => setPage(1);

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>原始热点</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={onOpenTopics}
          >
            <Lightbulb size={16} aria-hidden="true" />
            候选选题
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新热点
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleFetchSources()}
            disabled={fetchingSources}
          >
            {fetchingSources ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            抓取公开来源
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setShowImportForm((value) => !value)}
          >
            <Plus size={16} aria-hidden="true" />
            手动导入备用
          </button>
        </div>
      </header>

      {showImportForm && (
        <form className="manual-import-panel" onSubmit={(event) => void handleManualImport(event)}>
          <label>
            <span>标题</span>
            <input
              value={manualDraft.title}
              onChange={(event) =>
                setManualDraft({ ...manualDraft, title: event.target.value })
              }
              required
            />
          </label>
          <label>
            <span>来源</span>
            <input
              value={manualDraft.source_platform}
              onChange={(event) =>
                setManualDraft({
                  ...manualDraft,
                  source_platform: event.target.value,
                })
              }
            />
          </label>
          <label>
            <span>链接（可选）</span>
            <input
              value={manualDraft.url}
              onChange={(event) =>
                setManualDraft({ ...manualDraft, url: event.target.value })
              }
            />
          </label>
          <label>
            <span>分类</span>
            <input
              value={manualDraft.category}
              onChange={(event) =>
                setManualDraft({ ...manualDraft, category: event.target.value })
              }
            />
          </label>
          <label className="manual-import-wide">
            <span>摘要</span>
            <textarea
              value={manualDraft.summary}
              onChange={(event) =>
                setManualDraft({ ...manualDraft, summary: event.target.value })
              }
              rows={2}
            />
          </label>
          <label>
            <span>标签</span>
            <input
              placeholder="AI，商业，工具"
              value={manualDraft.tags}
              onChange={(event) =>
                setManualDraft({ ...manualDraft, tags: event.target.value })
              }
            />
          </label>
          <label className="manual-import-wide">
            <span>备注</span>
            <textarea
              value={manualDraft.notes}
              onChange={(event) =>
                setManualDraft({ ...manualDraft, notes: event.target.value })
              }
              rows={2}
            />
          </label>
          <div className="manual-import-actions">
            <button className="button button-primary" type="submit">
              保存到热点池
            </button>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => setShowImportForm(false)}
            >
              取消
            </button>
          </div>
        </form>
      )}

      <section className="summary-strip" aria-label="热点概况">
        <SummaryItem label="当前结果" value={hotspots.pagination.total} />
        <SummaryItem
          label="可直接做"
          value={hotspots.items.filter((item) => !item.is_filtered).length}
          tone="good"
        />
        <SummaryItem
          label="已生成项目"
          value={hotspots.items.filter((item) => item.has_project).length}
          tone="normal"
        />
      </section>

      <section className="filter-bar hotspot-filters" aria-label="热点筛选">
        <SelectField
          label="按分类筛选"
          value={category}
          options={hotspots.filters.categories}
          emptyLabel="全部分类"
          onChange={(value) => {
            setCategory(value);
            resetPage();
          }}
        />
        <SelectField
          label="按来源筛选"
          value={sourcePlatform}
          options={hotspots.filters.sources}
          emptyLabel="全部来源"
          onChange={(value) => {
            setSourcePlatform(value);
            resetPage();
          }}
        />
        <label className="select-field">
          <span>排序方式</span>
          <select
            value={sortBy}
            onChange={(event) => {
              setSortBy(event.target.value as ScoreSort);
              resetPage();
            }}
          >
            <option value="topic_score">选题价值优先</option>
            <option value="hot_score">热度优先</option>
            <option value="save_score">收藏价值优先</option>
          </select>
        </label>
        <label className="check-field">
          <input
            type="checkbox"
            checked={unfilteredOnly}
            onChange={(event) => {
              setUnfilteredOnly(event.target.checked);
              resetPage();
            }}
          />
          只看未过滤热点
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
            正在读取热点...
          </div>
        ) : hotspots.items.length === 0 ? (
          <div className="empty-state empty-state-stacked">
            <strong>还没有原始热点。</strong>
            <span>优先使用自动抓取公开来源；手动导入只作为备用。</span>
            <div>
              <button
                className="button button-secondary"
                type="button"
                onClick={() => setShowImportForm(true)}
              >
                手动导入备用
              </button>
              <button
                className="button button-secondary"
                type="button"
                onClick={() => void handleFetchSources()}
                disabled={fetchingSources}
              >
                抓取公开来源
              </button>
            </div>
          </div>
        ) : (
          <div className="hotspot-list">
            {hotspots.items.map((hotspot) => (
              <HotspotRow
                key={hotspot.id}
                hotspot={hotspot}
                building={buildingId === hotspot.id}
                onOpen={() => void openDetail(hotspot.id)}
                onBuild={() => void handleBuildProject(hotspot)}
              />
            ))}
          </div>
        )}
      </section>

      {hotspots.pagination.total_pages > 1 && (
        <nav className="pagination" aria-label="热点池分页">
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
            第 {page} / {hotspots.pagination.total_pages} 页
          </span>
          <button
            className="icon-button"
            type="button"
            aria-label="下一页"
            disabled={page >= hotspots.pagination.total_pages || loading}
            onClick={() => setPage((value) => value + 1)}
          >
            <ChevronRight size={18} aria-hidden="true" />
          </button>
        </nav>
      )}

      {(selected || detailLoading) && (
        <HotspotDetailDialog
          hotspot={selected}
          loading={detailLoading}
          building={selected ? buildingId === selected.id : false}
          onBuild={() => selected && void handleBuildProject(selected)}
          onClose={() => setSelected(null)}
        />
      )}
    </main>
  );
}

function HotspotRow({
  hotspot,
  building,
  onOpen,
  onBuild,
}: {
  hotspot: HotspotItem;
  building: boolean;
  onOpen: () => void;
  onBuild: () => void;
}) {
  return (
    <article className={`hotspot-row ${hotspot.is_filtered ? "filtered" : ""}`}>
      <div className="hotspot-main">
        <div className="hotspot-title-line">
          <button
            className="project-title"
            type="button"
            aria-label={`查看 ${hotspot.title}`}
            onClick={onOpen}
          >
            {hotspot.title}
          </button>
          <span className="category-label">{hotspot.category}</span>
          <span className="category-label">{originLabel(hotspot.data_origin)}</span>
          <span className={`status-badge ${credibilityTone(hotspot.source_credibility)}`}>
            来源可信度：{hotspot.source_credibility}
          </span>
          {hotspot.is_filtered && <span className="risk-label">已过滤</span>}
          {hotspot.has_project && <span className="status-badge status-complete">已有项目</span>}
        </div>
        <p className="hotspot-source">
          {hotspot.source_platform} · {sourceTypeLabel(hotspot.source_type)} ·{" "}
          {formatDate(hotspot.published_at)}
        </p>
        <p className="hotspot-summary">{hotspot.summary || "暂无摘要"}</p>
        <div className="tag-list">
          {hotspot.tags.slice(0, 5).map((tag) => (
            <span key={tag}>{tag}</span>
          ))}
        </div>
        {hotspot.is_filtered && (
          <p className="filter-reason">
            <CircleSlash2 size={15} aria-hidden="true" />
            {hotspot.filter_reason || "已被规则过滤"}
          </p>
        )}
        {hotspot.duplicate_reason && (
          <p className="filter-reason">
            <CircleSlash2 size={15} aria-hidden="true" />
            去重：{hotspot.duplicate_reason}
          </p>
        )}
      </div>

      <div className="score-group" aria-label={`${hotspot.title}评分`}>
        <ScoreItem label="热度" value={hotspot.hot_score} icon="hot" />
        <ScoreItem label="选题" value={hotspot.topic_score} icon="topic" />
        <ScoreItem label="收藏" value={hotspot.save_score} icon="save" />
        <ScoreItem label="风险" value={hotspot.risk_score} icon="risk" />
      </div>

      <div className="hotspot-actions">
        <button className="button button-secondary" type="button" onClick={onOpen}>
          查看详情
        </button>
        {hotspot.url ? (
          <a
            className="button button-secondary"
            href={hotspot.url}
            target="_blank"
            rel="noreferrer"
          >
            查看原文
            <ArrowUpRight size={15} aria-hidden="true" />
          </a>
        ) : (
          <button className="button button-secondary" type="button" disabled>
            暂无原文链接
          </button>
        )}
        <button
          className="button button-primary"
          type="button"
          aria-label={`创建 ${hotspot.title} 的内容项目`}
          onClick={onBuild}
          disabled={hotspot.is_filtered || hotspot.has_project || building}
          title={
            hotspot.is_filtered
              ? "已过滤热点不能创建项目"
              : hotspot.has_project
                ? "该热点已经生成项目"
                : ""
          }
        >
          {building ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <FolderPlus size={16} aria-hidden="true" />
          )}
          {building
            ? "创建中"
            : hotspot.has_project
              ? "已创建项目"
              : "创建内容项目"}
        </button>
      </div>
    </article>
  );
}

function HotspotDetailDialog({
  hotspot,
  loading,
  building,
  onBuild,
  onClose,
}: {
  hotspot: HotspotItem | null;
  loading: boolean;
  building: boolean;
  onBuild: () => void;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="detail-dialog hotspot-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="hotspot-detail-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div>
            <p className="dialog-label">热点详情</p>
            <h2 id="hotspot-detail-title">{hotspot?.title || "正在读取..."}</h2>
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

        {loading || !hotspot ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取详情...
          </div>
        ) : (
          <div className="hotspot-detail-content">
            <div className="detail-score-row">
              <ScoreItem label="热度分" value={hotspot.hot_score} icon="hot" />
              <ScoreItem label="选题价值" value={hotspot.topic_score} icon="topic" />
              <ScoreItem label="收藏价值" value={hotspot.save_score} icon="save" />
              <ScoreItem label="风险分" value={hotspot.risk_score} icon="risk" />
            </div>
            <dl className="detail-grid">
              <div>
                <dt>原文标题</dt>
                <dd>{hotspot.original_title || hotspot.title}</dd>
              </div>
              <div>
                <dt>来源站点</dt>
                <dd>{hotspot.source_platform}</dd>
              </div>
              <div>
                <dt>来源类型</dt>
                <dd>{sourceTypeLabel(hotspot.source_type)}</dd>
              </div>
              <div>
                <dt>来源可信度</dt>
                <dd>{hotspot.source_credibility}：{hotspot.source_credibility_reason}</dd>
              </div>
              <div>
                <dt>原文链接</dt>
                <dd>{hotspot.original_url || hotspot.url || "暂无"}</dd>
              </div>
              <div>
                <dt>分类</dt>
                <dd>{hotspot.category}</dd>
              </div>
              <div>
                <dt>数据来源</dt>
                <dd>{originLabel(hotspot.data_origin)}</dd>
              </div>
              <div>
                <dt>新鲜度分</dt>
                <dd>{Math.round(hotspot.freshness_score)}</dd>
              </div>
              <div>
                <dt>发布时间</dt>
                <dd>{formatDate(hotspot.published_at)}</dd>
              </div>
              <div>
                <dt>抓取时间</dt>
                <dd>{formatDate(hotspot.fetched_at)}</dd>
              </div>
            </dl>
            <div className="detail-text">
              <strong>正文摘要</strong>
              <p>{hotspot.summary || "暂无摘要"}</p>
            </div>
            <div className="tag-list">
              {hotspot.tags.map((tag) => (
                <span key={tag}>{tag}</span>
              ))}
            </div>
            {hotspot.is_filtered && (
              <p className="message message-error">
                <CircleSlash2 size={17} aria-hidden="true" />
                过滤原因：{hotspot.filter_reason || "规则过滤"}
              </p>
            )}
            {hotspot.duplicate_reason && (
              <p className="message">
                去重记录：{hotspot.duplicate_reason}
              </p>
            )}
            <div className="dialog-actions">
              {hotspot.url ? (
                <a
                  className="button button-secondary"
                  href={hotspot.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看原文
                  <ArrowUpRight size={15} aria-hidden="true" />
                </a>
              ) : (
                <button className="button button-secondary" type="button" disabled>
                  暂无原文链接
                </button>
              )}
              <button
                className="button button-primary"
                type="button"
                onClick={onBuild}
                disabled={hotspot.is_filtered || hotspot.has_project || building}
              >
                {building ? (
                  <LoaderCircle className="spin" size={16} aria-hidden="true" />
                ) : (
                  <FolderPlus size={16} aria-hidden="true" />
                )}
                {hotspot.has_project ? "已创建项目" : "创建内容项目"}
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function SelectField({
  label,
  value,
  options,
  emptyLabel,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  emptyLabel: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="select-field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{emptyLabel}</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

function ScoreItem({
  label,
  value,
  icon,
}: {
  label: string;
  value: number;
  icon: "hot" | "topic" | "save" | "risk";
}) {
  const Icon =
    icon === "hot" ? Flame : icon === "topic" ? Sparkles : icon === "risk" ? AlertTriangle : Bookmark;
  return (
    <div className={`score-item score-${icon}`}>
      <span>
        <Icon size={14} aria-hidden="true" />
        {label}
      </span>
      <strong>{Math.round(value)}</strong>
    </div>
  );
}

function SummaryItem({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: number;
  tone?: "normal" | "good";
}) {
  return (
    <div className={`summary-item summary-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function sourceTypeLabel(value: string): string {
  const labels: Record<string, string> = {
    news: "新闻",
    community: "社区",
    video: "视频",
    hotlist: "热榜",
    video_hotlist: "视频热榜",
    product: "产品",
    manual: "手动导入",
  };
  return labels[value] || value || "其他";
}

function originLabel(value: string): string {
  if (value === "sample") return "示例";
  if (value === "manual") return "手动";
  if (value === "real") return "真实";
  return value || "真实";
}

function credibilityTone(value: string): string {
  if (value === "高") return "status-good";
  if (value === "中") return "status-warning";
  return "status-muted";
}

function formatDate(value: string | null): string {
  if (!value) {
    return "时间未知";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
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

export default HotspotPool;
