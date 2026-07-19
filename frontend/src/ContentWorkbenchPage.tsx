import { useState } from "react";

import ContentQualityPage from "./ContentQualityPage";
import CopywritingSamplePage from "./CopywritingSamplePage";
import SecondaryTabs from "./SecondaryTabs";
import ScriptWorkspace from "./ScriptWorkspace";

interface ContentWorkbenchPageProps {
  pipelineId: number | null;
  onBackTopics?: () => void;
  onOpenMaterials?: (pipelineId: number) => void;
  onOpenEditing?: (pipelineId: number) => void;
}

function ContentWorkbenchPage({
  pipelineId,
  onOpenMaterials,
}: ContentWorkbenchPageProps) {
  const [tab, setTab] = useState<"script" | "quality" | "samples">("script");
  return (
    <>
      <SecondaryTabs
        label="脚本视图"
        items={[
          { id: "script", label: "脚本" },
          { id: "quality", label: "质检" },
          { id: "samples", label: "脚本引擎" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "script" && (
        <ScriptWorkspace
          pipelineId={pipelineId}
          onOpenContentQuality={() => setTab("quality")}
          onOpenMaterials={onOpenMaterials}
        />
      )}
      {tab === "quality" && (
        <ContentQualityPage
          pipelineId={pipelineId}
          onBackWorkspace={() => setTab("script")}
          autoCheckOnLoad
        />
      )}
      {tab === "samples" && <CopywritingSamplePage />}
    </>
  );
}

export default ContentWorkbenchPage;
