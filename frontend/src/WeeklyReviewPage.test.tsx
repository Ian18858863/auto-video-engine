import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import WeeklyReviewPage from "./WeeklyReviewPage";

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("复盘周报空态", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("零发布数据时不展示趋势或方向建议，只提供证据采集入口", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          period: {
            start: "2026-07-12T00:00:00+00:00",
            end: "2026-07-19T00:00:00+00:00",
          },
          published_count: 0,
          data_note: "最近 7 天没有发布数据，无法判断趋势、因果或下周方向。",
          published_videos: [],
          high_favorite_videos: [],
          high_share_videos: [],
          long_tail_videos: [],
          stalled_videos: [],
          continue_topics: [],
          pause_topics: [],
          next_week_do: [],
          next_week_avoid: [],
          local_rules_only: true,
          real_ai_called: false,
        }),
      ),
    );
    const onOpenTracking = vi.fn();
    const user = userEvent.setup();

    render(<WeeklyReviewPage onOpenTracking={onOpenTracking} />);

    expect(
      await screen.findByText("本周还没有可复盘的发布数据。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("先新增发布记录和数据快照；有证据后才会生成趋势和建议。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "有长尾潜力" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "下周建议做什么" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "下周建议避免什么" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "去发布数据追踪" }));
    expect(onOpenTracking).toHaveBeenCalledTimes(1);
  });

  it("按当前项目读取周报并诚实显示读取状态", () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);

    render(<WeeklyReviewPage pipelineId={18} />);

    expect(screen.getByText("正在读取复盘周报...")).toBeInTheDocument();
    expect(screen.queryByText("正在生成复盘周报...")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/reviews/weekly/report?pipeline_id=18",
      expect.any(Object),
    );
  });
});
