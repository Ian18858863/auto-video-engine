import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-react";

import {
  getAccountStyle,
  resetAccountStyle,
  saveAccountStyle,
} from "./api";
import type {
  AccountStyleLibrary,
  SceneTemplateItem,
  StructurePreferenceItem,
  StyleListItem,
} from "./types";
import "./styles.css";

function AccountStylePage() {
  const [style, setStyle] = useState<AccountStyleLibrary | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  const loadStyle = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setStyle(await getAccountStyle());
    } catch (loadError) {
      setError(toMessage(loadError, "账号风格库读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStyle();
  }, [loadStyle, refreshKey]);

  async function handleSave() {
    if (!style) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const saved = await saveAccountStyle(style);
      setStyle(saved);
      setNotice("账号风格库已保存，后续生成流程会读取这份最新配置。");
    } catch (saveError) {
      setError(toMessage(saveError, "账号风格库保存失败。"));
    } finally {
      setSaving(false);
    }
  }

  async function handleReset() {
    if (!window.confirm("确认恢复默认账号风格库？当前自定义内容会被覆盖。")) {
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const reset = await resetAccountStyle();
      setStyle(reset);
      setNotice("已恢复默认账号风格库，配置已经生效。");
    } catch (resetError) {
      setError(toMessage(resetError, "恢复默认风格库失败。"));
    } finally {
      setSaving(false);
    }
  }

  function updateStyle(patch: Partial<AccountStyleLibrary>) {
    setStyle((current) => (current ? { ...current, ...patch } : current));
  }

  return (
    <main className="app-shell style-shell">
      <header className="page-header">
        <div>
          <h1>账号风格库</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading || saving}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新配置
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void handleReset()}
            disabled={loading || saving}
          >
            <RotateCcw size={16} aria-hidden="true" />
            恢复默认风格库
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={() => void handleSave()}
            disabled={loading || saving || !style}
          >
            {saving ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Save size={16} aria-hidden="true" />
            )}
            保存修改
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

      {loading ? (
        <div className="empty-state">
          <LoaderCircle className="spin" size={24} aria-hidden="true" />
          正在读取账号风格库...
        </div>
      ) : !style ? (
        <div className="empty-state empty-state-stacked">
          <strong>账号风格库没有读取成功。</strong>
          <span>可以重新读取配置；如果后端未启动，请先启动后端再刷新。</span>
          <div>
            <button
              className="button button-primary"
              type="button"
              onClick={() => void loadStyle()}
            >
              <RefreshCw size={16} aria-hidden="true" />
              重新读取
            </button>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => void handleReset()}
            >
              <RotateCcw size={16} aria-hidden="true" />
              恢复默认风格库
            </button>
          </div>
        </div>
      ) : (
        <>
          <section className="summary-strip" aria-label="账号风格库状态">
            <SummaryItem
              label="最后保存时间"
              value={formatDate(style.last_saved_at)}
            />
            <SummaryItem
              label="当前配置"
              value={style.is_active ? "已生效" : "未生效"}
              tone={style.is_active ? "good" : "warning"}
            />
            <SummaryItem
              label="配置来源"
              value={style.using_default ? "默认风格" : "已自定义"}
            />
          </section>

          <section className="style-section style-positioning">
            <div>
              <h2>账号定位</h2>
            </div>
            <label className="style-text-field">
              <span>账号定位</span>
              <textarea
                value={style.account_positioning}
                onChange={(event) =>
                  updateStyle({ account_positioning: event.target.value })
                }
              />
            </label>
          </section>

          <div className="style-grid">
            <ListSection
              title="目标观众"
              items={style.target_audiences}
              onChange={(items) => updateStyle({ target_audiences: items })}
            />
            <ListSection
              title="内容方向"
              items={style.content_directions}
              onChange={(items) => updateStyle({ content_directions: items })}
            />
            <ListSection
              title="表达风格"
              items={style.expression_styles}
              onChange={(items) => updateStyle({ expression_styles: items })}
            />
            <ListSection
              title="禁用表达"
              items={style.banned_expressions}
              showCategory
              onChange={(items) => updateStyle({ banned_expressions: items })}
            />
          </div>

          <StructureSection
            items={style.structure_preferences}
            onChange={(items) => updateStyle({ structure_preferences: items })}
          />

          <div className="style-grid">
            <TemplateSection
              title="开头偏好"
              sceneLabel="开头场景"
              templateLabel="开头模板"
              items={style.opening_templates}
              onChange={(items) => updateStyle({ opening_templates: items })}
            />
            <TemplateSection
              title="结尾偏好"
              sceneLabel="结尾场景"
              templateLabel="结尾模板"
              items={style.ending_templates}
              onChange={(items) => updateStyle({ ending_templates: items })}
            />
          </div>

          <ValidationSection style={style} />
        </>
      )}
    </main>
  );
}

