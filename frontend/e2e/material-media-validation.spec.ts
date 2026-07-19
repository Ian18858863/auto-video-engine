import { expect, test } from "@playwright/test";

const onePixelPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZB7sAAAAASUVORK5CYII=",
  "base64",
);

const candidates = [
  {
    candidate_id: "cand-verified",
    source: "Pexels",
    preview_url:
      "/api/pipeline/8/material-tasks/shot-1/candidate-pool/cand-verified/preview",
    preview_media_type: "image/jpeg",
    url: "https://www.pexels.com/photo/example-101/",
    rights: "Pexels License",
    evidence_kind: "atmosphere",
    status: "candidate",
    reason: "主体与镜头任务一致",
    media_validation: {
      status: "verified",
      media_kind: "image",
      mime_type: "image/jpeg",
      sha256: "a".repeat(64),
      size_bytes: 631,
      width: 12,
      height: 8,
      duration_seconds: null,
      codec: "jpeg",
      container: "jpeg",
      decode_verified: true,
    },
    rights_evidence: {
      schema_version: 1,
      provider: "Pexels",
      provider_asset_id: "101",
      media_type: "photo",
      asset_page_url: "https://www.pexels.com/photo/example-101/",
      contributor: {
        provider_user_id: "7",
        name: "Ada",
        profile_url: "https://www.pexels.com/@ada/",
      },
      license_snapshot: {
        license_name: "Pexels License",
        license_url: "https://www.pexels.com/legal-pages/license/",
        terms_url: "https://help.pexels.com/hc/en-us/articles/900005880463-What-are-the-Terms-and-Conditions",
        observed_at: "2026-07-18T09:30:00+00:00",
        terms_version_or_last_updated: "",
      },
      attribution: {
        license_required: false,
        api_display_requirement: "在使用 API 结果的界面显著链接至 Pexels，并尽可能标注创作者。",
        recommended_credit: "Photo by Ada on Pexels",
      },
      rights_review: {
        status: "unreviewed",
        intended_use: "short-video",
        identifiable_people: "unknown",
        brands_or_logos: "unknown",
        property_or_architecture: "unknown",
        music_or_third_party_audio: "unknown",
        third_party_clearance: "unknown",
        restriction_notes: [],
        reviewed_by: "",
        reviewed_at: "",
      },
      source_record: {
        search_query: "office",
        record_checked_at: "2026-07-18T09:30:00+00:00",
      },
    },
  },
  ...["cand-legacy-1", "cand-legacy-2"].map((candidateId, index) => ({
    candidate_id: candidateId,
    source: index === 0 ? "Pixabay" : "官方页面",
    preview_url: `/api/pipeline/8/material-tasks/shot-1/candidate-pool/${candidateId}/preview`,
    preview_media_type: "image/jpeg",
    url: `https://example.test/${candidateId}`,
    rights: "commercial-use terms captured",
    evidence_kind: index === 0 ? "atmosphere" : "direct_evidence",
    status: "candidate",
    reason: "旧项目候选",
  })),
];

test("素材页可区分服务端已验证媒体和待复核旧候选", async ({ page }, testInfo) => {
  let rightsCleared = false;
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/pipeline/8/material-package" && request.method() === "GET") {
      await route.fulfill({
        json: {
          pipeline_id: 8,
          package_note: "媒体校验浏览器验收",
          script: { use_avatar: false },
          material_tasks: [
            {
              shot_id: "shot-1",
              time_range: "00:00-00:05",
              recommended_visual: "真实候选画面",
              jianying_keywords: ["AI"],
              asset_keywords: ["AI"],
              source_priority: ["Pexels视频", "Pixabay", "官方证据画面"],
              status: "待执行",
              material_path: "",
              candidate_pool: {
                shot_id: "shot-1",
                revision: rightsCleared ? 1 : 0,
                locked_candidate_id: null,
                candidates: candidates.map((candidate) =>
                  candidate.candidate_id === "cand-verified" && rightsCleared
                    ? {
                        ...candidate,
                        rights_evidence: {
                          ...candidate.rights_evidence,
                          rights_review: {
                            ...candidate.rights_evidence!.rights_review,
                            status: "cleared_for_intended_use",
                            intended_use: "当前项目短视频",
                            identifiable_people: "no",
                            brands_or_logos: "no",
                            property_or_architecture: "no",
                            music_or_third_party_audio: "no",
                            third_party_clearance: "not_required",
                            reviewed_by: "Ian",
                            reviewed_at: "2026-07-18T10:00:00+00:00",
                          },
                        },
                      }
                    : candidate,
                ),
              },
            },
          ],
          graphic_tasks: [],
          avatar_tasks: [],
        },
      });
      return;
    }
    if (path.endsWith("/cand-verified/rights-review") && request.method() === "POST") {
      rightsCleared = true;
      const reviewed = candidates.map((candidate) =>
        candidate.candidate_id === "cand-verified"
          ? {
              ...candidate,
              rights_evidence: {
                ...candidate.rights_evidence,
                rights_review: {
                  ...candidate.rights_evidence!.rights_review,
                  status: "cleared_for_intended_use",
                  intended_use: "当前项目短视频",
                  identifiable_people: "no",
                  brands_or_logos: "no",
                  property_or_architecture: "no",
                  music_or_third_party_audio: "no",
                  third_party_clearance: "not_required",
                  reviewed_by: "Ian",
                  reviewed_at: "2026-07-18T10:00:00+00:00",
                },
              },
            }
          : candidate,
      );
      await route.fulfill({
        json: {
          shot_id: "shot-1",
          revision: 1,
          locked_candidate_id: null,
          candidates: reviewed,
        },
      });
      return;
    }
    if (path.endsWith("/preview")) {
      await route.fulfill({
        status: 200,
        contentType: "image/png",
        body: onePixelPng,
      });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: `unhandled ${path}` } });
  });

  await page.goto("/materials/8");
  await expect(page.getByRole("heading", { name: "素材", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "素材候选池" })).toBeVisible();
  await expect(page.getByText("媒体已验证")).toBeVisible();
  await expect(page.getByText("JPEG · 12×8 · 631 B")).toBeVisible();
  await expect(page.getByText("旧候选，锁定前会复核")).toHaveCount(2);
  await page.getByText("核对本次使用权利").click();
  await page.getByLabel("可识别人像").selectOption("no");
  await page.getByLabel("品牌或 Logo").selectOption("no");
  await page.getByLabel("场地或建筑").selectOption("no");
  await page.getByLabel("音乐或第三方音频").selectOption("no");
  await page.getByLabel("第三方许可").selectOption("not_required");
  await page.getByRole("button", { name: "确认本次用途已清权" }).click();
  await expect(page.getByText("本次用途已清权", { exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("material-media-validation.png"),
    fullPage: true,
  });

  const layout = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    viewportWidth: window.innerWidth,
    overlay: Boolean(
      document.querySelector(
        "[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay",
      ),
    ),
  }));
  expect(layout.overlay).toBe(false);
  expect(layout.bodyWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
  if (testInfo.project.name === "mobile-375x812-edge") {
    const mobileTaskRowDisplay = await page
      .locator(".material-task-table tbody tr")
      .first()
      .evaluate((row) => getComputedStyle(row).display);
    expect(mobileTaskRowDisplay, "手机端素材任务应折叠成可读卡片").toBe("grid");
  }
  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
