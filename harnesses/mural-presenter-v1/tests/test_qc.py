from __future__ import annotations

from geometry_fixture import geometry, write_geometry

from pathlib import Path
import json

from PIL import Image
import pytest

from owner.registry import OwnerRegistry
from qc.content import ContentFinding, ContentQC
from qc.routing import (
    IssueLedger,
    IssueLedgerError,
    IssueStatus,
    OwnerIntent,
    RevisionRouter,
    RouteTarget,
    infer_owner_intent,
)
from qc.visual import PixelEvidence, ReviewVerdict, VisualCandidate, VisualQC
from infer import build_inspect_handlers
from runtime.config import RunSpec
from runtime.tools import ToolContext, WriteScope
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, file_sha256


@pytest.mark.parametrize("positions,target,expected", [
    ([1], None, "single"),
    ([2, 3], None, "group"),
    ([1, 2], None, "orchestrator"),
    ([], None, "orchestrator"),
    ([1], "image", "image"),
    ([1], "orchestrator", "orchestrator"),
])
def test_compact_verification_binds_artifacts_and_routes_by_coverage(
    tmp_path: Path, positions: list[int], target: str | None, expected: str,
) -> None:
    from infer import PresenterKernel
    from runtime.tools import STABLE_TOOL_SCHEMAS, ToolCall

    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    spec = RunSpec.from_inputs(raw_user_query="检查三页", output_workspace=tmp_path,
        environment={"MODEL_API_FORMAT": "openai", "MODEL": "fixture",
                     "MODEL_BASE_URL": "https://example.test"})
    kernel = PresenterKernel(spec)
    try:
        kernel.workspace.register_pages([1, 2, 3])
        owners = {
            "single": kernel.owners.accept(role="slide", pages=[1]),
            "group": kernel.owners.accept(role="slide", pages=[2, 3], group_id="group"),
            "image": kernel.owners.accept(role="image", pages=[1]),
        }
        issue = {"positions": positions, "problem": "图像主体被裁成窄条",
                 "suggested_fix": "扩大图片区并重新裁切"}
        if target:
            issue["target"] = target
        schema = STABLE_TOOL_SCHEMAS["verification_submit"]["properties"]["issues"]["items"]
        assert set(schema["required"]) == {"positions", "problem", "suggested_fix"}
        context = kernel.services.orchestrator_tool_context()
        arguments = {"status": "issues_found", "issues": [issue]}
        result = kernel.tools.execute(context, ToolCall("report", "verification_submit", arguments))
        assert result.ok, result
        record, = kernel.issues.issues()
        assert record.blocks_positive_admission and not record.objective
        assert record.summary == issue["problem"]
        assert record.evidence["page_uids"] == [kernel.workspace.page(n).page_uid for n in positions]
        assert len(record.evidence["artifact_versions"]) == len(positions or [1, 2, 3])
        route = json.loads(result.content)["routes"][0]
        assert route["owner_id"] == ("orchestrator" if expected == "orchestrator" else owners[expected].owner_id)
        repeated = kernel.tools.execute(context, ToolCall("repeat", "verification_submit",
            {**arguments, "issues": [{**issue, "positions": list(reversed(positions))}]}))
        assert repeated.ok and json.loads(repeated.content)["routes"][0]["target"] == "none"
        assert len(kernel.issues.issues()) == 1
        ready = kernel.tools.execute(context, ToolCall("ready", "verification_submit",
            {"status": "issues_resolved", "issues": [], "resolved_issue_ids": [record.issue_id]}))
        assert not ready.ok and ready.error_code == "verification_pages_not_inspected"
        assert kernel.issues.issue(record.issue_id).blocks_positive_admission
        invalid = kernel.tools.execute(context, ToolCall("invalid", "verification_submit",
            {"status": "issues_found", "issues": [{**issue, "problem": "new issue"}, {**issue, "positions": [99]}]}))
        assert not invalid.ok and "99" in str(invalid)
        assert len(kernel.issues.issues()) == 1
        if expected == "single":
            retargeted = kernel.tools.execute(context, ToolCall("retarget", "verification_submit",
                {**arguments, "issues": [{**issue, "target": "image"}]}))
            assert retargeted.ok
            assert json.loads(retargeted.content)["routes"][0]["owner_id"] == owners["image"].owner_id
            assert kernel.issues.issue(record.issue_id).owner_hint == "image"
            assert len(kernel.issues.issues()) == 1
            assert kernel.tools.execute(context, ToolCall("omit-target", "verification_submit", arguments)).ok
            assert kernel.issues.issue(record.issue_id).owner_hint == "image"
    finally:
        kernel.orchestration.close(wait=True)


