import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertTriangle,
  ListChecks,
  LoaderCircle,
  RefreshCw,
  Video,
} from "lucide-react";

import { completeTodayTask, getTodayTasks } from "./api";
import type { TodayTaskItem, TodayTasksResponse } from "./types";
import "./styles.css";

const emptyDashboard: TodayTasksResponse = {
  date: "",
  hotspots: [],
  topic_ideas: [],
  collectible_ideas: [],
  projects: [],
  confirmations: [],
  publish_reminders: [],
  suggestions: [],
  decision_items: [],
  running_items: [],
  finished_items: [],
  handled_task_ids: [],
};

interface TodayTaskPageProps {
  onOpenHotspot?: (hotspotId: number) => void;
  onOpenTopic?: (topicId: number) => void;
  onOpenProject?: (pipelineId: number) => void;
  onOpenWorkspace?: (pipelineId: number) => void;
  onOpenMaterials?: (pipelineId: number) => void;
  onOpenContentQuality?: (pipelineId: number) => void;
  onOpenEditing?: (pipelineId: number) => void;
  onOpenFinished?: (pipelineId: number) => void;
  onOpenTracking?: (postId: number | null) => void;
  onOpenReview?: (postId: number | null) => void;
  onOpenTopics?: () => void;
}

function TodayTaskPage({
  onOpenHotspot,
  onOpenTopic,
  onOpenProject,
  onOpenWorkspace,
  onOpenMaterials,
  onOpenEditing,
  onOpenFinished,
  onOpenTracking,
  onOpenReview,
  onOpenTopics,
}: TodayTaskPageProps) {
  const [dashboard, setDashboard] =
    useState<TodayTasksResponse>(emptyDashboard);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [error, setError] = useState("");
  const [completingId, setCompletingId] = useState<string | null>(null);

  const loadDashboard = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setDashboard(await getTodayTasks());
    } catch (loadError) {
      setError(toMessage(loadError, "任务读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard, refreshKey]);

  function openTask(task: TodayTaskItem) {
    switch (task.target_type) {
      case "hotspot":
        if (task.target_id !== null) onOpenHotspot?.(task.target_id);
        break;
      case "topic":
        if (task.target_id !== null) onOpenTopic?.(task.target_id);
        break;
      case "topic_pool":
        onOpenTopics?.();
        break;
      case "project":
        if (task.target_id !== null) onOpenProject?.(task.target_id);
        break;
      case "workspace":
        if (task.target_id !== null) onOpenWorkspace?.(task.target_id);
        break;
      case "materials":
        if (task.target_id !== null) onOpenMaterials?.(task.target_id);
        break;
      case "editing":
        if (task.target_id !== null) onOpenEditing?.(task.target_id);
        break;
      case "finished":
        if (task.target_id !== null) onOpenFinished?.(task.target_id);
        break;
      case "tracking":
        onOpenTracking?.(task.target_id);
        break;
      case "review":
        onOpenReview?.(task.target_id);
        break;
    }
  }

  async function handleCompleteTask(task: TodayTaskItem) {
    setCompletingId(task.task_id);
    setError("");
    try {
      const result = await completeTodayTask(task.task_id);
      setDashboard((current) => markTaskDone(current, task.task_id, result.handled_task_ids));
    } catch (completeError) {
      setError(toMessage(completeError, "任务完成状态保存失败。"));
    } finally {
      setCompletingId(null);
    }
  }

  const attentionItems = uniqueTasks(dashboard.confirmations || []).filter(
    (task) => !task.is_done,
  );
  const attentionIds = new Set(attentionItems.map((task) => task.task_id));
  const decisionItems = uniqueTasks(dashboard.decision_items || []).filter(
    (task) => !task.is_done && !attentionIds.has(task.task_id),
  );
  const runningItems =
    dashboard.running_items?.length > 0
      ? dashboard.running_items.filter((task) => !task.is_done)
      : dashboard.projects.filter((task) => !task.is_done);
  return (
    <main className="app-shell today-shell">
      <header className="page-header">
        <div>
          <h1>任务</h1>
        </div>
        <div className="page-header-actions">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新任务
          </button>
        </div>
      </header>

      {error && (
        <p className="message message-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          {error}
        </p>
      )}

      {loading ? (
        <div className="empty-state today-loading">
          <LoaderCircle className="spin" size={24} aria-hidden="true" />
          正在整理今天该看的内容...
        </div>
      ) : (
        <section className="today-stack" aria-label="任务进度">
          <TaskSection
            title="需要处理"
            icon={<AlertTriangle size={17} aria-hidden="true" />}
            empty="当前没有失败、待批准或证据失效项目。"
            tone={attentionItems.length > 0 ? "warning" : "normal"}
          >
            {attentionItems.map((task) => (
              <TaskCard
                key={task.task_id}
                task={task}
                mode="decision"
                onOpen={() => openTask(task)}
                allowComplete={false}
                forceShowReason
              />
            ))}
          </TaskSection>
          <TaskSection
            title="决策项目"
            icon={<ListChecks size={17} aria-hidden="true" />}
            empty="暂无待确认选题或脚本。"
            tone={decisionItems.length > 0 ? "warning" : "normal"}
          >
            {decisionItems.map((task) => (
              <TaskCard
                key={task.task_id}
                task={task}
                mode="decision"
                onOpen={() => openTask(task)}
                onComplete={() => void handleCompleteTask(task)}
                completing={completingId === task.task_id}
              />
            ))}
          </TaskSection>
          <TaskSection
            title="进行项目"
            icon={<Video size={17} aria-hidden="true" />}
            empty="当前没有正在推进的项目。"
          >
            {runningItems.map((task) => (
              <TaskCard
                key={task.task_id}
                task={task}
                mode="running"
                onOpen={() => openTask(task)}
              />
            ))}
          </TaskSection>
        </section>
      )}
    </main>
  );
}

function TaskSection({
  title,
  icon,
  empty,
  tone = "normal",
  children,
}: {
  title: string;
  icon: ReactNode;
  empty: string;
  tone?: "normal" | "warning";
  children: ReactNode;
}) {
  const items = Array.isArray(children) ? children.filter(Boolean) : children;
  const isEmpty = Array.isArray(items) ? items.length === 0 : !items;
  return (
    <section className={`today-section today-section-${tone}`}>
      <h2>
        {icon}
        {title}
      </h2>
      {isEmpty ? <p className="today-empty">{empty}</p> : <div>{children}</div>}
    </section>
  );
}

function TaskCard({
  task,
  mode,
  onOpen,
  onComplete,
  completing = false,
  allowComplete = true,
  forceShowReason = false,
}: {
  task: TodayTaskItem;
  mode: "decision" | "running";
  onOpen: () => void;
  onComplete?: () => void;
  completing?: boolean;
  allowComplete?: boolean;
  forceShowReason?: boolean;
}) {
  const compactDecision = typeof task.meta.decision_type === "string";
  const title = mode === "running" ? runningStageTitle(task) : task.title;
  const subtitle = mode === "running" ? runningProgressText(task) : task.subtitle;
  const actionLabel =
    task.target_type === "finished" ? "去成片验收" : task.next_action;
  return (
    <article className={`today-card today-card-${mode}`}>
      <div className="today-card-main">
        <div className="today-card-title">
          <strong>{title}</strong>
          {taskVideoLabel(task) && (
            <span className="status-badge status-active">{taskVideoLabel(task)}</span>
          )}
        </div>
        <span>{subtitle}</span>
        {!compactDecision &&
          (forceShowReason || task.meta.show_reason === true) && <p>{task.reason}</p>}
      </div>
      {mode === "decision" && (
        <div className="today-card-actions">
          <button
            className="button button-primary"
            type="button"
            aria-label={`执行 ${task.title}`}
            onClick={onOpen}
          >
            {actionLabel}
          </button>
          {allowComplete && task.target_type !== "finished" && (
            <button
              className="button button-secondary"
              type="button"
              aria-label={`完成 ${task.title}`}
              onClick={onComplete}
              disabled={completing}
            >
              {completing && (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              )}
              完成
            </button>
          )}
        </div>
      )}
    </article>
  );
}

function uniqueTasks(items: TodayTaskItem[]): TodayTaskItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.task_id)) return false;
    seen.add(item.task_id);
    return true;
  });
}

