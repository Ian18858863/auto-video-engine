import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  LoaderCircle,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";

import {
  composeTopicIdeaCollection,
  generateTopicIdeaScript,
  getHotspots,
  getTopicFilterSettings,
  getTopicIdeaDetail,
  getTopicIdeas,
  importManualHotspot,
  runAutoHotspotIntake,
  runBrowserHotspotIntake,
  saveTopicFilterSettings,
  setTopicEventSelection,
} from "./api";
import type {
  HotspotItem,
  HotspotListResponse,
  TopicCategoryBonusRule,
  TopicFilterSettings,
  TopicFilterWeights,
  TopicIdeaItem,
  TopicIdeaListResponse,
  TopicKeywordBonusRule,
} from "./types";
import "./styles.css";

const emptyList: TopicIdeaListResponse = {
  items: [],
  pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
  filters: { hotspots: [] },
};

const emptyHotspotList: HotspotListResponse = {
  items: [],
  pagination: { page: 1, page_size: 100, total: 0, total_pages: 0 },
  filters: { categories: [], sources: [] },
};

const HOTSPOT_CATEGORIES = ["社会", "财经", "科技", "消费", "AI", "国际", "生活", "娱乐"] as const;
const TOPIC_PAGE_SIZE = 100;

const defaultTopicFilterSettings: TopicFilterSettings = {
  weights: {
    impact: 25,
    account_fit: 20,
    freshness_heat: 20,
    cross_source_resonance: 15,
    ordinary_relevance: 20,
  },
  current_period_limit: 5,
  backup_pool_limit: 10,
  source_limit: 3,
  minimum_score: 40,
  freshness_window_hours: 36,
  bonus_rules: {
    categories: [
      { id: "category_ai", label: "AI 加分", category: "AI", score: 3, enabled: true },
    ],
    keywords: [
      { id: "keyword_ai", label: "AI 关键词", keyword: "AI", score: 2, enabled: true },
    ],
  },
  last_saved_at: "",
  is_active: true,
  using_default: true,
};

interface TopicIdeaPoolProps {
  initialHotspotId?: number | null;
  initialTopicIdeaId?: number | null;
  onOpenProject?: (pipelineId: number) => void;
  onOpenWorkspace?: (pipelineId: number) => void;
  onOpenHotspots?: () => void;
}

