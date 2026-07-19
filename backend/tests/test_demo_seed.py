from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models import CopywritingSample, HotItem, ProductionPipeline, VideoPost
from app.seed.service import clear_demo_data, seed_demo_data


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_seed_demo_data_creates_marked_examples_and_can_clear(tmp_path):
    with make_session() as session:
        result = seed_demo_data(session, material_root=tmp_path / "短视频工厂")

        assert result["created"] is True
        assert session.query(HotItem).filter(HotItem.title.like("示例：%")).count() == 3
        assert session.query(ProductionPipeline).filter(
            ProductionPipeline.project_name.like("示例：%")
        ).count() == 1
        assert session.query(VideoPost).filter(VideoPost.video_title.like("示例：%")).count() == 1
        assert session.query(CopywritingSample).filter(
            CopywritingSample.title.like("示例：%")
        ).count() == 1

        duplicate = seed_demo_data(session, material_root=tmp_path / "短视频工厂")
        cleared = clear_demo_data(session)

        assert duplicate["created"] is False
        assert cleared["deleted"] is True
        assert session.query(HotItem).filter(HotItem.title.like("示例：%")).count() == 0


def test_seed_demo_data_honors_the_independent_project_root(tmp_path):
    material_root = tmp_path / "material-root"
    project_root = tmp_path / "project-root"
    project_root.mkdir(parents=True)

    with make_session() as session:
        result = seed_demo_data(
            session,
            material_root=material_root,
            project_root=project_root,
        )

        pipeline = session.get(ProductionPipeline, result["pipeline_id"])
        assert pipeline is not None
        assert pipeline.status == "需要人工确认"
        assert Path(pipeline.project_dir).resolve().parent == project_root.resolve()
        assert not (material_root / "项目").exists()
