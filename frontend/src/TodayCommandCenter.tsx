import { useEffect, useState } from "react";

import ProjectListPage from "./ProjectListPage";
import SecondaryTabs from "./SecondaryTabs";
import TodayTaskPage from "./TodayTaskPage";

interface TodayCommandCenterProps {
  targetPipelineId: number | null;
  onOpenHotspot?: (hotspotId: number) => void;
  onOpenTopic?: (topicId: number) => void;
  onOpenProject?: (pipelineId: number) => void;
  onOpenWorkspace?: (pipelineId: number) => void;
  onOpenMaterials?: (pipelineId: number) => void;
  onOpenContentQuality?: (pipelineId: number | null) => void;
  onOpenEditing?: (pipelineId: number) => void;
  onOpenFinished?: (pipelineId: number) => void;
  onOpenTracking?: (postId: number | null) => void;
  onOpenReview?: (postId: number | null) => void;
  onOpenTopics?: () => void;
}

function TodayCommandCenter({
  targetPipelineId,
  onOpenHotspot,
  onOpenTopic,
  onOpenProject,
  onOpenWorkspace,
  onOpenMaterials,
  onOpenContentQuality,
  onOpenEditing,
  onOpenFinished,
  onOpenTracking,
  onOpenReview,
  onOpenTopics,
}: TodayCommandCenterProps) {
  const [tab, setTab] = useState<"tasks" | "projects">(
    targetPipelineId !== null ? "projects" : "tasks",
  );

  useEffect(() => {
    if (targetPipelineId !== null) {
      setTab("projects");
    }
  }, [targetPipelineId]);

  return (
    <>
      <SecondaryTabs
        label="任务视图"
        items={[
          { id: "tasks", label: "今日任务" },
          { id: "projects", label: "项目列表" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "tasks" && (
        <TodayTaskPage
          onOpenHotspot={onOpenHotspot}
          onOpenTopic={onOpenTopic}
          onOpenProject={onOpenProject}
          onOpenWorkspace={onOpenWorkspace}
          onOpenMaterials={onOpenMaterials}
          onOpenContentQuality={onOpenContentQuality}
          onOpenEditing={onOpenEditing}
          onOpenFinished={onOpenFinished}
          onOpenTracking={onOpenTracking}
          onOpenReview={onOpenReview}
          onOpenTopics={onOpenTopics}
        />
      )}
      {tab === "projects" && (
        <ProjectListPage
          initialPipelineId={targetPipelineId}
          onOpenWorkspace={onOpenWorkspace}
          onOpenMaterials={onOpenMaterials}
          onOpenHotspots={onOpenTopics}
          onOpenEditing={onOpenEditing}
          onOpenFinished={onOpenFinished}
        />
      )}
    </>
  );
}

export default TodayCommandCenter;
