#!/usr/bin/env python3
"""Shared, read-only generation artifact provenance checks."""

from __future__ import annotations

from typing import Any


ARTIFACT_INTERVENTION_FIELDS = (
    "normalization_performed",
    "render_performed",
    "canvas_fit_performed",
)


def artifact_intervention_performed(value: Any) -> bool:
    """Return whether a sidecar admits any benchmark-side artifact mutation."""
    return isinstance(value, dict) and any(
        value.get(field) is True for field in ARTIFACT_INTERVENTION_FIELDS
    )


def explicitly_unmodified_artifacts(value: Any) -> bool:
    """Require historical sidecars to explicitly deny every intervention."""
    return isinstance(value, dict) and all(
        value.get(field) is False for field in ARTIFACT_INTERVENTION_FIELDS
    )


def strict_native_metadata(metadata: dict[str, Any]) -> bool:
    """Accept current metadata only when upstream and native artifacts passed."""
    success = metadata.get("success")
    artifact_audit = metadata.get("artifact_audit")
    normalization = metadata.get("normalization")
    upstream = metadata.get("upstream")
    manifest_record = (
        upstream.get("manifest_record") if isinstance(upstream, dict) else None
    )
    return bool(
        metadata.get("generation_contract_satisfied") is True
        and isinstance(success, dict)
        and success.get("artifact_contract_satisfied") is True
        and isinstance(artifact_audit, dict)
        and artifact_audit.get("mode") == "read_only_native_output"
        and not artifact_intervention_performed(normalization)
        and isinstance(manifest_record, dict)
        and manifest_record.get("status") == "completed"
    )


def legacy_native_recovery(
    metadata: dict[str, Any],
    recovery: dict[str, Any] | None,
) -> bool:
    """Accept an old run only when its sidecars prove no artifact intervention."""
    upstream = metadata.get("upstream")
    manifest_record = (
        upstream.get("manifest_record") if isinstance(upstream, dict) else None
    )
    return bool(
        metadata.get("schema_version") == "generation_metadata_v1"
        and metadata.get("generation_contract_satisfied") is True
        and isinstance(manifest_record, dict)
        and manifest_record.get("status") == "completed"
        and isinstance(recovery, dict)
        and recovery.get("schema_version") == "slide_run_recovery_v2"
        and recovery.get("benchmark_pass") is True
        and recovery.get("raw_status") == "completed"
        and recovery.get("visual_artifact_available") is True
        and explicitly_unmodified_artifacts(recovery)
    )
