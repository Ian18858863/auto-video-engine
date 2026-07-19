import { useEffect, useState } from "react";
import {
  BarChart3,
  ClipboardList,
  Clapperboard,
  Film,
  Flame,
  PackageOpen,
  ScrollText,
  Settings,
  type LucideIcon,
} from "lucide-react";

import ContentWorkbenchPage from "./ContentWorkbenchPage";
import EditingExportPage from "./EditingExportPage";
import FinishedVideoPage from "./FinishedVideoPage";
import HotspotTopicPage from "./HotspotTopicPage";
import MaterialPackagePage from "./MaterialPackagePage";
import PublishReviewPage from "./PublishReviewPage";
import SettingsPage from "./SettingsPage";
import TodayCommandCenter from "./TodayCommandCenter";
import "./styles.css";

type PageName =
  | "today"
  | "topics"
  | "workbench"
  | "materials"
  | "editing"
  | "finished"
  | "publishReview"
  | "settings";

const pagePaths: Record<PageName, string> = {
  today: "/tasks",
  topics: "/topics",
  workbench: "/scripts",
  materials: "/materials",
  editing: "/editing",
  finished: "/finished",
  publishReview: "/review",
  settings: "/settings",
};

const projectRoutePages = new Set<PageName>([
  "workbench",
  "materials",
  "editing",
  "finished",
]);

interface AppLocation {
  page: PageName;
  pipelineId: number | null;
}

function parsePipelineId(value: string | null | undefined): number | null {
  if (!value || !/^\d+$/.test(value)) return null;
  const pipelineId = Number(value);
  return Number.isSafeInteger(pipelineId) && pipelineId > 0 ? pipelineId : null;
}

function pageForPath(pathname: string): PageName {
  const path = pathname.replace(/\/+$/, "") || "/";
  const match = (Object.entries(pagePaths) as Array<[PageName, string]>).find(
    ([, basePath]) => path === basePath || path.startsWith(`${basePath}/`),
  );
  return match?.[0] ?? "today";
}

function readAppLocation(): AppLocation {
  const pathname = window.location.pathname;
  const page = pageForPath(pathname);
  const basePath = pagePaths[page];
  const pathRemainder = pathname.slice(basePath.length).replace(/^\/+/, "");
  const query = new URLSearchParams(window.location.search);
  const pathPipelineId =
    page === "today" || projectRoutePages.has(page)
      ? parsePipelineId(pathRemainder.split("/")[0])
      : null;
  const pipelineId =
    pathPipelineId ??
    parsePipelineId(
      query.get("pipeline_id") ?? query.get("pipelineId") ?? query.get("project_id"),
    );
  return { page, pipelineId };
}

function pathForAppLocation(page: PageName, pipelineId: number | null): string {
  const basePath = pagePaths[page];
  if (pipelineId === null) return basePath;
  if (page === "today" || page === "publishReview") {
    return `${basePath}?pipeline_id=${encodeURIComponent(String(pipelineId))}`;
  }
  if (projectRoutePages.has(page)) return `${basePath}/${pipelineId}`;
  return basePath;
}

const navigationItems: Array<{
  page: PageName;
  label: string;
  icon: LucideIcon;
}> = [
  { page: "today", label: "任务", icon: ClipboardList },
  { page: "topics", label: "选题", icon: Flame },
  { page: "workbench", label: "脚本", icon: ScrollText },
  { page: "materials", label: "素材", icon: PackageOpen },
  { page: "editing", label: "剪辑", icon: Film },
  { page: "finished", label: "成片", icon: Clapperboard },
  { page: "publishReview", label: "复盘", icon: BarChart3 },
  { page: "settings", label: "设置", icon: Settings },
];

