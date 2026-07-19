import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clipboard,
  LoaderCircle,
  Plus,
  RefreshCw,
  Save,
  Star,
  Trash2,
} from "lucide-react";

import {
  createCopywritingSample,
  deleteCopywritingSample,
  getCopywritingSampleDetail,
  getCopywritingSamples,
  updateCopywritingSample,
  type CopywritingSampleFilters,
} from "./api";
import type {
  CopywritingSampleItem,
  CopywritingSampleListResponse,
  CopywritingSamplePayload,
} from "./types";
import "./styles.css";

const DEFAULT_TYPES = [
  "开头钩子",
  "标题",
  "封面文案",
  "口播片段",
  "结尾引导",
  "收藏清单结构",
  "小白转译表达",
  "商业逻辑拆解表达",
  "风口避坑表达",
  "趋势解释表达",
  "发布文案",
];

const DEFAULT_SCENES = [
  "热点解释",
  "清单型内容",
  "小白转译",
  "风口避坑",
  "商业逻辑拆解",
  "AI 工具介绍",
  "财经新闻转译",
  "趋势判断",
  "数据复盘",
  "评论区回应",
];

const EMPTY_FORM = {
  title: "",
  sample_type: "开头钩子",
  sample_text: "",
  scene: "热点解释",
  content_direction: "",
  structure_type: "",
  why_reference: "",
  reusable_points: "",
  avoid_points: "",
  tagsText: "",
  enabled: true,
  is_key_reference: false,
  notes: "",
};

type SampleFormState = typeof EMPTY_FORM;

