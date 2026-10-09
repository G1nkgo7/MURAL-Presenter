from __future__ import annotations

from geometry_fixture import geometry, write_geometry

import json
from pathlib import Path
import stat

import pytest

from owner.context import OwnerContextStore, OwnerPacket, ResumeMode
from owner.registry import OwnerRegistry, OwnerRegistryError
from runtime.config import RunSpec
from runtime.recovery import SegmentCheckpoint


@pytest.fixture(autouse=True)
def configured_owner_workspace(tmp_path):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))


def test_orchestrator_checkpoint_is_private_exact_and_not_an_owner(tmp_path):
    from runtime.production import read_scopes_for_role, write_scopes_for_role
    from runtime.tools import ToolContext
    store = OwnerContextStore(tmp_path, max_string_chars=256)
    checkpoint = SegmentCheckpoint.initial([
        {"role": "user", "content": "原始内容" * 500},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "private", "signature": "signed"}]},
        {"role": "user", "content": "continue"},
    ])
    identity = {"deck_id": "one", "workspace": str(tmp_path), "raw_user_query": "query", "skill_tree_sha256": "pinned"}
    store.save_orchestrator(checkpoint, identity=identity, artifact_hashes={}, live_cursor=3)
    value = store.load_orchestrator()
    assert value["segment"] == checkpoint.to_dict()
    assert "owner" not in value and "packet" not in value and "messages" not in value
    path = store.path_for("orchestrator")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for role in ("orchestrator", "slide", "research", "image"):
        context = ToolContext(role, role, read_scopes=read_scopes_for_role(role, skill_name="mural-presenter-v1", pages=[1]),
                              write_scopes=write_scopes_for_role(role, pages=[1]))
        assert not context.may_read(path.relative_to(tmp_path).as_posix())
        assert not context.may_write(path.relative_to(tmp_path).as_posix())
    open_segment = SegmentCheckpoint.initial([{"role": "assistant", "content": [
        {"type": "tool_use", "id": "open", "name": "write_file", "input": {}}]}])
    with pytest.raises(ValueError, match="open tool transaction"):
        store.save_orchestrator(open_segment, identity=identity, artifact_hashes={}, live_cursor=4)
    assert store.load_orchestrator() == value


def test_removed_material_owner_cannot_create_new_checkpoint_identity(tmp_path):
    registry = OwnerRegistry(tmp_path, deck_id="current-role-contract")
    registry.accept(role="research", group_id="current-research")
    before = registry.path.read_bytes()
    with pytest.raises(OwnerRegistryError, match="unsupported owner role 'material'"):
        registry.accept(role="material", group_id="removed")
    assert registry.path.read_bytes() == before


def test_checkpoint_and_training_share_one_blob_and_reject_corruption(tmp_path):
    import base64
    from types import SimpleNamespace
    from runtime.raw_trace import externalize_binary
    from export.training import TrainingExporter
    from state.workspace import WorkspaceState
    from qc.routing import IssueLedger
    workspace = WorkspaceState(tmp_path)
    owner = OwnerRegistry(tmp_path, deck_id="shared-bytes").accept(role="slide", pages=[1])
    source = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(b"unchanged-bytes").decode()}
    messages = [{"role": "user", "content": [{"type": "image", "source": source}]}]
    original = SegmentCheckpoint.initial(messages)
    store = OwnerContextStore(tmp_path)
    for _ in range(2):
        store.save(owner=owner, packet=OwnerPacket.compile(owner, _run_spec(tmp_path)), segment=original, artifact_hashes={})
    disk = json.loads(store.path_for(owner.owner_id).read_text())
    assert "messages" not in disk and source["data"] not in json.dumps(disk)
    assert store.load(owner.owner_id).segment == original
    raw = externalize_binary(tmp_path, source)
    exporter = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis")
    exporter.capture(owner.owner_id, "slide", original)
    assert not exporter.rows_path.exists() and not exporter.manifest_path.exists()
    assert not (tmp_path / "_trace/training").exists()
    blobs = list((tmp_path / "_trace/raw/blobs").iterdir())
    assert len(blobs) == 1 and blobs[0].relative_to(tmp_path).as_posix() == raw["path"]
    assert len(list(store.directory.glob("*.json"))) == 1
    blobs[0].write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.load(owner.owner_id)


def test_checkpoint_image_conversion_does_not_reinterpret_tool_arguments(tmp_path):
    from runtime.raw_trace import checkpoint_images
    messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "write_file",
                 "input": {"source": {"type": "base64", "data": "not an image"}}}]}]
    assert checkpoint_images(tmp_path, messages) == messages
    assert not (tmp_path / "_trace/raw").exists()


def _run_spec(tmp_path: Path) -> RunSpec:
    if not (tmp_path / "config.json").exists():
        write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    return RunSpec.from_inputs(
        raw_user_query="请制作证据驱动的中文演示稿。\n原始换行必须保留。",
        output_workspace=tmp_path,
        environment={
            "MODEL_API_FORMAT": "openai",
            "MODEL": "test-model",
            "MODEL_BASE_URL": "https://example.test",
        },
    )




def test_owner_packet_preserves_raw_query_and_frozen_canvas(tmp_path: Path) -> None:
    owner = OwnerRegistry(tmp_path, deck_id="deck-context").accept(
        role="slide", pages=[2, 3], group_id="evidence"
    )

    packet = OwnerPacket.compile(
        owner,
        _run_spec(tmp_path),
        artifact_refs=[{"artifact_id": "asset-logo", "sha256": "abc"}],
    )

    assert packet.raw_user_query.endswith("原始换行必须保留。")
    assert "\n" in packet.raw_user_query
    assert packet.artifact_refs[0]["artifact_id"] == "asset-logo"


