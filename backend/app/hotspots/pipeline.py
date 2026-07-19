from __future__ import annotations

from sqlalchemy.orm import Session

from app.hotspots.repository import save_hotspot
from app.hotspots.schemas import HotspotCandidate, PipelineRunResult
from app.hotspots.sources.base import HotspotSource
from app.hotspots.sources.errors import HotspotSourceError
from app.models import SourceRun
from app.models.entities import utc_now


class HotspotPipeline:
    def __init__(self, session: Session):
        self.session = session

    def import_manual(self, candidate: HotspotCandidate):
        return save_hotspot(self.session, candidate)

    def run_sources(self, sources: list[HotspotSource]) -> PipelineRunResult:
        result = PipelineRunResult()
        for source in sources:
            source_run = SourceRun(source_name=source.name, started_at=utc_now())
            try:
                candidates = source.fetch()
                result.fetched_count += len(candidates)
                for candidate in candidates:
                    item, created = save_hotspot(self.session, candidate)
                    if created:
                        result.saved_count += 1
                        if item.is_filtered:
                            result.filtered_count += 1
                    else:
                        result.duplicate_count += 1
                source_run.success = True
                source_run.status = "成功"
                source_run.items_fetched = len(candidates)
            except HotspotSourceError as exc:
                source_run.success = False
                source_run.is_rate_limited = exc.is_rate_limited
                source_run.status = "限流" if exc.is_rate_limited else "失败"
                source_run.error_message = str(exc)
                result.failed_sources.append(source.name)
                if exc.is_rate_limited:
                    result.rate_limited_sources.append(source.name)
            except Exception as exc:
                source_run.success = False
                source_run.status = "失败"
                source_run.error_message = str(exc)
                result.failed_sources.append(source.name)
            finally:
                source_run.ended_at = utc_now()
                self.session.add(source_run)
                self.session.commit()
        return result
