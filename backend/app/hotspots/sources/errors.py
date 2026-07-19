from __future__ import annotations


class HotspotSourceError(RuntimeError):
    def __init__(self, message: str, *, is_rate_limited: bool = False):
        super().__init__(message)
        self.is_rate_limited = is_rate_limited


class RateLimitedSourceError(HotspotSourceError):
    def __init__(self, message: str):
        super().__init__(message, is_rate_limited=True)
