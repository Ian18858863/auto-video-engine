import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, LoaderCircle, RefreshCw } from "lucide-react";

import AccountStylePage from "./AccountStylePage";
import SecondaryTabs from "./SecondaryTabs";
import {
  clearDemoData,
  getRuntimeSettingsStatus,
  getAutomationSourceStatus,
  openBrowserHotspotLoginBrowser,
  runAutoHotspotIntake,
  runBrowserHotspotIntake,
  runManualV1Test,
  seedDemoData,
} from "./api";
import type {
  AutoHotspotRunResult,
  ManualV1RunResult,
  RuntimeCapabilityTruth,
  RuntimeSettingsStatus,
  SourceRunStatus,
} from "./types";

function SettingsPage() {
  const [tab, setTab] =
    useState<"style" | "sources" | "ai" | "paths" | "checks">("style");
  return (
    <>
      <SecondaryTabs
        label="设置视图"
        items={[
          { id: "style", label: "账号风格" },
          { id: "sources", label: "数据源" },
          { id: "ai", label: "AI 配置" },
          { id: "paths", label: "路径配置" },
          { id: "checks", label: "系统检查" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "style" && <AccountStylePage />}
      {tab === "sources" && <SourceSettingsPanel />}
      {tab === "ai" && <AiSettingsPanel />}
      {tab === "paths" && <PathSettingsPanel />}
      {tab === "checks" && <SystemCheckPanel />}
    </>
  );
}

function SourceSettingsPanel() {
  const [statuses, setStatuses] = useState<SourceRunStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [browserRunning, setBrowserRunning] = useState(false);
  const [loginOpening, setLoginOpening] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    setLoading(true);
    setError("");
    getAutomationSourceStatus()
      .then((result) => setStatuses(result.items))
      .catch((loadError) => setError(toMessage(loadError, "数据源配置读取失败。")))
      .finally(() => setLoading(false));
  }, [refreshKey]);

  async function handleRunSources() {
    setRunning(true);
    setError("");
    setNotice("");
    try {
      const result: AutoHotspotRunResult = await runAutoHotspotIntake({
        force: true,
        generate_topics: true,
        max_hotspots_for_topics: 15,
      });
      const latest = await getAutomationSourceStatus();
      setStatuses(latest.items);
      setNotice(
        `抓取 ${result.fetched_count} 条，新增 ${result.saved_count} 条，候选事件 ${result.generated_topic_idea_count} 个。`,
      );
    } catch (runError) {
      setError(toMessage(runError, "公开源抓取失败。"));
    } finally {
      setRunning(false);
    }
  }

  async function handleRunBrowserSources() {
    setBrowserRunning(true);
    setError("");
    setNotice("");
    try {
      const result = await runBrowserHotspotIntake({ force: true });
      const latest = await getAutomationSourceStatus();
      setStatuses(latest.items);
      setNotice(result.message);
    } catch (runError) {
      setError(toMessage(runError, "浏览器公开热点采集失败。"));
    } finally {
      setBrowserRunning(false);
    }
  }

  async function handleOpenLoginBrowser() {
    setLoginOpening(true);
    setError("");
    setNotice("");
    try {
      const result = await openBrowserHotspotLoginBrowser();
      setNotice(result.message);
    } catch (openError) {
      setError(toMessage(openError, "打开登录浏览器失败。"));
    } finally {
      setLoginOpening(false);
    }
  }

  const summary = summarizeSourceStatuses(statuses);
  const groups = groupSourceStatuses(statuses);

  return (
    <main className="app-shell source-settings-shell">
      <header className="page-header">
        <div>
          <h1>数据源配置</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-primary"
            type="button"
            onClick={() => void handleRunSources()}
            disabled={loading || running || browserRunning}
          >
            {running ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            {running ? "抓取中" : "立即抓取公开源"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleRunBrowserSources()}
            disabled={loading || running || browserRunning || loginOpening}
          >
            {browserRunning ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            {browserRunning ? "采集中" : "采集浏览器热点"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleOpenLoginBrowser()}
            disabled={loading || running || browserRunning || loginOpening}
          >
            {loginOpening ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            {loginOpening ? "打开中" : "打开登录浏览器"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading || running || browserRunning || loginOpening}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新状态
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
      <section className="source-overview-grid" aria-label="数据源概况">
        <SourceMetric label="启用来源" value={statuses.length || "0"} />
        <SourceMetric label="成功" value={summary.success} tone="good" />
        <SourceMetric label="失败" value={summary.failed} tone={summary.failed ? "danger" : "normal"} />
        <SourceMetric label="限流" value={summary.limited} tone={summary.limited ? "warning" : "normal"} />
      </section>
      <section className="source-config-panel">
        <div>
          <span>配置文件</span>
          <strong>config/source_config.yaml</strong>
        </div>
        <div>
          <span>公开源类型</span>
          <strong>国内公开热榜、浏览器公开采集、RSS、Google News RSS、Hacker News、Reddit</strong>
        </div>
        <div>
          <span>抓取策略</span>
          <strong>低频采集、不绕验证码、不批量下载视频、失败隔离</strong>
        </div>
        <div>
          <span>手动导入</span>
          <strong>备用入口，不参与主流程优先级</strong>
        </div>
      </section>
      <section className="source-group-list" aria-label="数据源运行状态">
        {loading ? (
          <span className="muted-text">
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
            正在读取状态...
          </span>
        ) : statuses.length === 0 ? (
          <div className="empty-state empty-state-stacked">
            <strong>当前没有启用的数据源。</strong>
            <span>在配置文件里添加 RSS 或关键词后，自动抓取入口会读取它们。</span>
          </div>
        ) : (
          groups.map((group) => (
            <section className="source-group-card" key={group.name}>
              <header>
                <h2>{group.name}</h2>
                <span>{group.items.length} 个来源</span>
              </header>
              <div className="source-status-list">
                {group.items.map((item, index) => (
                  <article
                    className="source-status-row"
                    key={`${item.source_name}-${index}`}
                  >
                    <div>
                      <strong>{item.source_name}</strong>
                      <small>{formatSourceTime(item.last_run_at)}</small>
                    </div>
                    <span className={`status-badge ${sourceStatusClass(item)}`}>
                      {item.is_rate_limited ? "限流" : item.status}
                    </span>
                    <span>成功 {item.success_count ?? item.items_fetched} 条</span>
                    <p>{item.failure_reason || "运行正常"}</p>
                  </article>
                ))}
              </div>
            </section>
          ))
        )}
      </section>
    </main>
  );
}

function SourceMetric({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: number | string;
  tone?: "normal" | "good" | "warning" | "danger";
}) {
  return (
    <article className={`source-metric-card source-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function summarizeSourceStatuses(statuses: SourceRunStatus[]) {
  return statuses.reduce(
    (summary, item) => {
      if (item.is_rate_limited || item.status === "限流") summary.limited += 1;
      else if (item.status === "成功") summary.success += 1;
      else if (item.status === "失败") summary.failed += 1;
      else summary.pending += 1;
      return summary;
    },
    { success: 0, failed: 0, limited: 0, pending: 0 },
  );
}

function groupSourceStatuses(statuses: SourceRunStatus[]) {
  const groupOrder = [
    "国内公开热榜",
    "浏览器公开采集",
    "公共 RSS",
    "Google News RSS",
    "Hacker News",
    "Reddit",
  ];
  const grouped = new Map<string, SourceRunStatus[]>();
  for (const status of statuses) {
    const group = sourceGroupName(status.source_name);
    grouped.set(group, [...(grouped.get(group) || []), status]);
  }
  return groupOrder
    .filter((name) => grouped.has(name))
    .map((name) => ({ name, items: grouped.get(name) || [] }));
}

function sourceGroupName(sourceName: string): string {
  if (sourceName.startsWith("Reddit/")) return "Reddit";
  if (sourceName === "Hacker News") return "Hacker News";
  if (sourceName.startsWith("Google News")) return "Google News RSS";
  if (/微博|抖音|小红书|财联社.*网页|网页版|公开发现页/.test(sourceName)) {
    return "浏览器公开采集";
  }
  if (/百度|头条|B站/.test(sourceName)) return "国内公开热榜";
  return "公共 RSS";
}

function sourceStatusClass(item: SourceRunStatus): string {
  if (item.is_rate_limited || item.status === "限流") return "status-warning";
  if (item.status === "成功") return "status-complete";
  if (item.status === "需要重新登录") return "status-warning";
  if (item.status === "失败") return "status-danger";
  return "status-muted";
}

function formatSourceTime(value: string | null): string {
  if (!value) return "尚未抓取";
  return value.replace("T", " ").slice(0, 16);
}

function AiSettingsPanel() {
  const { status, error, loading } = useRuntimeStatus();
  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>AI Provider 配置</h1>
          <p>安装、配置、验证和生产可用性分别判断；页面不会回显任何 Key。</p>
        </div>
      </header>
      <section className="settings-panel">
        {loading && <p className="muted-text">正在读取服务端能力状态...</p>}
        {error && <p className="message message-error">{error}</p>}
        {status && (
          <>
            <div>
              <span>当前 provider</span>
              <strong>{status.provider.name}</strong>
              <small>{runtimeStatusLabel(status.provider.status)}</small>
            </div>
            <div>
              <span>生产可用性</span>
              <strong>
                {runtimeProviderProductionReady(status.provider)
                  ? "允许进入生产配置"
                  : "当前阻断"}
              </strong>
              <small>{status.provider.reason}</small>
            </div>
            <div>
              <span>密钥状态</span>
              <strong>{status.provider.secret_configured ? "已配置（不显示内容）" : "未配置"}</strong>
            </div>
            {Object.entries(status.capabilities).map(([key, capability]) => (
              <div key={key}>
                <span>{runtimeCapabilityLabel(key)}</span>
                <strong>{runtimeStatusLabel(capability.status)}</strong>
                <small>{capability.reason}</small>
              </div>
            ))}
          </>
        )}
      </section>
      {status && (
        <>
          <CapabilityTruthSection
            title="Providers 能力状态"
            description="配置存在不代表连通或可生产；未做真实 smoke 时一律显示未验证。"
            items={status.providers}
            kind="provider"
          />
          <CapabilityTruthSection
            title="Skills 能力状态"
            description="安装 SKILL.md 只证明文件存在；接入产品适配器并通过真实项目验收后才可能生产可用。"
            items={status.skills}
            kind="skill"
          />
        </>
      )}
    </main>
  );
}

function CapabilityTruthSection({
  title,
  description,
  items,
  kind,
}: {
  title: string;
  description: string;
  items: RuntimeCapabilityTruth[];
  kind: "provider" | "skill";
}) {
  return (
    <section className="capability-truth-section">
      <header className="capability-truth-heading">
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </header>
      <div className="capability-truth-grid">
        {items.map((item) => (
          <article className="capability-truth-card" key={item.id}>
            <header>
              <div>
                <h3>{item.name}</h3>
                <p>{item.purpose}</p>
              </div>
              <span className={`status-badge ${capabilityTruthClass(item)}`}>
                {capabilityContractViolation(item)
                  ? "状态冲突（已阻断）"
                  : runtimeStatusLabel(item.status)}
              </span>
            </header>
            <div className="capability-truth-facts">
              <div>
                <span>{kind === "provider" ? "适配器" : "安装"}</span>
                <strong>
                  {truthStateLabel(
                    item.installed,
                    kind === "provider" ? "已内置" : "已安装",
                    kind === "provider" ? "未内置" : "未安装",
                  )}
                </strong>
              </div>
              <div>
                <span>配置</span>
                <strong>{truthStateLabel(item.configured, "已配置", "未配置")}</strong>
              </div>
              <div>
                <span>验证</span>
                <strong>
                  {item.verified
                    ? item.last_verified_at || "验证证据不完整"
                    : "从未验证"}
                </strong>
              </div>
              <div>
                <span>生产可用性</span>
                <strong>
                  {capabilityProductionReady(item) ? "生产可用" : "当前阻断"}
                </strong>
              </div>
            </div>
            <p className="capability-truth-line">
              <span>版本</span>
              <strong>版本 {item.declared_version || "未记录"}</strong>
            </p>
            {kind === "provider" && item.secret_configured !== undefined && (
              <p className="capability-truth-line">
                <span>Key</span>
                <strong>
                  {item.secret_configured ? "Key 已配置（不显示内容）" : "Key 未配置"}
                </strong>
              </p>
            )}
            {item.applicable_route && (
              <p className="capability-truth-line">
                <span>适用路线</span>
                <strong>{item.applicable_route}</strong>
              </p>
            )}
            <p className="capability-truth-line">
              <span>影响模块</span>
              <strong>{item.affected_modules.join("、") || "无"}</strong>
            </p>
            <p className="capability-truth-line">
              <span>Fallback</span>
              <strong>{item.fallback}</strong>
            </p>
            {item.cost_class && (
              <p className="capability-truth-line">
                <span>费用</span>
                <strong>{item.cost_class}</strong>
              </p>
            )}
            <p className="capability-truth-reason">{item.reason}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function PathSettingsPanel() {
  const { status, error, loading } = useRuntimeStatus();
  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>路径配置</h1>
        </div>
      </header>
      <section className="settings-panel">
        {loading && <p className="muted-text">正在读取物理路径...</p>}
        {error && <p className="message message-error">{error}</p>}
        {status?.paths.map((item) => (
          <div key={item.key}>
            <span>{runtimePathLabel(item.key)}</span>
            <strong>{item.path || "路径被阻断"}</strong>
            <small>
              {item.status === "ready"
                ? `已解析到 ${item.drive} 盘`
                : item.status === "missing"
                  ? "目录尚未创建，写入前会由受控入口创建"
                  : item.reason || "路径未通过安全校验"}
            </small>
          </div>
        ))}
        {status && (
          <div>
            <span>存储硬规则</span>
            <strong>{status.storage_policy.payload_drive_required}</strong>
            <small>运行时路径已解析；C 盘不会承载项目 payload。</small>
          </div>
        )}
      </section>
    </main>
  );
}

function useRuntimeStatus(): {
  status: RuntimeSettingsStatus | null;
  loading: boolean;
  error: string;
} {
  const [status, setStatus] = useState<RuntimeSettingsStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setLoading(true);
    void getRuntimeSettingsStatus()
      .then((result) => {
        if (active) setStatus(result);
      })
      .catch((loadError) => {
        if (active) setError(toMessage(loadError, "服务端能力状态读取失败"));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  return { status, loading, error };
}

function runtimeStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    test_only: "仅测试",
    configured_unverified: "已配置但未验证",
    blocked_missing_config: "缺少配置",
    blocked_unknown_provider: "未知 provider",
    installed_not_integrated: "已安装，未接入",
    installed_unverified: "已安装，未验证",
    not_installed: "未安装",
    blocked_path_policy: "路径被阻止",
    blocked_observation: "检查被阻止",
    blocked_missing_dependency: "缺少依赖",
    disabled: "已停用",
    local_real: "本地真实能力",
    compatibility_draft: "兼容草稿",
    deferred: "暂缓",
  };
  return labels[status] || status;
}

function capabilityTruthClass(item: RuntimeCapabilityTruth): string {
  if (capabilityContractViolation(item)) return "status-danger";
  if (capabilityProductionReady(item)) return "status-complete";
  if (
    item.status.startsWith("blocked") ||
    item.status === "not_installed"
  ) {
    return "status-danger";
  }
  if (item.installed || item.configured) return "status-warning";
  return "status-muted";
}

function truthStateLabel(
  value: boolean | null,
  yesLabel: string,
  noLabel: string,
): string {
  if (value === null) return "无法判断";
  return value ? yesLabel : noLabel;
}

function capabilityProductionReady(item: RuntimeCapabilityTruth): boolean {
  return (
    item.production_allowed &&
    item.installed === true &&
    item.configured === true &&
    item.verified === true &&
    Boolean(item.last_verified_at) &&
    item.status !== "test_only" &&
    !item.status.startsWith("blocked")
  );
}

function capabilityContractViolation(item: RuntimeCapabilityTruth): boolean {
  return item.production_allowed && !capabilityProductionReady(item);
}

function runtimeProviderProductionReady(
  provider: RuntimeSettingsStatus["provider"],
): boolean {
  return (
    provider.production_allowed &&
    provider.verified &&
    provider.secret_configured &&
    provider.status !== "test_only" &&
    !provider.status.startsWith("blocked")
  );
}

function runtimeCapabilityLabel(key: string): string {
  const labels: Record<string, string> = {
    modern_render: "现代渲染",
    legacy_export: "旧导出链",
    external_publish: "外部平台发布",
  };
  return labels[key] || key;
}

function runtimePathLabel(key: string): string {
  const labels: Record<string, string> = {
    DATA_DIR: "本地数据目录",
    EXPORT_DIR: "导出目录",
    BREAKDOWN_DIR: "拉片报告目录",
    SOURCE_CONFIG_PATH: "公开数据源配置",
    MATERIAL_ROOT_DIR: "素材根目录",
    PROJECT_ROOT_DIR: "项目输出目录",
    JIANYING_DOWNLOAD_DIR: "剪映下载目录",
    CANDIDATE_SCREENSHOT_DIR: "候选截图目录",
  };
  return labels[key] || key;
}

function SystemCheckPanel() {
  const [demoBusy, setDemoBusy] = useState(false);
  const [v1Busy, setV1Busy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [v1Result, setV1Result] = useState<ManualV1RunResult | null>(null);
  const [v1Draft, setV1Draft] = useState({
    title: "AI 搜索正在改变普通人获取信息的方式",
    summary:
      "越来越多用户开始用 AI 搜索替代传统搜索引擎，信息获取方式正在变化，背后涉及平台流量、广告模式和内容生态变化。",
    source_platform: "手动跑通测试",
    url: "",
    category: "AI / 科技趋势 / 商业逻辑",
    tags: "AI，科技趋势，商业逻辑",
    notes: "用于 fake provider 下的 V1 自动闭环验收。",
  });

  async function handleSeedDemo() {
    setDemoBusy(true);
    setNotice("");
    setError("");
    try {
      const result = await seedDemoData();
      setNotice(String(result.message || "示例数据已准备。"));
    } catch (seedError) {
      setError(toMessage(seedError, "生成示例数据失败。"));
    } finally {
      setDemoBusy(false);
    }
  }

  async function handleClearDemo() {
    setDemoBusy(true);
    setNotice("");
    setError("");
    try {
      await clearDemoData();
      setNotice("示例数据已清空。");
    } catch (clearError) {
      setError(toMessage(clearError, "清空示例数据失败。"));
    } finally {
      setDemoBusy(false);
    }
  }

  async function handleManualV1Test() {
    setV1Busy(true);
    setNotice("");
    setError("");
    setV1Result(null);
    try {
      const result = await runManualV1Test({
        title: v1Draft.title,
        summary: v1Draft.summary,
        source_platform: v1Draft.source_platform || "手动跑通测试",
        url: v1Draft.url,
        category: v1Draft.category || "AI / 科技趋势 / 商业逻辑",
        tags: v1Draft.tags
          .split(/[,，\s/]+/)
          .map((tag) => tag.trim())
          .filter(Boolean)
          .slice(0, 5),
        notes: v1Draft.notes,
      });
      if (result.provider !== "fake" || result.real_ai_called) {
        throw new Error("安全校验失败：本轮必须使用 fake provider，不能调用真实 AI。");
      }
      setV1Result(result);
      setNotice(result.message);
    } catch (runError) {
      setError(toMessage(runError, "V1 跑通测试失败。"));
    } finally {
      setV1Busy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <h1>系统检查</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleSeedDemo()}
            disabled={demoBusy}
          >
            生成示例数据
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleClearDemo()}
            disabled={demoBusy}
          >
            清空示例数据
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
      <section className="manual-v1-panel manual-v1-compact" aria-label="V1 自动跑通验收">
        <div className="manual-v1-copy">
          <span>辅助验收</span>
        </div>
        <div className="manual-v1-form">
          <label>
            <span>热点标题</span>
            <input
              value={v1Draft.title}
              onChange={(event) => setV1Draft({ ...v1Draft, title: event.target.value })}
            />
          </label>
          <label className="manual-v1-wide">
            <span>热点摘要</span>
            <textarea
              value={v1Draft.summary}
              onChange={(event) => setV1Draft({ ...v1Draft, summary: event.target.value })}
              rows={2}
            />
          </label>
          <label>
            <span>来源平台</span>
            <input
              value={v1Draft.source_platform}
              onChange={(event) =>
                setV1Draft({ ...v1Draft, source_platform: event.target.value })
              }
            />
          </label>
          <label>
            <span>分类</span>
            <input
              value={v1Draft.category}
              onChange={(event) => setV1Draft({ ...v1Draft, category: event.target.value })}
            />
          </label>
          <label>
            <span>标签，可选</span>
            <input
              value={v1Draft.tags}
              onChange={(event) => setV1Draft({ ...v1Draft, tags: event.target.value })}
            />
          </label>
          <button
            className="button button-primary no-wrap"
            type="button"
            onClick={() => void handleManualV1Test()}
            disabled={v1Busy || !v1Draft.title.trim()}
          >
            {v1Busy ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={16} aria-hidden="true" />
            )}
            手动跑通测试
          </button>
        </div>
        {v1Result && (
          <div className="manual-v1-result">
            <strong>{v1Result.message}</strong>
            <span>
              fake provider；质检 {v1Result.quality_check.total_score ?? "-"} 分；返修{" "}
              {v1Result.repair_attempts} 次。
            </span>
          </div>
        )}
      </section>
    </main>
  );
}

function toMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export default SettingsPage;
