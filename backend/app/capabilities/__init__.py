"""Read-only capability truth for the Settings module."""

from app.capabilities.service import build_provider_statuses, build_skill_statuses

__all__ = ["build_provider_statuses", "build_skill_statuses"]
