# ARCHITECTURE

## 当前架构

- 前端：React + Vite + TypeScript。
- 后端：Python FastAPI。
- 数据库：SQLite。
- 文件导出：本地目录保存素材库、拉片报告、SOP。
- AI：统一 provider 接口，默认 fake provider，后续支持豆包、DeepSeek、Kimi、通义、OpenAI 和 OpenAI-compatible API。
- 项目导出目录：默认使用 `D:/短视频工厂/exports/`；项目素材根目录默认使用 `D:/短视频工厂/`。后续成片、热点 CSV 和素材包导出都不再默认写入 C 盘项目目录。
- `D:/短视频工厂` 日常目录约定：根目录只保留 `项目`、`exports`、`归档`、`备选素材` 和 `目录说明.txt`。`项目` 保存当前内容项目，`exports` 保存成片/热点 CSV/导出文件，`归档` 保存旧测试和旧结构文件，`备选素材` 保存 Ian 手动导入的全局备用素材。
- 每个项目目录只保留 5 个素材相关入口：`Pexels素材`、`官方录屏`、`信息动效`、`全部素材`、`素材信息`。项目实际使用的素材都会归入 `全部素材`；来源素材会同时归入对应来源目录。旧目录名 `原始素材`、`官方证据画面`、`PPT动态图文`、`本地素材匹配`、`数字人素材`、`已选用素材`、`已拒绝素材`、`剪映素材` 不再由新代码创建。
- 自动素材目录：默认设计为 `D:/素材/`，包含 `备用`、`自动下载/Pexels/视频`、`自动下载/Pexels/照片`、`自动下载/官方页面`、`自动生成` 和 `素材使用记录.json`；Pexels Key 只从 `自动下载/Pexels/API Key` 读取。
- 外部素材源抽象：`backend/app/materials/source_clients.py` 提供统一候选、下载、SHA256 指纹去重和中文失败原因结构。当前已接入 Pixabay 与 Coverr；Pixabay Key 读取本地 `D:/素材/自动下载/Pixabay/API Key` 或 `Pixabay API*` 文件，Coverr 暂按公开接口轻量接入。
- 素材任务执行：`POST /api/pipeline/{pipeline_id}/material-tasks/run-next` 每次只推进一条素材任务；官方页面录屏和 Pexels 是主通道，单通道最多等待 3 分钟，失败后记录中文原因；信息动效只作为兜底。`官方页面录屏中`、`官方页面录屏失败`、`Pexels下载中`、`Pexels下载失败`、`信息动效生成中` 都属于可恢复中间状态，页面或请求中断后再次执行会优先恢复最前面的未完成任务，不会跳到后面的任务。`POST /api/pipeline/{pipeline_id}/material-tasks/{shot_id}/skip` 可手动跳过单条任务并推进队列。
- 新的 route-free 异步提交边界由 `material_execution_submission_store.py`、`material_execution_submission_owner_lease.py` 和 `material_execution_submission_coordinator.py` 组成：每个 D: sidecar 只有一个专用线程持有的稳定 Windows named mutex；SQLite owner epoch 与 CAS revision 共同 fencing 旧 callback。它目前只是一条内部安全 seam，不接入 `main.py`，不替换旧 `/run-next`，也不把普通 `exclusive_reacquire` 说成已观察到进程死亡。
  - 新 owner-lane 的具体组装由 `material_operation_factories.py` 和
    `material_execution_runtime.py` 负责：factory 从服务端 fresh
    `material_tasks` 映射严格 typed operation plan；素材步骤顶层保留服务端
    `project_name`/`project_key`，旧快照使用 `pipeline-{id}` 兜底。runtime 以 D: 主库的
    deterministic sibling sidecar 为提交存储，按 owner lease -> registry ->
    fresh command factory -> coordinator 顺序启动，按 lane cleanup -> lease
    顺序关闭。runner 仍按 lane 懒创建；这是一条 route-free seam，尚未写入
    `main.py` lifespan 或替换旧 `/run-next`。