def test_context_checkpoint_preserves_image_bytes_but_capsule_is_compact_and_boundary_closed(
    tmp_path: Path,
) -> None:
    owner = OwnerRegistry(tmp_path, deck_id="deck-checkpoint").accept(
        role="image", pages=[1], group_id="hero"
    )
    packet = OwnerPacket.compile(owner, _run_spec(tmp_path))
    messages = (
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "QUJD" * 400,
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-1", "name": "render"}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-open", "name": "render"}],
        },
    )
    segment = SegmentCheckpoint.initial(messages, segment_id="segment-context")
    store = OwnerContextStore(tmp_path)

    stored = store.save(
        owner=owner,
        packet=packet,
        segment=segment,
        artifact_hashes={"asset-logo": "abc"},
        capsule={"preview": messages[0]["content"][0]},
    )

    serialized = store.path_for(owner.owner_id).read_text(encoding="utf-8")
    assert stored.segment.messages[0]["content"][0] == messages[0]["content"][0]
    assert "QUJDQUJD" not in json.dumps(stored.capsule)
    assert "omitted_base64" in serialized
    assert stat.S_IMODE(store.path_for(owner.owner_id).stat().st_mode) == 0o600
    assert "tool-open" not in serialized
    assert stored.segment.messages == stored.messages
    assert store.resume_mode(owner) == ResumeMode.RESUME
    json.loads(serialized)


def test_context_store_preserves_all_closed_transactions(tmp_path: Path) -> None:
    owner = OwnerRegistry(tmp_path, deck_id="deck-bounded").accept(
        role="slide", pages=[1], group_id="bounded"
    )
    packet = OwnerPacket.compile(owner, _run_spec(tmp_path))
    messages = (
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "closed", "name": "vision_analyze"}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "closed", "content": "ok"}],
        },
        {"role": "assistant", "content": "new-1"},
        {"role": "user", "content": "new-2"},
    )
    segment = SegmentCheckpoint.initial(messages, segment_id="segment-bounded")
    store = OwnerContextStore(tmp_path)

    stored = store.save(
        owner=owner,
        packet=packet,
        segment=segment,
        artifact_hashes={},
    )

    assert stored.messages == messages
    assert stored.segment == segment


def test_capsule_limit_never_truncates_actual_owner_input(tmp_path):
    owner = OwnerRegistry(tmp_path, deck_id="long-unsigned").accept(role="slide", pages=[1])
    segment = SegmentCheckpoint.initial([{"role": "user", "content": "x" * 1000}], segment_id="full-text")
    store = OwnerContextStore(tmp_path, max_string_chars=256)
    saved = store.save(owner=owner, packet=OwnerPacket.compile(owner, _run_spec(tmp_path)), segment=segment, artifact_hashes={})
    assert saved.segment == segment


def test_resumable_checkpoint_preserves_valid_image_transport_past_text_truncation_limit(tmp_path):
    import base64
    from io import BytesIO
    from PIL import Image
    from runtime.vision import image_source, normalize_images
    owner = OwnerRegistry(tmp_path, deck_id="pixel-memory").accept(role="slide", pages=[1])
    packet = OwnerPacket.compile(owner, _run_spec(tmp_path))
    payload = BytesIO()
    Image.effect_noise((128, 128), 100).save(payload, format="PNG")
    source = image_source(payload.getvalue())
    assert len(source["data"]) > 256
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Current crop: renders/slide_01.png; source SHA remains in tool receipt"},
        {"type": "image", "source": source}]}]
    store = OwnerContextStore(tmp_path, max_string_chars=256)
    stored = store.save(owner=owner, packet=packet, segment=SegmentCheckpoint.initial(messages), artifact_hashes={})
    restored = list(stored.segment.messages)
    normalize_images(restored)
    assert restored == messages
    assert base64.b64decode(restored[0]["content"][1]["source"]["data"]) == payload.getvalue()
    assert "omitted_base64" not in json.dumps(restored)


def test_private_owner_save_preserves_signed_context_until_runtime_compacts(
    tmp_path: Path,
) -> None:
    owner = OwnerRegistry(tmp_path, deck_id="deck-signed").accept(
        role="slide", pages=[1], group_id="signed"
    )
    packet = OwnerPacket.compile(owner, _run_spec(tmp_path))
    messages = (
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "必须原样保存的签名思考",
                    "signature": "provider-signature",
                }
            ],
        },
        {"role": "user", "content": "x" * 2000},
    )
    segment = SegmentCheckpoint(
        segment_id="segment-signed",
        predecessor_segment_id=None,
        messages=messages,
        provider_affinity_id="channel-signed",
    )
    store = OwnerContextStore(tmp_path, max_string_chars=300)

    stored = store.save(
        owner=owner,
        packet=packet,
        segment=segment,
        artifact_hashes={},
    )

    serialized = json.dumps(stored.segment.to_dict(), ensure_ascii=False)
    assert stored.segment == segment
    assert "provider-signature" in serialized
    assert stat.S_IMODE(store.path_for(owner.owner_id).stat().st_mode) == 0o600


def test_unreplayable_omitted_image_checkpoint_requires_rebuild(tmp_path):
    owner = OwnerRegistry(tmp_path, deck_id="invalid-pixel-source").accept(role="slide", pages=[1])
    store = OwnerContextStore(tmp_path)
    store.save(owner=owner, packet=OwnerPacket.compile(owner, _run_spec(tmp_path)), artifact_hashes={},
               segment=SegmentCheckpoint.initial([{"role": "user", "content": [
                   {"type": "image", "source": {"type": "omitted_base64", "sha256": "old-image"}}]}]))
    assert store.resume_mode(owner) == ResumeMode.REBUILD
