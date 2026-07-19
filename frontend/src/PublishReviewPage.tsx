import { useState } from "react";

import DataReviewPage from "./DataReviewPage";
import SecondaryTabs from "./SecondaryTabs";
import VideoTrackingPage from "./VideoTrackingPage";
import WeeklyReviewPage from "./WeeklyReviewPage";

function PublishReviewPage({ pipelineId = null }: { pipelineId?: number | null }) {
  const [tab, setTab] = useState<"tracking" | "review" | "weekly">("tracking");
  return (
    <>
      <SecondaryTabs
        label="复盘视图"
        items={[
          { id: "tracking", label: "发布数据" },
          { id: "review", label: "数据复盘" },
          { id: "weekly", label: "周报建议" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "tracking" && (
        <VideoTrackingPage
          key={`tracking-${pipelineId ?? "all"}`}
          initialPipelineId={pipelineId}
          onOpenReviews={() => setTab("review")}
        />
      )}
      {tab === "review" && (
        <DataReviewPage
          key={`review-${pipelineId ?? "all"}`}
          initialPipelineId={pipelineId}
          onOpenTracking={() => setTab("tracking")}
        />
      )}
      {tab === "weekly" && (
        <WeeklyReviewPage
          pipelineId={pipelineId}
          onOpenTracking={() => setTab("tracking")}
        />
      )}
    </>
  );
}

export default PublishReviewPage;
