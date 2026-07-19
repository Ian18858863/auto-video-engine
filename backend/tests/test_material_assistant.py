from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.materials.assistant import (
    DownloadResult,
    JianyingAutomationClient,
    JianyingMaterialAssistant,
)
from app.materials.local_library import LocalMaterial, LocalMaterialLibrary
from app.materials.paths import (
    MaterialPathError,
    ProjectMaterialPaths,
    resolve_portable_material_root,
)
from app.materials.planner import ShotSegment, build_material_plan
from app.materials.scoring import CandidateMaterial, is_download_allowed, score_candidate
from app.materials.writeback import write_edit_manifest, write_script_md, write_shotlist_csv


class FakeJianyingClient:
    def __init__(self, download_file: Path | None = None, fail: bool = False):
        self.download_file = download_file
        self.fail = fail
        self.download_calls = 0

    def search(self, keywords: list[str]) -> list[CandidateMaterial]:
        return [
            CandidateMaterial(
                material_id="good-1",
                title="科技办公 数据分析",
                description="干净的科技办公场景，适合商业和 AI 工具主题",
                keywords=["AI", "科技", "办公", "商业"],
                source="剪映素材",
                license_type="member",
                quality_flags=["clean", "sharp", "stable"],
                orientation="portrait",
            ),
            CandidateMaterial(
                material_id="bad-1",
                title="娱乐搞笑土味模板",
                description="花哨低质娱乐素材",
                keywords=["娱乐"],
                source="剪映素材",
                license_type="free",
                quality_flags=["low_quality"],
            ),
        ]

    def download(self, candidate: CandidateMaterial, download_dir: Path) -> DownloadResult:
        self.download_calls += 1
        if self.fail:
            return DownloadResult(success=False, error_message="下载失败：登录状态异常")
        assert self.download_file is not None
        return DownloadResult(success=True, downloaded_path=self.download_file)


def test_chinese_project_directories_can_be_created(tmp_path):
    paths = ProjectMaterialPaths(root=tmp_path / "短视频工厂", project_name="测试项目")

    paths.ensure_directories()

    for dirname in ["项目", "exports", "归档", "备选素材"]:
        assert (paths.root / dirname).is_dir()
    assert paths.project_dir.exists()
    for dirname in [
        "Pexels素材",
        "官方录屏",
        "信息动效",
        "全部素材",
        "素材信息",
    ]:
        assert (paths.project_dir / dirname).is_dir()
    for dirname in [
        "原始素材",
        "官方证据画面",
        "PPT动态图文",
        "本地素材匹配",
        "数字人素材",
        "已选用素材",
        "已拒绝素材",
        "剪映素材",
    ]:
        assert not (paths.project_dir / dirname).exists()
    assert paths.local_library_dir == paths.backup_material_dir


def test_missing_d_drive_returns_clear_error_without_c_drive_fallback():
    paths = ProjectMaterialPaths(root=Path("D:/短视频工厂"), project_name="测试项目")

    with pytest.raises(MaterialPathError, match="D 盘不存在"):
        paths.ensure_directories(drive_exists=lambda drive: False)


def test_c_drive_material_root_is_rejected():
    paths = ProjectMaterialPaths(root=Path("C:/短视频工厂"), project_name="测试项目")

    with pytest.raises(MaterialPathError, match="不能配置到 C 盘"):
        paths.ensure_directories()


def test_portable_material_root_falls_back_when_windows_drive_is_unavailable(tmp_path):
    fallback = tmp_path / "exports" / "短视频工厂"

    resolved = resolve_portable_material_root(
        Path("D:/短视频工厂"),
        fallback_root=fallback,
        drive_exists=lambda drive: False,
    )

    assert resolved == fallback


def test_portable_material_root_never_keeps_c_drive_workspace_path(tmp_path):
    fallback = tmp_path / "exports" / "短视频工厂"

    resolved = resolve_portable_material_root(
        Path("C:/workspace/短视频工厂"),
        fallback_root=fallback,
    )

    assert resolved == fallback


def test_portable_material_root_keeps_configured_local_path(tmp_path):
    configured = tmp_path / "短视频工厂"

    resolved = resolve_portable_material_root(configured)

    assert resolved == configured