function App() {
  const [initialLocation] = useState<AppLocation>(() => readAppLocation());
  const [activePage, setActivePage] = useState<PageName>(initialLocation.page);
  const [targetPipelineId, setTargetPipelineId] = useState<number | null>(
    initialLocation.pipelineId,
  );
  const [targetHotspotId, setTargetHotspotId] = useState<number | null>(null);
  const [targetTopicIdeaId, setTargetTopicIdeaId] = useState<number | null>(null);

  useEffect(() => {
    const syncLocation = () => {
      const nextLocation = readAppLocation();
      setActivePage(nextLocation.page);
      setTargetPipelineId(nextLocation.pipelineId);
      setTargetHotspotId(null);
      setTargetTopicIdeaId(null);
    };

    window.addEventListener("popstate", syncLocation);

    const initialPath = pathForAppLocation(
      initialLocation.page,
      initialLocation.pipelineId,
    );
    const currentPath = `${window.location.pathname}${window.location.search}`;
    if (currentPath !== initialPath) {
      window.history.replaceState({}, "", initialPath);
    }

    return () => window.removeEventListener("popstate", syncLocation);
  }, [initialLocation]);

  function navigate(page: PageName, pipelineId: number | null) {
    const nextPath = pathForAppLocation(page, pipelineId);
    const currentPath = `${window.location.pathname}${window.location.search}`;
    if (currentPath !== nextPath) {
      window.history.pushState({}, "", nextPath);
    }
    setActivePage(page);
    setTargetPipelineId(pipelineId);
  }

  function openToday() {
    // Back buttons from a project stage return to that project's task card.
    // The global sidebar still calls navigate directly and intentionally
    // clears the project when Ian asks for the all-project task overview.
    navigate("today", targetPipelineId);
  }

  function openTopics() {
    navigate("topics", null);
    setTargetHotspotId(null);
    setTargetTopicIdeaId(null);
  }

  function openProject(pipelineId: number) {
    navigate("today", pipelineId);
  }

  function openWorkspace(pipelineId: number) {
    navigate("workbench", pipelineId);
  }

  function openContentQuality(pipelineId: number | null = null) {
    navigate("workbench", pipelineId ?? targetPipelineId);
  }

  function openMaterials(pipelineId: number) {
    navigate("materials", pipelineId);
  }

  function openEditing(pipelineId: number) {
    navigate("editing", pipelineId);
  }

  function openFinished(pipelineId: number) {
    navigate("finished", pipelineId);
  }

  return (
    <div className="app-layout">
      <aside className="app-sidebar">
        <div className="sidebar-brand">
          <strong>Auto Video Engine</strong>
          <span>V1 自动化</span>
        </div>
        <nav className="app-navigation" aria-label="主导航">
          {navigationItems.map((item) => {
            const Icon = item.icon;
            const active = activePage === item.page;
            return (
              <button
                key={item.page}
                className={active ? "nav-tab active" : "nav-tab"}
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => {
                  const preservePipeline =
                    projectRoutePages.has(item.page) || item.page === "publishReview";
                  navigate(
                    item.page,
                    preservePipeline ? targetPipelineId : null,
                  );
                  if (item.page === "topics") {
                    setTargetHotspotId(null);
                    setTargetTopicIdeaId(null);
                  }
                }}
              >
                <Icon size={18} aria-hidden="true" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
      </aside>
      <div className="app-main">
        {activePage === "today" && (
          <TodayCommandCenter
            targetPipelineId={targetPipelineId}
            onOpenHotspot={(hotspotId) => {
              navigate("topics", null);
              setTargetHotspotId(hotspotId);
              setTargetTopicIdeaId(null);
            }}
            onOpenTopic={(topicIdeaId) => {
              navigate("topics", null);
              setTargetHotspotId(null);
              setTargetTopicIdeaId(topicIdeaId);
            }}
            onOpenProject={openProject}
            onOpenWorkspace={openWorkspace}
            onOpenMaterials={openMaterials}
            onOpenContentQuality={openContentQuality}
            onOpenEditing={openEditing}
            onOpenFinished={openFinished}
            onOpenTracking={() => navigate("publishReview", targetPipelineId)}
            onOpenReview={() => navigate("publishReview", targetPipelineId)}
            onOpenTopics={openTopics}
          />
        )}
        {activePage === "topics" && (
          <HotspotTopicPage
            initialHotspotId={targetHotspotId}
            initialTopicIdeaId={targetTopicIdeaId}
            onOpenProject={openProject}
            onOpenWorkspace={openWorkspace}
          />
        )}
        {activePage === "workbench" && (
          <ContentWorkbenchPage
            pipelineId={targetPipelineId}
            onBackTopics={openTopics}
            onOpenMaterials={openMaterials}
            onOpenEditing={openEditing}
          />
        )}
        {activePage === "materials" && (
          <MaterialPackagePage
            pipelineId={targetPipelineId}
            onOpenProjects={openToday}
          />
        )}
        {activePage === "editing" && (
          <EditingExportPage
            pipelineId={targetPipelineId}
            onOpenToday={openToday}
            onOpenMaterials={openMaterials}
          />
        )}
        {activePage === "finished" && (
          <FinishedVideoPage
            pipelineId={targetPipelineId}
            onOpenToday={openToday}
            onOpenEditing={openEditing}
            onOpenTracking={() => navigate("publishReview", targetPipelineId)}
          />
        )}
        {activePage === "publishReview" && (
          <PublishReviewPage pipelineId={targetPipelineId} />
        )}
        {activePage === "settings" && <SettingsPage />}
      </div>
    </div>
  );
}

export default App;