- 配音字幕：`backend/app/audio/` 提供本地 TTS 和 SRT 生成。V1 默认 `windows_sapi`，通过 `POST /api/pipeline/{pipeline_id}/voiceover/generate` 为脚本生成 `口播音频.wav` 和 `字幕.srt`；火山/豆包 TTS provider 位置已保留。
- 视频参数：`backend/app/video/settings.py` 集中管理输出规格，默认 9:16、1080×1920、30fps、H.264/AAC，并支持后续传入横屏或更高分辨率配置。
- 成片导出：`app.materials.end_to_end_video` 同时支持开发 smoke test 和真实项目导出。成片页默认调用 `POST /api/pipeline/{pipeline_id}/finished-video/export`，从 `production_pipelines`、`script_assets` 和素材任务输出摘要读取真实项目脚本、字幕和素材路径，再串联官方页面慢速滚动画面、Pexels/Pixabay/Coverr 候选素材、信息动效、素材使用记录、本地 TTS、SRT 字幕和 ffmpeg 合成，输出 1080×1920 H.264/AAC MP4 到 `D:/短视频工厂/归档/成片测试/`。没有选中项目时，前端不会再偷偷调用固定测试脚本。
- 脚本工厂：`backend/app/pipeline/script_work_order_service.py` 负责在 `素材信息/脚本工厂/<job_id>/` 生成 `script_factory_prompt.md`、`script_factory_context.json`、`script_factory_result_template.json`，从同目录读取 `script_factory_result.json`，写回 `ScriptAsset`，重置下游素材/导出步骤，并用事实来源和本地内容质检决定是否允许继续到素材任务；全程不调用外部 AI API，且禁止写入 C 盘。
- 素材链路 smoke：`python -m app.materials.run_material_pipeline_smoke` 会真实跑官方页面录屏、Pexels、Pixabay、Coverr、备用素材扫描、测试成片和多版本模拟，结果写入 `D:/短视频工厂/归档/素材链路测试_*`，并在命令行输出中文成功/失败/跳过原因。
- 热点更新：选题页“更新今日事件（含浏览器热点）”会先跑浏览器公开热点采集，再跑 RSS/公开源抓取和本地候选事件生成；设置页“采集浏览器热点”只跑浏览器来源。
- 选题筛选标准：`backend/app/topic_ideas/filter_settings.py` 使用 `app_config.topic_filter_settings` 保存全局配置；`GET/PUT /api/topic-filter-settings` 负责读取和保存。五维基础权重必须合计 100，分类/关键词额外加分独立叠加；保存后会触发候选池重新计算。
- 选题评分：`event_scoring.event_score()` 读取当前筛选标准，返回 `base_total`、`bonus_total` 和 `bonus_reasons`；`local_generator` 和 `pool_service` 用同一份配置生成本期已选 5 条、备选 10 条以及同来源上限。

## 当前目录