def _candidate(index: int, *, png_hash: str | None = None) -> VisualCandidate:
    digest = png_hash or f"{index + 1:064x}"
    return VisualCandidate(
        candidate_id=f"candidate-{index}",
        kind="projection_readability",
        severity="major",
        page=index + 1,
        summary=f"P{index + 1:02d} 主体小字在投影距离下不可读。",
        owner_hint="slide",
        objective=True,
        evidence=(
            PixelEvidence(
                png_sha256=digest,
                x=10,
                y=20,
                width=100,
                height=40,
                metric="estimated_text_px",
                measured=12,
                threshold=18,
            ),
        ),
    )


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), color).save(path)


def test_major_candidate_budget_becomes_unresolved_not_pass(tmp_path: Path) -> None:
    candidates = tuple(_candidate(index) for index in range(3))
    ledger = IssueLedger(tmp_path)
    ledger.register_visual_candidates(candidates)

    reviews = VisualQC.review_candidates(
        candidates,
        lambda _candidate: {
            "verdict": "confirmed",
            "rationale": "像素测量与视觉复核一致",
        },
        max_reviews=1,
    )
    issues = ledger.record_visual_reviews(reviews)

    assert [review.verdict for review in reviews] == [
        ReviewVerdict.CONFIRMED,
        ReviewVerdict.UNRESOLVED,
        ReviewVerdict.UNRESOLVED,
    ]
    assert [issue.status for issue in issues] == [
        IssueStatus.OPEN,
        IssueStatus.UNRESOLVED,
        IssueStatus.UNRESOLVED,
    ]
    with pytest.raises(IssueLedgerError, match="unresolved major issues"):
        ledger.verification(
            scope="deck", verdict="pass", issue_ids=[], actor="owner-review"
        )


def test_same_unchanged_pixels_do_not_reopen_resolved_issue(tmp_path: Path) -> None:
    candidate = _candidate(0, png_hash="a" * 64)
    ledger = IssueLedger(tmp_path)
    first = ledger.record_visual_reviews(
        [
            VisualQC.review_candidates(
                [candidate], lambda _item: ReviewVerdict.CONFIRMED
            )[0]
        ]
    )[0]
    ledger.verification(
        scope="page:1",
        verdict="pass",
        issue_ids=[first.issue_id],
        actor="owner-review",
        rationale="修订后复核通过",
    )

    same = ledger.register_visual_candidates([candidate])[0]
    changed = ledger.register_visual_candidates(
        [_candidate(0, png_hash="b" * 64)]
    )[0]

    assert same.issue_id == first.issue_id
    assert same.status == IssueStatus.RESOLVED
    assert changed.issue_id != first.issue_id
    assert changed.status == IssueStatus.CANDIDATE


def test_content_qc_preserves_raw_query_and_reviews_visible_text(tmp_path: Path) -> None:
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    workspace = WorkspaceState(tmp_path)
    source = tmp_path / "material.md"
    source.write_text("审计值为 42。", encoding="utf-8")
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        """
        <html><head><style>.secret{display:none}</style></head>
        <body><h1>审计结果</h1><p>值为 42</p>
        <script>window.hidden='不要出现';</script>
        <div aria-hidden="true">可见辅助文案</div>
        <div hidden>真正隐藏文案</div></body></html>
        """,
        encoding="utf-8",
    )
    run_spec = RunSpec.from_inputs(
        raw_user_query="第一行要求。\n第二行必须保留。",
        output_workspace=tmp_path,
        environment={
            "MODEL_API_FORMAT": "anthropic",
            "MODEL": "test-model",
            "MODEL_BASE_URL": "https://example.test",
        },
    )
    packet = ContentQC.build_packet(
        run_spec,
        workspace,
        pages=[1],
        source_paths=["material.md"],
    )

    def reviewer(value):
        assert value.raw_user_query == "第一行要求。\n第二行必须保留。"
        assert value.pages[0].text == "审计结果 值为 42 可见辅助文案"
        return [
            {
                "kind": "source_note_missing",
                "severity": "minor",
                "pages": [1],
                "summary": "可见页面没有来源脚注。",
                "owner_hint": "slide",
                "evidence": {"source_sha256": value.sources[0].sha256},
            }
        ]

    findings = ContentQC.review(packet, reviewer)
    assert findings[0].kind == "source_note_missing"
    assert packet.sources[0].sha256 == file_sha256(source)


