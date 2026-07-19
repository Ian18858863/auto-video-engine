import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import VideoTrackingPage from "./VideoTrackingPage";

const record = {
  id: 5,
  pipeline_id: 18,
  project_name: "AI 工具采购",
  video_title: "企业为什么开始采购 AI 工具",
  platform: "抖音",
  published_at: "2026-06-15T10:00:00Z",
  video_url: "https://example.com/video",
  video_format: "数字人口播",
  duration_seconds: 48,
  views: 1800,
  likes: 130,
  comments: 22,
  favorites: 70,
  shares: 35,
  followers_gained: 18,
  completion_rate: 46,
  average_watch_seconds: 30,
  latest_updated_at: "2026-06-15T12:00:00Z",
  current_stage: "2小时",
  current_status: "有潜力",
  notes: "持续观察",
  snapshot_count: 2,
  favorite_rate: 3.89,
  share_rate: 1.94,
  view_velocity: 800,
};

const detail = {
  ...record,
  video_url: "https://example.com/video",
  snapshots: [
    {
      id: 1,
      captured_at: "2026-06-15T11:00:00Z",
      elapsed_minutes: 60,
      views: 1000,
      likes: 80,
      comments: 12,
      favorites: 40,
      shares: 18,
      followers_gained: 9,
      completion_rate: 42.5,
      average_watch_seconds: 28,
      notes: "1 小时",
    },
    {
      id: 2,
      captured_at: "2026-06-15T12:00:00Z",
      elapsed_minutes: 120,
      views: 1800,
      likes: 130,
      comments: 22,
      favorites: 70,
      shares: 35,
      followers_gained: 18,
      completion_rate: 46,
      average_watch_seconds: 30,
      notes: "2 小时",
    },
  ],
};

function listResponse(items = [record]) {
  return {
    items,
    pagination: { page: 1, page_size: 20, total: items.length, total_pages: 1 },
    filters: {
      platforms: ["抖音", "B站"],
      projects: [{ id: 18, title: "AI 工具采购" }],
      statuses: ["观察中", "正常", "加速", "掉速", "有潜力", "需要复盘"],
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

describe("发布数据追踪页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/video-posts" && init?.method === "POST") {
          return jsonResponse({ ...record, id: 6, video_title: "新发布视频" });
        }
        if (url === "/api/video-posts/5" && init?.method === "PUT") {
          return jsonResponse({ ...record, video_title: "更新后标题" });
        }
        if (url === "/api/video-posts/5/snapshots" && init?.method === "POST") {
          return jsonResponse({ ...record, views: 2600, snapshot_count: 3 });
        }
        if (url === "/api/video-posts/5") {
          return jsonResponse(detail);
        }
        if (url.startsWith("/api/video-posts?")) {
          return jsonResponse(listResponse());
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("能打开并显示最新发布数据", async () => {
    render(<VideoTrackingPage />);

    expect(
      screen.getByRole("heading", { name: "发布数据追踪" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("企业为什么开始采购 AI 工具"),
    ).toBeInTheDocument();
    expect(screen.getByText("1,800")).toBeInTheDocument();
    expect(screen.getAllByText("有潜力").length).toBeGreaterThan(0);
  });

  it("从项目上下文初始化并只加载该项目", async () => {
    render(<VideoTrackingPage initialPipelineId={18} />);

    await screen.findByText("企业为什么开始采购 AI 工具");

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("pipeline_id=18"),
      expect.any(Object),
    );
  });

  it("没有发布记录时提供新增入口", async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/video-posts?")) {
        return jsonResponse(listResponse([]));
      }
      if (url === "/api/video-posts" && init?.method === "POST") {
        return jsonResponse({ ...record, id: 6, video_title: "新发布视频" });
      }
      throw new Error(`未处理的请求：${url}`);
    });
    const user = userEvent.setup();
    render(<VideoTrackingPage />);

    expect(await screen.findByText("还没有发布记录。")).toBeInTheDocument();
    const addButtons = screen.getAllByRole("button", { name: "新增发布记录" });
    await user.click(addButtons[addButtons.length - 1]);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("能新增和编辑发布记录", async () => {
    const user = userEvent.setup();
    render(<VideoTrackingPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.click(screen.getByRole("button", { name: "新增发布记录" }));
    const createDialog = screen.getByRole("dialog");
    await user.selectOptions(within(createDialog).getByLabelText("关联项目"), "18");
    await user.type(within(createDialog).getByLabelText("视频标题"), "新发布视频");
    await user.selectOptions(within(createDialog).getByLabelText("发布平台"), "抖音");
    await user.type(
      within(createDialog).getByLabelText("发布时间"),
      "2026-06-15T12:00",
    );
    await user.click(within(createDialog).getByRole("button", { name: "保存发布记录" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/video-posts",
        expect.objectContaining({ method: "POST" }),
      );
    });

    await user.click(
      screen.getByRole("button", {
        name: "编辑 企业为什么开始采购 AI 工具",
      }),
    );
    const editDialog = screen.getByRole("dialog");
    const titleInput = within(editDialog).getByLabelText("视频标题");
    await user.clear(titleInput);
    await user.type(titleInput, "更新后标题");
    await user.click(within(editDialog).getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/video-posts/5",
        expect.objectContaining({ method: "PUT" }),
      );
    });
  });

  it("能更新最新数据并保存快照", async () => {
    const user = userEvent.setup();
    render(<VideoTrackingPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.click(
      screen.getByRole("button", {
        name: "更新 企业为什么开始采购 AI 工具 的数据",
      }),
    );
    const dialog = screen.getByRole("dialog");
    const viewsInput = within(dialog).getByLabelText("播放量");
    await user.clear(viewsInput);
    await user.type(viewsInput, "2600");
    await user.click(within(dialog).getByRole("button", { name: "保存数据快照" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/video-posts/5/snapshots",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("能查看历史快照", async () => {
    const user = userEvent.setup();
    render(<VideoTrackingPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.click(
      screen.getByRole("button", {
        name: "查看 企业为什么开始采购 AI 工具 的历史",
      }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("1 小时")).toBeInTheDocument();
    expect(within(dialog).getByText("2 小时")).toBeInTheDocument();
    expect(within(dialog).queryByText("2,600")).not.toBeInTheDocument();
  });

  it("能筛选、排序并刷新本地数据", async () => {
    const user = userEvent.setup();
    render(<VideoTrackingPage />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.selectOptions(screen.getByLabelText("按平台筛选"), "抖音");
    await user.selectOptions(screen.getByLabelText("按项目筛选"), "18");
    await user.selectOptions(screen.getByLabelText("按状态筛选"), "有潜力");
    await user.selectOptions(screen.getByLabelText("排序方式"), "favorites");

    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("sort_by=favorites"),
        expect.any(Object),
      );
    });

    const callsBeforeRefresh = vi.mocked(fetch).mock.calls.length;
    await user.click(screen.getByRole("button", { name: "刷新本地数据" }));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledTimes(callsBeforeRefresh + 1),
    );
  });

  it("能从发布数据追踪跳到数据复盘", async () => {
    const user = userEvent.setup();
    const onOpenReviews = vi.fn();
    render(<VideoTrackingPage onOpenReviews={onOpenReviews} />);
    await screen.findByText("企业为什么开始采购 AI 工具");

    await user.click(screen.getByRole("button", { name: "数据复盘" }));

    expect(onOpenReviews).toHaveBeenCalledTimes(1);
  });
});