```text
backend/
  app/
    main.py
    account_style/
      service.py
    ai/
      factory.py
      service.py
      providers/
    content_quality/
      schemas.py
      service.py
    copywriting_samples/
      schemas.py
      service.py
    script_engine/
      schemas.py
      service.py
    core/
      config.py
      database.py
    hotspots/
      api_schemas.py
      list_service.py
      pipeline.py
      source_policy.py
      sources/
        factory.py
    materials/
      asset_paths.py
      asset_registry.py
      source_clients.py
      download_pexels_sample.py
      end_to_end_video.py
      official_capture.py
      pexels.py
      run_end_to_end_video_test.py
      assistant.py
      planner.py
      scoring.py
      writeback.py
      adapters/
        factory.py
        desktop.py
        fake.py
        browser.py
        analysis.py
        config.py
        errors.py
      calibrate_jianying.py
      controlled_download.py
      test_jianying_download.py
    audio/
      tts_service.py
      subtitle_service.py
    models/
      entities.py
    pipeline/
      audio_service.py
      exporter.py
      list_service.py
      material_execution_service.py
      material_execution_application.py
      material_execution_api_context.py
      material_execution_runtime.py
      material_execution_submission_store.py
      material_execution_submission_owner_lease.py
      material_execution_submission_coordinator.py
      material_operation_factories.py
      material_operation_protocol.py
      material_operation_runner.py
      material_operation_worker.py
      production_material_execution.py
      material_package_service.py
      models.py
      rewrite_service.py
      schemas.py
      script_service.py
      script_work_order_service.py
      service.py
      v1_acceptance_service.py
      workspace_service.py
    video/
      settings.py
    prompts/
      *.md
    publishing/
      schemas.py
      service.py
    reviews/
      service.py
      weekly_service.py
    seed/
      service.py
    today/
      service.py
    topic_ideas/
      pool_service.py
      service.py
  tests/
    test_health.py
frontend/
  src/
    api.ts
    AccountStylePage.tsx
    AccountStylePage.test.tsx
    App.tsx
    App.test.tsx
    AppNavigation.test.tsx
    ContentQualityPage.tsx
    ContentQualityPage.test.tsx
    CopywritingSamplePage.tsx
    CopywritingSamplePage.test.tsx
    HotspotPool.tsx
    HotspotPool.test.tsx
    main.tsx
    ProjectListPage.tsx
    ScriptWorkspace.tsx
    ScriptWorkspace.test.tsx
    TopicIdeaPool.tsx
    TopicIdeaPool.test.tsx
    VideoTrackingPage.tsx
    VideoTrackingPage.test.tsx
    DataReviewPage.tsx
    DataReviewPage.test.tsx
    EndToEndVideoResultPanel.tsx
    MaterialPackagePage.tsx
    WeeklyReviewPage.tsx
    TodayTaskPage.tsx
    TodayTaskPage.test.tsx
    styles.css
    types.ts
  index.html
  package.json
  vite.config.ts
docs/
  superpowers/
    specs/
    plans/
```

## 模块关系

