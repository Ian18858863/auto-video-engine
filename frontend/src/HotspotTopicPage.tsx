import TopicIdeaPool from "./TopicIdeaPool";

interface HotspotTopicPageProps {
  initialHotspotId: number | null;
  initialTopicIdeaId: number | null;
  onOpenProject?: (pipelineId: number) => void;
  onOpenWorkspace?: (pipelineId: number) => void;
}

function HotspotTopicPage({
  initialHotspotId,
  initialTopicIdeaId,
  onOpenProject,
  onOpenWorkspace,
}: HotspotTopicPageProps) {
  return (
    <TopicIdeaPool
      initialHotspotId={initialHotspotId}
      initialTopicIdeaId={initialTopicIdeaId}
      onOpenProject={onOpenProject}
      onOpenWorkspace={onOpenWorkspace}
    />
  );
}

export default HotspotTopicPage;