function CopywritingSamplePage() {
  const [filters, setFilters] = useState<CopywritingSampleFilters>({
    sampleType: "",
    scene: "",
    contentDirection: "",
    tag: "",
    keyword: "",
    enabledOnly: false,
    keyReferenceOnly: false,
    page: 1,
    pageSize: 20,
  });
  const [data, setData] = useState<CopywritingSampleListResponse | null>(null);
  const [selected, setSelected] = useState<CopywritingSampleItem | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<SampleFormState>(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  const loadSamples = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await getCopywritingSamples(filters));
    } catch (loadError) {
      setError(toMessage(loadError, "脚本引擎读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void loadSamples();
  }, [loadSamples, refreshKey]);

  const sampleTypes = useMemo(
    () => mergeOptions(DEFAULT_TYPES, data?.filters.sample_types || []),
    [data],
  );
  const scenes = useMemo(
    () => mergeOptions(DEFAULT_SCENES, data?.filters.scenes || []),
    [data],
  );

  function updateFilter(patch: Partial<CopywritingSampleFilters>) {
    setFilters((current) => ({ ...current, ...patch, page: 1 }));
  }

  function updateForm(patch: Partial<SampleFormState>) {
    setForm((current) => ({ ...current, ...patch }));
  }

  function startCreate() {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setNotice("");
    setError("");
  }

  function startEdit(item: CopywritingSampleItem) {
    setEditingId(item.id);
    setForm(formFromItem(item));
    setSelected(item);
    setNotice("");
    setError("");
  }

  async function viewDetail(item: CopywritingSampleItem) {
    setError("");
    try {
      setSelected(await getCopywritingSampleDetail(item.id));
    } catch (detailError) {
      setError(toMessage(detailError, "样本详情读取失败。"));
    }
  }

  async function saveSample() {
    const payload = payloadFromForm(form);
    if (!payload.title.trim() || !payload.sample_text.trim()) {
      setError("请至少填写样本标题和样本文本。");
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const saved =
        editingId === null
          ? await createCopywritingSample(payload)
          : await updateCopywritingSample(editingId, payload);
      setSelected(saved);
      setEditingId(saved.id);
      setForm(formFromItem(saved));
      setNotice("脚本引擎配置已保存。后续生成流程会读取启用样本和结构规则。");
      setRefreshKey((value) => value + 1);
    } catch (saveError) {
      setError(toMessage(saveError, "脚本样本保存失败。"));
    } finally {
      setSaving(false);
    }
  }

  async function removeSample(item: CopywritingSampleItem) {
    if (!window.confirm(`确认删除脚本样本「${item.title}」？`)) {
      return;
    }
    setError("");
    setNotice("");
    try {
      await deleteCopywritingSample(item.id);
      if (selected?.id === item.id) setSelected(null);
      if (editingId === item.id) startCreate();
      setNotice("脚本样本已删除。");
      setRefreshKey((value) => value + 1);
    } catch (deleteError) {
      setError(toMessage(deleteError, "脚本样本删除失败。"));
    }
  }

  async function toggleSample(
    item: CopywritingSampleItem,
    patch: Partial<Pick<CopywritingSampleItem, "enabled" | "is_key_reference">>,
  ) {
    setError("");
    setNotice("");
    try {
      const updated = await updateCopywritingSample(item.id, {
        ...payloadFromItem(item),
        ...patch,
      });
      if (selected?.id === item.id) setSelected(updated);
      if (editingId === item.id) setForm(formFromItem(updated));
      setNotice("样本状态已更新。");
      setRefreshKey((value) => value + 1);
    } catch (toggleError) {
      setError(toMessage(toggleError, "样本状态更新失败。"));
    }
  }

  async function copySample(item: CopywritingSampleItem) {
    try {
      const writeText = navigator.clipboard?.writeText;
      if (typeof writeText !== "function") {
        throw new Error("clipboard unavailable");
      }
      await writeText.call(navigator.clipboard, item.sample_text);
      setNotice("脚本样本已复制。注意：只学习表达方式和结构，不直接复制原文。");
    } catch {
      setError("复制失败，可以手动选中样本文本复制。");
    }
  }

  return (
    <main className="app-shell copy-shell">
      <header className="page-header">
        <div>
          <h1>脚本引擎</h1>
        </div>
        <div className="page-header-actions">
          <button className="button button-secondary" type="button" onClick={startCreate}>
            <Plus size={16} aria-hidden="true" />
            新增脚本样本
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新脚本引擎
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

      <section className="copy-filters" aria-label="脚本引擎筛选">
        <label>
          <span>样本类型筛选</span>
          <select
            value={filters.sampleType}
            onChange={(event) => updateFilter({ sampleType: event.target.value })}
          >
            <option value="">全部类型</option>
            {sampleTypes.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>适用场景筛选</span>
          <select
            value={filters.scene}
            onChange={(event) => updateFilter({ scene: event.target.value })}
          >
            <option value="">全部场景</option>
            {scenes.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>内容方向筛选</span>
          <input
            value={filters.contentDirection}
            onChange={(event) => updateFilter({ contentDirection: event.target.value })}
            placeholder="例如：AI 工具"
          />
        </label>
        <label>
          <span>标签筛选</span>
          <input
            value={filters.tag}
            onChange={(event) => updateFilter({ tag: event.target.value })}
            placeholder="例如：商业逻辑"
          />
        </label>
        <label className="copy-filter-wide">
          <span>搜索样本文本</span>
          <input
            value={filters.keyword}
            onChange={(event) => updateFilter({ keyword: event.target.value })}
            placeholder="搜索标题、脚本文本、可复用点..."
          />
        </label>
        <label className="copy-checkbox">
          <input
            type="checkbox"
            checked={filters.enabledOnly}
            onChange={(event) => updateFilter({ enabledOnly: event.target.checked })}
          />
          只看启用
        </label>
        <label className="copy-checkbox">
          <input
            type="checkbox"
            checked={filters.keyReferenceOnly}
            onChange={(event) => updateFilter({ keyReferenceOnly: event.target.checked })}
          />
          只看重点参考
        </label>
      </section>

      <section className="copy-layout">
        <div className="copy-list-panel">
          <div className="panel-title-row">
            <h2>样本列表</h2>
            <span>{data ? `${data.pagination.total} 条` : "读取中"}</span>
          </div>
          {loading ? (
            <div className="empty-state">
              <LoaderCircle className="spin" size={22} aria-hidden="true" />
              正在读取脚本引擎...
            </div>
          ) : data?.items.length ? (
            <div className="copy-sample-list">
              {data.items.map((item) => (
                <article className="copy-sample-row" key={item.id}>
                  <div>
                    <div className="copy-row-title">
                      <h3>{item.title}</h3>
                      {item.is_key_reference && (
                        <span className="copy-badge key">
                          <Star size={13} aria-hidden="true" />
                          重点参考
                        </span>
                      )}
                      <span className={item.enabled ? "copy-badge on" : "copy-badge off"}>
                        {item.enabled ? "已启用" : "已停用"}
                      </span>
                    </div>
                    <p>{item.sample_text.slice(0, 120)}</p>
                    <div className="copy-meta">
                      <span>{item.sample_type || "未分类"}</span>
                      <span>{item.scene || "未设置场景"}</span>
                      <span>{item.content_direction || "未设置方向"}</span>
                    </div>
                    <div className="tag-list">
                      {item.tags.map((tag) => (
                        <span key={tag}>{tag}</span>
                      ))}
                    </div>
                  </div>
                  <div className="copy-actions">
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void viewDetail(item)}
                    >
                      查看详情
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => startEdit(item)}
                    >
                      编辑
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() =>
                        void toggleSample(item, { enabled: !item.enabled })
                      }
                    >
                      {item.enabled ? "停用" : "启用"}
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() =>
                        void toggleSample(item, {
                          is_key_reference: !item.is_key_reference,
                        })
                      }
                    >
                      {item.is_key_reference ? "取消重点" : "设为重点"}
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void copySample(item)}
                    >
                      <Clipboard size={15} aria-hidden="true" />
                      复制文案
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void removeSample(item)}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                      删除
                    </button>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty-state empty-state-stacked">
              <strong>还没有脚本样本。</strong>
              <span>可以先新增一条你认可的开头、标题、结尾或镜头结构。</span>
              <button
                className="button button-primary"
                type="button"
                onClick={startCreate}
              >
                <Plus size={16} aria-hidden="true" />
                新增脚本样本
              </button>
            </div>
          )}
        </div>

        <aside className="copy-side-panel">
          <SampleForm
            form={form}
            editingId={editingId}
            sampleTypes={sampleTypes}
            scenes={scenes}
            saving={saving}
            onChange={updateForm}
            onSave={() => void saveSample()}
          />

          <SampleDetail sample={selected} />
        </aside>
      </section>
    </main>
  );
}

