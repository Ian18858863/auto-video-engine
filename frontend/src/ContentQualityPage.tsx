import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ClipboardCheck,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

import {
  checkManualContentQuality,
  checkPipelineContentQuality,
  getScriptWorkspace,
} from "./api";
import type { ContentQualityResult, ScriptWorkspaceData } from "./types";
import "./styles.css";

interface ContentQualityPageProps {
  pipelineId?: number | null;
  onBackWorkspace?: (pipelineId: number) => void;
  autoCheckOnLoad?: boolean;
}

function ContentQualityPage({
  pipelineId = null,
  onBackWorkspace,
  autoCheckOnLoad = false,
}: ContentQualityPageProps) {
  const [workspace, setWorkspace] = useState<ScriptWorkspaceData | null>(null);
  const [manualText, setManualText] = useState("");
  const [manualTitle, setManualTitle] = useState("手动粘贴内容");
  const [result, setResult] = useState<ContentQualityResult | null>(null);
  const [loadingWorkspace, setLoadingWorkspace] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  const loadWorkspace = useCallback(async () => {
    if (pipelineId === null) {
      setWorkspace(null);
      return;
    }
    setLoadingWorkspace(true);
    setError("");
    try {
      const data = await getScriptWorkspace(pipelineId);
      setWorkspace(data);
      setManualTitle(data.project_name || "脚本工作台内容");
    } catch (loadError) {
      setError(toMessage(loadError, "脚本读取失败。"));
    } finally {
      setLoadingWorkspace(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace, refreshKey]);

  useEffect(() => {
    if (!autoCheckOnLoad || pipelineId === null || !workspace || result || checking) {
      return;
    }
    void handleCheckPipeline();
  }, [autoCheckOnLoad, pipelineId, workspace, result, checking]);

  async function handleCheckPipeline() {
    if (pipelineId === null) return;
    setChecking(true);
    setError("");
    setNotice("");
    try {
      const checked = await checkPipelineContentQuality(pipelineId);
      setResult(checked);
      setNotice(`质检记录已保存：#${checked.id}`);
    } catch (checkError) {
      setError(toMessage(checkError, "脚本质检失败。"));
    } finally {
      setChecking(false);
    }
  }

  async function handleCheckManual() {
    const text = manualText.trim();
    if (!text) {
      setError("请先粘贴要质检的文案。");
      return;
    }
    setChecking(true);
    setError("");
    setNotice("");
    try {
      const checked = await checkManualContentQuality({
        text,
        sourceTitle: manualTitle || "手动粘贴内容",
      });
      setResult(checked);
      setNotice(`质检记录已保存：#${checked.id}`);
    } catch (checkError) {
      setError(toMessage(checkError, "手动文本质检失败。"));
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="app-shell quality-shell">
      <header className="page-header">
        <div>
          <h1>内容质检</h1>
        </div>
        <div className="page-header-actions">
          {pipelineId !== null && (
            <button
              className="button button-secondary"
              type="button"
              onClick={() => onBackWorkspace?.(pipelineId)}
            >
              <ArrowLeft size={16} aria-hidden="true" />
              返回脚本工作台
            </button>
          )}
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loadingWorkspace}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新脚本
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

      <section className="quality-layout">
        <div className="quality-inputs">
          <section className="quality-panel">
            <h2>从脚本工作台质检</h2>
            {pipelineId === null ? (
              <p className="muted-text">当前没有选中的脚本。请先打开脚本页。</p>
            ) : loadingWorkspace ? (
              <p className="muted-text">
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
                正在读取脚本...
              </p>
            ) : workspace ? (
              <>
                <dl className="quality-script-summary">
                  <div>
                    <dt>项目</dt>
                    <dd>{workspace.project_name}</dd>
                  </div>
                  <div>
                    <dt>脚本标题</dt>
                    <dd>{workspace.script.title || workspace.topic.title}</dd>
                  </div>
                  <div>
                    <dt>口播摘要</dt>
                    <dd>{workspace.script.voiceover.slice(0, 90) || "暂无口播稿"}</dd>
                  </div>
                </dl>
                <button
                  className="button button-primary"
                  type="button"
                  onClick={() => void handleCheckPipeline()}
                  disabled={checking}
                >
                  {checking ? (
                    <LoaderCircle className="spin" size={16} aria-hidden="true" />
                  ) : (
                    <ClipboardCheck size={16} aria-hidden="true" />
                  )}
                  质检当前脚本
                </button>
              </>
            ) : (
              <p className="muted-text">脚本未读取到。</p>
            )}
          </section>

          <section className="quality-panel">
            <h2>手动粘贴文本质检</h2>
            <label className="quality-field">
              <span>内容名称</span>
              <input
                value={manualTitle}
                onChange={(event) => setManualTitle(event.target.value)}
              />
            </label>
            <label className="quality-field">
              <span>要检查的文案</span>
              <textarea
                value={manualText}
                onChange={(event) => setManualText(event.target.value)}
                placeholder="可以粘贴口播稿、标题、封面文案、字幕、发布文案..."
              />
            </label>
            <button
              className="button button-primary"
              type="button"
              onClick={() => void handleCheckManual()}
              disabled={checking}
            >
              {checking ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : (
                <ClipboardCheck size={16} aria-hidden="true" />
              )}
              开始质检
            </button>
          </section>
        </div>

        <section className="quality-result-panel" aria-live="polite">
          <h2>质检结果</h2>
          {result ? <QualityResult result={result} /> : <EmptyResult />}
        </section>
      </section>
    </main>
  );
}

function QualityResult({ result }: { result: ContentQualityResult }) {
  return (
    <div className="quality-result">
      <div className="quality-score-row">
        <div className={`quality-score ${scoreTone(result.total_score)}`}>
          <span>总分</span>
          <strong>{result.total_score}</strong>
        </div>
        <Metric label="是否可以发布" value={result.publish_suggestion} />
        <Metric label="风险等级" value={result.risk_level} />
        <Metric label="收藏价值" value={result.collect_value} />
        <Metric label="适合做系列" value={result.series_worthy ? "是" : "否"} />
      </div>

      <p className={`quality-max-issue quality-risk-${result.risk_level}`}>
        <ShieldAlert size={17} aria-hidden="true" />
        最大问题：{result.max_issue}
      </p>

      <ResultSection
        title="问题列表"
        empty="暂时没有明显问题。"
        items={result.issues}
      />

      <ResultSection
        title="修改建议"
        empty="暂无修改建议。"
        items={result.suggestions}
      />

      <div className="quality-tag-grid">
        <TagBlock
          title="命中的禁用表达"
          items={result.matched_banned_expressions}
          empty="没有命中禁用表达"
        />
        <TagBlock
          title="需要解释的专业术语"
          items={result.professional_terms_need_explanation}
          empty="没有未解释术语"
        />
        <TagBlock
          title="检查维度"
          items={result.checked_dimensions}
          empty="暂无维度"
        />
      </div>

      <p className="quality-note">
        规则来源：{result.style_source}。本次只用本地规则，未调用真实 AI。
      </p>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="quality-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ResultSection({
  title,
  empty,
  items,
}: {
  title: string;
  empty: string;
  items: string[];
}) {
  return (
    <section className="quality-list-section">
      <h3>{title}</h3>
      {items.length ? (
        <ul>
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="muted-text">{empty}</p>
      )}
    </section>
  );
}

function TagBlock({
  title,
  items,
  empty,
}: {
  title: string;
  items: string[];
  empty: string;
}) {
  return (
    <div>
      <h3>{title}</h3>
      {items.length ? (
        <div className="tag-list">
          {items.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      ) : (
        <p className="muted-text">{empty}</p>
      )}
    </div>
  );
}

function EmptyResult() {
  return (
    <div className="empty-state quality-empty">
      还没有质检结果。可以从脚本工作台选择脚本，或手动粘贴文案检查。
    </div>
  );
}

function scoreTone(score: number): string {
  if (score >= 80) return "quality-score-good";
  if (score >= 60) return "quality-score-warning";
  return "quality-score-danger";
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default ContentQualityPage;
