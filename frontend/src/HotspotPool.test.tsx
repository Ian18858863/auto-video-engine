import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HotspotPool from "./HotspotPool";

const hotspots = [
  {
    id: 2,
    source_platform: "Hacker News",
    source_type: "community",
    title: "AI 工具进入企业采购流程",
    url: "https://example.com/ai-buying",
    published_at: "2026-06-15T09:00:00+08:00",
    fetched_at: "2026-06-15T09:05:00+08:00",
    summary: "企业开始把 AI 工具放进真实预算。",
    metrics: { rank: 1, comments: 120 },
    tags: ["AI", "商业"],
    category: "AI",
    is_filtered: false,
    filter_reason: "",
    hot_score: 88,
    topic_score: 95,
    save_score: 90,
    has_project: false,
  },
  {
    id: 1,
    source_platform: "Google News",
    source_type: "news",
    title: "消费趋势发生新变化",
    url: "https://example.com/consumer",
    published_at: "2026-06-15T08:00:00+08:00",
    fetched_at: "2026-06-15T08:05:00+08:00",
    summary: "新的消费行为开始影响品牌决策。",
    metrics: {},
    tags: ["消费", "趋势"],
    category: "消费",
    is_filtered: true,
    filter_reason: "低信息价值",
    hot_score: 76,
    topic_score: 70,
    save_score: 65,
    has_project: true,
  },
];

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

describe("热点池页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (
          url === "/api/pipeline/hotspots/2/build-project" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            pipeline_id: 9,
            hotspot_id: 2,
            project_name: "AI 工具进入企业采购流程",
            status: "已完成",
            provider: "fake",
          });
        }
        if (url === "/api/hotspots/2") {
          return jsonResponse(hotspots[0]);
        }
        if (url === "/api/hotspots/import" && init?.method === "POST") {
          return jsonResponse({
            created: true,
            message: "手动热点已保存",
            item: { ...hotspots[0], id: 3, url: "" },
          });
        }
        if (url.startsWith("/api/hotspots?")) {
          return jsonResponse({
            items: hotspots,
            pagination: {
              page: 1,
              page_size: 20,
              total: 2,
              total_pages: 1,
            },
            filters: {
              categories: ["AI", "消费"],
              sources: ["Google News", "Hacker News"],
            },
          });
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("能打开并显示热点列表", async () => {
    render(<HotspotPool />);

    expect(screen.getByRole("heading", { name: "原始热点" })).toBeInTheDocument();
    expect(
      await screen.findByText("AI 工具进入企业采购流程"),
    ).toBeInTheDocument();
    expect(screen.getByText("消费趋势发生新变化")).toBeInTheDocument();
    expect(screen.getByText("95")).toBeInTheDocument();
  });

  it("能筛选未过滤热点、分类和来源", async () => {
    const user = userEvent.setup();
    render(<HotspotPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(screen.getByRole("checkbox", { name: "只看未过滤热点" }));
    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("unfiltered_only=true"),
        expect.any(Object),
      );
    });

    await user.selectOptions(screen.getByLabelText("按分类筛选"), "AI");
    await user.selectOptions(
      screen.getByLabelText("按来源筛选"),
      "Hacker News",
    );
    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("source_platform=Hacker+News"),
        expect.any(Object),
      );
    });
  });

  it("能按三种分数排序", async () => {
    const user = userEvent.setup();
    render(<HotspotPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.selectOptions(screen.getByLabelText("排序方式"), "save_score");

    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        expect.stringContaining("sort_by=save_score"),
        expect.any(Object),
      );
    });
  });

  it("能点击热点查看详情", async () => {
    const user = userEvent.setup();
    render(<HotspotPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(
      screen.getByRole("button", {
        name: "查看 AI 工具进入企业采购流程",
      }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(
      within(dialog).getByText("企业开始把 AI 工具放进真实预算。"),
    ).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/api/hotspots/2", expect.any(Object));
  });

  it("能手动创建内容项目且结果为 fake provider", async () => {
    const user = userEvent.setup();
    render(<HotspotPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(
      screen.getByRole("button", {
        name: "创建 AI 工具进入企业采购流程 的内容项目",
      }),
    );

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/pipeline/hotspots/2/build-project",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_name: "AI 工具进入企业采购流程",
          }),
        },
      );
    });
    expect(
      await screen.findByText(/项目已创建.*fake provider/),
    ).toBeInTheDocument();
  });

  it("能从热点池跳到候选选题池", async () => {
    const user = userEvent.setup();
    const onOpenTopics = vi.fn();
    render(<HotspotPool onOpenTopics={onOpenTopics} />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(screen.getByRole("button", { name: "候选选题" }));

    expect(onOpenTopics).toHaveBeenCalledTimes(1);
  });

  it("手动导入热点时链接可以留空", async () => {
    const user = userEvent.setup();
    render(<HotspotPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(screen.getByRole("button", { name: "手动导入备用" }));
    await user.type(screen.getByLabelText("标题"), "AI 搜索正在改变普通人获取信息的方式");
    await user.type(screen.getByLabelText("摘要"), "没有原文链接时也要能先进入热点池。");
    await user.click(screen.getByRole("button", { name: "保存到热点池" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/hotspots/import",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"url":""'),
        }),
      );
    });
    expect(await screen.findByText("手动热点已保存")).toBeInTheDocument();
  });
});
