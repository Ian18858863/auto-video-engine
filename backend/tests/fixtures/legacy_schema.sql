-- Frozen from verified backup:
-- D:/短视频工厂/归档/系统备份/phase0-prechange-20260711-163851/
-- trend_video_factory.pre-phase0.sqlite
-- SHA-256: A349DD2EBD58FC5F65EEA97E38AAFA05C26F209B86A8CF64EE98DB624DE0BEF1
-- Do not regenerate from current ORM metadata.
PRAGMA foreign_keys=OFF;
BEGIN;

CREATE TABLE ai_generations (
    id INTEGER NOT NULL,
    generation_type VARCHAR(80) NOT NULL,
    input_hash VARCHAR(128) NOT NULL,
    input_content JSON NOT NULL,
    output_content JSON NOT NULL,
    provider VARCHAR(80) NOT NULL,
    model VARCHAR(120) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_ai_generations_generation_type
    ON ai_generations (generation_type);
CREATE UNIQUE INDEX ix_ai_generations_input_hash
    ON ai_generations (input_hash);

CREATE TABLE app_config (
    id INTEGER NOT NULL,
    "key" VARCHAR(120) NOT NULL,
    value JSON NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_app_config_key ON app_config ("key");

CREATE TABLE copywriting_samples (
    id INTEGER NOT NULL,
    title VARCHAR(300) NOT NULL,
    sample_type VARCHAR(80) NOT NULL,
    sample_text TEXT NOT NULL,
    scene VARCHAR(100) NOT NULL,
    content_direction VARCHAR(120) NOT NULL,
    structure_type VARCHAR(120) NOT NULL,
    why_reference TEXT NOT NULL,
    reusable_points TEXT NOT NULL,
    avoid_points TEXT NOT NULL,
    tags JSON NOT NULL,
    enabled BOOLEAN NOT NULL,
    is_key_reference BOOLEAN NOT NULL,
    notes TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_copywriting_samples_content_direction
    ON copywriting_samples (content_direction);
CREATE INDEX ix_copywriting_samples_enabled ON copywriting_samples (enabled);
CREATE INDEX ix_copywriting_samples_is_key_reference
    ON copywriting_samples (is_key_reference);
CREATE INDEX ix_copywriting_samples_sample_type
    ON copywriting_samples (sample_type);
CREATE INDEX ix_copywriting_samples_scene ON copywriting_samples (scene);
CREATE INDEX ix_copywriting_samples_title ON copywriting_samples (title);

CREATE TABLE hot_items (
    id INTEGER NOT NULL,
    source_platform VARCHAR(100) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    title VARCHAR(300) NOT NULL,
    url VARCHAR(800) NOT NULL,
    published_at DATETIME,
    fetched_at DATETIME NOT NULL,
    summary VARCHAR(300) NOT NULL,
    metrics JSON NOT NULL,
    tags JSON NOT NULL,
    category VARCHAR(50) NOT NULL,
    is_filtered BOOLEAN NOT NULL,
    filter_reason VARCHAR(200) NOT NULL,
    hot_score FLOAT NOT NULL,
    topic_score FLOAT NOT NULL,
    save_score FLOAT NOT NULL,
    is_favorite BOOLEAN NOT NULL,
    has_topic_ideas BOOLEAN NOT NULL,
    has_script BOOLEAN NOT NULL,
    data_origin VARCHAR(30) DEFAULT 'real',
    origin_note VARCHAR(200) DEFAULT '',
    risk_score FLOAT DEFAULT 0,
    freshness_score FLOAT DEFAULT 0,
    duplicate_reason VARCHAR(300) DEFAULT '',
    duplicate_group_key VARCHAR(160) DEFAULT '',
    lifecycle_status VARCHAR(40) DEFAULT 'active',
    archived_at DATETIME,
    archive_reason VARCHAR(200) DEFAULT '',
    PRIMARY KEY (id)
);
CREATE INDEX ix_hot_items_category ON hot_items (category);
CREATE INDEX ix_hot_items_data_origin ON hot_items (data_origin);
CREATE INDEX ix_hot_items_duplicate_group_key
    ON hot_items (duplicate_group_key);
CREATE INDEX ix_hot_items_has_script ON hot_items (has_script);
CREATE INDEX ix_hot_items_has_topic_ideas ON hot_items (has_topic_ideas);
CREATE INDEX ix_hot_items_is_favorite ON hot_items (is_favorite);
CREATE INDEX ix_hot_items_is_filtered ON hot_items (is_filtered);
CREATE INDEX ix_hot_items_lifecycle_status ON hot_items (lifecycle_status);
CREATE INDEX ix_hot_items_source_platform ON hot_items (source_platform);
CREATE INDEX ix_hot_items_source_type ON hot_items (source_type);
CREATE INDEX ix_hot_items_title ON hot_items (title);
CREATE UNIQUE INDEX ix_hot_items_url ON hot_items (url);

CREATE TABLE source_runs (
    id INTEGER NOT NULL,
    source_name VARCHAR(120) NOT NULL,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    success BOOLEAN NOT NULL,
    items_fetched INTEGER NOT NULL,
    error_message TEXT NOT NULL,
    status VARCHAR(20) DEFAULT '失败',
    is_rate_limited BOOLEAN DEFAULT 0,
    PRIMARY KEY (id)
);
CREATE INDEX ix_source_runs_is_rate_limited
    ON source_runs (is_rate_limited);
CREATE INDEX ix_source_runs_source_name ON source_runs (source_name);
CREATE INDEX ix_source_runs_success ON source_runs (success);

CREATE TABLE topic_ideas (
    id INTEGER NOT NULL,
    hot_item_id INTEGER NOT NULL,
    title VARCHAR(300) NOT NULL,
    core_view VARCHAR(500) NOT NULL,
    recommended_format VARCHAR(80) NOT NULL,
    why_watch VARCHAR(500) NOT NULL,
    why_save VARCHAR(500) NOT NULL,
    comment_trigger VARCHAR(500) NOT NULL,
    risk_note VARCHAR(500) NOT NULL,
    recommended_duration VARCHAR(80) NOT NULL,
    hook VARCHAR(500) NOT NULL,
    is_selected BOOLEAN NOT NULL,
    video_type VARCHAR(80) DEFAULT 'single_deep_dive',
    generation_source VARCHAR(80) DEFAULT 'fake_provider',
    recommendation_score FLOAT DEFAULT 0,
    recommendation_bucket VARCHAR(40) DEFAULT 'backup',
    source_hotspot_ids JSON DEFAULT '[]',
    source_hotspot_titles JSON DEFAULT '[]',
    is_example BOOLEAN DEFAULT 0,
    PRIMARY KEY (id),
    FOREIGN KEY(hot_item_id) REFERENCES hot_items (id)
);
CREATE INDEX ix_topic_ideas_hot_item_id ON topic_ideas (hot_item_id);
CREATE INDEX ix_topic_ideas_is_selected ON topic_ideas (is_selected);
CREATE INDEX ix_topic_ideas_recommendation_bucket
    ON topic_ideas (recommendation_bucket);
CREATE INDEX ix_topic_ideas_recommendation_score
    ON topic_ideas (recommendation_score);

CREATE TABLE script_assets (
    id INTEGER NOT NULL,
    topic_idea_id INTEGER NOT NULL,
    voiceover TEXT NOT NULL,
    storyboard JSON NOT NULL,
    subtitles JSON NOT NULL,
    animation_prompts JSON NOT NULL,
    title_options JSON NOT NULL,
    cover_copy JSON NOT NULL,
    publish_copy JSON NOT NULL,
    edit_checklist JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(topic_idea_id) REFERENCES topic_ideas (id)
);
CREATE INDEX ix_script_assets_topic_idea_id
    ON script_assets (topic_idea_id);

CREATE TABLE production_pipelines (
    id INTEGER NOT NULL,
    hotspot_id INTEGER NOT NULL,
    topic_idea_id INTEGER,
    script_asset_id INTEGER,
    project_name VARCHAR(200) NOT NULL,
    project_dir VARCHAR(800) NOT NULL,
    status VARCHAR(40) NOT NULL,
    provider VARCHAR(80) NOT NULL,
    model VARCHAR(120) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(hotspot_id) REFERENCES hot_items (id),
    FOREIGN KEY(topic_idea_id) REFERENCES topic_ideas (id),
    FOREIGN KEY(script_asset_id) REFERENCES script_assets (id)
);
CREATE INDEX ix_production_pipelines_hotspot_id
    ON production_pipelines (hotspot_id);
CREATE INDEX ix_production_pipelines_script_asset_id
    ON production_pipelines (script_asset_id);
CREATE INDEX ix_production_pipelines_status
    ON production_pipelines (status);
CREATE INDEX ix_production_pipelines_topic_idea_id
    ON production_pipelines (topic_idea_id);

CREATE TABLE content_quality_checks (
    id INTEGER NOT NULL,
    pipeline_id INTEGER,
    script_asset_id INTEGER,
    source_type VARCHAR(80) NOT NULL,
    source_title VARCHAR(300) NOT NULL,
    checked_text TEXT NOT NULL,
    result JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(pipeline_id) REFERENCES production_pipelines (id),
    FOREIGN KEY(script_asset_id) REFERENCES script_assets (id)
);
CREATE INDEX ix_content_quality_checks_pipeline_id
    ON content_quality_checks (pipeline_id);
CREATE INDEX ix_content_quality_checks_script_asset_id
    ON content_quality_checks (script_asset_id);
CREATE INDEX ix_content_quality_checks_source_type
    ON content_quality_checks (source_type);

CREATE TABLE pipeline_steps (
    id INTEGER NOT NULL,
    pipeline_id INTEGER NOT NULL,
    name VARCHAR(80) NOT NULL,
    step_order INTEGER NOT NULL,
    status VARCHAR(40) NOT NULL,
    error TEXT NOT NULL,
    output_summary JSON NOT NULL,
    started_at DATETIME,
    ended_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(pipeline_id) REFERENCES production_pipelines (id)
);
CREATE INDEX ix_pipeline_steps_pipeline_id ON pipeline_steps (pipeline_id);
CREATE INDEX ix_pipeline_steps_status ON pipeline_steps (status);

CREATE TABLE video_posts (
    id INTEGER NOT NULL,
    platform VARCHAR(80) NOT NULL,
    video_title VARCHAR(300) NOT NULL,
    published_at DATETIME,
    video_url VARCHAR(800) NOT NULL,
    topic_type VARCHAR(80) NOT NULL,
    video_format VARCHAR(80) NOT NULL,
    hook_type VARCHAR(80) NOT NULL,
    duration_seconds INTEGER NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER NOT NULL,
    comments INTEGER NOT NULL,
    favorites INTEGER NOT NULL,
    shares INTEGER NOT NULL,
    followers_gained INTEGER NOT NULL,
    completion_rate FLOAT NOT NULL,
    average_watch_seconds FLOAT NOT NULL,
    profile_visits INTEGER NOT NULL,
    current_view_velocity FLOAT NOT NULL,
    notes TEXT NOT NULL,
    pipeline_id INTEGER,
    updated_at DATETIME,
    PRIMARY KEY (id)
);
CREATE INDEX ix_video_posts_pipeline_id ON video_posts (pipeline_id);
CREATE INDEX ix_video_posts_platform ON video_posts (platform);

CREATE TABLE video_post_snapshots (
    id INTEGER NOT NULL,
    video_post_id INTEGER NOT NULL,
    captured_at DATETIME NOT NULL,
    elapsed_minutes FLOAT NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER NOT NULL,
    comments INTEGER NOT NULL,
    favorites INTEGER NOT NULL,
    shares INTEGER NOT NULL,
    followers_gained INTEGER NOT NULL,
    completion_rate FLOAT NOT NULL,
    average_watch_seconds FLOAT NOT NULL,
    notes TEXT NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(video_post_id) REFERENCES video_posts (id)
);
CREATE INDEX ix_video_post_snapshots_captured_at
    ON video_post_snapshots (captured_at);
CREATE INDEX ix_video_post_snapshots_video_post_id
    ON video_post_snapshots (video_post_id);

INSERT INTO hot_items (
    id, source_platform, source_type, title, url, published_at, fetched_at,
    summary, metrics, tags, category, is_filtered, filter_reason, hot_score,
    topic_score, save_score, is_favorite, has_topic_ideas, has_script,
    data_origin, origin_note, risk_score, freshness_score, duplicate_reason,
    duplicate_group_key, lifecycle_status, archived_at, archive_reason
) VALUES (
    101, 'fixture-platform', 'manual', '冻结热点',
    'https://example.invalid/hot/101', '2026-07-01 01:02:03',
    '2026-07-01 02:03:04', '保留摘要',
    '{"views":123,"nested":{"ok":true}}', '["迁移","JSON"]', '技术',
    0, '', 98.5, 87.25, 76.75, 1, 1, 1, NULL, 'legacy-origin',
    1.5, 2.5, '', 'group-101', 'active', NULL, ''
);
INSERT INTO topic_ideas (
    id, hot_item_id, title, core_view, recommended_format, why_watch,
    why_save, comment_trigger, risk_note, recommended_duration, hook,
    is_selected, video_type, generation_source, recommendation_score,
    recommendation_bucket, source_hotspot_ids, source_hotspot_titles,
    is_example
) VALUES (
    201, 101, '冻结选题', '核心观点', '口播', '值得看', '值得收藏',
    '评论触发', '风险提示', '60 秒', '开头钩子', 1,
    'single_deep_dive', NULL, 88.75, 'recommended', '[101]',
    '["冻结热点"]', 0
);
INSERT INTO script_assets (
    id, topic_idea_id, voiceover, storyboard, subtitles, animation_prompts,
    title_options, cover_copy, publish_copy, edit_checklist, created_at
) VALUES (
    301, 201, '冻结口播稿',
    '[{"shot":1,"text":"镜头一"}]',
    '[{"start":0,"text":"字幕"}]',
    '[{"prompt":"动画"}]', '[{"title":"标题"}]',
    '[{"copy":"封面"}]', '{"body":"发布文案"}',
    '[{"checked":true}]', '2026-07-02 03:04:05'
);
INSERT INTO production_pipelines (
    id, hotspot_id, topic_idea_id, script_asset_id, project_name, project_dir,
    status, provider, model, created_at, updated_at
) VALUES (
    401, 101, 201, 301, '冻结项目', 'D:/fixtures/project-401', '进行中',
    'fake', 'fake-low-cost', '2026-07-02 04:05:06', '2026-07-02 05:06:07'
);
INSERT INTO pipeline_steps (
    id, pipeline_id, name, step_order, status, error, output_summary,
    started_at, ended_at
) VALUES (
    501, 401, '冻结步骤', 1, '已完成', '', '{"artifact":"ok"}',
    '2026-07-02 06:07:08', '2026-07-02 07:08:09'
);
INSERT INTO content_quality_checks (
    id, pipeline_id, script_asset_id, source_type, source_title, checked_text,
    result, created_at
) VALUES (
    601, 401, 301, 'pipeline', '冻结检查', '检查文本',
    '{"passed":true,"score":91.5}', '2026-07-03 01:02:03'
);
INSERT INTO ai_generations (
    id, generation_type, input_hash, input_content, output_content, provider,
    model, created_at
) VALUES (
    701, 'script', 'fixture-input-hash', '{"topic_id":201}',
    '{"voiceover":"冻结口播稿"}', 'fixture-provider', 'fixture-model',
    '2026-07-03 02:03:04'
);
INSERT INTO copywriting_samples (
    id, title, sample_type, sample_text, scene, content_direction,
    structure_type, why_reference, reusable_points, avoid_points, tags,
    enabled, is_key_reference, notes, created_at, updated_at
) VALUES (
    801, '冻结样本', 'hook', '样本文本', '开头', 'AI', '反差', '可复用',
    '节奏', '夸张', '["标签一","标签二"]', 1, 1, '保留备注',
    '2026-07-03 03:04:05', '2026-07-03 04:05:06'
);
INSERT INTO video_posts (
    id, platform, video_title, published_at, video_url, topic_type,
    video_format, hook_type, duration_seconds, views, likes, comments,
    favorites, shares, followers_gained, completion_rate,
    average_watch_seconds, profile_visits, current_view_velocity, notes,
    pipeline_id, updated_at
) VALUES (
    901, 'fixture-video', '冻结发布记录', '2026-07-04 01:02:03',
    'https://example.invalid/video/901', '热点', '口播', '反差', 61,
    1001, 202, 33, 44, 55, 66, 0.77, 42.5, 88, 9.25,
    '发布备注', 401, NULL
);
INSERT INTO video_post_snapshots (
    id, video_post_id, captured_at, elapsed_minutes, views, likes, comments,
    favorites, shares, followers_gained, completion_rate,
    average_watch_seconds, notes
) VALUES (
    1001, 901, '2026-07-04 02:03:04', 60.5, 1001, 202, 33, 44, 55, 66,
    0.77, 42.5, '子表快照必须保留'
);
INSERT INTO source_runs (
    id, source_name, started_at, ended_at, success, items_fetched,
    error_message, status, is_rate_limited
) VALUES (
    1101, 'fixture-source', '2026-07-05 01:02:03', '2026-07-05 01:03:04',
    1, 12, '', NULL, 0
);
INSERT INTO app_config (id, "key", value, updated_at) VALUES (
    1201, 'fixture-config', '{"enabled":true,"items":[1,2,3]}',
    '2026-07-05 02:03:04'
);

COMMIT;
