import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TopicIdeaPool from "./TopicIdeaPool";
import type { TopicIdeaItem } from "./types";

const nowIso = new Date().toISOString();

const baseIdea = {
  core_view: "用大白话讲清楚发生了什么，以及普通人应该怎么看。",
  recommended_format: "合集事件",
  why_watch: "能快速看懂今天的重要变化。",
  why_save: "可以复用这套判断标准。",
  comment_trigger: "你更关注哪件事？",
  risk_note: "不做投资建议。",
  recommended_duration: "合集内 12-25 秒",
  hook: "今天这件事不用看太复杂。",
  video_type: "collection_event",
  generation_source: "local_rule",
  recommendation_bucket: "today",
  source_credibility: "高",
  source_credibility_reason: "有原文 URL、发布时间和正文摘要。",
  source_published_at: [nowIso],
  source_fetched_at: [nowIso],
  source_summaries: ["这是一段来自原文的正文摘要。"],
  is_example: false,
  has_script: false,
  script_asset_id: null,
  pipeline_id: null,
  fits_collection: true,
  can_single_deep_dive: false,
  score_breakdown: {
    impact: 84,
    account_fit: 88,
    freshness_heat: 76,
    cross_source_resonance: 78,
    ordinary_relevance: 90,
    risk: 10,
    total: 87,
  },
};

const ideas: TopicIdeaItem[] = [
  {
    ...baseIdea,
    id: 12,
    hot_item_id: 2,
    hotspot_title: "AI 工具进入企业采购流程",
    title: "AI采购",
    recommendation_score: 91,
    source_hotspot_ids: [2],
    source_hotspot_titles: ["AI 工具进入企业采购流程"],
    source_hotspot_urls: ["https://example.com/ai-buy"],
    source_platforms: ["Hacker News"],
    is_selected: false,
    recommendation_bucket: "backup",
  },
  {
    ...baseIdea,
    id: 11,
    hot_item_id: 1,
    hotspot_title: "消费趋势发生新变化",
    title: "消费变化",
    recommendation_score: 88,
    source_hotspot_ids: [1],
    source_hotspot_titles: ["消费趋势发生新变化"],
    source_hotspot_urls: ["https://example.com/consumer"],
    source_platforms: ["Google News"],
    is_selected: true,
    has_script: true,
    script_asset_id: 31,
    pipeline_id: 9,
  },
  {
    ...baseIdea,
    id: 13,
    hot_item_id: 3,
    hotspot_title: "电商平台调整服务规则",
    title: "电商规则",
    recommendation_score: 86,
    source_hotspot_ids: [3],
    source_hotspot_titles: ["电商平台调整服务规则"],
    source_hotspot_urls: ["https://example.com/ecommerce"],
    source_platforms: ["TechCrunch"],
    is_selected: true,
  },
  {
    ...baseIdea,
    id: 14,
    hot_item_id: 4,
    hotspot_title: "车企价格战继续",
    title: "车企降价",
    recommendation_score: 84,
    source_hotspot_ids: [4],
    source_hotspot_titles: ["车企价格战继续"],
    source_hotspot_urls: ["https://example.com/car"],
    source_platforms: ["The Verge"],
    is_selected: true,
    recommendation_bucket: "backup",
  },
];

const collectionIdea = {
  ...baseIdea,
  id: 99,
  hot_item_id: 1,
  hotspot_title: "消费趋势发生新变化",
  title: "今日3件事",
  video_type: "multi_hotspot_collection",
  recommended_format: "多热点合集型",
  recommendation_score: 92,
  recommendation_bucket: "today",
  source_hotspot_ids: [1, 3, 4],
  source_hotspot_titles: ["消费趋势发生新变化", "电商平台调整服务规则", "车企价格战继续"],
  source_hotspot_urls: [
    "https://example.com/consumer",
    "https://example.com/ecommerce",
    "https://example.com/car",
  ],
  source_platforms: ["Google News", "TechCrunch", "The Verge"],
  is_selected: true,
};

const hotspotCategories = ["社会", "财经", "科技", "消费", "AI", "国际", "生活", "娱乐"];