function TopicIdeaPool({
  initialHotspotId = null,
  initialTopicIdeaId = null,
  onOpenProject,
  onOpenWorkspace,
  onOpenHotspots,
}: TopicIdeaPoolProps) {
  const [ideas, setIdeas] = useState(emptyList);
  const [hotspots, setHotspots] = useState(emptyHotspotList);
  const [loading, setLoading] = useState(true);
  const [updatingToday, setUpdatingToday] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<TopicIdeaItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [hotspotLoading, setHotspotLoading] = useState(true);
  const [selectingId, setSelectingId] = useState<number | null>(null);
  const [generatingId, setGeneratingId] = useState<number | null>(null);
  const [hotspotCategoryFilter, setHotspotCategoryFilter] = useState("");
  const [hotspotSourceFilter, setHotspotSourceFilter] = useState("");
  const [hotspotSortBy, setHotspotSortBy] = useState<"hot_score" | "topic_score" | "save_score">("topic_score");
  const [filterSettings, setFilterSettings] = useState<TopicFilterSettings>(defaultTopicFilterSettings);
  const [filterSettingsOpen, setFilterSettingsOpen] = useState(false);
  const [filterSettingsDraft, setFilterSettingsDraft] = useState<TopicFilterSettings>(defaultTopicFilterSettings);
  const [savingFilterSettings, setSavingFilterSettings] = useState(false);
  const openedInitialTopic = useRef<number | null>(null);

  const loadTopicIdeas = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setIdeas(
        await getTopicIdeas({
          hotspotId: initialHotspotId === null ? "" : String(initialHotspotId),
          page: 1,
          pageSize: TOPIC_PAGE_SIZE,
        }),
      );
    } catch (loadError) {
      setError(toMessage(loadError, "候选事件读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [initialHotspotId]);

  const loadHotspots = useCallback(async () => {
    setHotspotLoading(true);
    setError("");
    try {
      setHotspots(
        await getHotspots({
          category: hotspotCategoryFilter,
          sourcePlatform: hotspotSourceFilter,
          unfilteredOnly: false,
          sortBy: hotspotSortBy,
          page: 1,
          pageSize: 100,
        }),
      );
    } catch (loadError) {
      setError(toMessage(loadError, "今日热点事件读取失败。"));
    } finally {
      setHotspotLoading(false);
    }
  }, [hotspotCategoryFilter, hotspotSortBy, hotspotSourceFilter]);

  const loadFilterSettings = useCallback(async () => {
    try {
      const settings = await getTopicFilterSettings();
      setFilterSettings(settings);
      setFilterSettingsDraft(settings);
    } catch (loadError) {
      setError(toMessage(loadError, "筛选标准读取失败。"));
    }
  }, []);

  useEffect(() => {
    void loadTopicIdeas();
  }, [loadTopicIdeas]);

  useEffect(() => {
    void loadFilterSettings();
  }, [loadFilterSettings]);

  useEffect(() => {
    void loadHotspots();
  }, [loadHotspots]);

  useEffect(() => {
    if (
      initialTopicIdeaId !== null &&
      openedInitialTopic.current !== initialTopicIdeaId
    ) {
      openedInitialTopic.current = initialTopicIdeaId;
      void openDetail(initialTopicIdeaId);
    }
  }, [initialTopicIdeaId]);

  async function openDetail(topicIdeaId: number) {
    setDetailLoading(true);
    setError("");
    try {
      setSelected(await getTopicIdeaDetail(topicIdeaId));
    } catch (detailError) {
      setError(toMessage(detailError, "候选事件详情读取失败。"));
    } finally {
      setDetailLoading(false);
    }
  }

  async function handleToggleCurrentPeriod(idea: TopicIdeaItem) {
    const inCurrentPeriod = isCurrentPeriodIdea(idea);
    setSelectingId(idea.id);
    setNotice("");
    setError("");
    try {
      let updated: TopicIdeaItem = idea;
      if (inCurrentPeriod) {
        if (idea.is_selected) {
          updated = await setTopicEventSelection(idea.id, false);
        }
        updated = { ...updated, is_selected: false };
        setNotice(`已移出本期：「${displayEventTitle(updated)}」。`);
      } else {
        updated = await setTopicEventSelection(idea.id, true);
        setNotice(`已选入本期：「${displayEventTitle(updated)}」。`);
      }
      setIdeas((current) => ({
        ...current,
        items: current.items.map((item) =>
          item.id === updated.id ? { ...item, ...updated } : item,
        ),
      }));
      setSelected((current) =>
        current?.id === updated.id
          ? { ...current, ...updated }
          : current,
      );
    } catch (selectError) {
      setError(toMessage(selectError, "更新本期事件失败。"));
    } finally {
      setSelectingId(null);
    }
  }

  async function handleComposeAndGenerate(selectedEvents: TopicIdeaItem[]) {
    if (selectedEvents.length < 1) {
      setError("请先至少选 1 件事件，再生成本期字幕/口播稿。");
      return;
    }
    setGeneratingId(-1);
    setNotice("");
    setError("");
    try {
      const collection = await composeTopicIdeaCollection(
        selectedEvents.map((idea) => idea.id),
      );
      const result = await generateTopicIdeaScript(collection.id);
      if (result.provider !== "fake") {
        throw new Error("安全校验失败：当前流程不是 fake provider。");
      }
      const collectionWithScript = {
        ...collection,
        is_selected: true,
        has_script: true,
        script_asset_id: result.script_asset_id,
        pipeline_id: result.pipeline_id,
      };
      setIdeas((current) => {
        const exists = current.items.some((item) => item.id === collection.id);
        return {
          ...current,
          items: exists
            ? current.items.map((item) =>
                item.id === collection.id ? collectionWithScript : item,
              )
            : [collectionWithScript, ...current.items],
        };
      });
      setNotice(
        `已把 ${selectedEvents.length} 件事件组合成一期视频，并生成测试字幕/口播稿（fake provider）。`,
      );
      if (onOpenWorkspace) {
        onOpenWorkspace(result.pipeline_id);
      } else {
        onOpenProject?.(result.pipeline_id);
      }
    } catch (generateError) {
      setError(toMessage(generateError, "生成本期字幕/口播稿失败。"));
    } finally {
      setGeneratingId(null);
    }
  }

  async function handleUpdateTodayEvents() {
    setUpdatingToday(true);
    setNotice("");
    setError("");
    try {
      const browserResult = await runBrowserHotspotIntake({
        force: true,
      });
      const result = await runAutoHotspotIntake({
        force: true,
        generate_topics: true,
        max_hotspots_for_topics: 70,
      });
      await Promise.all([loadTopicIdeas(), loadHotspots()]);
      const warningParts = [
        browserResult.failed_sources.length > 0
          ? `浏览器失败 ${browserResult.failed_sources.length} 个来源`
          : "",
        browserResult.needs_login_sources.length > 0
          ? `需要重新登录 ${browserResult.needs_login_sources.join("、")}`
          : "",
        result.failed_sources.length > 0
          ? `公开源失败 ${result.failed_sources.length} 个来源`
          : "",
        result.rate_limited_sources.length > 0
          ? `限流 ${result.rate_limited_sources.length} 个来源`
          : "",
      ].filter(Boolean);
      setNotice(
        `已更新今日事件：浏览器新增 ${browserResult.saved_count} 条，公开源新增 ${result.saved_count} 条，生成候选事件 ${result.generated_topic_idea_count} 个。${
          warningParts.length > 0 ? ` ${warningParts.join("，")}。` : ""
        }`,
      );
    } catch (updateError) {
      setError(toMessage(updateError, "更新今日事件失败。"));
    } finally {
      setUpdatingToday(false);
    }
  }

  function openFilterSettings() {
    setFilterSettingsDraft(structuredClone(filterSettings));
    setFilterSettingsOpen(true);
  }

  async function handleSaveFilterSettings() {
    setSavingFilterSettings(true);
    setNotice("");
    setError("");
    try {
      const saved = await saveTopicFilterSettings(filterSettingsDraft);
      setFilterSettings(saved);
      setFilterSettingsDraft(saved);
      setFilterSettingsOpen(false);
      await loadTopicIdeas();
      setNotice("筛选标准已更新，并已重新筛选候选事件。");
    } catch (saveError) {
      setError(toMessage(saveError, "筛选标准保存失败。"));
    } finally {
      setSavingFilterSettings(false);
    }
  }

  const activeIdeas = dedupeIdeasByTitle(ideas.items);
  const decisionWindow = rankIdeasForRecommendationWindow(
    activeIdeas.filter(isEventIdea),
    {
      limit: filterSettings.current_period_limit + filterSettings.backup_pool_limit,
      sourceLimit: filterSettings.source_limit,
    },
  );
  const selectedEvents = decisionWindow.slice(0, filterSettings.current_period_limit);
  const defaultSelectedIds = new Set(selectedEvents.map((idea) => idea.id));
  const backupIdeas = decisionWindow
    .filter((idea) => !defaultSelectedIds.has(idea.id))
    .slice(0, filterSettings.backup_pool_limit);
  const canGenerateDailyScript = selectedEvents.length >= 1;

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>选题</h1>
        </div>
        <div className="page-header-actions topic-page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleUpdateTodayEvents()}
            disabled={loading || updatingToday}
          >
            {updatingToday ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            {updatingToday ? "更新中" : "更新今日事件（含浏览器热点）"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={openFilterSettings}
          >
            <SlidersHorizontal size={16} aria-hidden="true" />
            更新筛选标准
          </button>
        </div>
      </header>

      <section className="collection-recommendation" aria-label="本期事件组合">
        <div>
          <h2>本期已选</h2>
        </div>
        {selectedEvents.length > 0 ? (
          <ol className="selected-event-list">
            {selectedEvents.map((idea) => {
              const title = displayEventTitle(idea);
              return (
                <li key={idea.id}>
                  <span>
                    <strong>{title}</strong>
                    <small>
                      <TopicMetaBadges idea={idea} />
                    </small>
                  </span>
                  <div className="selected-event-actions">
                    <button
                      className="button button-secondary compact-button current-event-toggle"
                      type="button"
                      aria-label={`移出本期 ${title}`}
                      onClick={() => void handleToggleCurrentPeriod(idea)}
                      disabled={selectingId === idea.id}
                    >
                      <span className="default-label">已选入</span>
                      <span className="hover-label">移出本期</span>
                    </button>
                    <button
                      className="button button-secondary compact-button"
                      type="button"
                      aria-label={`查看本期详情 ${title}`}
                      onClick={() => void openDetail(idea.id)}
                    >
                      查看详情
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="today-empty">还没有选入本期的事件。</p>
        )}
        {selectedEvents.length > 6 && (
          <p className="collection-warning">内容较多，建议精简，但仍可生成。</p>
        )}
        <button
          className="button button-primary"
          type="button"
          onClick={() => void handleComposeAndGenerate(selectedEvents)}
          disabled={!canGenerateDailyScript || generatingId === -1}
        >
          {generatingId === -1 ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <Sparkles size={16} aria-hidden="true" />
          )}
          生成测试口播稿
        </button>
        <p className="collection-warning">
          测试模式：使用 fake provider，不调用真实 AI。
        </p>
      </section>

      {notice && (
        <div className="message message-success" role="status">
          <CheckCircle2 size={17} aria-hidden="true" />
          <span>{notice}</span>
        </div>
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
            正在读取候选事件...
          </div>
        ) : ideas.items.length === 0 ? (
          <div className="empty-state empty-state-stacked">
            <strong>还没有候选事件。</strong>
            <span>先运行自动热点抓取，系统会把公开热点精炼成候选事件。</span>
            {onOpenHotspots && (
              <button
                className="button button-primary"
                type="button"
                onClick={onOpenHotspots}
              >
                去自动热点
              </button>
            )}
          </div>
        ) : (
          <>
            <TopicPoolSection title="备选池" empty="其余事件会在这里备用。">
              {backupIdeas.map((idea) => (
                <TopicIdeaRow
                  key={idea.id}
                  idea={idea}
                  selecting={selectingId === idea.id}
                  onOpen={() => void openDetail(idea.id)}
                  onSelect={() => void handleToggleCurrentPeriod(idea)}
                />
              ))}
            </TopicPoolSection>
            <TodayHotspotEvents
              items={hotspots.items}
              total={hotspots.pagination.total}
              filters={hotspots.filters}
              loading={hotspotLoading}
              category={hotspotCategoryFilter}
              source={hotspotSourceFilter}
              sortBy={hotspotSortBy}
              onCategoryChange={setHotspotCategoryFilter}
              onSourceChange={setHotspotSourceFilter}
              onSortChange={setHotspotSortBy}
              onManualImport={() => void handleQuickManualImport()}
            />
          </>
        )}
      </section>

      {(selected || detailLoading) && (
        <TopicIdeaDetailDialog
          idea={selected}
          loading={detailLoading}
          onClose={() => setSelected(null)}
        />
      )}
      {filterSettingsOpen && (
        <TopicFilterSettingsDialog
          draft={filterSettingsDraft}
          saving={savingFilterSettings}
          onChange={setFilterSettingsDraft}
          onClose={() => setFilterSettingsOpen(false)}
          onSave={() => void handleSaveFilterSettings()}
        />
      )}
    </main>
  );

  async function handleQuickManualImport() {
    const title = window.prompt("请输入热点标题");
    if (!title?.trim()) return;
    setNotice("");
    setError("");
    try {
      await importManualHotspot({
        title: title.trim(),
        source_platform: "手动导入",
        url: "",
        summary: "",
        category: hotspotCategoryFilter || "其他",
        tags: [],
        published_at: null,
        notes: "",
      });
      setNotice("已手动导入热点。");
      await loadHotspots();
    } catch (importError) {
      setError(toMessage(importError, "手动导入失败。"));
    }
  }
}

function TopicIdeaRow({
  idea,
  selecting,
  onOpen,
  onSelect,
}: {
  idea: TopicIdeaItem;
  selecting: boolean;
  onOpen: () => void;
  onSelect: () => void;
}) {
  const selectable = idea.video_type !== "multi_hotspot_collection";
  const inCurrentPeriod = isCurrentPeriodIdea(idea);
  const title = displayEventTitle(idea);
  return (
    <article className={`topic-row ${inCurrentPeriod ? "selected" : ""}`}>
      <div className="topic-main">
        <div className="topic-title-line">
          <button
            className="project-title"
            type="button"
            aria-label={`查看 ${title}`}
            onClick={onOpen}
          >
            {title}
          </button>
        </div>
        <div className="topic-meta-line">
          <TopicMetaBadges idea={idea} />
        </div>
      </div>

      <div className="topic-actions">
        <button
          className={`button button-secondary ${inCurrentPeriod ? "current-event-toggle" : ""}`}
          type="button"
          aria-label={`${inCurrentPeriod ? "移出本期" : "选入本期"} ${title}`}
          onClick={onSelect}
          disabled={selecting || !selectable}
        >
          {selecting ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <Check size={16} aria-hidden="true" />
          )}
          {inCurrentPeriod ? (
            <>
              <span className="default-label">已选入</span>
              <span className="hover-label">移出本期</span>
            </>
          ) : (
            "选入本期"
          )}
        </button>
        <button className="button button-secondary" type="button" onClick={onOpen}>
          查看详情
        </button>
      </div>
    </article>
  );
}

function TopicPoolSection({
  title,
  empty,
  children,
}: {
  title: string;
  empty: string;
  children: ReactNode;
}) {
  const items = Array.isArray(children) ? children.filter(Boolean) : children;
  const isEmpty = Array.isArray(items) ? items.length === 0 : !items;
  return (
    <section className="topic-pool-section">
      <h2>{title}</h2>
      {isEmpty ? (
        <p className="today-empty">{empty}</p>
      ) : (
        <div className="topic-list">{children}</div>
      )}
    </section>
  );
}

const weightLabels: Array<[keyof TopicFilterWeights, string]> = [
  ["impact", "影响力度"],
  ["account_fit", "账号匹配度"],
  ["freshness_heat", "时效热度"],
  ["cross_source_resonance", "跨源共振"],
  ["ordinary_relevance", "普通人相关度"],
];

function TopicFilterSettingsDialog({
  draft,
  saving,
  onChange,
  onClose,
  onSave,
}: {
  draft: TopicFilterSettings;
  saving: boolean;
  onChange: (value: TopicFilterSettings) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  const weightTotal = weightLabels.reduce((sum, [key]) => sum + Number(draft.weights[key] || 0), 0);
  const canSave = weightTotal === 100 && !saving;
  const updateWeight = (key: keyof TopicFilterWeights, value: string) => {
    onChange({
      ...draft,
      weights: { ...draft.weights, [key]: numberValue(value) },
    });
  };
  const updateNumber = (
    key: "current_period_limit" | "backup_pool_limit" | "source_limit" | "minimum_score",
    value: string,
  ) => {
    onChange({ ...draft, [key]: numberValue(value) });
  };
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="detail-dialog topic-filter-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="topic-filter-settings-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div>
            <p className="dialog-label">选题规则</p>
            <h2 id="topic-filter-settings-title">更新筛选标准</h2>
          </div>
          <button className="icon-button" type="button" aria-label="关闭筛选标准" onClick={onClose}>
            <X size={19} aria-hidden="true" />
          </button>
        </header>
        <div className="topic-filter-body">
          <p className="settings-note">
            五个基础权重合计必须等于 100。额外加分单独计算，不挤占基础权重。
          </p>
          <section className="topic-filter-panel">
            <h3>五维基础权重</h3>
            <div className="topic-filter-grid">
              {weightLabels.map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={draft.weights[key]}
                    onChange={(event) => updateWeight(key, event.target.value)}
                    aria-label={label}
                  />
                </label>
              ))}
            </div>
            <p className={weightTotal === 100 ? "settings-total ok" : "settings-total warning"}>
              当前合计 {weightTotal}
            </p>
          </section>
          <section className="topic-filter-panel">
            <h3>推荐窗口</h3>
            <div className="topic-filter-grid">
              <label>
                本期已选数量
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={draft.current_period_limit}
                  onChange={(event) => updateNumber("current_period_limit", event.target.value)}
                />
              </label>
              <label>
                备选池数量
                <input
                  type="number"
                  min={0}
                  max={30}
                  value={draft.backup_pool_limit}
                  onChange={(event) => updateNumber("backup_pool_limit", event.target.value)}
                />
              </label>
              <label>
                同一来源最多几条
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={draft.source_limit}
                  onChange={(event) => updateNumber("source_limit", event.target.value)}
                />
              </label>
              <label>
                最低入池分
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={draft.minimum_score}
                  onChange={(event) => updateNumber("minimum_score", event.target.value)}
                />
              </label>
            </div>
          </section>
          <BonusRuleEditor
            title="分类额外加分"
            type="category"
            rules={draft.bonus_rules.categories}
            onChange={(categories) =>
              onChange({ ...draft, bonus_rules: { ...draft.bonus_rules, categories } })
            }
          />
          <BonusRuleEditor
            title="关键词额外加分"
            type="keyword"
            rules={draft.bonus_rules.keywords}
            onChange={(keywords) =>
              onChange({ ...draft, bonus_rules: { ...draft.bonus_rules, keywords } })
            }
          />
        </div>
        <div className="dialog-actions">
          <button className="button button-secondary" type="button" onClick={onClose}>
            取消
          </button>
          <button className="button button-primary" type="button" onClick={onSave} disabled={!canSave}>
            {saving ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : null}
            保存并重新筛选
          </button>
        </div>
      </section>
    </div>
  );
}

function BonusRuleEditor({
  title,
  type,
  rules,
  onChange,
}: {
  title: string;
  type: "category" | "keyword";
  rules: TopicCategoryBonusRule[] | TopicKeywordBonusRule[];
  onChange: (rules: any[]) => void;
}) {
  const valueField = type === "category" ? "category" : "keyword";
  const labelPrefix = type === "category" ? "分类" : "关键词";
  const scorePrefix = type === "category" ? "分类加分" : "关键词加分";
  function updateRule(index: number, partial: Record<string, unknown>) {
    onChange(rules.map((rule, ruleIndex) => (ruleIndex === index ? { ...rule, ...partial } : rule)));
  }
  function addRule() {
    const nextIndex = rules.length + 1;
    onChange([
      ...rules,
      {
        id: `${type}_${Date.now()}`,
        label: `${labelPrefix}加分${nextIndex}`,
        [valueField]: "",
        score: 1,
        enabled: true,
      },
    ]);
  }
  return (
    <section className="topic-filter-panel">
      <div className="topic-filter-section-header">
        <h3>{title}</h3>
        <button className="button button-secondary compact-button" type="button" onClick={addRule}>
          <Plus size={14} aria-hidden="true" />
          添加{labelPrefix}加分
        </button>
      </div>
      <div className="bonus-rule-list">
        {rules.map((rule, index) => (
          <div className="bonus-rule-row" key={rule.id || index}>
            <label>
              启用
              <input
                type="checkbox"
                checked={rule.enabled}
                onChange={(event) => updateRule(index, { enabled: event.target.checked })}
              />
            </label>
            <label>
              标签
              <input
                value={rule.label}
                onChange={(event) => updateRule(index, { label: event.target.value })}
              />
            </label>
            <label>
              {labelPrefix}
              <input
                aria-label={`${labelPrefix} ${index + 1}`}
                value={String((rule as any)[valueField] || "")}
                onChange={(event) => updateRule(index, { [valueField]: event.target.value })}
              />
            </label>
            <label>
              加分
              <input
                aria-label={`${scorePrefix} ${index + 1}`}
                type="number"
                min={0}
                max={30}
                value={rule.score}
                onChange={(event) => updateRule(index, { score: numberValue(event.target.value) })}
              />
            </label>
            <button
              className="button button-secondary compact-button"
              type="button"
              onClick={() => onChange(rules.filter((_, ruleIndex) => ruleIndex !== index))}
            >
              删除
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function TodayHotspotEvents({
  items,
  total,
  filters,
  loading,
  category,
  source,
  sortBy,
  onCategoryChange,
  onSourceChange,
  onSortChange,
  onManualImport,
}: {
  items: HotspotItem[];
  total: number;
  filters: HotspotListResponse["filters"];
  loading: boolean;
  category: string;
  source: string;
  sortBy: "hot_score" | "topic_score" | "save_score";
  onCategoryChange: (value: string) => void;
  onSourceChange: (value: string) => void;
  onSortChange: (value: "hot_score" | "topic_score" | "save_score") => void;
  onManualImport: () => void;
}) {
  const categoryCounts = filters.category_counts || {};
  const sourceCounts = filters.source_counts || {};
  const sourceOptions = sourceFilterOptions(filters.sources, sourceCounts, source);
  return (
    <section className="today-hotspot-events" aria-label="今日热点事件">
      <header>
        <h2>今日热点事件</h2>
        <span>当前共 {total} 条，当前显示 {items.length} 条</span>
      </header>
      <div className="topic-hotspot-tools">
        <label>
          分类
          <select
            value={category}
            onChange={(event) => onCategoryChange(event.target.value)}
          >
            <option value="">全部分类</option>
            {HOTSPOT_CATEGORIES.map((item) => (
              <option value={item} key={item}>
                {item} {categoryCounts[item] ?? 0}条
              </option>
            ))}
          </select>
        </label>
        <label>
          来源
          <select
            value={source}
            onChange={(event) => onSourceChange(event.target.value)}
          >
            <option value="">全部来源</option>
            {sourceOptions.map((item) => (
              <option value={item} key={item}>
                {item} {sourceCounts[item] ?? 0}条
              </option>
            ))}
          </select>
        </label>
        <label>
          排序
          <select
            value={sortBy}
            onChange={(event) =>
              onSortChange(event.target.value as "hot_score" | "topic_score" | "save_score")
            }
          >
            <option value="topic_score">选题价值分</option>
            <option value="hot_score">热度分</option>
            <option value="save_score">收藏价值分</option>
          </select>
        </label>
        <button className="button button-secondary compact-button" type="button" onClick={onManualImport}>
          <Plus size={14} aria-hidden="true" />
          手动导入
        </button>
      </div>
      {loading ? (
        <p className="today-empty">正在刷新今日热点事件...</p>
      ) : items.length > 0 ? (
        <ul className="hotspot-event-list">
          {items.map((item) => (
            <li key={item.id}>
              <span>
                <strong>{(item.original_title || item.title || "今日事件").trim()}</strong>
                <small>
                  {item.source_platform || "公开来源"} · {hotspotCategory(item)} ·{" "}
                  {formatDateTime(item.published_at || item.fetched_at)}
                  {item.data_origin.includes("manual") ? " · 手动导入" : ""}
                </small>
              </span>
              {item.url || item.original_url ? (
                <a
                  className="button button-secondary compact-button"
                  href={item.url || item.original_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看原文
                </a>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <div className="empty-state empty-state-stacked">
          <strong>当前筛选下没有热点。</strong>
          <span>可以换一个分类/来源，或者点“手动导入”补充热点。</span>
        </div>
      )}
    </section>
  );
}

function sourceFilterOptions(
  sources: string[],
  counts: Record<string, number>,
  currentSource: string,
): string[] {
  const counted = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([name]) => name);
  const sourceSet = new Set(counted.length > 0 ? counted : sources);
  if (currentSource) sourceSet.add(currentSource);
  return Array.from(sourceSet).sort();
}

function TopicIdeaDetailDialog({
  idea,
  loading,
  onClose,
}: {
  idea: TopicIdeaItem | null;
  loading: boolean;
  onClose: () => void;
}) {
  const sourceUrls = idea?.source_hotspot_urls ?? [];
  const firstSourceUrl = sourceUrls.find(Boolean) ?? "";
  const firstPublishedAt = firstNonEmpty(idea?.source_published_at ?? []);
  const firstSummary = firstNonEmpty(idea?.source_summaries ?? []);
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="topic-detail-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div>
            <p className="dialog-label">候选事件详情</p>
            <h2 id="topic-detail-title">
              {idea ? sourceHotspotText(idea) : "正在读取..."}
            </h2>
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
        {loading || !idea ? (
          <div className="empty-state">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取详情...
          </div>
        ) : (
          <div className="topic-detail-content">
            <DetailItem label="原始热点" value={sourceHotspotText(idea)} />
            <DetailItem label="原文链接" value={firstSourceUrl || "暂无"} />
            <DetailItem label="来源" value={sourcePlatformText(idea)} />
            <DetailItem label="发布时间" value={formatDateTime(firstPublishedAt)} />
            <DetailItem label="正文摘要" value={firstSummary || idea.core_view} />
            <DetailItem label="总分" value={String(Math.round(idea.recommendation_score))} />
            <DetailItem label="评分构成" value={scoreBreakdownText(idea)} />
            <DetailItem label="推荐理由" value={idea.why_watch} />
            <DetailItem label="与普通人的关系" value={idea.core_view} />
            <DetailItem label="深挖/合集" value={deepDiveLabel(idea)} />
            {sourceUrls.length > 0 && (
              <div className="dialog-actions">
                {sourceUrls.slice(0, 5).map((url, index) => (
                  <a
                    className="button button-secondary"
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    key={url}
                  >
                    查看原文{sourceUrls.length > 1 ? index + 1 : ""}
                  </a>
                ))}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="topic-detail-item">
      <strong>{label}</strong>
      <p>{value || "暂无"}</p>
    </div>
  );
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function sourceHotspotText(idea: TopicIdeaItem): string {
  const titles = idea.source_hotspot_titles?.length
    ? idea.source_hotspot_titles
    : [idea.hotspot_title];
  return titles
    .map((title) => title.trim())
    .filter(Boolean)
    .slice(0, 5)
    .join(" / ");
}

function displayEventTitle(idea: TopicIdeaItem): string {
  return sourceHotspotText(idea) || idea.hotspot_title || idea.title || "今日事件";
}

function sourcePlatformText(idea: TopicIdeaItem): string {
  const platforms = idea.source_platforms?.filter(Boolean) ?? [];
  if (platforms.length > 0) return platforms.slice(0, 2).join(" / ");
  return "公开来源";
}

function isEventIdea(idea: TopicIdeaItem): boolean {
  return idea.video_type !== "multi_hotspot_collection" && !idea.is_example;
}

function isCurrentPeriodIdea(idea: TopicIdeaItem): boolean {
  return idea.is_selected || idea.recommendation_bucket === "today";
}

function dedupeIdeasByTitle(ideas: TopicIdeaItem[]): TopicIdeaItem[] {
  const seen = new Set<string>();
  const sorted = [...ideas].sort(
    (left, right) => right.recommendation_score - left.recommendation_score,
  );
  return sorted.filter((idea) => {
    const key = normalizeEventTitle(displayEventTitle(idea));
    if (!key) return true;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function rankIdeasForRecommendationWindow(
  ideas: TopicIdeaItem[],
  options: { limit: number; sourceLimit: number },
): TopicIdeaItem[] {
  const sourceCounts: Record<string, number> = {};
  const selected: TopicIdeaItem[] = [];
  const ranked = [...ideas].sort(
    (left, right) => right.recommendation_score - left.recommendation_score,
  );
  for (const idea of ranked) {
    const source = sourceKeyForIdea(idea);
    if ((sourceCounts[source] || 0) >= options.sourceLimit) {
      continue;
    }
    selected.push(idea);
    sourceCounts[source] = (sourceCounts[source] || 0) + 1;
    if (selected.length >= options.limit) {
      break;
    }
  }
  return selected;
}

function sourceKeyForIdea(idea: TopicIdeaItem): string {
  return idea.source_platforms?.find(Boolean)?.trim() || "公开来源";
}

function categoryKeyForIdea(idea: TopicIdeaItem): string {
  const text = [
    idea.title,
    idea.hotspot_title,
    idea.core_view,
    idea.recommended_format,
    idea.why_watch,
    ...(idea.source_hotspot_titles || []),
  ].join(" ");
  return scoredHotspotCategory(text);
}

function normalizeEventTitle(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^\p{Script=Han}\p{Letter}\p{Number}]+/gu, "")
    .trim();
}

function deepDiveLabel(idea: TopicIdeaItem): string {
  return idea.can_single_deep_dive ||
    idea.video_type === "single_deep_dive" ||
    idea.video_type === "single_deep_dive_candidate"
    ? "深挖"
    : "合集";
}

function firstNonEmpty(values: Array<string | null | undefined>): string {
  return values.find((value) => Boolean(value?.trim()))?.trim() ?? "";
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function hotspotCategory(item: HotspotItem): (typeof HOTSPOT_CATEGORIES)[number] {
  const text = `${item.category} ${item.title} ${item.original_title} ${(item.tags || []).join(" ")}`;
  const normalizedCategory = normalizeHotspotCategory(item.category);
  if (normalizedCategory) return normalizedCategory;
  if (isEntertainmentOnlyText(text)) return "娱乐";
  if (hasAny(text, ["AI", "人工智能", "大模型", "智能体", "Agent", "OpenAI", "DeepSeek", "豆包"])) return "AI";
  return scoredHotspotCategory(text);
}

function isEntertainmentOnlyText(text: string): boolean {
  return hasAny(text, [
    "电影",
    "上映",
    "明星",
    "艺人",
    "综艺",
    "电视剧",
    "演唱会",
    "游戏",
    "手游",
    "电竞",
    "体育",
    "比赛",
    "八卦",
  ]) && !hasAny(text, [
    "平台",
    "票房",
    "院线",
    "流媒体",
    "商业",
    "广告",
    "AI",
    "人工智能",
    "影视工业",
    "内容产业",
    "商业模式",
    "公司战略",
  ]);
}

function normalizeHotspotCategory(category: string): (typeof HOTSPOT_CATEGORIES)[number] | "" {
  if (category === "商业") return "财经";
  if (category === "互联网趋势") return "科技";
  if ((HOTSPOT_CATEGORIES as readonly string[]).includes(category)) {
    return category as (typeof HOTSPOT_CATEGORIES)[number];
  }
  return "";
}

function scoredHotspotCategory(text: string): (typeof HOTSPOT_CATEGORIES)[number] {
  const scores: Record<(typeof HOTSPOT_CATEGORIES)[number], number> = {
    社会: 0,
    财经: keywordHits(text, ["财经", "金融", "股市", "基金", "房贷", "楼市", "利率", "价格", "涨价", "降价", "裁员", "就业"]),
    科技: keywordHits(text, ["科技", "芯片", "机器人", "Robotaxi", "自动驾驶", "智驾", "手机", "新能源", "硬件", "系统"]),
    消费: keywordHits(text, ["消费", "电商", "外卖", "零售", "平台补贴", "用户", "品牌", "门店", "商家"]),
    AI: keywordHits(text, ["AI", "人工智能", "大模型", "智能体", "Agent", "OpenAI", "DeepSeek", "豆包"]),
    国际: keywordHits(text, ["国际", "全球", "美国", "欧洲", "日本", "韩国", "海外", "关税", "出口"]),
    生活: keywordHits(text, ["生活", "教育", "医疗", "出行", "住房", "通勤", "旅游", "养老", "医保"]),
    娱乐: keywordHits(text, ["电影", "上映", "明星", "综艺", "游戏", "体育"]),
  };
  const ranked = (Object.entries(scores) as Array<[(typeof HOTSPOT_CATEGORIES)[number], number]>)
    .sort((left, right) => right[1] - left[1]);
  return ranked[0][1] > 0 ? ranked[0][0] : "社会";
}

function keywordHits(text: string, keywords: string[]): number {
  const lower = text.toLowerCase();
  return keywords.reduce(
    (score, keyword) => score + (lower.includes(keyword.toLowerCase()) ? 1 : 0),
    0,
  );
}

function hasAny(text: string, keywords: string[]): boolean {
  const lower = text.toLowerCase();
  return keywords.some((keyword) => lower.includes(keyword.toLowerCase()));
}

function scoreBreakdownText(idea: TopicIdeaItem): string {
  const breakdown = idea.score_breakdown || {};
  const parts = [
    ["影响力度", breakdown.impact],
    ["账号匹配度", breakdown.account_fit],
    ["时效热度", breakdown.freshness_heat],
    ["跨源共振", breakdown.cross_source_resonance],
    ["普通人相关度", breakdown.ordinary_relevance],
    ["风险分", breakdown.risk],
  ];
  const text = parts
    .filter(([, value]) => typeof value === "number")
    .map(([label, value]) => `${label} ${Math.round(Number(value))}`)
    .join(" / ");
  return text || "暂无评分构成";
}

function TopicMetaBadges({ idea }: { idea: TopicIdeaItem }) {
  return (
    <>
      <span className="category-label">总分 {Math.round(idea.recommendation_score)}</span>
      <span className="category-label">来源 {sourcePlatformText(idea)}</span>
      {highScoreLabels(idea).map((label) => (
        <span className="category-label score-highlight" key={label}>
          {label}
        </span>
      ))}
    </>
  );
}

function highScoreLabels(idea: TopicIdeaItem): string[] {
  const breakdown = idea.score_breakdown || {};
  const labels: Array<[string, unknown]> = [
    ["影响力高", breakdown.impact],
    ["账号匹配度高", breakdown.account_fit],
    ["时效高", breakdown.freshness_heat],
    ["跨源共振高", breakdown.cross_source_resonance],
    ["普通人相关度高", breakdown.ordinary_relevance],
  ];
  return labels
    .filter(([, value]) => typeof value === "number" && Number(value) >= 80)
    .map(([label]) => label)
    .concat(idea.score_bonus_reasons || []);
}

function numberValue(value: string): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

export default TopicIdeaPool;