function markTaskDone(
  dashboard: TodayTasksResponse,
  taskId: string,
  handledTaskIds: string[],
): TodayTasksResponse {
  const mark = (items: TodayTaskItem[]) =>
    items.map((item) =>
      item.task_id === taskId ? { ...item, is_done: true } : item,
    );
  return {
    ...dashboard,
    hotspots: mark(dashboard.hotspots || []),
    topic_ideas: mark(dashboard.topic_ideas || []),
    collectible_ideas: mark(dashboard.collectible_ideas || []),
    projects: mark(dashboard.projects || []),
    confirmations: mark(dashboard.confirmations || []),
    publish_reminders: mark(dashboard.publish_reminders || []),
    suggestions: mark(dashboard.suggestions || []),
    decision_items: mark(dashboard.decision_items || []),
    running_items: mark(dashboard.running_items || []),
    finished_items: mark(dashboard.finished_items || []),
    handled_task_ids: handledTaskIds,
  };
}

function taskVideoLabel(task: TodayTaskItem): string {
  const label = task.meta.video_type_label;
  return typeof label === "string" ? label : "";
}

function runningStageTitle(task: TodayTaskItem): string {
  if (task.meta.has_failed === true) {
    return "流程需要检查";
  }
  const currentStep =
    typeof task.meta.current_step === "string"
      ? task.meta.current_step
      : task.subtitle.replace(/^当前步骤：/, "");
  const labels: Record<string, string> = {
    生成候选选题: "正在生成候选事件",
    选择候选选题: "正在确定本期事件",
    生成脚本素材: "正在生成脚本",
    生成素材任务: "正在处理素材",
    "生成 Codex 图文任务": "正在补充图文",
    生成数字人字段: "正在准备配音字段",
    创建中文项目目录: "正在整理项目文件",
    生成剪辑清单: "正在生成剪辑方案",
    "导出 script.md": "正在写出口播文件",
    "导出 shotlist.csv": "正在写出分镜表",
    "导出 edit_manifest.json": "正在写出剪辑清单",
  };
  return labels[currentStep] || `正在${currentStep || "推进流程"}`;
}

function runningProgressText(task: TodayTaskItem): string {
  const completed =
    typeof task.meta.completed_steps === "number" ? task.meta.completed_steps : null;
  const total = typeof task.meta.total_steps === "number" ? task.meta.total_steps : null;
  const stageTitle = runningStageTitle(task);
  const rawProjectName =
    typeof task.meta.project_name === "string"
      ? task.meta.project_name
      : task.title !== stageTitle
        ? task.title
        : "";
  const projectName = rawProjectName ? ` · ${rawProjectName}` : "";
  if (completed !== null && total !== null && total > 0) {
    return `${completed}/${total}${projectName}`;
  }
  return task.subtitle || task.reason;
}

function toMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default TodayTaskPage;