def test_router_reuses_owner_and_requires_new_catalog_evidence(tmp_path: Path) -> None:
    workspace = WorkspaceState(tmp_path)
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    slide_owner = registry.accept(role="slide", pages=[2], group_id="evidence")
    registry.accept(role="image", pages=[2], group_id="evidence-images")
    ledger = IssueLedger(tmp_path)
    issue = ledger.record_content_findings(
        [
            ContentFinding(
                finding_id="finding-image",
                kind="broken_image_resource",
                severity="major",
                pages=(2,),
                summary="页面图片资源没有形成像素。",
                owner_hint="slide",
                objective=True,
                evidence={"html_sha256": "html-v1"},
            )
        ]
    )[0]
    router = RevisionRouter(ledger, registry, artifacts=artifacts)

    first = router.route_issue(issue.issue_id)
    repeated = router.route_issue(issue.issue_id)

    assert first.target == RouteTarget.SLIDE
    assert first.owner_id == slide_owner.owner_id
    assert repeated.target == RouteTarget.NONE
    assert repeated.reason == "no_new_artifact_or_catalog_evidence"

    image_path = workspace.paths.assets / "new-evidence.png"
    _write_png(image_path, (120, 20, 80))
    artifacts.record_image_result(
        artifact_id="image-new-evidence",
        owner_id="owner-image-source",
        path=image_path,
        status="available",
        source_url="https://source.example/new.png",
    )
    with_new_evidence = router.route_issue(issue.issue_id)
    assert with_new_evidence.target == RouteTarget.SLIDE
    assert with_new_evidence.activation_fingerprint != first.activation_fingerprint


def test_ambiguous_image_problem_is_not_assigned_to_an_arbitrary_specialist(tmp_path):
    registry = OwnerRegistry(tmp_path, deck_id="multiple-images")
    first = registry.accept(role="image", pages=[1], group_id="cover")
    router = RevisionRouter(IssueLedger(tmp_path), registry)
    assert router._find_owner(RouteTarget.IMAGE, [1]) == first
    registry.accept(role="image", pages=[1], group_id="figure")
    assert router._find_owner(RouteTarget.IMAGE, [1]) is None


@pytest.mark.parametrize("positions", [[1], [1, 3]])
@pytest.mark.parametrize("target", [None, "slide"])
def test_router_returns_direct_page_repairs_to_main_without_creating_an_owner(tmp_path, positions, target):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2, 3])
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    for number in (1, 3):
        registry.claim_orchestrator_page(number)
    child = registry.accept(role="slide", pages=[2], group_id="independent")
    ledger = IssueLedger(tmp_path)
    request = {"positions": positions, "problem": "标题将目标错误写成既有成果。",
               "suggested_fix": "只修正标题的事实边界；保留数据、图表、来源和版式。"}
    if target:
        request["target"] = target
    issue = ledger.record_orchestrator_issues([request])[0]
    before = registry.path.read_bytes()
    router = RevisionRouter(ledger, registry, artifacts=ArtifactStore(tmp_path, workspace_state=workspace))
    action = router.route_issue(issue.issue_id)
    assert action.target == RouteTarget.ORCHESTRATOR
    assert action.owner_id == "orchestrator" and action.pages == tuple(positions)
    assert ledger.issue(issue.issue_id).evidence == issue.evidence
    assert ledger.issue(issue.issue_id).evidence["suggested_fix"] == request["suggested_fix"]
    assert registry.path.read_bytes() == before
    assert len(registry.list_current()) == 2 and not registry.list_archived()
    assert router.route_issue(issue.issue_id).target == RouteTarget.NONE
    resumed = RevisionRouter(IssueLedger(tmp_path),
        OwnerRegistry(tmp_path, deck_id=workspace.deck_id), artifacts=router.artifacts)
    assert resumed.route_issue(issue.issue_id).target == RouteTarget.NONE
    second = ledger.record_orchestrator_issues([{**request, "positions": [2]}])[0]
    routed = resumed.route_issue(second.issue_id)
    assert routed.target == RouteTarget.SLIDE and routed.owner_id == child.owner_id