function SampleForm({
  form,
  editingId,
  sampleTypes,
  scenes,
  saving,
  onChange,
  onSave,
}: {
  form: SampleFormState;
  editingId: number | null;
  sampleTypes: string[];
  scenes: string[];
  saving: boolean;
  onChange: (patch: Partial<SampleFormState>) => void;
  onSave: () => void;
}) {
  return (
    <section className="copy-editor">
      <div className="panel-title-row">
          <h2>{editingId === null ? "新增脚本样本" : "编辑脚本样本"}</h2>
        {editingId !== null && <span>#{editingId}</span>}
      </div>
      <label>
        <span>样本标题</span>
        <input
          value={form.title}
          onChange={(event) => onChange({ title: event.target.value })}
        />
      </label>
      <div className="copy-editor-grid">
        <label>
          <span>样本类型</span>
          <select
            value={form.sample_type}
            onChange={(event) => onChange({ sample_type: event.target.value })}
          >
            {sampleTypes.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>适用场景</span>
          <select
            value={form.scene}
            onChange={(event) => onChange({ scene: event.target.value })}
          >
            {scenes.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        <span>样本文本 / 脚本结构</span>
        <textarea
          value={form.sample_text}
          onChange={(event) => onChange({ sample_text: event.target.value })}
        />
      </label>
      <div className="copy-editor-grid">
        <label>
          <span>内容方向</span>
          <input
            value={form.content_direction}
            onChange={(event) => onChange({ content_direction: event.target.value })}
          />
        </label>
        <label>
          <span>结构类型</span>
          <input
            value={form.structure_type}
            onChange={(event) => onChange({ structure_type: event.target.value })}
          />
        </label>
      </div>
      <label>
        <span>为什么值得参考</span>
        <textarea
          value={form.why_reference}
          onChange={(event) => onChange({ why_reference: event.target.value })}
        />
      </label>
      <label>
        <span>可复用点</span>
        <textarea
          value={form.reusable_points}
          onChange={(event) => onChange({ reusable_points: event.target.value })}
        />
      </label>
      <label>
        <span>不适合复用的地方</span>
        <textarea
          value={form.avoid_points}
          onChange={(event) => onChange({ avoid_points: event.target.value })}
        />
      </label>
      <label>
        <span>标签</span>
        <input
          value={form.tagsText}
          onChange={(event) => onChange({ tagsText: event.target.value })}
          placeholder="用逗号分隔，例如：开头,商业逻辑"
        />
      </label>
      <label>
        <span>备注</span>
        <textarea
          value={form.notes}
          onChange={(event) => onChange({ notes: event.target.value })}
        />
      </label>
      <div className="copy-editor-flags">
        <label>
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(event) => onChange({ enabled: event.target.checked })}
          />
          启用这个样本
        </label>
        <label>
          <input
            type="checkbox"
            checked={form.is_key_reference}
            onChange={(event) => onChange({ is_key_reference: event.target.checked })}
          />
          设为重点参考
        </label>
      </div>
      <button
        className="button button-primary"
        type="button"
        onClick={onSave}
        disabled={saving}
      >
        {saving ? (
          <LoaderCircle className="spin" size={16} aria-hidden="true" />
        ) : (
          <Save size={16} aria-hidden="true" />
        )}
        保存样本
      </button>
    </section>
  );
}

function SampleDetail({ sample }: { sample: CopywritingSampleItem | null }) {
  return (
    <section className="copy-detail">
      <h2>脚本样本详情</h2>
      {sample ? (
        <>
          <h3>{sample.title}</h3>
          <p className="copy-detail-text">{sample.sample_text}</p>
          <dl>
            <div>
              <dt>样本类型</dt>
              <dd>{sample.sample_type || "未设置"}</dd>
            </div>
            <div>
              <dt>适用场景</dt>
              <dd>{sample.scene || "未设置"}</dd>
            </div>
            <div>
              <dt>内容方向</dt>
              <dd>{sample.content_direction || "未设置"}</dd>
            </div>
            <div>
              <dt>结构类型</dt>
              <dd>{sample.structure_type || "未设置"}</dd>
            </div>
            <div>
              <dt>为什么值得参考</dt>
              <dd>{sample.why_reference || "未填写"}</dd>
            </div>
            <div>
              <dt>可复用点</dt>
              <dd>{sample.reusable_points || "未填写"}</dd>
            </div>
            <div>
              <dt>不适合复用的地方</dt>
              <dd>{sample.avoid_points || "未填写"}</dd>
            </div>
            <div>
              <dt>创建时间</dt>
              <dd>{formatDate(sample.created_at)}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{formatDate(sample.updated_at)}</dd>
            </div>
          </dl>
        </>
      ) : (
        <p className="muted-text">点击“查看详情”后，这里会显示完整样本信息。</p>
      )}
    </section>
  );
}

function payloadFromForm(form: SampleFormState): CopywritingSamplePayload {
  return {
    title: form.title,
    sample_type: form.sample_type,
    sample_text: form.sample_text,
    scene: form.scene,
    content_direction: form.content_direction,
    structure_type: form.structure_type,
    why_reference: form.why_reference,
    reusable_points: form.reusable_points,
    avoid_points: form.avoid_points,
    tags: parseTags(form.tagsText),
    enabled: form.enabled,
    is_key_reference: form.is_key_reference,
    notes: form.notes,
  };
}

function payloadFromItem(item: CopywritingSampleItem): CopywritingSamplePayload {
  return {
    title: item.title,
    sample_type: item.sample_type,
    sample_text: item.sample_text,
    scene: item.scene,
    content_direction: item.content_direction,
    structure_type: item.structure_type,
    why_reference: item.why_reference,
    reusable_points: item.reusable_points,
    avoid_points: item.avoid_points,
    tags: item.tags,
    enabled: item.enabled,
    is_key_reference: item.is_key_reference,
    notes: item.notes,
  };
}

function formFromItem(item: CopywritingSampleItem): SampleFormState {
  return {
    ...payloadFromItem(item),
    tagsText: item.tags.join(","),
  };
}

function parseTags(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function mergeOptions(defaults: string[], values: string[]): string[] {
  return Array.from(new Set([...defaults, ...values.filter(Boolean)]));
}

function formatDate(value: string): string {
  if (!value) return "未记录";
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

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default CopywritingSamplePage;
