"""Delivery operations shared by Orchestrator and Review entry points."""

from .deck_core import audit, finalize, sync_speech

__all__ = ["audit", "finalize", "sync_speech"]