const hotspotSamples = [
  ["社会", "公共服务规则调整"],
  ["财经", "房贷利率继续下调"],
  ["科技", "国产芯片限制放松"],
  ["消费", "外卖平台调整补贴"],
  ["AI", "AI搜索开始收费"],
  ["国际", "美国关税政策变化"],
  ["生活", "医保报销规则调整"],
  ["娱乐", "某电影 6 月 26 日上映"],
] as const;

const defaultFilterSettings = {
  weights: {
    impact: 25,
    account_fit: 20,
    freshness_heat: 20,
    cross_source_resonance: 15,
    ordinary_relevance: 20,
  },
  current_period_limit: 5,
  backup_pool_limit: 10,
  source_limit: 3,
  minimum_score: 40,
  freshness_window_hours: 36,
  bonus_rules: {
    categories: [
      { id: "category_ai", label: "AI 加分", category: "AI", score: 3, enabled: true },
    ],
    keywords: [
      { id: "keyword_ai", label: "AI 关键词", keyword: "AI", score: 2, enabled: true },
    ],
  },
  last_saved_at: nowIso,
  is_active: true,
  using_default: true,
};

const todayHotspots = hotspotSamples.map(([expectedCategory, title], index) => ({
  id: 200 + index,
  original_title: title,
  original_url: `https://example.com/hotspot-${expectedCategory}`,
  source_platform: "Google News",
  source_type: "rss",
  title,
  url: `https://example.com/hotspot-${expectedCategory}`,
  published_at: index === 1 ? "2026-06-01T09:00:00+08:00" : nowIso,
  fetched_at: nowIso,
  summary: `${title}摘要。`,
  metrics: {},
  tags: [],
  category: expectedCategory,
  data_origin: "real",
  origin_note: "",
  is_filtered: false,
  filter_reason: "",
  hot_score: 80,
  topic_score: 90 - index,
  save_score: 70,
  risk_score: 0,
  freshness_score: 90,
  duplicate_reason: "",
  source_credibility: "高",
  source_credibility_reason: "有原文 URL、发布时间和正文摘要。",
  has_project: false,
}));

const entertainmentIdea: TopicIdeaItem = {
  ...baseIdea,
  id: 88,
  hot_item_id: 88,
  hotspot_title: "某电影 6 月 26 日上映",
  title: "电影上映",
  recommendation_score: 82,
  source_hotspot_ids: [88],
  source_hotspot_titles: ["某电影 6 月 26 日上映"],
  source_hotspot_urls: ["https://example.com/movie"],
  source_platforms: ["百度热搜"],
  is_selected: true,
  recommendation_bucket: "today",
};

const additionalDefaultIdea: TopicIdeaItem = {
  ...baseIdea,
  id: 89,
  hot_item_id: 89,
  hotspot_title: "平台规则影响普通人获取信息",
  title: "平台规则变化",
  recommendation_score: 83,
  source_hotspot_ids: [89],
  source_hotspot_titles: ["平台规则影响普通人获取信息"],
  source_hotspot_urls: ["https://example.com/platform-rule"],
  source_platforms: ["财联社"],
  is_selected: false,
  recommendation_bucket: "backup",
};

let topicIdeas: TopicIdeaItem[] = ideas;
let composeResponse: TopicIdeaItem = collectionIdea;

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

function makeSelectedIdea(id: number, title = `事件${id}`): TopicIdeaItem {
  return {
    ...baseIdea,
    id,
    hot_item_id: id,
    hotspot_title: `${title} 原始标题`,
    title,
    recommendation_score: 90 - (id % 10),
    source_hotspot_ids: [id],
    source_hotspot_titles: [`${title} 原始标题`],
    source_hotspot_urls: [`https://example.com/topic-${id}`],
    source_platforms: ["Google News"],
    is_selected: true,
    recommendation_bucket: "backup",
  };
}

