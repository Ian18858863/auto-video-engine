import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CopywritingSamplePage from "./CopywritingSamplePage";
import type { CopywritingSampleItem, CopywritingSamplePayload } from "./types";

const baseSample: CopywritingSampleItem = {
  id: 1,
  title: "别只看热度的开头",
  sample_type: "开头钩子",
  sample_text: "这件事表面上是一个热点，但背后真正值得看的，是平台为什么开始抢这个入口。",
  scene: "热点解释",
  content_direction: "商业热点",
  structure_type: "热点背后逻辑型",
  why_reference: "开头先压住情绪，再把注意力拉到商业逻辑。",
  reusable_points: "表面事件 → 背后逻辑 → 入口变化",
  avoid_points: "不要照抄原句，不要夸张成全网疯传。",
  tags: ["开头", "商业逻辑"],
  enabled: true,
  is_key_reference: true,
  created_at: "2026-06-16T10:00:00+08:00",
  updated_at: "2026-06-16T10:00:00+08:00",
  notes: "适合热点解释。",
};

const titleSample: CopywritingSampleItem = {
  ...baseSample,
  id: 2,
  title: "AI 工具标题样本",
  sample_type: "标题",
  sample_text: "看懂 AI 工具采购，先看这 3 点",
  scene: "清单型内容",
  content_direction: "AI 工具",
  structure_type: "收藏清单型",
  tags: ["标题", "AI"],
  enabled: false,
  is_key_reference: false,
};

let samples: CopywritingSampleItem[];
let nextId = 3;
let clipboardWrite: ReturnType<typeof vi.fn>;

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

function listResponse(url: string) {
  const query = url.split("?")[1] || "";
  const params = new URLSearchParams(query);
  let items = [...samples];
  const sampleType = params.get("sample_type") || "";
  const scene = params.get("scene") || "";
  const tag = params.get("tag") || "";
  const keyword = params.get("keyword") || "";
  if (sampleType) items = items.filter((item) => item.sample_type === sampleType);
  if (scene) items = items.filter((item) => item.scene === scene);
  if (tag) items = items.filter((item) => item.tags.includes(tag));
  if (keyword) {
    items = items.filter(
      (item) => item.sample_text.includes(keyword) || item.title.includes(keyword),
    );
  }
  if (params.get("enabled_only") === "true") {
    items = items.filter((item) => item.enabled);
  }
  if (params.get("key_reference_only") === "true") {
    items = items.filter((item) => item.is_key_reference);
  }
  return {
    items,
    pagination: {
      page: 1,
      page_size: 20,
      total: items.length,
      total_pages: items.length ? 1 : 0,
    },
    filters: {
      sample_types: ["开头钩子", "标题"],
      scenes: ["热点解释", "清单型内容"],
      content_directions: ["商业热点", "AI 工具"],
      tags: ["开头", "商业逻辑", "标题", "AI"],
    },
    reference_rule: "只学习表达方式和结构，不直接复制原文。",
    style_priority: "账号风格库优先；样本库只做参考。",
    real_ai_called: false,
  };
}

function parsePayload(init?: RequestInit): CopywritingSamplePayload {
  return JSON.parse(String(init?.body || "{}")) as CopywritingSamplePayload;
}