def test_script_shots_generate_material_tasks_keywords_graphic_tasks_and_avatar_fields():
    shots = [
        ShotSegment(
            start="00:00",
            end="00:05",
            voiceover="为什么这个 AI 工具突然进入企业采购？",
            recommended_visual="动态标题 + 关键词强调",
        ),
        ShotSegment(
            start="00:05",
            end="00:12",
            voiceover="背景是企业开始把 AI 工具放进真实预算。",
            recommended_visual="科技办公、商业会议、新闻感 B-roll",
        ),
        ShotSegment(
            start="00:12",
            end="00:25",
            voiceover="真正要看的是它怎么省钱、省人，还能把流程跑起来。",
            recommended_visual="商业逻辑流程图",
        ),
    ]

    plan = build_material_plan(shots, project_name="AI工具采购")

    assert len(plan.material_tasks) == 3
    assert [task.material_task_id for task in plan.material_tasks] == [
        "material-shot-01",
        "material-shot-02",
        "material-shot-03",
    ]
    assert any(task.needs_pexels for task in plan.material_tasks)
    assert any(task.needs_official_capture for task in plan.material_tasks)
    assert any(task.needs_info_motion for task in plan.material_tasks)
    assert any(task.needs_codex_graphic for task in plan.material_tasks)
    assert plan.material_tasks[0].asset_keywords
    assert "剪映素材" not in plan.material_tasks[0].source_priority
    assert plan.material_tasks[0].avatar_required is True
    assert plan.material_tasks[0].avatar_position == "右下角"
    assert plan.material_tasks[0].avatar_state in {"正在讲解", "强调重点", "总结收束", "轻微动作"}
    assert plan.graphic_tasks
    assert plan.graphic_tasks[0].save_path.endswith("信息动效")


def test_approved_shot_director_fields_survive_material_task_projection():
    plan = build_material_plan(
        [
            ShotSegment(
                shot_id="shot-evidence-01",
                beat_id="beat-01",
                beat_type="evidence",
                start="00:00",
                end="00:06",
                voiceover="官方数据说明这个趋势",
                recommended_visual="官方数据页面",
                visual_intent="投屏数据变化",
                viewer_takeaway="观众要看懂数据如何支持结论",
                evidence_kind="direct_evidence",
                source_refs=[{"url": "https://example.com/evidence", "kind": "source"}],
                acceptance_conditions=["必须显示来源"],
                forbidden_items=["不得用气氛素材冒充事实"],
                director_version="semantic-director-v1",
            )
        ],
        project_name="Shot Director test",
    )

    task = plan.material_tasks[0]
    assert task.shot_id == "shot-evidence-01"
    assert task.material_task_id == "material-shot-evidence-01"
    assert task.beat_id == "beat-01"
    assert task.beat_type == "evidence"
    assert task.evidence_kind == "direct_evidence"
    assert task.source_refs[0]["url"] == "https://example.com/evidence"
    assert task.acceptance_conditions == ["必须显示来源"]


def test_semantic_beats_with_one_source_shot_get_unique_material_task_ids():
    plan = build_material_plan(
        [
            ShotSegment(
                shot_id="shot-01",
                beat_id="beat-01",
                start="00:00",
                end="00:02",
                voiceover="先停下来",
            ),
            ShotSegment(
                shot_id="shot-01",
                beat_id="beat-02",
                start="00:02",
                end="00:05",
                voiceover="再理解原因",
            ),
        ],
        project_name="semantic beats",
    )

    assert [task.shot_id for task in plan.material_tasks] == [
        "shot-01",
        "shot-01-beat-02",
    ]
    assert len({task.material_task_id for task in plan.material_tasks}) == 2


def test_candidate_scoring_allows_only_high_quality_free_or_member_material():
    task = build_material_plan(
        [
            ShotSegment(
                start="00:05",
                end="00:12",
                voiceover="AI 工具进入企业采购，重点是商业逻辑和办公效率。",
                recommended_visual="科技办公、企业会议",
            )
        ],
        project_name="AI工具采购",
    ).material_tasks[0]
    candidate = CandidateMaterial(
        material_id="good-1",
        title="AI 科技办公 商业数据",
        description="清晰稳定的科技办公素材",
        keywords=["AI", "科技", "办公", "商业"],
        source="剪映素材",
        license_type="member",
        quality_flags=["clean", "sharp", "stable"],
        orientation="portrait",
    )

    score = score_candidate(task, candidate, existing_keywords=[])

    assert score.total_score >= 70
    assert score.portrait_fit_score == 100
    assert is_download_allowed(score) is True