function ListSection({
  title,
  items,
  showCategory = false,
  onChange,
}: {
  title: string;
  items: StyleListItem[];
  showCategory?: boolean;
  onChange: (items: StyleListItem[]) => void;
}) {
  const [newLabel, setNewLabel] = useState("");

  function updateItem(index: number, patch: Partial<StyleListItem>) {
    onChange(items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)));
  }

  function addItem() {
    const label = newLabel.trim();
    if (!label) return;
    onChange([
      ...items,
      {
        id: `custom_${Date.now()}`,
        label,
        enabled: true,
        ...(showCategory ? { category: "自定义", reason: "" } : {}),
      },
    ]);
    setNewLabel("");
  }

  return (
    <section className="style-section">
      <h2>{title}</h2>
      <div className="style-item-list">
        {items.map((item, index) => (
          <div className="style-item-row" key={item.id}>
            <label className="style-toggle">
              <input
                type="checkbox"
                checked={item.enabled}
                aria-label={`启用${item.label || title}`}
                onChange={(event) => updateItem(index, { enabled: event.target.checked })}
              />
              启用
            </label>
            <input
              aria-label={`${title}条目${index + 1}`}
              value={item.label}
              onChange={(event) => updateItem(index, { label: event.target.value })}
            />
            {showCategory && (
              <>
                <input
                  aria-label={`${title}分类${index + 1}`}
                  value={item.category || ""}
                  onChange={(event) => updateItem(index, { category: event.target.value })}
                />
                <input
                  aria-label={`${title}原因${index + 1}`}
                  value={item.reason || ""}
                  onChange={(event) => updateItem(index, { reason: event.target.value })}
                />
              </>
            )}
            <button
              className="icon-button"
              type="button"
              aria-label={`删除${item.label || title}`}
              onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
            >
              <Trash2 size={16} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
      <div className="style-add-row">
        <label>
          <span>新增{title}</span>
          <input
            value={newLabel}
            onChange={(event) => setNewLabel(event.target.value)}
          />
        </label>
        <button className="button button-secondary" type="button" onClick={addItem}>
          <Plus size={15} aria-hidden="true" />
          添加{title}
        </button>
      </div>
    </section>
  );
}

function StructureSection({
  items,
  onChange,
}: {
  items: StructurePreferenceItem[];
  onChange: (items: StructurePreferenceItem[]) => void;
}) {
  function updateItem(index: number, patch: Partial<StructurePreferenceItem>) {
    onChange(items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)));
  }

  function addStructure() {
    onChange([
      ...items,
      {
        id: `structure_${Date.now()}`,
        name: "新结构类型",
        framework: "第一步 → 第二步 → 第三步 → 总结",
        enabled: true,
      },
    ]);
  }

  return (
    <section className="style-section">
      <div className="style-section-title">
        <div>
          <h2>内容结构偏好</h2>
        </div>
        <button className="button button-secondary" type="button" onClick={addStructure}>
          <Plus size={15} aria-hidden="true" />
          添加结构
        </button>
      </div>
      <div className="structure-list">
        {items.map((item, index) => (
          <article className="structure-card" key={item.id}>
            <label className="style-toggle">
              <input
                type="checkbox"
                checked={item.enabled}
                aria-label={`启用结构${item.name}`}
                onChange={(event) => updateItem(index, { enabled: event.target.checked })}
              />
              启用
            </label>
            <label>
              <span>结构名称</span>
              <input
                aria-label={`结构名称${index + 1}`}
                value={item.name}
                onChange={(event) => updateItem(index, { name: event.target.value })}
              />
            </label>
            <label>
              <span>写法框架</span>
              <textarea
                aria-label={`结构框架${index + 1}`}
                value={item.framework}
                onChange={(event) => updateItem(index, { framework: event.target.value })}
              />
            </label>
            <button
              className="button button-secondary"
              type="button"
              aria-label={`删除结构${item.name}`}
              onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
            >
              <Trash2 size={15} aria-hidden="true" />
              删除结构
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}

function TemplateSection({
  title,
  sceneLabel,
  templateLabel,
  items,
  onChange,
}: {
  title: string;
  sceneLabel: string;
  templateLabel: string;
  items: SceneTemplateItem[];
  onChange: (items: SceneTemplateItem[]) => void;
}) {
  function updateItem(index: number, patch: Partial<SceneTemplateItem>) {
    onChange(items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)));
  }

  function addTemplate() {
    onChange([
      ...items,
      {
        id: `template_${Date.now()}`,
        scene: "新场景",
        template: "这里写可复用模板，但不要写死每条视频。",
        enabled: true,
      },
    ]);
  }

  return (
    <section className="style-section">
      <div className="style-section-title">
        <div>
          <h2>{title}</h2>
        </div>
        <button className="button button-secondary" type="button" onClick={addTemplate}>
          <Plus size={15} aria-hidden="true" />
          添加模板
        </button>
      </div>
      <div className="style-item-list">
        {items.map((item, index) => (
          <div className="template-row" key={item.id}>
            <label className="style-toggle">
              <input
                type="checkbox"
                checked={item.enabled}
                aria-label={`启用${item.scene}`}
                onChange={(event) => updateItem(index, { enabled: event.target.checked })}
              />
              启用
            </label>
            <label>
              <span>{sceneLabel}</span>
              <input
                aria-label={`${sceneLabel}${index + 1}`}
                value={item.scene}
                onChange={(event) => updateItem(index, { scene: event.target.value })}
              />
            </label>
            <label>
              <span>{templateLabel}</span>
              <textarea
                aria-label={`${templateLabel}${index + 1}`}
                value={item.template}
                onChange={(event) => updateItem(index, { template: event.target.value })}
              />
            </label>
            <button
              className="icon-button"
              type="button"
              aria-label={`删除${item.scene}`}
              onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
            >
              <Trash2 size={16} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function ValidationSection({ style }: { style: AccountStyleLibrary }) {
  return (
    <section className="style-section validation-section">
      <h2>风格校验节点</h2>
      <div className="validation-grid">
        <div>
          <h3>专业词必须解释</h3>
          <p>
            {style.validation_rules.professional_terms_require_explanation
              ? "已启用：专业财经词必须配大白话解释。"
              : "未启用"}
          </p>
          <div className="tag-list">
            {style.validation_rules.professional_terms.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </div>
        </div>
        <div>
          <h3>默认常识句式禁用</h3>
          <div className="tag-list">
            {style.validation_rules.default_common_knowledge_phrases.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </div>
        </div>
        <div>
          <h3>大白话转译例子</h3>
          <ul>
            {style.validation_rules.plain_language_examples.map((item) => (
              <li key={item.term}>
                <strong>{item.term}</strong>：{item.plain_explanation}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3>后续质检会检查</h3>
          <ul>
            {style.validation_rules.checks.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

function SummaryItem({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: string;
  tone?: "normal" | "good" | "warning";
}) {
  return (
    <div className={`summary-item summary-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function formatDate(value: string): string {
  if (!value) {
    return "默认配置";
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

export default AccountStylePage;
