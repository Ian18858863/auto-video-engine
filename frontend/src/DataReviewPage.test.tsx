import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataReviewPage from "./DataReviewPage";

const review = {
  video_post_id: 5,
  pipeline_id: 18,
  project_name: "AI 工具采购",
  video_title: "企业为什么开始采购 AI 工具",
  platform: "抖音",
  published_at: "2026-06-14T10:00:00Z",
  views: 1800,
  likes: 130,
  comments: 22,
  favorites: 90,
  shares: 45,
  followers_gained: 20,
  completion_rate: 52,
  average_watch_seconds: 35,
  favorite_rate: 5,
  share_rate: 2.5,
  follower_rate: 1.11,
  view_velocity: 400,
  growth_trend: "长尾增长",
  judgement: "值得继续做",
  reasons: ["收藏率高，内容有保存价值。", "24 小时后仍在增长，有长尾潜力。"],
  next_suggestion: "继续做同类选题，并保留当前结构。",
  snapshot_count: 2,
};

const detail = {
  ...review,
  snapshots: [
    {
      id: 1,
      captured_at: "2026-06-14T12:00:00Z",
      elapsed_minutes: 120,
      views: 1000,
      likes: 80,
      comments: 12,
      favorites: 45,
      shares: 20,
      followers_gained: 8,
      completion_rate: 48,
      average_watch_seconds: 32,
      notes: "2 小时",
    },
    {
      id: 2,
      captured_at: "2026-06-15T12:00:00Z",
      elapsed_minutes: 1560,
      views: 1800,
      likes: 130,
      comments: 22,
      favorites: 90,
      shares: 45,
      followers_gained: 20,
      completion_rate: 52,
      average_watch_seconds: 35,
      notes: "24 小时后",
    },
  ],
};

function listResponse() {
  return {
    items: [review],
    pagination: { page: 1, page_size: 20, total: 1, total_pages: 1 },
    filters: {
      platforms: ["抖音", "B站"],
      projects: [{ id: 18, title: "AI 工具采购" }],
      judgements: ["表现一般", "有潜力", "值得继续做", "需要调整", "不建议继续"],
    },
  };
}

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("数据复盘页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/reviews/5") return jsonResponse(detail);
        if (url.startsWith("/api/reviews?")) return jsonResponse(listResponse());
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("能打开并显示发布数据、比率、趋势和基础判断", async () => {
    render(<DataReviewPage />);

    expect(screen.getByRole("heading", { name: "数据复盘" })).toBeInTheDocument();
    expect(
      await screen.findByText("企业为什么开始采购 AI 工具"),
    ).toBeInTheDocument();
    expect(screen.getByText("5%")).toBeInTheDocument();
    expect(screen.getByText("2.5%")).toBeInTheDocument();
    expect(screen.getByText("1.11%")).toBeInTheDocument();
    expect(screen.getByText("长尾增长")).toBeInTheDocument();
    expect(screen.getAllByText("值得继续做").length).toBeGreaterThan(0);
  });

  it("从项目上下文初始化并只加载该项目", async () => {
    render(<DataReviewPage initialPipelineId={18} />);

    await screen.findByText("企业为什么开始采购 AI 工具");

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("pipeline_id=18"),
      expect.any(Object),
    );
  });

  it("没有复盘数据时提供下一步入口", async () => {
    const onOpenTracking = vi.fn();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/reviews?")) {
        return jsonResponse({
          ...listResponse(),
          items: [],
          pagination: { page: 1, page_size: 20, total: 0, total_pages: 0 },
        });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    const user = userEvent.setup();

    render(<DataReviewPage onOpenTracking={onOpenTracking} />);

    expect(await screen.findByText("当前没有可复盘的发布数据。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "去发布数据追踪" }));
    expect(onOpenTracking).toHaveBeenCalledTimes(1);
  });

  it("加载时诚实说明正在读取而不是生成复盘", () => {
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    render(<DataReviewPage />);

    expect(screen.getByText("正在读取复盘数据...")).toBeInTheDocument();
    expect(screen.queryByText("正在生成基础复盘...")).not.toBeInTheDocument();
  });

  it("能按平台、项目和发布时间筛选", async () => {
    const user = userEvent.setup();
    render(<DataReviewPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.selectOptions(screen.getByLabelText("按平台筛选"), "抖音");
    await user.selectOptions(screen.getByLabelText("按项目筛选"), "18");
    await user.type(screen.getByLabelText("开始日期"), "2026-06-01");
    await user.type(screen.getByLabelText("结束日期"), "2026-06-15");

    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("published_from=2026-06-01"),
        expect.any(Object),
      );
    });
  });

  it("能按收藏率、转发率和播放增速排序", async () => {
    const user = userEvent.setup();
    render(<DataReviewPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.selectOptions(screen.getByLabelText("排序方式"), "favorite_rate");
    await waitFor(() =>
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("sort_by=favorite_rate"),
        expect.any(Object),
      ),
    );
    await user.selectOptions(screen.getByLabelText("排序方式"), "share_rate");
    await user.selectOptions(screen.getByLabelText("排序方式"), "view_velocity");

    await waitFor(() =>
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("sort_by=view_velocity"),
        expect.any(Object),
      ),
    );
  });

  it("能查看单条视频详细复盘、建议和快照变化", async () => {
    const user = userEvent.setup();
    render(<DataReviewPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.click(
      screen.getByRole("button", {
        name: "查看 企业为什么开始采购 AI 工具 的详细复盘",
      }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("继续做同类选题，并保留当前结构。")).toBeInTheDocument();
    expect(within(dialog).getByText("24 小时后")).toBeInTheDocument();
    expect(within(dialog).getByText("播放 1,800")).toBeInTheDocument();
  });
});