def test_safe_landscape_scores_below_portrait_but_can_still_download():
    task = build_material_plan(
        [
            ShotSegment(
                start="00:05",
                end="00:12",
                voiceover="财经新闻背后的商业逻辑。",
                recommended_visual="财经办公背景",
            )
        ],
        project_name="财经新闻",
    ).material_tasks[0]
    portrait = CandidateMaterial(
        material_id="portrait",
        title="财经新闻 商业办公",
        description="清晰稳定的商业背景",
        keywords=["财经新闻", "商业", "办公"],
        source="剪映素材",
        license_type="free",
        quality_flags=["clean", "sharp", "stable"],
        orientation="portrait",
    )
    landscape = CandidateMaterial(
        material_id="landscape",
        title="财经新闻 商业办公",
        description="清晰稳定的商业背景",
        keywords=["财经新闻", "商业", "办公"],
        source="剪映素材",
        license_type="free",
        quality_flags=["clean", "sharp", "stable"],
        orientation="landscape",
        subject_centered=True,
        key_info_safe=True,
        background_suitable=True,
        overlay_safe=True,
    )

    portrait_score = score_candidate(task, portrait, existing_keywords=[])
    landscape_score = score_candidate(task, landscape, existing_keywords=[])

    assert portrait_score.portrait_fit_score == 100
    assert landscape_score.portrait_fit_score == 75
    assert portrait_score.total_score > landscape_score.total_score
    assert is_download_allowed(landscape_score) is True


def test_unsafe_landscape_is_rejected_even_when_other_scores_are_high():
    task = build_material_plan(
        [
            ShotSegment(
                start="00:05",
                end="00:12",
                voiceover="财经新闻背后的商业逻辑。",
                recommended_visual="财经办公背景",
            )
        ],
        project_name="财经新闻",
    ).material_tasks[0]
    candidate = CandidateMaterial(
        material_id="unsafe-landscape",
        title="财经新闻 商业办公",
        description="清晰稳定的商业背景",
        keywords=["财经新闻", "商业", "办公"],
        source="剪映素材",
        license_type="member",
        quality_flags=["clean", "sharp", "stable"],
        orientation="landscape",
        subject_centered=False,
        key_info_safe=False,
        background_suitable=False,
        overlay_safe=False,
    )

    score = score_candidate(task, candidate, existing_keywords=[])

    assert score.portrait_fit_score == 0
    assert is_download_allowed(score) is False
    assert "竖屏" in score.reason


def test_low_score_or_risk_material_will_not_download(tmp_path):
    source_file = tmp_path / "candidate.mp4"
    source_file.write_text("fake video", encoding="utf-8")
    client = FakeJianyingClient(download_file=source_file)
    paths = ProjectMaterialPaths(root=tmp_path / "短视频工厂", project_name="测试项目")
    assistant = JianyingMaterialAssistant(paths=paths, client=client)
    task = build_material_plan(
        [
            ShotSegment(
                start="00:00",
                end="00:05",
                voiceover="讲 AI 工具采购趋势",
                recommended_visual="科技办公",
            )
        ],
        project_name="测试项目",
    ).material_tasks[0]
    risky = CandidateMaterial(
        material_id="risk-1",
        title="赌博暴力猎奇素材",
        description="低俗高风险内容",
        keywords=["赌博", "暴力"],
        source="剪映素材",
        license_type="free",
        risk_flags=["赌博", "暴力"],
        quality_flags=["low_quality"],
    )

    result = assistant.process_task(task, candidates=[risky])

    assert result.status == "需要人工确认"
    assert result.selected_material_path is None
    assert client.download_calls == 0
    assert result.rejected_candidates[0].reason


def test_downloaded_material_is_moved_to_chinese_project_dir_and_renamed(tmp_path):
    source_file = tmp_path / "downloaded.mp4"
    source_file.write_text("fake video", encoding="utf-8")
    client = FakeJianyingClient(download_file=source_file)
    paths = ProjectMaterialPaths(root=tmp_path / "短视频工厂", project_name="测试项目")
    assistant = JianyingMaterialAssistant(paths=paths, client=client)
    task = build_material_plan(
        [
            ShotSegment(
                start="00:05",
                end="00:12",
                voiceover="AI 工具进入企业采购，重点是商业逻辑和办公效率。",
                recommended_visual="科技办公、商业会议",
            )
        ],
        project_name="测试项目",
    ).material_tasks[0]

    result = assistant.process_task(task)

    assert result.status == "已下载"
    assert result.selected_material_path is not None
    selected_path = Path(result.selected_material_path)
    assert selected_path.exists()
    assert "全部素材" in str(selected_path)
    assert selected_path.name.startswith("00-05_素材_")
    assert (paths.pexels_material_dir / selected_path.name).is_file()
    assert client.download_calls == 1
    assert task.evidence_kind == "atmosphere"
    assert task.source_url == ""
    assert task.is_locally_generated is False


