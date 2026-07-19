import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HotspotTopicPage from "./HotspotTopicPage";

const hotspot = {
  id: 2,
  original_title: "AI 工具进入企业采购流程",
  original_url: "https://example.com/ai-buying",
  source_platform: "Hacker News",
  source_type: "community",
  title: "AI 工具进入企业采购流程",
  url: "https://example.com/ai-buying",
  published_at: "2026-06-15T09:00:00+08:00",
  fetched_at: "2026-06-15T09:05:00+08:00",
  summary: "企业开始把 AI 工具放进真实预算。",
  metrics: {},
  tags: ["AI", "商业"],
  category: "AI",
  data_origin: "real",
  origin_note: "",
  is_filtered: false,
  filter_reason: "",
  hot_score: 88,
  topic_score: 95,
  save_score: 90,
  risk_score: 0,
  freshness_score: 96,
  duplicate_reason: "",
  source_credibility: "高",
  source_credibility_reason: "有原文 URL、发布时间和正文摘要。",
  has_project: false,
};

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("热点选题聚合页", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/hotspots/2") {
          return jsonResponse(hotspot);
        }
        if (url.startsWith("/api/hotspots?")) {
          return jsonResponse({
            items: [hotspot],
            pagination: { page: 1, page_size: 20, total: 1, total_pages: 1 },
            filters: { categories: ["AI"], sources: ["Hacker News"] },
          });
        }
        if (url.startsWith("/api/topic-ideas?")) {
          return jsonResponse({
            items: [],
            pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
            filters: { hotspots: [] },
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("不再显示候选事件/原始热点双标签，热点入口统一进入选题页", async () => {
    render(<HotspotTopicPage initialHotspotId={2} initialTopicIdeaId={null} />);

    expect(await screen.findByRole("heading", { name: "选题" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "候选事件" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "原始热点" })).not.toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/hotspots?"),
      expect.any(Object),
    ));
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/topic-ideas\?.*hotspot_id=2/),
      expect.any(Object),
    );
  });
});