- 热点抓取模块负责抓取、清洗、去重、评分，不调用 AI。
- 热点查询模块通过 `GET /api/hotspots` 和 `GET /api/hotspots/{hotspot_id}` 只读热点表，支持分类、来源、未过滤筛选和三项分数排序；是否已有项目通过 `production_pipelines.hotspot_id` 判断。
- 热点来源策略集中在 `backend/app/hotspots/source_policy.py`：统一识别浏览器热点来源、公开热榜来源和内部测试来源。正式热点列表与候选推荐会过滤 `手动跑通测试`、`manual_v1_test`、sample/test 和乱码来源。
- 热点列表在未选择具体来源时使用来源均衡排序：先按当前排序结果让每个来源至少露出 1 条，再按来源上限和分数继续补位，避免 36氪/IT之家等旧高分来源把浏览器热点挤出前页。
- 热点查询模块对外只暴露固定八类：社会、财经、科技、消费、AI、国际、生活、娱乐。后端会把原始分类和标题/摘要/标签归并到这八类，并返回 `category_counts` 与 `source_counts`，供今日热点事件筛选使用。
- 热点入口模块通过 `POST /api/hotspots/import` 支持手动导入，通过 `POST /api/hotspots/fetch` 按 `config/source_config.yaml` 构建 RSS、Google News、Hacker News、Reddit 来源。单个来源失败只写入 `source_runs`，不会中断其他来源。
- 浏览器公开热点采集模块位于 `backend/app/hotspots/browser_collection.py`，通过 `browser_hotspot_sources` 配置百度、微博、抖音、小红书和财联社公开页面。采集使用独立 Chrome profile 保存登录态，结果追加导出 CSV，并写入热点池；遇到验证码、登录过期或安全验证时只记录失败，不绕过限制。
- `POST /api/automation/browser-hotspots/run` 和根目录 `更新公开热点CSV.cmd` 可手动触发浏览器低频采集。当前不做高频循环，不自动下载视频。
- 手动导入热点的链接为可选；无链接热点会在数据库内部使用不可见占位 URL 避免唯一索引冲突，对外接口、页面和候选选题来源仍显示为空链接。
- AI provider 模块负责统一调用 fake provider 或 OpenAI-compatible provider，并把所有 AI 输出写入 `ai_generations` 缓存表。真实 provider 请求失败、超时、鉴权失败或额度不足时会回退 fake provider，并继续缓存 fake 输出。
- 热点选题模块负责把自动抓取后的热点精炼成候选选题；页面只保留一个“选题”入口，不再拆成“候选事件 / 原始热点”两个标签。
- 今日热点事件是热点池的简化展示：读取同一套热点数据、分类、来源和排序筛选，展示当前结果总数与手动导入入口；不再用独立日期过滤导致分类数量失真。
- 今日热点事件的分类下拉固定显示八类并带全量计数；来源下拉只显示当前筛选范围内真实有数据的来源。切换分类、来源或排序只重新请求热点列表，不重新请求候选事件，避免页面跳回备选池。
- 今日候选推荐按五维 100 分评分：影响力度 25%、账号匹配度 20%、时效热度 20%、跨源共振 15%、普通人相关度 20%。同标题跨来源热点会合并为一个候选事件，多个来源提升跨源共振分。
- 本地候选事件生成会读取全部真实、未过滤、未归档热点后再做事件分组；选择候选组时优先给浏览器热点来源和公开热榜来源进入窗口的机会，然后再按综合分补足，避免 SQL 阶段只截取高分 RSS 源导致新来源永远不可见。选题页手动更新默认生成最多 70 条本地候选，给最终来源降重留足候选池。
- 选题页从全量候选中按综合分排序后做来源降重：前 15 条同一来源最多 3 条，前 5 条显示为“本期已选”，第 6-15 条显示为“备选池”。候选生成阶段会尽量保留热点候选，来源上限只作用于最终推荐窗口。旧 `is_selected` 只作为人工标记，不再决定默认本期推荐。
- 默认选题池只展示正式推荐事件：必须是 `local_rule` 生成、真实来源、非 sample/test/manual、非乱码、非低价值页面、36 小时内仍有效、且综合分达到 40 分以上的通用事件候选。fake provider 生成的角度、合集成稿、旧测试数据和低分/低价值垃圾内容不会进入默认本期已选/备选池；正常情况下前端按来源降重后显示本期 5 条 + 备选 10 条，只有真实可验证合格候选不足 15 条时才少显示。
- 低价值选题过滤集中在 `hotspots/eligibility.py`：单纯股票涨跌、无上下文短标签/账号名、低价值生活/备孕/育儿内容、用户反馈/协议/隐私/消息页、B站用户主页和文档下载页都会在候选生成与读取时被二次拦截。
- 本地候选选题的可见标题必须保持原始热点标题，不做压缩、改写或钩子化；解释角度、钩子和判断放在详情字段，不污染标题。
- 候选选题支持两种内容形态：单选题深挖型和多热点合集型。多热点合集型从 3-5 个重要热点组合成一期视频，按重要性排序，讲清发生了什么、为什么重要、普通人怎么看。
- `GET /api/topic-ideas` 查询选题和脚本/项目状态，支持按热点筛选；查询与选择操作不调用 AI。
- `POST /api/topic-ideas/{topic_idea_id}/select` 在同一热点内切换当前选题；`POST /api/topic-ideas/{topic_idea_id}/generate-script` 复用内容生产编排并强制使用 fake provider。该接口会至少推进到“创建中文项目目录”，因此生成脚本后已经具备素材任务和项目文件夹，素材真实获取仍由素材页的“开始/继续素材获取”触发。
- 内容生产编排模块从 `hotspot_id` 手动启动，按生成候选选题、选择候选选题、生成脚本素材、生成素材任务、生成 Codex 图文任务、生成数字人字段、创建中文项目目录和三个文件导出共 10 步执行。
- `POST /api/pipeline/manual-v1-test` 是 V1 可用性验收入口：从手动热点开始自动跑候选选题、脚本、内容质检、最多 2 次本地规则返修、素材任务、素材库和导出文件。当前强制 fake provider，不调用真实 AI。
- 编排步骤写入 `production_pipelines` 和 `pipeline_steps`。每步使用未开始、进行中、已完成、失败、需要人工确认五种状态，并记录开始时间、完成时间、错误和输出摘要。
- `GET /api/pipeline/{pipeline_id}` 只读取并整理状态；`POST /api/pipeline/{pipeline_id}/continue` 从未完成或失败步骤继续。已完成步骤默认跳过，失败上游恢复后会使相关下游任务和导出失效并重新生成。
- `GET /api/pipeline` 通过独立 `list_service.py` 只读查询全部项目，汇总当前步骤、完成进度、失败原因和下一步建议，并支持状态、失败、人工确认筛选及分页；该查询不会调用 provider 或执行生成任务。
- 项目列表前端使用 React + Vite + TypeScript，开发环境通过 Vite `/api` 代理访问本地 FastAPI。页面只读取项目列表和详情；只有用户点击“继续执行”时才调用已有 continue 接口。
- 热点池前端使用相同的本地 `/api` 代理读取热点列表和详情。只有用户点击“创建内容项目”时才调用主流程接口；页面会校验返回 provider 必须为 `fake`。
- 内容工作台通过 `GET /api/pipeline/{pipeline_id}/workspace` 聚合热点、选题、`script_assets`、口播稿、分镜、字幕、标题、封面文案、发布文案、素材任务、图文任务、数字人字段和导出文件。
- 内容工作台支持 `POST /api/pipeline/{pipeline_id}/rewrite` 局部重写开头、结尾、标题、封面、字幕和发布文案。当前重写只用本地规则/fake 路径，读取账号风格库和启用文案样本库，不调用真实 AI。
- 内容工作台支持 Codex 脚本工厂流程：`POST /api/pipeline/{pipeline_id}/script-work-order/build` 生成 D 盘任务包文件，`POST /api/pipeline/{pipeline_id}/script-work-order/dispatch` 返回真实派发状态或手动新建 Codex 对话方案，`PUT /api/pipeline/{pipeline_id}/script-work-order/import` 从结果文件导入结构化脚本，`POST /api/pipeline/{pipeline_id}/script-work-order/accept` 运行事实来源和本地质检并决定是否继续到素材任务。该流程返回状态、路径、真实 AI 调用标记、失败原因和下一步建议。
- 素材库模块通过 `GET /api/pipeline/{pipeline_id}/material-package` 查看项目目录、导出文件、官方页面录屏任务、Pexels 任务、信息动效任务、图文任务和数字人字段；通过 `POST /api/pipeline/{pipeline_id}/material-tasks/run-next` 每次只执行一个素材任务，成功写回素材路径，失败写回中文原因并推进下一条等待任务；通过 `POST /api/pipeline/{pipeline_id}/exports/rebuild` 重新导出口播稿文件、分镜表、剪辑清单、时间轴文件、粗剪方案、字幕文件和剪辑说明；通过素材任务状态接口标记人工确认、完成或拒绝。
- 自动剪辑 / 成片导出模块通过 `GET /api/pipeline/{pipeline_id}/editing-export` 读取脚本、分镜、素材任务、字幕和数字人字段，输出时间轴结构、SRT 字幕、剪辑说明和一个智能推荐模板；V1 不默认生成 3-5 个成片版本，不操作剪映桌面版，不生成真实数字人。
- 端到端成片模块通过 `POST /api/pipeline/{pipeline_id}/finished-video/export` 导出正式项目成片；`POST /api/editing-export/end-to-end-test` 仅保留为开发 smoke test。素材匹配优先关键词，再匹配分类、来源，并增加官方证据画面、Pexels/Pixabay/Coverr 和信息动效的语义兜底；每段记录匹配原因，缺素材时明确标记“缺素材”。
- 成片管理模块通过 `GET /api/editing-export/finished-videos` 读取已生成的最终 MP4，通过 `GET /api/editing-export/finished-videos/{video_id}/video` 提供浏览器预览，通过打开文件夹、导出和删除接口管理单条成片。删除只清理该成片生成目录，不碰 `D:/素材` 原始素材库。
- 成片页只展示最终成片管理入口；测试脚本、字幕、口播音频和素材匹配明细不在成片页展示，后续保留在剪辑页/项目详情中编辑。
- 发布数据追踪模块使用 `video_posts` 保存视频最新数据，使用 `video_post_snapshots` 保存每次手动更新的历史快照。
- `GET /api/video-posts` 支持平台、项目、状态筛选和发布时间、播放量、收藏数、转发数排序；新增、编辑和快照接口都只写本地数据库，不调用 AI 或平台接口。
- 发布追踪前端每 30 秒重新读取本地数据。用户手动录入新数据后立即刷新，并可查看同一视频的历史变化。
- 当前阶段和状态由代码规则计算：按发布时间显示 30 分钟到 7 天阶段，根据增长、收藏率、转发率和低播放情况标记观察中、正常、加速、掉速、有潜力或需要复盘。
- 数据复盘模块通过 `GET /api/reviews` 和 `GET /api/reviews/{video_post_id}` 只读发布记录与快照，不新增复盘表，也不调用 AI。
- 复盘服务即时计算收藏率、转发率、涨粉率、播放增速和增长趋势，再按固定规则给出表现一般、有潜力、值得继续做、需要调整、不建议继续五类判断。
- 数据复盘前端支持平台、项目、发布时间和判断筛选，支持按收藏率、转发率、涨粉率和播放增速排序，并在详情中展示判断依据、下一步建议和快照变化。
- 复盘周报通过 `GET /api/reviews/weekly/report` 基于最近 7 天手动录入数据生成本地规则周报，展示本周发布、高收藏、高转发、长尾、掉速、继续方向、暂停方向和下周建议。
- 今日任务模块通过 `GET /api/today/tasks` 聚合决策项目、进行项目和成片状态；没有候选事件时也会生成“待确认选题”提醒，引导先进入选题页更新/选择事件。
- `POST /api/today/tasks/{task_id}/complete` 只把当前任务日的提醒标记为已处理；任务日按北京时间 09:15 切换，第二天早上刷新后提醒会重新出现。
- 今日任务前端作为默认首页，回答今天推荐做什么、哪些项目在跑、哪些失败或需要确认、哪些已经可剪辑/导出。合集型选题可从今日任务进入热点选题页。
- 今日任务排序和提醒全部使用本地规则，不调用 AI、不触发项目生成、不做平台抓取。数字人口播和图文/PPT 动画不拆开推荐，统一展示为推荐推进的视频项目。
- 账号风格库使用 `app_config` 的固定 key `account_style_library` 保存，是后续选题、脚本、标题、封面、字幕、发布文案、内容质检和复盘建议的全局唯一风格配置源。配置不存在时后端会自动创建默认风格库；保存后下一次读取立即生效，不需要重启服务。
- 账号风格库后端提供 `GET /api/account-style`、`PUT /api/account-style`、`POST /api/account-style/reset`。默认配置包含账号定位、目标观众、内容方向、表达风格、禁用表达、结构-框架映射、开头/结尾场景模板和风格校验规则。
- 候选选题生成和脚本素材生成已把账号风格库写入 AI 输入内容；当前仍使用 fake provider 测试，不调用真实 AI。
- 账号风格库前端支持中文查看和编辑，列表类配置支持单条新增、删除、修改和启用/停用；恢复默认会二次确认，并显示最后保存时间和配置是否已生效。
- 内容质检模块通过 `POST /api/content-quality/manual/check` 和 `POST /api/content-quality/pipeline/{pipeline_id}/check` 做本地规则检查，不调用 provider。质检读取账号风格库最新配置，检查禁用表达、默认常识句式、未解释专业术语、营销号风险、投资建议风险、判断力、收藏价值和账号定位匹配。
- 内容质检记录保存到 `content_quality_checks`，包含来源、关联 pipeline/script、原文、质检结果和创建时间。脚本工作台可直接进入内容质检，内容质检页面也支持手动粘贴文本。
- 脚本引擎使用 `backend/app/script_engine/` 作为新服务入口，前端调用 `/api/script-engine`；数据库暂时复用旧 `copywriting_samples` 表作为兼容层，旧 `/api/copywriting-samples` 接口仍保留，避免迁移期破坏历史数据。
- 脚本引擎保存样本标题、类型、正文、适用场景、内容方向、结构类型、参考理由、可复用点、不适合复用点、标签、启用状态和重点参考状态；它只学习结构、节奏、转译方式和表达方法，不直接复制原文。
- 候选选题生成已自动读取账号风格库；脚本素材生成和局部重写已自动读取账号风格库与启用状态的脚本引擎样本。账号风格库仍是最高优先级，脚本引擎样本只作为表达方式参考；为兼容旧缓存，生成输入中短期同时保留 `copywriting_samples` 字段。
- 内容质检已自动读取账号风格库最新配置，包括禁用表达、结构规则、开头偏好和结尾偏好；保存账号风格库后无需重启，后续质检立即使用最新配置。
- 示例数据模块通过 `POST /api/demo-data/seed` 和 `DELETE /api/demo-data/clear` 手动创建/清空明确带有“示例：”前缀的本地演示数据。示例项目仍通过 fake provider 生成，不调用真实 AI。
- Prompt 模板保存在 `backend/app/prompts/`，以中文 Markdown 维护候选选题、脚本、标题、封面、字幕、发布文案、内容质检、素材任务和图文任务 prompt。当前只做准备，不自动调用真实 AI。
- 全局导航位于 `App.tsx`，左侧固定保留 7 个入口：今日任务、热点选题、内容工作台、素材库、自动剪辑、发布复盘、设置。当前页面通过高亮和 `aria-current="page"` 标记。
- 页面间常用入口已打通：今日任务可进入热点选题、内容工作台、素材库、自动剪辑和发布复盘；热点选题已有脚本可直接打开内容工作台；项目列表保留在今日任务内，并可通过目标 pipeline id 自动打开项目详情。
- 素材任务、Codex 图文任务和数字人字段以 JSON 保存到步骤 `output_summary`，因此暂停后可以恢复，不需要重新调用已完成的选题或脚本 AI 步骤。
- 当前编排层显式强制使用 fake provider。即使环境变量配置了真实 provider，也不会调用真实 AI；官方录屏、Pexels、信息动效、Codex skill 和数字人生成同样先只输出任务。
- 编排导出层在 `素材信息` 目录生成 `script.md`、`shotlist.csv` 和 `edit_manifest.json`，供后续素材下载和自动剪辑继续使用。
- 当前脚本工作台只负责查看已有脚本与任务；后续再增加局部重写、内容质检和重新导出等手动操作。
- 自动素材地基包含 `AutoMaterialPaths`、`MaterialUsageRegistry`、`PexelsAssetClient`、`OfficialCapturePlan` 和 `OfficialPageCaptureService`：路径模块负责 D 盘中文目录和 Key 文件路径，使用记录负责两天内禁用重复素材和按使用次数降权，Pexels 模块负责视频/照片目标路径与去重，官方页面模块负责默认滚动录屏计划、正文选择器、广告避让、Playwright 慢速滚动截帧和 MP4 合成。
- 从热点创建项目时，素材任务会继承热点原文 URL、来源平台和原始标题。素材执行器优先用 `official_source_url` 做官方页面真实录屏；失败时记录中文原因，再进入 Pexels 和信息动效兜底。
- 本地信息动效引擎已部署到 `D:/素材/自动生成/_engine/remotion-hyperframes/`，使用 Remotion 渲染 9:16 信息动效 MP4。`HyperFrames by HeyGen` 是 Codex 插件，不是可本地安装的公开包；本项目用 `tools/remotion-hyperframes-engine/` 维护一套自建 HyperFrames 风格模板，当前包含数据流背景、玻璃拟态信息卡、关键词动效和信号扩散环。输出目录为 `D:/素材/自动生成/信息动效/`。
- Pexels 真实下载最小入口位于 `backend/app/materials/download_pexels_sample.py`，可通过 `python -m app.materials.download_pexels_sample --keyword "technology office"` 手动运行。它读取 `D:/素材/自动下载/Pexels/API Key` 或兼容读取 `API Key.env`，分别下载 1 条视频和 1 张图片到 `D:/素材/自动下载/Pexels/视频`、`D:/素材/自动下载/Pexels/照片`，并输出中文 JSON 状态。
- Pexels 下载写正式文件前会先写 `.part` 临时文件，计算 SHA256 文件指纹，并把指纹写入 `D:/素材/素材指纹.json`。发现重复指纹时删除临时文件并返回中文拒绝原因，避免仅靠文件名去重。
- 剪映素材助手和 desktop adapter 已退出默认素材主线，当前只作为 legacy 代码隔离保留，避免历史项目或旧测试读取时立即断裂。
- 新素材任务记录官方证据画面、Pexels、信息动效和备用素材的轻量状态，供后续页面展示进度和失败位置。生成任务时只将第一条设为 `进行中`，后续任务为 `等待`；素材页会显示 `等待 / 进行中 / 已完成 / 失败`，不再把所有未匹配任务假显示为进行中。
- 当前素材执行器优先尝试 Pexels，Pexels 不可用且镜头需要信息动效时会生成基础信息动效；Pexels 输出到 `D:/素材/自动下载/Pexels/视频` 或 `D:/素材/自动下载/Pexels/照片`，信息动效输出到 `D:/素材/自动生成`。
- 校准工具从剪映窗口截图收集本机坐标，写入 `config/jianying_ui_profile.yaml`。配置包含搜索框、搜索结果、候选卡片、每个候选下载按钮、分辨率和缩放比例；下载状态区域仅为可选辅助。
- 受控下载 runner 将桌面操作、人工候选确认和评分下载分开；命令层固定最多处理 3 个候选、下载 1 个素材，并把结果写入项目的 `素材信息` 目录。
- 下载是否完成由本地目录监听器判断：发现新文件后连续检查文件大小，稳定后才返回成功。剪映 UI 提示不参与核心判定。
- 候选评分包含竖屏适配分。竖屏素材优先；横屏素材只有在主体居中、裁切不丢信息、适合作背景且不影响字幕和右下角数字人时才允许下载。
- 根目录 `app` 只作为命令模块入口，实际业务代码仍位于 `backend/app`。
- 数字人辅助出镜后续作为素材库的一部分，默认右下角辅助层；V1 先在脚本、分镜和 `edit_manifest.json` 里预留字段。
- 账号风格库和文案风格引擎为脚本工作台提供风格规则。
- 内容质检负责发布前检查脚本质量和平台风险。
- 拉片工作台沉淀对标视频结构、节奏和 SOP。
- 对标博主库按当前决策暂不做，避免硬套外部风格导致账号表达跑偏。
- 账号数据中台已具备发布记录、快照历史和基础状态判断；后续再把这些数据接入复盘和选题反哺。
- 项目管家暂不做页面；如需检查项目状态，后续按需整理 TASKS、CHANGELOG、提交记录和日志。今日任务已按本地规则聚合完成。
