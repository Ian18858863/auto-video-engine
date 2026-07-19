import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AccountStylePage from "./AccountStylePage";

function jsonResponse(data: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

function accountStyle(overrides: Record<string, unknown> = {}) {
  return {
    account_positioning: "用大白话讲清楚新趋势、新环境、新风口、新工具背后的底层商业逻辑。",
    target_audiences: [
      { id: "students", label: "小白学生", enabled: true },
      { id: "workers", label: "普通上班族", enabled: true },
    ],
    content_directions: [
      { id: "ai_tools", label: "AI 工具", enabled: true },
      { id: "business", label: "商业热点", enabled: true },
    ],
    expression_styles: [
      { id: "plain", label: "大白话", enabled: true },
      { id: "calm", label: "冷静直接", enabled: true },
    ],
    banned_expressions: [
      { id: "shock", label: "震惊", category: "营销号", reason: "", enabled: true },
      {
        id: "everyone_knows",
        label: "大家都知道",
        category: "默认常识句式",
        reason: "降低小白心理门槛",
        enabled: true,
      },
      {
        id: "finance_terms",
        label: "未解释的专业财经术语",
        category: "理解门槛",
        reason: "必须配大白话解释",
        enabled: true,
      },
    ],
    structure_preferences: [
      {
        id: "beginner",
        name: "小白转译型",
        framework: "事件影响 → 通俗解释 → 背后逻辑 → 注意提醒",
        enabled: true,
      },
    ],
    opening_templates: [
      {
        id: "hotspot_opening",
        scene: "热点类开头",
        template: "这件事表面上是……，但背后真正值得看的其实是……",
        enabled: true,
      },
    ],
    ending_templates: [
      {
        id: "hotspot_ending",
        scene: "热点类结尾",
        template: "总结普通人该看懂的逻辑。",
        enabled: true,
      },
    ],
    validation_rules: {
      professional_terms_require_explanation: true,
      professional_terms: ["流动性收紧", "商业模式"],
      plain_language_examples: [
        {
          term: "流动性收紧",
          plain_explanation: "市场上的钱变少了，大家借钱和投资都更谨慎了。",
        },
      ],
      default_common_knowledge_phrases: ["大家都知道", "显而易见"],
      checks: ["是否用了禁用表达", "是否出现未解释的专业财经术语"],
    },
    last_saved_at: "2026-06-16T10:00:00+08:00",
    is_active: true,
    using_default: false,
    ...overrides,
  };
}

describe("账号风格库页面", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/account-style" && (!init || init.method === undefined)) {
          return jsonResponse(accountStyle());
        }
        if (url === "/api/account-style" && init?.method === "PUT") {
          const saved = JSON.parse(String(init.body));
          return jsonResponse({
            ...saved,
            last_saved_at: "2026-06-16T12:00:00+08:00",
            is_active: true,
            using_default: false,
          });
        }
        if (url === "/api/account-style/reset" && init?.method === "POST") {
          return jsonResponse(accountStyle({ using_default: true }));
        }
        throw new Error(`未处理的请求：${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("能打开并显示默认账号定位、保存时间和风格校验规则", async () => {
    render(<AccountStylePage />);

    expect(await screen.findByRole("heading", { name: "账号风格库" })).toBeInTheDocument();
    expect(screen.getByLabelText("账号定位")).toHaveValue(
      "用大白话讲清楚新趋势、新环境、新风口、新工具背后的底层商业逻辑。",
    );
    expect(screen.getByText("已生效")).toBeInTheDocument();
    expect(screen.getByDisplayValue("未解释的专业财经术语")).toBeInTheDocument();
    expect(screen.getAllByText("大家都知道").length).toBeGreaterThan(0);
    expect(screen.getByText("专业词必须解释")).toBeInTheDocument();
  });

  it("读取失败时不会一直停在加载中", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 502,
      json: async () => ({}),
      text: async () => "",
    } as Response);

    render(<AccountStylePage />);

    expect(
      await screen.findByText("账号风格库没有读取成功。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新读取" })).toBeInTheDocument();
    expect(screen.queryByText("正在读取账号风格库...")).not.toBeInTheDocument();
  });

  it("能编辑账号定位、目标观众、表达风格并保存", async () => {
    const user = userEvent.setup();
    render(<AccountStylePage />);
    await screen.findByRole("heading", { name: "账号风格库" });

    await user.clear(screen.getByLabelText("账号定位"));
    await user.type(screen.getByLabelText("账号定位"), "测试定位：讲清楚普通人能复用的商业判断。");
    await user.clear(screen.getByLabelText("目标观众条目1"));
    await user.type(screen.getByLabelText("目标观众条目1"), "刚入门的职场新人");
    await user.clear(screen.getByLabelText("表达风格条目1"));
    await user.type(screen.getByLabelText("表达风格条目1"), "像朋友解释");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/account-style",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    const putCall = vi.mocked(fetch).mock.calls.find(
      ([url, init]) => String(url) === "/api/account-style" && init?.method === "PUT",
    );
    const payload = JSON.parse(String(putCall?.[1]?.body));
    expect(payload.account_positioning).toBe("测试定位：讲清楚普通人能复用的商业判断。");
    expect(payload.target_audiences[0].label).toBe("刚入门的职场新人");
    expect(payload.expression_styles[0].label).toBe("像朋友解释");
    expect(await screen.findByText(/账号风格库已保存/)).toBeInTheDocument();
  });

  it("禁用表达和内容方向支持单条新增、删除和停用", async () => {
    const user = userEvent.setup();
    render(<AccountStylePage />);
    await screen.findByRole("heading", { name: "账号风格库" });

    await user.type(screen.getByLabelText("新增禁用表达"), "全网爆火");
    await user.click(screen.getByRole("button", { name: "添加禁用表达" }));
    await user.click(screen.getByLabelText("启用全网爆火"));
    await user.click(screen.getByRole("button", { name: "删除震惊" }));

    await user.type(screen.getByLabelText("新增内容方向"), "新职业变化");
    await user.click(screen.getByRole("button", { name: "添加内容方向" }));
    await user.click(screen.getByLabelText("启用新职业变化"));
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/account-style",
      expect.objectContaining({ method: "PUT" }),
    ));
    const putCall = [...vi.mocked(fetch).mock.calls].reverse().find(
      ([url, init]) => String(url) === "/api/account-style" && init?.method === "PUT",
    );
    const payload = JSON.parse(String(putCall?.[1]?.body));
    expect(payload.banned_expressions.some((item: { label: string }) => item.label === "震惊")).toBe(false);
    expect(
      payload.banned_expressions.find((item: { label: string }) => item.label === "全网爆火")
        .enabled,
    ).toBe(false);
    expect(
      payload.content_directions.find((item: { label: string }) => item.label === "新职业变化")
        .enabled,
    ).toBe(false);
  });

  it("能保存结构框架映射和开头结尾场景匹配规则", async () => {
    const user = userEvent.setup();
    render(<AccountStylePage />);
    await screen.findByRole("heading", { name: "账号风格库" });

    await user.clear(screen.getByLabelText("结构框架1"));
    await user.type(screen.getByLabelText("结构框架1"), "表面变化 → 人话解释 → 判断标准");
    await user.clear(screen.getByLabelText("开头模板1"));
    await user.type(screen.getByLabelText("开头模板1"), "这条新闻普通人不用全看。");
    await user.clear(screen.getByLabelText("结尾模板1"));
    await user.type(screen.getByLabelText("结尾模板1"), "以后再看类似变化，可以用这个标准判断。");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/account-style",
      expect.objectContaining({ method: "PUT" }),
    ));
    const putCall = [...vi.mocked(fetch).mock.calls].reverse().find(
      ([url, init]) => String(url) === "/api/account-style" && init?.method === "PUT",
    );
    const payload = JSON.parse(String(putCall?.[1]?.body));
    expect(payload.structure_preferences[0].framework).toBe(
      "表面变化 → 人话解释 → 判断标准",
    );
    expect(payload.opening_templates[0].template).toBe("这条新闻普通人不用全看。");
    expect(payload.ending_templates[0].template).toBe(
      "以后再看类似变化，可以用这个标准判断。",
    );
  });

  it("恢复默认风格库需要二次确认", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AccountStylePage />);
    await screen.findByRole("heading", { name: "账号风格库" });

    await user.click(screen.getByRole("button", { name: "恢复默认风格库" }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "确认恢复默认账号风格库？当前自定义内容会被覆盖。",
    );
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/account-style/reset",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
