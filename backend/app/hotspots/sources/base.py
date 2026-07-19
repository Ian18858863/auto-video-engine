from __future__ import annotations

from typing import Protocol

from app.hotspots.schemas import HotspotCandidate


class HotspotSource(Protocol):
    name: str

    def fetch(self) -> list[HotspotCandidate]:
        raise NotImplementedError
