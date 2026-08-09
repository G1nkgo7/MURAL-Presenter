#!/usr/bin/env python3
"""Deterministic stage ledger for Mural Presenter.

This module is intentionally not an Agent.  It does not call a model, make
creative decisions, or add a serial workflow stage.  It observes the tools and
canonical files that already exist, writes a small recoverable state ledger,
and supplies occasional anti-loop guidance to the current Agent.
"""

from __future__ import annotations

from collections import Counter
import glob
import hashlib
import json
import os
import re
import threading


_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:plan|research|materials|assets|slides|renders|_trace)/"
    r"[^\s`'\"<>]+)",
    re.I,
)


def _atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _relative_files(root, pattern):
    return sorted(
        os.path.relpath(path, root).replace(os.sep, "/")
        for path in glob.glob(os.path.join(root, pattern), recursive=True)
        if os.path.isfile(path)
    )


def _file_record(root, relative):
    absolute = os.path.join(root, relative)
    if not os.path.isfile(absolute):
        return None
    stat = os.stat(absolute)
    return {
        "path": relative.replace(os.sep, "/"),
        "bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _declared_material_outputs(text):
    return sorted(set(
        value.rstrip(".,;:)。，；：）")
        for value in _PATH_RE.findall(str(text or ""))
        if value.lower().startswith("materials/") and value.lower().endswith(".md")
    ))


def _declared_image_groups(text):
    """Return explicit Image group ids without interpreting presentation prose."""
    values = set()
    for match in re.finditer(
        r"(?:group[_-]?id|--group-id)\s*(?:[:=]|\s)\s*[`'\"]?"
        r"([A-Za-z0-9._-]+)",
        str(text or ""),
        re.I,
    ):
        values.add(match.group(1))
    return values


class StageObserver:
    """Maintain a compact requirement/output delta beside the immutable trace."""

    def __init__(self, agent, kind="other"):
        self.agent = agent
        self.kind = str(kind or "other").lower()
        self.path = os.path.join(agent.trace.sub_dir, "stage-state.json")
        trace_dir = os.path.abspath(agent.trace.sub_dir)
        marker = os.sep + "_trace" + os.sep
        self.root = os.path.abspath(getattr(agent, "ws", "") or (
            trace_dir.split(marker, 1)[0] if marker in trace_dir else trace_dir
        ))
        self.tool_counts = Counter()
        self.unique_queries = []
        self.unique_urls = []
        self.recent_files = []
        self.recent_vision = []
        self.single_evidence_turns = 0
        self.single_asset_vision_turns = 0
        self.last_evidence_turn = -1
        self.last_canonical_write_turn = -1
        self._research_batch_hint_sent = False
        self._research_gate = ""
        self._research_gate_reason = ""
        self._research_evidence_batches = 0
        self._research_checkpoint_signature = self._research_signature()
        self._image_contact_hint_sent = False
        self._checkpoint_hint_sent = False
        self._last_stage = ""
        self._image_gate = ""
        self._image_gate_reason = ""
        self._image_groups = _declared_image_groups(agent.initial_user)
        self._seen_created_assets = set(
            getattr(agent, "_created_asset_paths", set()) or set()
        )
        self._vision_cursor = len(list(getattr(agent, "vision_paths", []) or []))
        self._image_decision_signature = self._asset_decision_signature()
        self.progress_renewed = False
        self._progress_signature = ""
        self.state = {}
        self.refresh(turn=-1)

    @property
    def language(self):
        return str(getattr(self.agent, "prompt_language", "zh") or "zh").lower()

    def _append_unique(self, target, value, limit=24):
        value = str(value or "").strip()
        if not value or value in target:
            return
        target.append(value)
        if len(target) > limit:
            del target[:-limit]

    def _inventory(self):
        root = self.root
        known = []
        for relative in (
            "research/research.md",
            "plan/grounded-knowledge.md",
            "plan/design-brief.md",
            "plan/deck.md",
            "plan/theme.css",
            "plan/assets.md",
            "plan/assets.json",
            "assets/catalog.json",
            "_trace/review-issues.md",
            "speech.md",
            "base.css",
            "present.html",
        ):
            record = _file_record(root, relative)
            if record:
                known.append(record)
        groups = {
            "material_summaries": _relative_files(root, "materials/**/*.md"),
            "slide_plans": _relative_files(root, "plan/slide_*.md"),
            "slide_html": _relative_files(root, "slides/slide_*.html"),
            "rendered_pages": _relative_files(root, "renders/slide_*.png"),
            "asset_contact_sheets": _relative_files(root, "assets/**/*contact*.png"),
        }
        return known, groups

    def _asset_catalog_entries(self):
        path = os.path.join(self.root, "assets", "catalog.json")
        try:
            with open(path, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, TypeError):
            return []
        return [item for item in (payload.get("assets") or []) if isinstance(item, dict)]

    def _research_signature(self):
        path = os.path.join(self.root, "research", "research.md")
        try:
            with open(path, "rb") as stream:
                return hashlib.sha256(stream.read()).hexdigest()[:16]
        except OSError:
            return ""

    def _asset_decision_signature(self):
        """Hash semantic decisions by this Image worker, not raw candidate count.

        Downloads create ``unassigned`` catalog rows automatically.  Those are
        evidence, not progress.  Progress begins when the worker binds a row to
        an asset/group or records a review status.  Filtering by worker-created
        paths/group id prevents a concurrent Image worker from renewing this
        worker's progress lease.
        """
        if self.kind != "image":
            return ""
        created = set(getattr(self.agent, "_created_asset_paths", set()) or set())
        created |= self._seen_created_assets
        decisions = []
        for item in self._asset_catalog_entries():
            path = str(item.get("path") or "").replace("\\", "/")
            group_id = str(item.get("group_id") or "")
            if created or self._image_groups:
                if path not in created and group_id not in self._image_groups:
                    continue
            asset_id = str(item.get("asset_id") or "")
            status = str(item.get("status") or "unassigned")
            if not asset_id and status == "unassigned":
                continue
            decisions.append((path, asset_id, group_id, status))
        raw = json.dumps(sorted(decisions), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _is_asset_decision_tool(name, args):
        path = str((args or {}).get("path") or "").replace("\\", "/").lstrip("./")
        command = str((args or {}).get("command") or (args or {}).get("cmd") or "")
        return (
            (name in {"write_file", "patch"} and path == "assets/catalog.json")
            or (name == "terminal" and bool(re.search(
                r"\basset-(?:assign|review|finalize)\b", command
            )))
        )

    def preflight_tool(self, name, args):
        """Conditionally pause Image evidence expansion until state is decided.

        This is a workflow gate, not a total-call budget.  Any real semantic
        decision renews the lease, so long tasks can continue indefinitely while
        making progress.
        """
        name = str(name or "")
        args = args if isinstance(args, dict) else {}
        if self.kind == "research" and self._research_gate:
            if name not in {"web_search", "web_extract", "vision_analyze"}:
                return ""
            if self.language == "en":
                return (
                    f"Stage progress gate blocked {name}: write the verified findings, source "
                    "boundaries, and unresolved claims already established into "
                    "research/research.md before opening another evidence route. A real update "
                    "immediately reopens focused research; there is no total-call cap."
                )
            return (
                f"阶段进展门已阻止 {name}：请先把已经确认的事实、来源边界和未解决命题写入 "
                "research/research.md，再开启新的证据路线。文件发生真实更新后会立即恢复定向检索；"
                "这不是总调用次数上限。"
            )
        if self.kind != "image" or not self._image_gate:
            return ""
        if self._is_asset_decision_tool(name, args):
            return ""
        acquisition = {"web_search", "web_extract", "image_generate"}
        command = str(args.get("command") or "")
        is_acquisition = name in acquisition or (
            name == "terminal" and bool(re.search(r"\basset-download\b", command))
        )
        if not is_acquisition:
            # Reading, contact-sheet creation, image processing, and inspection
            # remain available.  Unchanged duplicate Vision is handled by the
            # pixel digest guard in tools.py.
            return ""
        if self.language == "en":
            action = (
                "inspect the local candidates already acquired"
                if self._image_gate == "inspect" else
                "record which inspected candidates are accepted, rejected, or still unresolved"
            )
            return (
                f"Stage progress gate blocked {name}: {action} before expanding the evidence "
                "pool. Update assets/catalog.json via deck.py asset-assign/asset-review. A real "
                "state decision immediately reopens search and generation; there is no total-call cap."
            )
        action = (
            "先检查本轮已经落地的本地候选"
            if self._image_gate == "inspect" else
            "先把已检查候选明确写成采用、淘汰或仍待解决"
        )
        return (
            f"阶段进展门已阻止 {name}：{action}，再扩展证据池。请用 deck.py "
            "asset-assign/asset-review 把决策写入 assets/catalog.json。一旦状态真正改变，"
            "搜索和生成会立即恢复；这不是总调用次数上限。"
        )

    def _expectations(self, groups):
        kind = self.kind
        expected = []
        if kind == "research":
            expected = ["research/research.md"]
        elif kind == "material":
            expected = _declared_material_outputs(self.agent.initial_user)
        elif kind == "image":
            expected = ["assets/catalog.json"]
        elif kind == "slide":
            pages = sorted(set(getattr(self.agent, "_assigned_pages", []) or []))
            expected = [f"slides/slide_{page:02d}.html" for page in pages]
            expected += [f"renders/slide_{page:02d}.png" for page in pages]
        elif kind == "review":
            expected = ["_trace/review-issues.md"]
        elif getattr(self.agent, "role", "") == "orchestrator":
            expected = ["plan/design-brief.md", "plan/deck.md"]
            revision_mode = bool((getattr(self.agent, "cfg", {}) or {}).get("_revision_mode"))
            if not revision_mode:
                expected.append("plan/theme.css")
            preferences = getattr(self.agent, "generation_preferences", {}) or {}
            try:
                page_count = int(preferences.get("page_count") or 0)
            except (TypeError, ValueError):
                page_count = 0
            if page_count > 0:
                expected.extend(
                    f"plan/slide_{page:02d}.md" for page in range(1, page_count + 1)
                )
            expected.extend(["base.css", "speech.md", "present.html"])
        existing = [path for path in expected if os.path.isfile(os.path.join(self.root, path))]
        missing = [path for path in expected if path not in existing]
        return expected, existing, missing

    def _stage(self, groups, existing):
        root = self.root
        if self.kind == "research":
            return "handoff_ready" if "research/research.md" in existing else "evidence_collection"
        if self.kind == "material":
            return "handoff_ready" if existing else "material_grounding"
        if self.kind == "image":
            if "assets/catalog.json" not in existing:
                return "asset_resolution"
            return "asset_review" if not groups["asset_contact_sheets"] else "handoff_ready"
        if self.kind == "slide":
            pages = sorted(set(getattr(self.agent, "_assigned_pages", []) or []))
            rendered = {
                int(match.group(1)) for path in groups["rendered_pages"]
                if (match := re.search(r"slide_(\d+)\.png$", path))
            }
            return "handoff_ready" if pages and set(pages) <= rendered else "page_production"
        if self.kind == "review":
            return "review_closeout" if "_trace/review-issues.md" in existing else "pixel_review"
        if getattr(self.agent, "role", "") == "orchestrator":
            if not os.path.isfile(os.path.join(root, "plan/design-brief.md")):
                return "grounding_and_direction"
            revision_mode = bool((getattr(self.agent, "cfg", {}) or {}).get("_revision_mode"))
            preferences = getattr(self.agent, "generation_preferences", {}) or {}
            try:
                page_count = int(preferences.get("page_count") or 0)
            except (TypeError, ValueError):
                page_count = 0
            plans_complete = page_count <= 0 or all(
                os.path.isfile(os.path.join(root, f"plan/slide_{page:02d}.md"))
                for page in range(1, page_count + 1)
            )
            planning_files_complete = all(
                os.path.isfile(os.path.join(root, path))
                for path in ("plan/deck.md", "base.css", "speech.md")
            ) and (revision_mode or os.path.isfile(os.path.join(root, "plan/theme.css")))
            if not plans_complete or not planning_files_complete:
                return "deck_planning"
            if not groups["slide_html"]:
                return "production_dispatch"
            if not os.path.isfile(os.path.join(root, "present.html")):
                return "quality_and_build"
            return "delivery"
        return "working"

    def _child_statuses(self):
        statuses = []
        for worker in list(getattr(self.agent, "worker_recs", []) or []):
            contract = worker.get("contract") or {}
            statuses.append({
                "label": worker.get("label"),
                "kind": worker.get("kind"),
                "clean": bool(worker.get("clean")),
                "status": contract.get("status"),
                "output": contract.get("output"),
                "validation_error": contract.get("validation_error"),
            })
        return statuses[-24:]

    def refresh(self, turn):
        known, groups = self._inventory()
        expected, existing, missing = self._expectations(groups)
        stage = self._stage(groups, existing)
        signature_payload = {
            "stage": stage,
            "known": known,
            "groups": {key: value for key, value in groups.items()},
            "children": self._child_statuses(),
        }
        signature = hashlib.sha256(
            json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        previous_signature = self._progress_signature
        if previous_signature and signature != previous_signature:
            # Any real canonical-file or child-handoff change renews the
            # progress lease.  Read/tool chatter is deliberately excluded from
            # the signature, so it cannot disguise a loop as progress.
            self.progress_renewed = True
        self._progress_signature = signature
        changed = stage != self._last_stage
        self._last_stage = stage
        self.state = {
            "schema_version": 2,
            "agent": str(getattr(self.agent, "label", "") or ""),
            "role": self.kind,
            "stage": stage,
            "turn": int(turn),
            "requirement_output_delta": {
                "expected": expected,
                "existing": existing,
                "missing": missing,
            },
            "canonical_files": known,
            "artifact_groups": {
                key: {"count": len(value), "recent": value[-8:]}
                for key, value in groups.items()
            },
            "activity": {
                "tools": dict(sorted(self.tool_counts.items())),
                "unique_queries": self.unique_queries[-12:],
                "unique_urls": self.unique_urls[-12:],
                "recent_files": self.recent_files[-12:],
                "recent_vision": self.recent_vision[-12:],
                "reads_completed": sorted(
                    getattr(self.agent, "_completed_read_paths", set()) or set()
                )[-24:],
                "reads_pending": dict(sorted(
                    (getattr(self.agent, "_pending_read_continuations", {}) or {}).items()
                )),
            },
            "convergence": {
                "mode": "progress_lease",
                "gate": self._image_gate or self._research_gate or "open",
                "reason": self._image_gate_reason or self._research_gate_reason or "none",
                "decision_signature": (
                    self._image_decision_signature
                    or self._research_checkpoint_signature
                    or "none"
                ),
            },
            "children": self._child_statuses(),
            "progress_signature": signature,
        }
        _atomic_json(self.path, self.state)
        return changed

    def observe(self, turn, tool_uses):
        self.progress_renewed = False
        names = []
        for tool_use in tool_uses:
            name = str(getattr(tool_use, "name", "") or "")
            args = getattr(tool_use, "input", {})
            args = args if isinstance(args, dict) else {}
            command_text = str(args.get("command") or args.get("cmd") or "")
            names.append(name)
            self.tool_counts[name] += 1
            if name == "web_search":
                self._append_unique(self.unique_queries, args.get("query"))
            elif name == "web_extract":
                urls = args.get("urls") or args.get("url") or []
                if isinstance(urls, str):
                    urls = [urls]
                for url in urls:
                    self._append_unique(self.unique_urls, url)
            if name in {"read_file", "write_file", "patch", "vision_analyze"}:
                path = args.get("path") or args.get("image_url")
                self._append_unique(self.recent_files, path)
            if name == "vision_analyze":
                path = str(args.get("image_url") or "")
                self._append_unique(self.recent_vision, path)

            if name in {
                "web_search", "web_extract", "image_generate",
                "vision_analyze", "delegate_task",
            } or (name == "terminal" and re.search(r"\basset-download\b", command_text)):
                self.last_evidence_turn = max(self.last_evidence_turn, int(turn))
            path_text = str(args.get("path") or "").replace("\\", "/")
            canonical_write = False
            if name in {"write_file", "patch"}:
                if self.kind == "research":
                    canonical_write = path_text == "research/research.md"
                elif self.kind == "material":
                    canonical_write = path_text.startswith("materials/") and path_text.endswith(".md")
                elif self.kind == "image":
                    canonical_write = path_text == "assets/catalog.json"
                elif self.kind == "review":
                    canonical_write = path_text == "_trace/review-issues.md"
                elif self.kind == "slide":
                    canonical_write = path_text.startswith("slides/slide_")
                elif getattr(self.agent, "role", "") == "orchestrator":
                    canonical_write = path_text.startswith("plan/")
            elif name == "terminal":
                if self.kind == "image":
                    canonical_write = bool(re.search(r"\basset-(?:assign|review|finalize)\b", command_text))
                elif self.kind == "review":
                    canonical_write = "review-issues.md" in command_text
                elif self.kind == "slide":
                    canonical_write = "slide_" in command_text and "render" not in command_text
            if canonical_write:
                self.last_canonical_write_turn = max(
                    self.last_canonical_write_turn, int(turn)
                )

        evidence_names = {"web_search", "web_extract"}
        if len(names) == 1 and names[0] in evidence_names:
            self.single_evidence_turns += 1
        else:
            self.single_evidence_turns = 0

        if self.kind == "research":
            evidence_indices = [
                index for index, tool_use in enumerate(tool_uses)
                if str(getattr(tool_use, "name", "")) in {
                    "web_search", "web_extract", "vision_analyze",
                }
            ]
            checkpoint_indices = [
                index for index, tool_use in enumerate(tool_uses)
                if str(getattr(tool_use, "name", "")) in {"write_file", "patch"}
                and str((getattr(tool_use, "input", {}) or {}).get("path") or "")
                .replace("\\", "/").lstrip("./") == "research/research.md"
            ]
            latest_evidence = max(evidence_indices, default=-1)
            latest_checkpoint = max(checkpoint_indices, default=-1)
            new_signature = self._research_signature()
            checkpoint_changed = (
                latest_checkpoint >= 0
                and new_signature
                and new_signature != self._research_checkpoint_signature
            )
            if checkpoint_changed and latest_checkpoint >= latest_evidence:
                self._research_checkpoint_signature = new_signature
                self._research_evidence_batches = 0
                self._research_gate = ""
                self._research_gate_reason = "canonical research checkpoint updated"
                self.progress_renewed = True
            elif latest_evidence >= 0:
                self._research_evidence_batches += 1
                if self._research_evidence_batches >= 2:
                    self._research_gate = "checkpoint"
                    self._research_gate_reason = (
                        "search and extraction batch completed without canonical synthesis"
                    )

        if self.kind == "image":
            # Accumulate successful single-asset inspections across interleaved
            # fetch/vision turns.  A fetch no longer resets the contact-sheet
            # hint, which was the loophole in the long hand-drip trace.
            current_vision = list(getattr(self.agent, "vision_paths", []) or [])
            new_vision = current_vision[self._vision_cursor:]
            self._vision_cursor = len(current_vision)
            contact_seen = any("contact" in str(path).lower() for path in new_vision)
            if contact_seen:
                self.single_asset_vision_turns = 0
            else:
                self.single_asset_vision_turns += sum(
                    1 for path in new_vision if str(path).replace("\\", "/").startswith("assets/")
                )

            created_now = set(
                getattr(self.agent, "_created_asset_paths", set()) or set()
            )
            new_created = created_now - self._seen_created_assets
            self._seen_created_assets |= created_now
            old_decision = self._image_decision_signature
            new_decision = self._asset_decision_signature()
            decision_changed = new_decision != old_decision
            self._image_decision_signature = new_decision

            last_vision_index = max(
                (index for index, tool_use in enumerate(tool_uses)
                 if str(getattr(tool_use, "name", "")) == "vision_analyze"),
                default=-1,
            )
            last_decision_index = max(
                (index for index, tool_use in enumerate(tool_uses)
                 if self._is_asset_decision_tool(
                     str(getattr(tool_use, "name", "")),
                     getattr(tool_use, "input", {}) if isinstance(
                         getattr(tool_use, "input", {}), dict
                     ) else {},
                 )),
                default=-1,
            )
            if decision_changed:
                self.progress_renewed = True
                self._image_gate = ""
                self._image_gate_reason = "semantic asset decision recorded"
            if new_created and last_decision_index < 0:
                self._image_gate = "inspect"
                self._image_gate_reason = "local candidate batch acquired but not inspected"
            if new_vision and last_decision_index <= last_vision_index:
                self._image_gate = "decide"
                self._image_gate_reason = "fresh asset pixels inspected but no catalog decision recorded"
            if last_decision_index > last_vision_index:
                self._image_gate = ""
                self._image_gate_reason = "semantic asset decision recorded after inspection"
        elif len(names) == 1 and names[0] == "vision_analyze":
            self.single_asset_vision_turns += 1
        elif names:
            self.single_asset_vision_turns = 0

        changed = self.refresh(turn)
        hints = []
        if (self.kind == "research" and self.single_evidence_turns >= 2
                and not self._research_batch_hint_sent):
            self._research_batch_hint_sent = True
            hints.append(
                "Research is issuing one evidence call per model turn. Put all remaining "
                "independent searches or extracts in one response so the Harness can run them "
                "concurrently. If the current evidence already supports the decision, update "
                "research/research.md and synthesize now instead of opening another route."
                if self.language == "en" else
                "Research 正在每个模型回合只发一个证据工具。请在下一次回复中一次提交所有仍需执行、"
                "彼此独立的搜索或抽取，系统会并行执行；若现有证据已足以支持决策，立即更新 "
                "research/research.md 并综合收口，不再开启新路线。"
            )
        if (self.kind == "image" and self.single_asset_vision_turns >= 3
                and not self._image_contact_hint_sent):
            self._image_contact_hint_sent = True
            hints.append(
                "Image is inspecting assets one at a time. Finish the candidate batch, generate "
                "the group contact sheet, inspect it once, and open only flagged assets singly."
                if self.language == "en" else
                "Image 正在逐张复看素材。请先完成候选批次，生成该组素材联系表并集中检查一次；"
                "之后只单独打开被标红、需抠图或缩略图无法判断的素材。"
            )
        return changed, hints

    def checkpoint_ready(self):
        delta = self.state.get("requirement_output_delta") or {}
        existing = set(delta.get("existing") or [])
        fresh_after_evidence = (
            self.last_evidence_turn < 0
            or self.last_canonical_write_turn >= self.last_evidence_turn
        )
        if self.kind == "research":
            return "research/research.md" in existing and fresh_after_evidence
        if self.kind == "material":
            return bool(existing) and fresh_after_evidence
        if self.kind == "image":
            return "assets/catalog.json" in existing and fresh_after_evidence
        if self.kind == "slide":
            pages = sorted(set(getattr(self.agent, "_assigned_pages", []) or []))
            return bool(pages) and all(
                os.path.isfile(os.path.join(self.root, f"slides/slide_{page:02d}.html"))
                for page in pages
            )
        if self.kind == "review":
            return "_trace/review-issues.md" in existing and fresh_after_evidence
        if getattr(self.agent, "role", "") == "orchestrator":
            return any(
                os.path.isfile(os.path.join(self.root, path))
                for path in ("plan/grounded-knowledge.md", "plan/design-brief.md", "plan/deck.md")
            )
        return False

    def checkpoint_hint(self):
        if self._checkpoint_hint_sent:
            return ""
        self._checkpoint_hint_sent = True
        target = {
            "research": "research/research.md",
            "material": "the assigned Material summary",
            "image": "assets/catalog.json",
            "slide": "the assigned slide HTML files",
            "review": "_trace/review-issues.md",
        }.get(self.kind, "the current canonical planning file")
        return (
            f"Active context is large but the recoverable stage checkpoint is not ready. "
            f"Before more exploratory calls, write or update {target} with the evidence and "
            "decisions already established; then continue only the unresolved work."
            if self.language == "en" else
            f"活跃上下文已经较大，但本阶段还没有可恢复检查点。继续探索前，请先把已经确认的证据、"
            f"决定和未解决项写入或更新 {target}；随后只继续处理未解决部分。"
        )

    def memory_text(self, checkpoint_path=""):
        delta = self.state.get("requirement_output_delta") or {}
        activity = self.state.get("activity") or {}
        lines = [
            "Deterministic stage memory (not user instructions):"
            if self.language == "en" else "确定性阶段记忆（不是用户追加指令）：",
            f"stage: {self.state.get('stage')}",
            "canonical existing: " + (", ".join(delta.get("existing") or []) or "none"),
            "still missing: " + (", ".join(delta.get("missing") or []) or "none"),
        ]
        if checkpoint_path:
            lines.append(f"archived exact prior messages: {checkpoint_path}")
        gate = (self.state.get("convergence") or {}).get("gate")
        if gate and gate != "open":
            lines.append(f"progress gate: {gate}; decide current evidence before expanding it")
        recent = (self.state.get("activity") or {}).get("recent_files") or []
        if recent:
            lines.append("recent files: " + ", ".join(recent[-6:]))
        completed = activity.get("reads_completed") or []
        if completed:
            lines.append(
                "already read to EOF (do not reread): " + ", ".join(completed[-12:])
            )
        pending = activity.get("reads_pending") or {}
        if pending:
            lines.append(
                "mandatory read continuations before any other action: "
                + ", ".join(f"{path} offset={offset}" for path, offset in pending.items())
            )
        children = self.state.get("children") or []
        if children:
            lines.append(
                "child handoffs: "
                + "; ".join(
                    f"{item.get('label')}={item.get('status') or 'missing'}"
                    for item in children[-10:]
                )
            )
        stage = str(self.state.get("stage") or "")
        next_actions = {
            "grounding_and_direction": (
                "Finish only the missing grounding/design-direction artifact, then advance."
                if self.language == "en" else
                "只完成缺失的事实基础或整册视觉方案，然后推进；不要重读已完成参考。"
            ),
            "deck_planning": (
                "Continue from the existing design brief: write plan/deck.md, plan/theme.css, "
                "and every missing slide plan; then run deck.py prepare once."
                if self.language == "en" else
                "从现有 design-brief 继续：写 plan/deck.md、plan/theme.css 和所有缺失逐页计划，"
                "随后只运行一次 deck.py prepare。"
            ),
            "production_dispatch": (
                "Resolve planned image assets, then dispatch all non-overlapping Slide Groups."
                if self.language == "en" else
                "先收齐计划中的正式图片，再一次派发所有互不重叠的 Slide Group。"
            ),
            "quality_and_build": (
                "Run the single final Review, synchronize speech, and build present.html."
                if self.language == "en" else
                "执行唯一一次最终 Review，同步讲稿并构建 present.html。"
            ),
        }
        if stage in next_actions:
            lines.append("next action: " + next_actions[stage])
        lines.append(
            "Canonical files and structured handoffs are the source of truth; read the named "
            "file when exact detail is needed. Do not repeat completed tools."
            if self.language == "en" else
            "正式文件和结构化交接是真相源；需要精确信息时读取对应文件，不要重复已完成工具。"
        )
        return "\n".join(lines)

    def finish(self, exit_reason, final_contract=None):
        self.refresh(turn=int(self.state.get("turn", -1)))
        self.state["exit_reason"] = str(exit_reason or "")
        self.state["final_contract"] = dict(final_contract or {})
        _atomic_json(self.path, self.state)