describe("脚本引擎页面", () => {
  beforeEach(() => {
    samples = [baseSample, titleSample];
    nextId = 3;
    clipboardWrite = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { writeText: clipboardWrite },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
    if (url.startsWith("/api/script-engine?")) {
          return jsonResponse(listResponse(url));
        }
    if (url === "/api/script-engine" && init?.method === "POST") {
          const payload = parsePayload(init);
          const created: CopywritingSampleItem = {
            ...payload,
            id: nextId++,
            created_at: "2026-06-16T11:00:00+08:00",
            updated_at: "2026-06-16T11:00:00+08:00",
          };
          samples.unshift(created);
          return jsonResponse(created);
        }
        const detailMatch = url.match(/^\/api\/script-engine\/(\d+)$/);
        if (detailMatch) {
          const sampleId = Number(detailMatch[1]);
          if (init?.method === "PUT") {
            const payload = parsePayload(init);
            const index = samples.findIndex((item) => item.id === sampleId);
            samples[index] = {
              ...samples[index],
              ...payload,
              updated_at: "2026-06-16T12:00:00+08:00",
            };
            return jsonResponse(samples[index]);
          }
          if (init?.method === "DELETE") {
            samples = samples.filter((item) => item.id !== sampleId);
            return jsonResponse({ deleted: true, id: sampleId });
          }
          return jsonResponse(samples.find((item) => item.id === sampleId));
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("页面能打开，并显示样本和新增入口", async () => {
    render(<CopywritingSamplePage />);

    expect(screen.getByRole("heading", { name: "脚本引擎" })).toBeInTheDocument();
    expect(await screen.findByText("别只看热度的开头")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新增脚本样本" })).toBeInTheDocument();
  });

  it("没有样本时提供新增入口", async () => {
    samples = [];

    render(<CopywritingSamplePage />);

    expect(await screen.findByText("还没有脚本样本。")).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: "新增脚本样本" }).length,
    ).toBeGreaterThan(0);
  });

  it("能新增、编辑和删除文案样本", async () => {
    const user = userEvent.setup();
    render(<CopywritingSamplePage />);
    await screen.findByText("别只看热度的开头");

    await user.click(screen.getByRole("button", { name: "新增脚本样本" }));
    await user.type(screen.getByLabelText("样本标题"), "新的结尾引导样本");
    await user.selectOptions(screen.getByLabelText("样本类型"), "结尾引导");
    await user.type(screen.getByLabelText("样本文本 / 脚本结构"), "普通人不用急着行动，但可以记住这个判断标准。");
    await user.type(screen.getByLabelText("内容方向"), "趋势判断");
    await user.type(screen.getByLabelText("结构类型"), "趋势解释型");
    await user.type(screen.getByLabelText("为什么值得参考"), "收尾克制，不制造焦虑。");
    await user.type(screen.getByLabelText("可复用点"), "给判断标准，不喊口号。");
    await user.type(screen.getByLabelText("不适合复用的地方"), "不要照抄原句。");
    await user.type(screen.getByLabelText("标签"), "结尾,判断");
    await user.click(screen.getByRole("button", { name: "保存样本" }));

    expect(await screen.findByText("脚本引擎配置已保存。后续生成流程会读取启用样本和结构规则。")).toBeInTheDocument();
    expect(screen.getAllByText("新的结尾引导样本").length).toBeGreaterThan(0);

    await user.click(within(rowFor("新的结尾引导样本")).getByRole("button", { name: "编辑" }));
    await user.clear(screen.getByLabelText("样本标题"));
    await user.type(screen.getByLabelText("样本标题"), "更新后的结尾样本");
    await user.click(screen.getByRole("button", { name: "保存样本" }));

    expect(await screen.findAllByText("更新后的结尾样本")).not.toHaveLength(0);

    await user.click(within(rowFor("更新后的结尾样本")).getByRole("button", { name: "删除" }));

    await waitFor(() =>
      expect(screen.queryByText("更新后的结尾样本")).not.toBeInTheDocument(),
    );
  });

  it("能筛选、搜索、查看详情和复制样本文案", async () => {
    const user = userEvent.setup();
    render(<CopywritingSamplePage />);
    await screen.findByText("别只看热度的开头");

    await user.selectOptions(screen.getByLabelText("样本类型筛选"), "标题");
    expect(await screen.findByText("AI 工具标题样本")).toBeInTheDocument();
    expect(screen.queryByText("别只看热度的开头")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("样本类型筛选"), "");
    await user.type(screen.getByLabelText("搜索样本文本"), "平台为什么");
    expect(await screen.findByText("别只看热度的开头")).toBeInTheDocument();

    await user.click(within(rowFor("别只看热度的开头")).getByRole("button", { name: "查看详情" }));
    expect(await screen.findByText("表面事件 → 背后逻辑 → 入口变化")).toBeInTheDocument();

    await user.click(within(rowFor("别只看热度的开头")).getByRole("button", { name: "复制文案" }));
    expect(await screen.findByText("脚本样本已复制。注意：只学习表达方式和结构，不直接复制原文。")).toBeInTheDocument();
  });

  it("能启用停用、设为重点参考，并发送筛选参数", async () => {
    const user = userEvent.setup();
    render(<CopywritingSamplePage />);
    await screen.findByText("AI 工具标题样本");

    await user.click(within(rowFor("AI 工具标题样本")).getByRole("button", { name: "启用" }));
    await waitFor(() =>
      expect(within(rowFor("AI 工具标题样本")).getByText("已启用")).toBeInTheDocument(),
    );

    await user.click(within(rowFor("AI 工具标题样本")).getByRole("button", { name: "设为重点" }));
    await waitFor(() =>
      expect(within(rowFor("AI 工具标题样本")).getByText("重点参考")).toBeInTheDocument(),
    );

    await user.click(screen.getByLabelText("只看启用"));
    await user.click(screen.getByLabelText("只看重点参考"));

    await waitFor(() => {
      const called = vi
        .mocked(fetch)
        .mock.calls.some(([url]) =>
          String(url).includes("enabled_only=true") &&
          String(url).includes("key_reference_only=true"),
        );
      expect(called).toBe(true);
    });
  });
});

function rowFor(title: string): HTMLElement {
  const row = screen.getAllByText(title).find((element) => element.closest("article"));
  if (!row) {
    throw new Error(`找不到样本行：${title}`);
  }
  return row.closest("article") as HTMLElement;
}