function makeBackupIdea(
  id: number,
  source: string,
  title: string,
  score: number,
): TopicIdeaItem {
  return {
    ...baseIdea,
    id,
    hot_item_id: id,
    hotspot_title: `${title} 原始标题`,
    title,
    recommendation_score: score,
    source_hotspot_ids: [id],
    source_hotspot_titles: [`${title} 原始标题`],
    source_hotspot_urls: [`https://example.com/backup-${id}`],
    source_platforms: [source],
    is_selected: false,
    recommendation_bucket: "backup",
  };
}

function makeCurrentIdea(
  id: number,
  source: string,
  title: string,
  score: number,
): TopicIdeaItem {
  return {
    ...makeBackupIdea(id, source, title, score),
    is_selected: true,
    recommendation_bucket: "today",
  };
}

describe("热点选题事件池页面", () => {
  beforeEach(() => {
    topicIdeas = [...ideas, additionalDefaultIdea, entertainmentIdea];
    composeResponse = collectionIdea;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const eventSelectionMatch = url.match(/^\/api\/topic-ideas\/(\d+)\/event-selection$/);
        if (eventSelectionMatch && init?.method === "POST") {
          const ideaId = Number(eventSelectionMatch[1]);
          const body = JSON.parse(String(init.body || "{}"));
          const idea =
            topicIdeas.find((item) => item.id === ideaId) ?? topicIdeas[0];
          return jsonResponse({ ...idea, is_selected: Boolean(body.selected) });
        }
        if (
          url === "/api/topic-ideas/compose-collection" &&
          init?.method === "POST"
        ) {
          return jsonResponse(composeResponse);
        }
        if (
          url === `/api/topic-ideas/${composeResponse.id}/generate-script` &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            pipeline_id: 18,
            topic_idea_id: composeResponse.id,
            script_asset_id: 42,
            project_name: composeResponse.title,
            status: "已完成",
            provider: "fake",
          });
        }
        if (
          url === "/api/automation/hotspots/run" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            source_count: 4,
            skipped_count: 0,
            fetched_count: 30,
            saved_count: 12,
            duplicate_count: 4,
            filtered_count: 2,
            archived_expired_count: 1,
            preserved_expired_count: 0,
            failed_sources: [],
            rate_limited_sources: [],
            source_statuses: [],
            generated_hotspot_count: 15,
            generated_topic_idea_count: 6,
            topic_generation: [],
            provider: "local_rule",
            real_ai_called: false,
            manual_import_is_fallback: true,
            message: "已更新今日事件。",
            ran_at: nowIso,
            next_due_at: nowIso,
          });
        }
        if (
          url === "/api/automation/browser-hotspots/run" &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            source_count: 7,
            fetched_count: 21,
            saved_count: 9,
            duplicate_count: 3,
            csv_written_count: 9,
            failed_sources: [],
            needs_login_sources: [],
            skipped_sources: [],
            csv_path: "exports/热点采集/browser_hotspots.csv",
            message: "浏览器热点已更新。",
            safety_note: "低频采集。",
          });
        }
        const detailMatch = url.match(/^\/api\/topic-ideas\/(\d+)$/);
        if (detailMatch) {
          const ideaId = Number(detailMatch[1]);
          return jsonResponse(
            topicIdeas.find((item) => item.id === ideaId) ?? topicIdeas[0],
          );
        }
        if (url.startsWith("/api/topic-ideas?")) {
          return jsonResponse({
            items: topicIdeas,
            pagination: {
              page: 1,
              page_size: 20,
              total: topicIdeas.length,
              total_pages: 1,
            },
            filters: {
              hotspots: [
                { id: 2, title: "AI 工具进入企业采购流程" },
                { id: 1, title: "消费趋势发生新变化" },
              ],
            },
          });
        }
        if (url === "/api/topic-filter-settings" && (!init?.method || init.method === "GET")) {
          return jsonResponse(defaultFilterSettings);
        }
        if (url === "/api/topic-filter-settings" && init?.method === "PUT") {
          return jsonResponse({
            ...defaultFilterSettings,
            ...JSON.parse(String(init.body || "{}")),
            using_default: false,
          });
        }
        if (url.startsWith("/api/hotspots?")) {
          return jsonResponse({
            items: todayHotspots,
            pagination: {
              page: 1,
              page_size: 100,
              total: todayHotspots.length,
              total_pages: 1,
            },
            filters: {
              categories: ["其他", "商业", ...hotspotCategories],
              sources: ["Google News", "没有数据的来源"],
              category_counts: Object.fromEntries(
                hotspotCategories.map((category) => [
                  category,
                  todayHotspots.filter((item) => item.category === category).length,
                ]),
              ),
              source_counts: {
                "Google News": todayHotspots.length,
              },
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

  it("默认显示本期已选、备选池和铺开的今日热点事件", async () => {
    render(<TopicIdeaPool />);

    expect(screen.getByRole("heading", { name: "选题" })).toBeInTheDocument();
    expect(await screen.findByText("AI 工具进入企业采购流程")).toBeInTheDocument();
    expect(screen.getAllByText(/消费趋势发生新变化/).length).toBeGreaterThan(0);
    expect(screen.queryByText("AI采购")).not.toBeInTheDocument();
    expect(screen.getAllByText(/总分/).length).toBeGreaterThan(0);
    expect(screen.queryByText("能快速看懂今天的重要变化。")).not.toBeInTheDocument();
    expect(screen.getByText("本期已选")).toBeInTheDocument();
    expect(screen.getByText("备选池")).toBeInTheDocument();
    expect(screen.getByText("今日热点事件")).toBeInTheDocument();
    const selectedSection = screen.getByLabelText("本期事件组合");
    const backupSection = screen.getByRole("heading", { name: "备选池" })
      .parentElement as HTMLElement;
    expect(within(selectedSection).queryByText("电影上映")).not.toBeInTheDocument();
    expect(within(backupSection).getByText("某电影 6 月 26 日上映")).toBeInTheDocument();
    const categorySelect = screen.getByLabelText("分类") as HTMLSelectElement;
    hotspotCategories.forEach((category) => {
      expect(Array.from(categorySelect.options).some((option) => option.value === category)).toBe(true);
    });
    expect(screen.queryByRole("group", { name: "今日热点事件" })).not.toBeInTheDocument();
    expect(document.querySelector("details")).not.toBeInTheDocument();
    expect(screen.queryByText("更多")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("候选事件概况")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("候选事件筛选")).not.toBeInTheDocument();
  });

  it("今日热点分类下拉只显示固定八类，来源下拉只显示实际有数据的来源", async () => {
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    const categorySelect = screen.getByLabelText("分类") as HTMLSelectElement;
    const sourceSelect = screen.getByLabelText("来源") as HTMLSelectElement;
    const categoryOptions = Array.from(categorySelect.options).map((option) => option.value);
    const sourceOptions = Array.from(sourceSelect.options).map((option) => option.value);

    expect(categoryOptions).toEqual(["", ...hotspotCategories]);
    expect(categoryOptions).not.toContain("其他");
    expect(categoryOptions).not.toContain("商业");
    expect(sourceOptions).toEqual(["", "Google News"]);
    expect(sourceOptions).not.toContain("没有数据的来源");
  });

  it("切换今日热点筛选只刷新热点列表，不重新刷新备选池和候选事件", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");
    vi.mocked(fetch).mockClear();

    await user.selectOptions(screen.getByLabelText("分类"), "AI");

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/hotspots?"),
        expect.any(Object),
      );
    });
    const requestedUrls = vi.mocked(fetch).mock.calls.map((call) => String(call[0]));
    expect(requestedUrls.some((url) => url.startsWith("/api/topic-ideas?"))).toBe(false);
    expect(screen.queryByText("正在读取候选事件...")).not.toBeInTheDocument();
    expect(screen.getByText("备选池")).toBeInTheDocument();
  });

  it("更新今日事件会先采集浏览器热点再抓取公开源并重新读取候选事件", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(screen.getByRole("button", { name: "更新今日事件（含浏览器热点）" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/automation/browser-hotspots/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          force: true,
        }),
      });
      expect(fetch).toHaveBeenCalledWith("/api/automation/hotspots/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          force: true,
          generate_topics: true,
          max_hotspots_for_topics: 70,
        }),
      });
    });
    expect(await screen.findByText(/已更新今日事件：浏览器新增 9 条，公开源新增 12 条/)).toBeInTheDocument();
  });

  it("可以打开筛选标准，要求五个基础权重合计 100，并保存额外关键词加分", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(screen.getByRole("button", { name: "更新筛选标准" }));
    const dialog = await screen.findByRole("dialog", { name: "更新筛选标准" });

    expect(within(dialog).getByText("五个基础权重合计必须等于 100。额外加分单独计算，不挤占基础权重。")).toBeInTheDocument();
    const impactInput = within(dialog).getByLabelText("影响力度") as HTMLInputElement;
    await user.clear(impactInput);
    await user.type(impactInput, "30");
    expect(within(dialog).getByText("当前合计 105")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "保存并重新筛选" })).toBeDisabled();

    const ordinaryInput = within(dialog).getByLabelText("普通人相关度") as HTMLInputElement;
    await user.clear(ordinaryInput);
    await user.type(ordinaryInput, "15");
    await user.click(within(dialog).getByRole("button", { name: "添加关键词加分" }));
    const newKeywordInput = within(dialog).getByLabelText("关键词 2") as HTMLInputElement;
    await user.clear(newKeywordInput);
    await user.type(newKeywordInput, "普通人");
    const newKeywordScore = within(dialog).getByLabelText("关键词加分 2") as HTMLInputElement;
    await user.clear(newKeywordScore);
    await user.type(newKeywordScore, "5");

    await user.click(within(dialog).getByRole("button", { name: "保存并重新筛选" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/topic-filter-settings",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    const putCall = vi
      .mocked(fetch)
      .mock.calls.find((call) => call[0] === "/api/topic-filter-settings" && call[1]?.method === "PUT");
    const payload = JSON.parse(String(putCall?.[1]?.body || "{}"));
    expect(payload.weights.impact).toBe(30);
    expect(payload.weights.ordinary_relevance).toBe(15);
    expect(payload.bonus_rules.keywords).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ keyword: "普通人", score: 5, enabled: true }),
      ]),
    );
  });

  it("备选池会按来源降权并隐藏重复标题", async () => {
    topicIdeas = [
      makeBackupIdea(701, "36氪", "同源事件一", 99),
      makeBackupIdea(702, "36氪", "同源事件二", 98),
      makeBackupIdea(703, "36氪", "同源事件三", 97),
      makeBackupIdea(704, "36氪", "同源事件四", 96),
      makeBackupIdea(705, "IT之家", "重复事件", 95),
      makeBackupIdea(706, "IT之家", "重复事件", 94),
      makeBackupIdea(707, "微博热搜网页版", "微博事件", 80),
      makeBackupIdea(708, "B站热门网页版", "B站事件", 79),
    ];
    render(<TopicIdeaPool />);

    await screen.findByText("同源事件一 原始标题");
    const selectedSection = screen.getByLabelText("本期事件组合");
    const backupSection = screen.getByRole("heading", { name: "备选池" })
      .parentElement as HTMLElement;
    const rows = within(backupSection).getAllByRole("article");
    const rowText = rows.map((row) => row.textContent || "");

    expect(within(selectedSection).getByText("微博事件 原始标题")).toBeInTheDocument();
    expect(rowText.join(" ")).toContain("B站事件 原始标题");
    expect(screen.getAllByText("重复事件 原始标题")).toHaveLength(1);
  });

  it("所有候选先统一排序，前 5 自动进本期，6-15 自动进备选，前 15 同来源最多 3 条", async () => {
    topicIdeas = [
      makeBackupIdea(801, "36氪", "AI 公司融资变化", 99),
      makeBackupIdea(802, "36氪", "新能源价格继续变化", 98),
      makeBackupIdea(803, "36氪", "商业平台收入调整", 97),
      makeBackupIdea(804, "36氪", "同源第四条应被降重", 96),
      makeBackupIdea(805, "36氪", "同源第五条应被降重", 95),
      makeBackupIdea(806, "IT之家", "手机 AI 功能升级", 94),
      makeBackupIdea(807, "IT之家", "芯片产业链变化", 93),
      makeBackupIdea(808, "IT之家", "消费电子变化", 92),
      makeBackupIdea(809, "微博热搜网页版", "社会服务规则调整", 91),
      makeBackupIdea(810, "百度热搜网页版", "财经价格出现变化", 90),
      makeBackupIdea(811, "小红书热点网页版", "消费体验出现新趋势", 89),
      makeBackupIdea(812, "B站热门网页版", "AI 工具使用方式变化", 88),
      makeBackupIdea(813, "抖音热点网页版", "生活方式发生变化", 87),
      makeBackupIdea(814, "财联社", "金融环境出现变化", 86),
      makeBackupIdea(815, "Hacker News", "海外科技产品变化", 85),
      makeBackupIdea(816, "Google News", "平台规则变化", 84),
      makeBackupIdea(817, "Reuters", "国际商业规则变化", 83),
      makeBackupIdea(818, "36氪", "同源第六条不该进前十五", 82),
    ];
    render(<TopicIdeaPool />);

    await screen.findByText(/AI 公司融资变化 原始标题/);
    const selectedSection = screen.getByLabelText("本期事件组合");
    const rows = within(selectedSection).getAllByRole("listitem");
    const selectedText = rows.map((row) => row.textContent || "");
    const backupSection = screen.getByRole("heading", { name: "备选池" })
      .parentElement as HTMLElement;
    const backupText = within(backupSection)
      .getAllByRole("article")
      .map((row) => row.textContent || "");
    const top15Text = [...selectedText, ...backupText].join(" ");

    expect(rows).toHaveLength(5);
    expect(backupText).toHaveLength(10);
    expect(selectedText[0]).toContain("AI 公司融资变化 原始标题");
    expect(selectedText[1]).toContain("新能源价格继续变化 原始标题");
    expect(selectedText[2]).toContain("商业平台收入调整 原始标题");
    expect(top15Text.match(/来源 36氪/g) || []).toHaveLength(3);
    expect(top15Text).not.toContain("同源第四条应被降重 原始标题");
    expect(top15Text).not.toContain("同源第六条不该进前十五 原始标题");
  });

  it("标题第一行保持原文，第二行显示总分、来源和高分维度标签", async () => {
    topicIdeas = [
      {
        ...makeBackupIdea(901, "财联社", "AI 搜索正在改变普通人获取信息的方式", 96),
        score_breakdown: {
          impact: 90,
          account_fit: 92,
          freshness_heat: 88,
          cross_source_resonance: 82,
          ordinary_relevance: 94,
          risk: 5,
          total: 91,
        },
      },
    ];
    render(<TopicIdeaPool />);

    await screen.findByText("AI 搜索正在改变普通人获取信息的方式 原始标题");
    const selectedSection = screen.getByLabelText("本期事件组合");

    expect(within(selectedSection).getByText("总分 96")).toBeInTheDocument();
    expect(within(selectedSection).getByText("来源 财联社")).toBeInTheDocument();
    expect(within(selectedSection).getByText("影响力高")).toBeInTheDocument();
    expect(within(selectedSection).getByText("账号匹配度高")).toBeInTheDocument();
    expect(within(selectedSection).getByText("时效高")).toBeInTheDocument();
    expect(within(selectedSection).getByText("跨源共振高")).toBeInTheDocument();
    expect(within(selectedSection).getByText("普通人相关度高")).toBeInTheDocument();
  });

  it("低匹配娱乐候选不硬删除，候选不足时仍补足 5 加 10 的推荐窗口", async () => {
    topicIdeas = [
      ...Array.from({ length: 8 }, (_, index) =>
        makeBackupIdea(
          1101 + index,
          `业务来源${index + 1}`,
          `业务趋势事件${index + 1}`,
          95 - index,
        ),
      ),
      ...Array.from({ length: 7 }, (_, index) =>
        makeBackupIdea(
          1201 + index,
          `娱乐来源${index + 1}`,
          `电影娱乐事件${index + 1}`,
          60 - index,
        ),
      ),
    ];
    render(<TopicIdeaPool />);
    await screen.findByText("业务趋势事件1 原始标题");

    const selectedSection = screen.getByLabelText("本期事件组合");
    const backupSection = screen.getByRole("heading", { name: "备选池" })
      .parentElement as HTMLElement;

    expect(within(selectedSection).getAllByRole("listitem")).toHaveLength(5);
    expect(within(backupSection).getAllByRole("article")).toHaveLength(10);
    expect(within(backupSection).getByText("电影娱乐事件7 原始标题")).toBeInTheDocument();
  });

  it("今日热点事件里的娱乐分类仍能按分类查看原始热点", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.selectOptions(screen.getByLabelText("分类"), "娱乐");

    expect(screen.getAllByText("某电影 6 月 26 日上映").length).toBeGreaterThan(0);
  });

  it("今日热点事件复用原始热点分类和筛选，不再只显示当天日期", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.selectOptions(screen.getByLabelText("分类"), "AI");
    expect(screen.getByText("AI搜索开始收费")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("分类"), "财经");
    expect(screen.getByText("房贷利率继续下调")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("分类"), "科技");
    expect(screen.getByText("国产芯片限制放松")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("分类"), "消费");
    expect(screen.getByText("外卖平台调整补贴")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("分类"), "国际");
    expect(screen.getByText("美国关税政策变化")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("分类"), "生活");
    expect(screen.getByText("医保报销规则调整")).toBeInTheDocument();
    expect(screen.getByText(/当前共 8 条/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /手动导入/ })).toBeInTheDocument();
  });

  it("详情里展示原始信息和判断依据，不显示旧的重复字段", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText("AI 工具进入企业采购流程");

    await user.click(
      screen.getByRole("button", { name: "查看本期详情 AI 工具进入企业采购流程" }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("原始热点")).toBeInTheDocument();
    expect(within(dialog).getByText("原文链接")).toBeInTheDocument();
    expect(within(dialog).getByText("发布时间")).toBeInTheDocument();
    expect(within(dialog).getByText("正文摘要")).toBeInTheDocument();
    expect(within(dialog).getByText("总分")).toBeInTheDocument();
    expect(within(dialog).getByText("深挖/合集")).toBeInTheDocument();
    expect(within(dialog).getByText("用大白话讲清楚发生了什么，以及普通人应该怎么看。")).toBeInTheDocument();
    expect(within(dialog).getByText("这是一段来自原文的正文摘要。")).toBeInTheDocument();
    expect(within(dialog).getByText(/普通人相关度/)).toBeInTheDocument();
    expect(within(dialog).getByText("合集")).toBeInTheDocument();
    expect(within(dialog).queryByText("人话标题")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("推荐池")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("推荐开头钩子")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("对应热点")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("来源热点")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "查看原文" })).toHaveAttribute(
      "href",
      "https://example.com/ai-buy",
    );
  });

  it("本期已选里的事件也能查看详情", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByRole("button", { name: "查看本期详情 消费趋势发生新变化" });

    const selectedSection = screen.getByLabelText("本期事件组合");
    await user.click(
      within(selectedSection).getByRole("button", {
        name: "查看本期详情 消费趋势发生新变化",
      }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("原始热点")).toBeInTheDocument();
    expect(within(dialog).getAllByText("消费趋势发生新变化").length).toBeGreaterThan(0);
  });

  it("备选事件仍可手动标记选入本期，但默认推荐不依赖手动状态", async () => {
    const user = userEvent.setup();
    topicIdeas = Array.from({ length: 7 }, (_, index) =>
      makeBackupIdea(1001 + index, `测试来源${index}`, `事件${index}`, 90 - index),
    );
    render(<TopicIdeaPool />);
    await screen.findByText("事件0 原始标题");

    await user.click(screen.getByRole("button", { name: "选入本期 事件5 原始标题" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/topic-ideas/1006/event-selection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected: true }),
      });
    });
    expect(await screen.findByText(/已选入本期/)).toBeInTheDocument();
  });

  it("备选池已选入按钮可以直接移出本期", async () => {
    const user = userEvent.setup();
    render(<TopicIdeaPool />);
    await screen.findByText(/车企价格战继续/);

    const selectedSection = screen.getByLabelText("本期事件组合");
    await user.click(
      within(selectedSection).getByRole("button", { name: "移出本期 车企价格战继续" }),
    );

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/topic-ideas/14/event-selection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected: false }),
      });
    });
  });

  it("能把已选事件组合成一期并生成 fake 字幕/口播稿", async () => {
    const user = userEvent.setup();
    const onOpenWorkspace = vi.fn();
    render(<TopicIdeaPool onOpenWorkspace={onOpenWorkspace} />);
    await screen.findAllByText(/消费趋势发生新变化/);

    expect(
      screen.getByText("测试模式：使用 fake provider，不调用真实 AI。"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "生成测试口播稿" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/topic-ideas/compose-collection",
        expect.objectContaining({ method: "POST" }),
      );
      expect(fetch).toHaveBeenCalledWith("/api/topic-ideas/99/generate-script", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
    });
    expect(await screen.findByText(/已把 5 件事件组合成一期视频/)).toBeInTheDocument();
    expect(onOpenWorkspace).toHaveBeenCalledWith(18);
  });

  it("本期已选 1 条也可以生成口播稿", async () => {
    const user = userEvent.setup();
    topicIdeas = [makeSelectedIdea(301, "AI工具变贵")];
    composeResponse = {
      ...collectionIdea,
      id: 3010,
      title: "今日1件事",
      source_hotspot_ids: [301],
      source_hotspot_titles: ["AI工具变贵 原始标题"],
      source_hotspot_urls: ["https://example.com/topic-301"],
    };
    render(<TopicIdeaPool />);
    await screen.findByText(/AI工具变贵 原始标题/);

    await user.click(screen.getByRole("button", { name: "生成测试口播稿" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/topic-ideas/compose-collection",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("本期已选 2 条也可以生成口播稿", async () => {
    const user = userEvent.setup();
    topicIdeas = [makeSelectedIdea(401, "AI搜索变天"), makeSelectedIdea(402, "车企继续降价")];
    composeResponse = {
      ...collectionIdea,
      id: 4010,
      title: "今日2件事",
      source_hotspot_ids: [401, 402],
      source_hotspot_titles: ["AI搜索变天 原始标题", "车企继续降价 原始标题"],
      source_hotspot_urls: ["https://example.com/topic-401", "https://example.com/topic-402"],
    };
    render(<TopicIdeaPool />);
    await screen.findByText(/AI搜索变天 原始标题/);

    await user.click(screen.getByRole("button", { name: "生成测试口播稿" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/topic-ideas/compose-collection",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("本期已选超过 5 条会先按配额显示 5 条，再生成测试口播稿", async () => {
    const user = userEvent.setup();
    topicIdeas = Array.from({ length: 7 }, (_, index) =>
      makeCurrentIdea(
        501 + index,
        `测试来源${index + 1}`,
        `事件${index + 1}`,
        90 - index,
      ),
    );
    composeResponse = {
      ...collectionIdea,
      id: 5010,
      title: "今日5件事",
      source_hotspot_ids: topicIdeas.slice(0, 5).map((idea) => idea.hot_item_id),
      source_hotspot_titles: topicIdeas.slice(0, 5).map((idea) => idea.hotspot_title),
      source_hotspot_urls: topicIdeas.slice(0, 5).flatMap((idea) => idea.source_hotspot_urls),
    };
    render(<TopicIdeaPool />);
    await screen.findByText(/事件1 原始标题/);
    expect(screen.queryByText("内容较多，建议精简，但仍可生成。")).not.toBeInTheDocument();
    expect(screen.getByLabelText("本期事件组合").querySelectorAll("li")).toHaveLength(5);

    await user.click(screen.getByRole("button", { name: "生成测试口播稿" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/topic-ideas/compose-collection",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});