def test_mixed_direct_and_delegated_page_issue_requires_main_decision(tmp_path):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2])
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    registry.claim_orchestrator_page(1)
    child = registry.accept(role="slide", pages=[2])
    ledger = IssueLedger(tmp_path)
    issue = ledger.record_orchestrator_issues([{"positions": [1, 2], "problem": "两页口径不同",
        "suggested_fix": "统一口径，保留两页各自的图表", "target": "slide"}])[0]
    before = registry.path.read_bytes()
    action = RevisionRouter(ledger, registry).route_issue(issue.issue_id)
    assert action.target == RouteTarget.ORCHESTRATOR and action.owner_id == "orchestrator"
    assert registry.page_owner(2).owner_id == child.owner_id
    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("changed", ["html", "png"])
def test_router_reactivates_only_for_affected_page_versions(tmp_path: Path, changed: str) -> None:
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2])
    for page in (1, 2):
        workspace.paths.page_html(page).write_text(f"<h1>Page {page}</h1>", encoding="utf-8")
        _write_png(workspace.paths.page_png(page), (page * 50, 20, 80))
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    owner = registry.accept(role="slide", pages=[1, 2], group_id="group")
    ledger = IssueLedger(tmp_path)
    issue = ledger.record_content_findings([ContentFinding(
        finding_id="finding", kind="content_missing", severity="major", pages=(1,),
        summary="结论仍未完整呈现。", owner_hint="slide", objective=True,
        evidence={"html_sha256": "original"})])[0]
    router = RevisionRouter(ledger, registry, artifacts=artifacts)
    first = router.route_issue(issue.issue_id)
    assert first.owner_id == owner.owner_id
    assert router.route_issue(issue.issue_id).target == RouteTarget.NONE
    workspace.paths.page_html(2).write_text("<h1>Unrelated edit</h1>", encoding="utf-8")
    assert router.route_issue(issue.issue_id).target == RouteTarget.NONE
    if changed == "html":
        workspace.paths.page_html(1).write_text("<h1>New evidence</h1>", encoding="utf-8")
    else:
        _write_png(workspace.paths.page_png(1), (220, 30, 70))
    second = router.route_issue(issue.issue_id)
    assert second.target == RouteTarget.SLIDE and second.owner_id == owner.owner_id
    assert second.activation_fingerprint != first.activation_fingerprint
    resumed = RevisionRouter(IssueLedger(tmp_path), registry, artifacts=artifacts)
    assert resumed.route_issue(issue.issue_id).target == RouteTarget.NONE
    assert len(ledger.issues()) == 1


@pytest.mark.parametrize("changed", ["deck.md", "base.css", "knowledge.md", "briefs/slide_01.md", "request"])
def test_router_allows_new_shared_inputs_without_requiring_a_page_edit_first(tmp_path, changed):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2])
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    owner = registry.accept(role="slide", pages=[1], group_id="one")
    ledger = IssueLedger(tmp_path)
    issue = ledger.record_orchestrator_issues([{"positions": [1], "problem": "Source claim is outdated",
                                               "suggested_fix": "Use the current evidence"}])[0]
    router = RevisionRouter(ledger, registry, artifacts=ArtifactStore(tmp_path, workspace_state=workspace), request_key="initial")
    first = router.route_issue(issue.issue_id)
    assert first.owner_id == owner.owner_id
    assert router.route_issue(issue.issue_id).target == RouteTarget.NONE
    if changed == "request":
        router.request_key = "new-user-request"
    else:
        path = tmp_path / changed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("New evidence or shared visual input")
    second = router.route_issue(issue.issue_id)
    assert second.target == RouteTarget.SLIDE and second.activation_fingerprint != first.activation_fingerprint
    assert router.route_issue(issue.issue_id).target == RouteTarget.NONE
    path = tmp_path / "briefs/slide_02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Unrelated page's new brief")
    assert router.route_issue(issue.issue_id).target == RouteTarget.NONE