def test_local_material_library_is_matched_before_jianying_download(tmp_path):
    local_file = tmp_path / "local.mp4"
    local_file.write_text("fake local video", encoding="utf-8")
    paths = ProjectMaterialPaths(root=tmp_path / "短视频工厂", project_name="测试项目")
    library = LocalMaterialLibrary(paths.local_library_dir)
    library.save(
        [
            LocalMaterial(
                name="AI科技办公本地素材",
                path=str(local_file),
                source="备选素材",
                keywords=["AI工具", "科技办公", "企业采购"],
                category="科技办公",
            )
        ]
    )
    client = FakeJianyingClient()
    assistant = JianyingMaterialAssistant(paths=paths, client=client, local_library=library)
    task = build_material_plan(
        [
            ShotSegment(
                start="00:05",
                end="00:12",
                voiceover="AI 工具进入企业采购，重点是科技办公和商业逻辑。",
                recommended_visual="科技办公",
            )
        ],
        project_name="测试项目",
    ).material_tasks[0]

    result = assistant.process_task(task)

    assert result.status == "本地已匹配"
    assert result.selected_material_path == str(local_file)
    assert client.download_calls == 0
    assert library.load()[0].usage_count == 1
    assert task.evidence_kind == "atmosphere"
    assert task.source_url == ""
    assert task.is_locally_generated is False


def test_download_failure_records_reason(tmp_path):
    client = FakeJianyingClient(fail=True)
    paths = ProjectMaterialPaths(root=tmp_path / "短视频工厂", project_name="测试项目")
    assistant = JianyingMaterialAssistant(paths=paths, client=client)
    task = build_material_plan(
        [
            ShotSegment(
                start="00:05",
                end="00:12",
                voiceover="AI 工具进入企业采购，重点是商业逻辑和办公效率。",
                recommended_visual="科技办公",
            )
        ],
        project_name="测试项目",
    ).material_tasks[0]

    result = assistant.process_task(task)

    assert result.status == "下载失败"
    assert "登录状态异常" in result.failure_reason


def test_writeback_outputs_edit_manifest_shotlist_and_script_md(tmp_path):
    paths = ProjectMaterialPaths(root=tmp_path / "短视频工厂", project_name="测试项目")
    paths.ensure_directories()
    task = build_material_plan(
        [
            ShotSegment(
                start="00:00",
                end="00:05",
                voiceover="为什么这个 AI 工具突然进入企业采购？",
                recommended_visual="动态标题 + 关键词强调",
            )
        ],
        project_name="测试项目",
    ).material_tasks[0]
    task.status = "已下载"
    task.material_path = str(paths.project_dir / "全部素材" / "00-05_素材_AI工具.mp4")
    task.source_type = "Pexels"
    task.evidence_kind = "atmosphere"
    task.source_url = "https://videos.pexels.com/example.mp4"
    task.is_locally_generated = False

    edit_manifest = write_edit_manifest(paths.project_dir, [task])
    shotlist = write_shotlist_csv(paths.project_dir, [task])
    script_md = write_script_md(paths.project_dir, [task])

    manifest_data = json.loads(edit_manifest.read_text(encoding="utf-8"))
    assert manifest_data["shots"][0]["material_path"] == task.material_path
    assert manifest_data["shots"][0]["avatar_required"] is True
    assert manifest_data["shots"][0]["avatar_position"] == "右下角"
    assert manifest_data["shots"][0]["evidence_kind"] == "atmosphere"
    assert manifest_data["shots"][0]["source_url"] == task.source_url
    assert manifest_data["shots"][0]["is_locally_generated"] is False
    shotlist_text = shotlist.read_text(encoding="utf-8")
    assert "素材状态" in shotlist_text
    assert "evidence_kind" in shotlist_text
    assert task.source_url in shotlist_text
    assert "官方证据" not in shotlist_text
    assert "是否需要数字人：是" in script_md.read_text(encoding="utf-8")
