import { describe, expect, it, vi } from "vitest";

import { getTodayTasks } from "./api";

describe("API 错误提示", () => {
  it("后端代理 502 时给出能看懂的中文原因", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 502,
          text: async () => "Bad Gateway",
          json: async () => {
            throw new Error("not json");
          },
        } as unknown as Response),
      ),
    );

    await expect(getTodayTasks()).rejects.toThrow(
      "后端服务连接失败（502）。请先双击“启动全部.cmd”，或确认后端 127.0.0.1:8000 已启动。",
    );

    vi.unstubAllGlobals();
  });
});