def test_router_accepts_a_new_specific_fix_plan_on_unchanged_pixels(tmp_path):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1])
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    owner = registry.accept(role="slide", pages=[1])
    ledger = IssueLedger(tmp_path)
    router = RevisionRouter(ledger, registry, artifacts=ArtifactStore(tmp_path, workspace_state=workspace))
    observation = {"positions": [1], "problem": "Image cropping does not change", "suggested_fix": "Adjust object-position"}
    first = ledger.record_orchestrator_issues([observation])[0]
    assert router.route_issue(first.issue_id).owner_id == owner.owner_id
    assert router.route_issue(first.issue_id).target == RouteTarget.NONE
    diagnosed = ledger.record_orchestrator_issues([{**observation, "suggested_fix": "Remove figure's default margin and set its height before cropping"}])[0]
    assert router.route_issue(diagnosed.issue_id).owner_id == owner.owner_id


def test_accepted_degradation_is_explicit_and_auditable(tmp_path: Path) -> None:
    ledger = IssueLedger(tmp_path)
    issue = ledger.record_visual_reviews(
        [
            VisualQC.review_candidates(
                [_candidate(0)], lambda _item: ReviewVerdict.UNRESOLVED
            )[0]
        ]
    )[0]

    with pytest.raises(IssueLedgerError, match="requires rationale"):
        ledger.verification(
            scope="page:1",
            verdict="accepted_degradation",
            issue_ids=[issue.issue_id],
            actor="orchestrator",
        )

    ledger.verification(
        scope="page:1",
        verdict="accepted_degradation",
        issue_ids=[issue.issue_id],
        actor="orchestrator",
        rationale="来源图片分辨率有限，已保留来源并采用可读裁切。",
    )
    accepted = ledger.issue(issue.issue_id)
    assert accepted.status == IssueStatus.ACCEPTED_DEGRADATION
    assert "分辨率有限" in accepted.resolution
    assert ledger.blocking_issues() == ()


def test_owner_intent_uses_tool_events_instead_of_magic_prose() -> None:
    assert infer_owner_intent(
        [{"event_type": "tool_use", "tool_name": "request_orchestrator"}]
    ) == OwnerIntent.HELP
    assert infer_owner_intent(
        [{"event_type": "tool_use", "tool_name": "artifact_commit"}]
    ) == OwnerIntent.REPAIR
    assert infer_owner_intent(
        [{"type": "task_completed", "content": "任意自由文本"}]
    ) == OwnerIntent.READY


def test_vision_handler_uses_actual_canvas_and_marks_current_pair_inspected(
    tmp_path: Path,
) -> None:
    # layout v2: initialize the workspace manifest before freezing the canvas.
    workspace = WorkspaceState(tmp_path)
    from owner.registry import OwnerRegistry
    OwnerRegistry(tmp_path, deck_id=workspace.deck_id).claim_orchestrator_page(1)
    canvas = write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        "<html><body>page</body></html>", encoding="utf-8"
    )
    workspace.register_scaffold(1)
    artifacts.declare(
        artifact_id="page-html-01",
        kind="page_html",
        path=workspace.paths.page_html(1),
        owner_id="orchestrator",
        page=1,
    )
    artifacts.commit(
        owner_id="orchestrator",
        artifact_ids=["page-html-01"],
        source_hashes={},
    )

    def renderer(_html: Path, png: Path, received: dict):
        assert received == canvas
        _write_png(png, (20, 100, 180))
        return {"geometry": received, "loaded_resources": [], "failed_resources": []}

    artifacts.render_pages([1], renderer)
    seen: list[dict] = []

    def analyzer(_path: Path, _question: str, _scope: str, received: dict):
        seen.append(received)
        return {"summary": "当前页没有确定性像素问题"}

    handler = build_inspect_handlers(workspace, artifacts, None, analyzer)[0]
    result = handler(
        ToolContext(owner_id="orchestrator", role="orchestrator", read_scopes=(WriteScope("renders"),)),
        {
            "path": "renders/slide_01.png",
            "question": "检查截断和重叠",
        },
    )

    assert seen == [canvas]
    assert result["inspection_id"].startswith("inspection-")
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    _, commit = build_inspect_handlers(workspace, artifacts, renderer, analyzer)
    commit(result["pending_inspection"])
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
