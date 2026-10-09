"""维护 canonical paths 与由真实文件哈希派生的页面生命周期。

HTML 文件存在只表示 scaffolded。页面必须依次提交 authored 源、记录同源 PNG、完成
检查并显式 ready；HTML、资源或无法证明同源的 PNG 改变都会使下游状态失效。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence
import uuid
from .locking import workspace_lock

from runtime.shared_objects import workspace_skill_module
from runtime.workspace_paths import (
    WORKSPACE_PATHS_SCHEMA,
    WORKSPACE_LAYOUT_VERSION,
    WorkspacePathError,
    WorkspacePaths,
)


WORKSPACE_STATE_SCHEMA = "mural.workspace-state.v1"


class WorkspaceStateError(WorkspacePathError):
    """表示工作区文件与页面生命周期合同不一致。"""

    def __init__(self, detail: str, *, diagnostics: Mapping[str, object] | None = None) -> None:
        self.diagnostics = diagnostics
        super().__init__(detail)


class PageRepairError(WorkspaceStateError):
    """携带写入前拒绝修复的稳定原因和当前产物证据。"""

    def __init__(self, code: str, diagnostics: Mapping[str, object]) -> None:
        self.code = code
        super().__init__(str(diagnostics["reason"]), diagnostics=dict(diagnostics))


class PageLifecycle(str, Enum):
    """列出 WebUI 与导出可以消费的页面机械状态。"""

    MISSING = "missing"
    SCAFFOLDED = "scaffolded"
    AUTHORED = "authored"
    RENDERED = "rendered"
    INSPECTED = "inspected"
    READY = "ready"


@dataclass(frozen=True)
class PageSnapshot:
    """保存从当前文件和持久交易共同派生的页面状态。"""

    page: int
    page_uid: str
    lifecycle: PageLifecycle
    html_path: str
    png_path: str
    html_sha256: str
    png_sha256: str
    authored_html_sha256: str
    rendered_html_sha256: str
    rendered_png_sha256: str
    inspected_html_sha256: str
    inspected_png_sha256: str
    stale_png: bool
    inspection_id: str = ""

    def to_dict(self) -> dict[str, object]:
        """把页面快照转换为 WebUI、QC 与导出共用的 JSON 对象。"""
        return {
            "page": self.page,
            "page_uid": self.page_uid,
            "lifecycle": self.lifecycle.value,
            "html_path": self.html_path,
            "png_path": self.png_path,
            "html_sha256": self.html_sha256,
            "png_sha256": self.png_sha256,
            "authored_html_sha256": self.authored_html_sha256,
            "rendered_html_sha256": self.rendered_html_sha256,
            "rendered_png_sha256": self.rendered_png_sha256,
            "inspected_html_sha256": self.inspected_html_sha256,
            "inspected_png_sha256": self.inspected_png_sha256,
            "stale_png": self.stale_png,
            "inspection_id": self.inspection_id,
        }


class WorkspaceState:
    """原子记录页面交易，并始终从磁盘重新派生有效生命周期。"""

    def __init__(self, workspace: str | Path) -> None:
        """创建标准 v2 目录并把 manifest 作为第一个持久文件写入。"""
        self.paths = WorkspacePaths.from_root(workspace)
        self.paths.initialize_directories()
        self.path = self.paths.state / "workspace.json"
        self._lock = workspace_lock(self.paths.root)
        self._page_edits: dict[int, tuple[str, dict[str, object]]] = {}
        # 仅复用本次运行的固定程序；CSS、字体授权与内容哈希仍实时读取。
        self._geometry_reader: Callable[[Path], dict[str, object]] | None = None
        self._config_module = None
        self.font_resolver: Callable[[str], tuple[Path, str]] | None = None
        with self._lock:
            from .revisions import recover_revision
            recover_revision(self.paths.root)
            state = self._read_state()
            if not self.path.exists():
                self._write_state(state)

    @property
    def deck_id(self) -> str:
        """返回持久工作区身份。"""
        with self._lock:
            return str(self._read_state()["deck_id"])

    def require_geometry(self) -> dict[str, object]:
        with self._lock:
            if self._geometry_reader is None:
                self._geometry_reader = workspace_skill_module(self.paths.root, "geometry").read_geometry
            return self._geometry_reader(self.paths.root)

    def creation_config(self, *, require_complete: bool = False) -> dict[str, object]:
        """只缓存固定解析程序；当前创作设置始终从 config.json 读取。"""
        with self._lock:
            if self._config_module is None:
                self._config_module = workspace_skill_module(self.paths.root, "deck_config")
            return self._config_module.read_config(self.paths.root, require_complete=require_complete)

    def presentation_config(self) -> dict[str, object]:
        return {"presentation_language": self.creation_config()["presentation_language"]}

    def is_page_author(self, page: int, owner_id: str) -> bool:
        from owner.registry import OwnerRegistry

        owner = OwnerRegistry(self.paths.root, deck_id=self.deck_id).page_owner(page)
        return owner is not None and owner.owner_id == owner_id

    def register_pages(self, pages: Sequence[int]) -> tuple[PageSnapshot, ...]:
        """为显式页码创建稳定技术身份，不读取 Plan 分组。"""
        numbers = tuple(sorted(set(_page_number(page) for page in pages)))
        if not numbers:
            return ()
        with self._lock:
            state = self._read_state()
            records = _page_records(state)
            for number in numbers:
                key = str(number)
                if key in records:
                    # 已存在:若之前被结构修订 retire 过、现在又显式注册(如 HTML 回填),
                    # 清除 retired 标记让它重新活跃;其余机械状态由磁盘 hash 派生,不动。
                    if records[key].get("retired"):
                        records[key].pop("retired", None)
                        self._append_event(state, "page_reinstated", number)
                    continue
                records[key] = self._new_page_record(state, number)
                self._append_event(state, "page_registered", number)
            state["pages"] = records
            self._write_state(state)
        return tuple(self.page(number) for number in numbers)

    def register_scaffold(self, page: int) -> PageSnapshot:
        """记录已存在 HTML scaffold，但绝不把它提升为 authored。"""
        number = _page_number(page)
        html_path = self.paths.page_html(number)
        if not html_path.is_file():
            raise WorkspaceStateError(f"page HTML is missing: {html_path}")
        self.register_pages([number])
        with self._lock:
            state = self._read_state()
            records = _page_records(state)
            record = records[str(number)]
            record["scaffold_html_sha256"] = file_sha256(html_path)
            record["last_transition"] = PageLifecycle.SCAFFOLDED.value
            records[str(number)] = record
            state["pages"] = records
            self._append_event(state, "page_scaffolded", number)
            self._write_state(state)
        return self.page(number)

    def reconcile_pages(self, present: Sequence[int]) -> tuple[int, ...]:
        """把页面记录对齐到磁盘实际存在的页集。

        ``register_pages`` 只增不减,而 Skill 的结构修订(delete/move-page)会让某些
        页号在磁盘上消失。此方法以 ``present`` 为准:不在其中的活跃页记录标记
        ``retired``(保留记录做审计,不物理删除——与"追加 + 从磁盘派生"的哲学一致);
        若某已 retired 的页号又重新出现(如 insert 回填同号),清除其 retired 标记。
        返回本次新标记为 retired 的页号,便于上层记录/日志。
        """
        target = {_page_number(page) for page in present}
        with self._lock:
            state = self._read_state()
            records = _page_records(state)
            newly_retired: list[int] = []
            reinstated: list[int] = []
            for key, record in records.items():
                number = int(record.get("page", int(key)))
                if number in target:
                    if record.get("retired"):
                        record.pop("retired", None)
                        reinstated.append(number)
                        self._append_event(state, "page_reinstated", number)
                elif not record.get("retired"):
                    record["retired"] = True
                    newly_retired.append(number)
                    self._append_event(state, "page_retired", number)
            if newly_retired or reinstated:
                state["pages"] = records
                self._write_state(state)
            return tuple(sorted(newly_retired))

    def mark_authored(self, page: int, *, html_sha256: str) -> PageSnapshot:
        """确认当前 HTML；只有内容变化才清除旧渲染和检查状态。"""
        number = _page_number(page)
        self.register_pages([number])
        html_path = self.paths.page_html(number)
        actual = file_sha256(html_path)
        if not actual or actual != str(html_sha256):
            raise WorkspaceStateError(
                f"authored HTML hash mismatch for page {number}: {actual!r}"
            )
        with self._lock:
            state = self._read_state()
            records = _page_records(state)
            record = records[str(number)]
            if record.get("authored_html_sha256") == actual:
                return self.page(number)
            edit = self._page_edits.get(number)
            if edit is not None:
                revision_key, budget = edit
                record.setdefault("repair_budgets", {})[revision_key] = copy.deepcopy(budget)
            record.update(
                {
                    "authored_html_sha256": actual,
                    "last_render": None,
                    "last_inspection": None,
                    "ready_pair": None,
                    "last_transition": PageLifecycle.AUTHORED.value,
                }
            )
            records[str(number)] = record
            state["pages"] = records
            self._append_event(state, "page_authored", number, html_sha256=actual)
            self._write_state(state)
        return self.page(number)

    @contextmanager
    def page_edit(
        self, page: int, *, content_sha256: str, repair_reason: str,
        revision_key: str,
    ) -> Iterator[None]:
        """同页写入和提交持锁；批次与 authored hash 同笔保存，不另建计数日志。"""
        number = _page_number(page)
        with self._lock:
            state = self._read_state()
            original = copy.deepcopy(_page_records(state).get(str(number)))
            html_hash = file_sha256(self.paths.page_html(number))
            if (original is None or html_hash == content_sha256 or not (
                original.get("inspected_once") or original.get("last_inspection")
                or original.get("repair_budgets")
            )):
                yield
                return
            budget = copy.deepcopy(original.get("repair_budgets", {}).get(
                revision_key, {"batches": [], "open": False}))
            if not budget["open"]:
                reason = str(repair_reason).strip()
                diagnostics = {
                    "page": number, "page_uid": original["page_uid"],
                    "html_sha256": html_hash,
                    "png_sha256": file_sha256(self.paths.page_png(number)),
                    "revision_key": revision_key, "batches_used": len(budget["batches"]),
                    "repair_reason": reason,
                }
                if not reason:
                    raise PageRepairError("repair_reason_required", {**diagnostics,
                        "reason": "开始修复已检查页面须提供真实 repair_reason；可直接复用已有问题或修复委派中的依据，不限制说明长度。"})
                budget["batches"].append({
                    "repair_reason": reason, "base_html_sha256": html_hash,
                    "base_png_sha256": diagnostics["png_sha256"],
                })
                budget["open"] = True
                self._page_edits[number] = (revision_key, budget)
            try:
                yield
            except BaseException:
                current = self._read_state()
                if _page_records(current).get(str(number)) != original:
                    current["pages"][str(number)] = original
                    self._append_event(current, "page_edit_rolled_back", number,
                        page_uid=original["page_uid"])
                    self._write_state(current)
                raise
            finally:
                self._page_edits.pop(number, None)

    def mark_rendered(
        self,
        page: int,
        *,
        html_sha256: str,
        png_sha256: str,
        presentation_config: Mapping[str, object] | None = None,
        preserve_equivalent_inspection: bool = False,
    ) -> PageSnapshot:
        """记录 PNG 与当前已提交 HTML 的精确同源哈希对。"""
        number = _page_number(page)
        html_actual = file_sha256(self.paths.page_html(number))
        png_actual = file_sha256(self.paths.page_png(number))
        if not html_actual or html_actual != str(html_sha256):
            raise WorkspaceStateError(f"render HTML hash mismatch for page {number}")
        if not png_actual or png_actual != str(png_sha256):
            raise WorkspaceStateError(f"render PNG hash mismatch for page {number}")
        with self._lock:
            current_config = self.presentation_config()
            if presentation_config is not None and dict(presentation_config) != current_config:
                raise WorkspaceStateError(f"page {number} presentation language changed during render")
            state = self._read_state()
            record = _required_page_record(state, number)
            if record.get("authored_html_sha256") != html_actual:
                raise WorkspaceStateError(
                    f"page {number} source must be committed before render"
                )
            if record.get("last_render") == {
                "html_sha256": html_actual, "png_sha256": png_actual,
                "presentation_config": current_config,
            }:
                return self.page(number)
            previous_render = record.get("last_render")
            previous_png = (
                str(previous_render.get("png_sha256") or "")
                if isinstance(previous_render, dict)
                else ""
            )
            prior_inspection = record.get("last_inspection")
            prior_ready = record.get("ready_pair")
            preserve_review = bool(
                preserve_equivalent_inspection
                and isinstance(previous_render, dict)
                and previous_render.get("html_sha256") == html_actual
                and previous_render.get("presentation_config") == current_config
                and previous_png
                and previous_png != png_actual
                and isinstance(prior_inspection, dict)
                and prior_inspection.get("html_sha256") == html_actual
                and prior_inspection.get("png_sha256") == previous_png
            )
            preserved_from = {
                "previous_png_sha256": previous_png,
                "current_png_sha256": png_actual,
                "basis": "same_html_resources_canvas_and_presentation",
            }
            next_inspection = None
            next_ready = None
            next_transition = PageLifecycle.RENDERED.value
            if preserve_review:
                next_inspection = copy.deepcopy(dict(prior_inspection))
                next_inspection.update(
                    png_sha256=png_actual,
                    equivalent_rerender=preserved_from,
                )
                next_transition = PageLifecycle.INSPECTED.value
                if isinstance(prior_ready, dict) and (
                    prior_ready.get("html_sha256") == html_actual
                    and prior_ready.get("png_sha256") == previous_png
                ):
                    next_ready = {
                        "html_sha256": html_actual,
                        "png_sha256": png_actual,
                        "equivalent_rerender": preserved_from,
                    }
                    next_transition = PageLifecycle.READY.value
            record.update(
                {
                    "last_render": {
                        "html_sha256": html_actual,
                        "png_sha256": png_actual,
                        "presentation_config": current_config,
                    },
                    "last_inspection": next_inspection,
                    "ready_pair": next_ready,
                    "last_transition": next_transition,
                }
            )
            records = _page_records(state)
            records[str(number)] = record
            state["pages"] = records
            self._append_event(
                state,
                "page_rendered_equivalent" if preserve_review else "page_rendered",
                number,
                html_sha256=html_actual,
                png_sha256=png_actual,
                **({"equivalent_rerender": preserved_from} if preserve_review else {}),
            )
            self._write_state(state)
        return self.page(number)

    def mark_inspected(
        self, page: int, *, inspection_id: str,
        expected: Mapping[str, object] | None = None,
        receipt: Mapping[str, object] | None = None,
    ) -> PageSnapshot:
        """确认视觉工具消费的是当前 HTML/PNG 哈希对。"""
        number = _page_number(page)
        evidence_id = str(inspection_id).strip()
        if not evidence_id:
            raise WorkspaceStateError("inspection_id must not be empty")
        with self._lock:
            state = self._read_state()
            record = _required_page_record(state, number)
            render = record.get("last_render")
            html_hash = file_sha256(self.paths.page_html(number))
            png_hash = file_sha256(self.paths.page_png(number))
            if expected is not None and expected.get("page_uid") != record.get("page_uid"):
                raise WorkspaceStateError(f"page {number} inspection identity/version changed before consumption",
                    diagnostics={"code": "page_identity_changed"})
            if expected is not None and any(
                expected.get(key) != actual for key, actual in (
                    ("page_uid", record.get("page_uid")),
                    ("html_sha256", html_hash), ("png_sha256", png_hash),
                )
            ):
                raise WorkspaceStateError(f"page {number} inspection version changed before consumption",
                    diagnostics={"code": "inspection_version_changed"})
            if (receipt is not None and receipt.get("presentation_config") != self.presentation_config()):
                raise WorkspaceStateError(f"page {number} presentation language changed before inspection consumption",
                    diagnostics={"code": "presentation_language_changed"})
            if not isinstance(render, dict) or (
                render.get("html_sha256") != html_hash
                or render.get("png_sha256") != png_hash
                or render.get("presentation_config") != self.presentation_config()
            ):
                raise WorkspaceStateError(
                    f"page {number} PNG is stale or was not rendered"
                )
            ready_pair = record.get("ready_pair")
            preserve_ready = isinstance(ready_pair, dict) and (
                ready_pair.get("html_sha256") == html_hash
                and ready_pair.get("png_sha256") == png_hash
            )
            prior_inspection = record.get("last_inspection")
            observation_only = receipt is not None and not self.is_page_author(number, str(receipt.get("owner_id") or ""))
            if observation_only:
                self._save_inspection_receipt(state, receipt)
                self._append_event(state, "page_observed", number, inspection_id=evidence_id)
                self._write_state(state)
                return self._derive_snapshot(record)
            record["inspected_once"] = True
            for budget in record.get("repair_budgets", {}).values():
                if budget["open"]:
                    budget["open"] = False
                    budget["batches"][-1].update(inspection_id=evidence_id,
                        html_sha256=html_hash, png_sha256=png_hash)
            record.update(
                {
                    "last_inspection": prior_inspection if observation_only else {
                        "inspection_id": evidence_id,
                        "html_sha256": html_hash,
                        "png_sha256": png_hash,
                    },
                    "ready_pair": ready_pair if preserve_ready else None,
                    "last_transition": (
                        PageLifecycle.READY.value
                        if preserve_ready
                        else PageLifecycle.INSPECTED.value
                    ),
                }
            )
            records = _page_records(state)
            records[str(number)] = record
            state["pages"] = records
            if receipt is not None:
                self._save_inspection_receipt(state, receipt)
            self._append_event(
                state,
                "page_observed" if observation_only else (
                    "inspection_reused" if receipt and receipt.get("reused_from_inspection_id") else "page_inspected"),
                number,
                inspection_id=evidence_id,
                ready_preserved=preserve_ready,
                **({"reused_from_inspection_id": receipt["reused_from_inspection_id"]}
                   if receipt and receipt.get("reused_from_inspection_id") else {}),
            )
            self._write_state(state)
        return self.page(number)

    def inspection_receipt(
        self, scope_key: str, members: Sequence[Mapping[str, object]],
    ) -> dict[str, object] | None:
        """复用同角色/目的/UID 的已消费像素；调用者先验证当前渲染依赖回执。"""
        with self._lock:
            state = self._read_state()
            identity = [str(scope_key), [[item["page_uid"], item["png_sha256"]] for item in members]]
            key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
            receipt = state.get("inspection_receipts", {}).get(key)
            if not isinstance(receipt, dict):
                return None
            prior = receipt.get("members") or []
            if len(prior) != len(members) or any(
                old.get(key) != current.get(key)
                for old, current in zip(prior, members)
                for key in ("page", "page_uid", "png_sha256")
            ):
                return None
            for member in members:
                page = self.page(int(member["page"]))
                if page.lifecycle not in {PageLifecycle.RENDERED, PageLifecycle.INSPECTED, PageLifecycle.READY} or any(
                    member.get(key) != getattr(page, key)
                    for key in ("page_uid", "html_sha256", "png_sha256")
                ):
                    return None
            if file_sha256(self.paths.resolve(str(receipt["path"]))) != receipt.get("sha256"):
                return None
            return copy.deepcopy(receipt)

    def cached_pixel_replay(
        self, descriptor: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Find this Owner's consumed current pixels without creating a new review purpose."""
        with self._lock:
            state = self._read_state()
            order = {str(event.get("inspection_id") or ""): index
                     for index, event in enumerate(state.get("events", ()))}
            receipts = sorted(state.get("inspection_receipts", {}).values(),
                              key=lambda receipt: order.get(str(receipt.get("inspection_id") or ""), -1)
                              if isinstance(receipt, dict) else -1, reverse=True)
            for receipt in receipts:
                if not isinstance(receipt, dict) or any(
                    receipt.get(key) != descriptor.get(key)
                    for key in ("kind", "owner_id", "path", "sha256", "presentation_config")
                ):
                    continue
                if descriptor.get("review_stage") in {"final", "blocking_recheck"} and receipt.get("review_stage") not in {"final", "blocking_recheck"}:
                    continue
                current = self.inspection_receipt(str(receipt.get("scope_key") or ""), descriptor["members"])
                if current is not None and current.get("members") == descriptor.get("members"):
                    return current
            return None

    def inspection_attempt_count(
        self, *, kind: str, page: int | None = None, page_uid: str = "",
        png_sha256: str = "", review_stages: Sequence[str] | None = None,
        owner_id: str = "", path: str = "",
        include_groups: bool = False,
    ) -> int:
        """Count consumed inspection receipts for the same current visual target."""
        normalized_stages = {
            str(stage).strip().lower()
            for stage in (review_stages or ())
            if str(stage).strip()
        }
        with self._lock:
            state = self._read_state()
            seen: set[str] = set()
            for receipt in state.get("inspection_receipts", {}).values():
                if not isinstance(receipt, dict):
                    continue
                target = receipt
                if include_groups and kind == "page" and receipt.get("kind") == "group" and page is not None:
                    target = next((member for member in receipt.get("members", ())
                                   if int(member.get("page") or 0) == int(page)), {})
                elif receipt.get("kind") != kind:
                    continue
                if owner_id and receipt.get("owner_id") != owner_id:
                    continue
                if path and receipt.get("path") != path:
                    continue
                if normalized_stages and str(receipt.get("review_stage") or "").lower() not in normalized_stages:
                    continue
                if page is not None and int(target.get("page") or 0) != int(page):
                    continue
                if page_uid and target.get("page_uid") != page_uid:
                    continue
                if png_sha256 and (
                    target.get("png_sha256") != png_sha256
                    and target.get("sha256") != png_sha256
                ):
                    continue
                inspection_id = str(receipt.get("reused_from_inspection_id") or receipt.get("inspection_id") or "")
                if inspection_id:
                    seen.add(inspection_id)
            return len(seen)

    def invalidate_previous_author_review(self, page: int, owner_id: str) -> None:
        """显式换作者保留历史回执与有效渲染，但要求新作者实际消费当前页面。"""
        with self._lock:
            state = self._read_state()
            record = state.get("pages", {}).get(str(page))
            if not record or not record.get("last_inspection"):
                return
            evidence_id = record["last_inspection"]["inspection_id"]
            if any(receipt.get("inspection_id") == evidence_id and receipt.get("owner_id") == owner_id
                   for receipt in state.get("inspection_receipts", {}).values()):
                return
            record.update(last_inspection=None, ready_pair=None)
            self._append_event(state, "page_author_changed", page, owner_id=owner_id,
                               previous_inspection_id=evidence_id)
            self._write_state(state)

    def group_inspection_receipt(self, owner_id: str, pages: Sequence[int]) -> dict[str, object] | None:
        """查找原作者已消费的同组像素；完成边界仍须校验当前资源依赖。"""
        with self._lock:
            snapshots = [self.page(page) for page in pages]
            if not snapshots or any(page.lifecycle not in {PageLifecycle.INSPECTED, PageLifecycle.READY}
                                    for page in snapshots):
                return None
            members = [{key: getattr(page, key) for key in ("page", "page_uid", "html_sha256", "png_sha256")}
                       for page in snapshots]
            config = self.presentation_config()
            for receipt in reversed(list(self._read_state().get("inspection_receipts", {}).values())):
                if (receipt.get("kind") != "group" or receipt.get("owner_id") != owner_id
                        or receipt.get("presentation_config") != config
                        or [member["page"] for member in receipt["members"]] != list(pages)):
                    continue
                current = self.inspection_receipt(str(receipt["scope_key"]), members)
                if current is not None:
                    return current
            return None

    def record_group_inspection(self, receipt: Mapping[str, object]) -> None:
        """联系表只提交组范围回执，原子比较全部成员，不替代单页检查。"""
        with self._lock:
            if receipt.get("presentation_config") != self.presentation_config():
                raise WorkspaceStateError("group presentation language changed before inspection consumption",
                    diagnostics={"code": "presentation_language_changed"})
            state = self._read_state()
            for member in receipt["members"]:
                page = self.page(int(member["page"]))
                if member.get("page_uid") != page.page_uid:
                    raise WorkspaceStateError("group inspection member identity/version changed before consumption",
                        diagnostics={"code": "page_identity_changed"})
                if page.lifecycle not in {PageLifecycle.RENDERED, PageLifecycle.INSPECTED, PageLifecycle.READY} or any(
                    member.get(key) != getattr(page, key)
                    for key in ("page_uid", "html_sha256", "png_sha256")
                ):
                    raise WorkspaceStateError("group inspection member version changed before consumption",
                        diagnostics={"code": "inspection_version_changed"})
            if file_sha256(self.paths.resolve(str(receipt["path"]))) != receipt.get("sha256"):
                raise WorkspaceStateError("group inspection image version changed before consumption",
                    diagnostics={"code": "inspection_version_changed"})
            self._save_inspection_receipt(state, receipt)
            self._append_event(state, "inspection_reused" if receipt.get("reused_from_inspection_id") else "group_inspected", 0,
                inspection_id=receipt["inspection_id"], scope_key=receipt["scope_key"],
                members=receipt["members"],
                **({"reused_from_inspection_id": receipt["reused_from_inspection_id"]}
                   if receipt.get("reused_from_inspection_id") else {}))
            self._write_state(state)

    def record_asset_inspection(self, receipt: Mapping[str, object]) -> None:
        """保存实际消费的素材图版本，不修改任何页面作者检查或生命周期。"""
        with self._lock:
            if file_sha256(self.paths.resolve(str(receipt["path"]))) != receipt.get("sha256"):
                raise WorkspaceStateError("asset inspection image changed before consumption",
                    diagnostics={"code": "inspection_version_changed"})
            state = self._read_state()
            self._save_inspection_receipt(state, receipt)
            self._append_event(state, "asset_inspected", 0, inspection_id=receipt["inspection_id"],
                path=receipt["path"], sha256=receipt["sha256"], owner_id=receipt["owner_id"])
            self._write_state(state)

    @staticmethod
    def _save_inspection_receipt(state: dict[str, object], receipt: Mapping[str, object]) -> None:
        receipts = dict(state.get("inspection_receipts") or {})
        identity = [str(receipt["scope_key"]), [[item["page_uid"], item["png_sha256"]] for item in receipt["members"]]]
        key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        receipts[key] = copy.deepcopy(dict(receipt))
        state["inspection_receipts"] = receipts

    def assert_ready_resources(self, pages: Sequence[int]) -> None:
        """仅在 ready / commit 边界重验现有渲染回执，不增加状态读取成本。"""
        from .artifacts import ArtifactStateError, ArtifactStore

        numbers = tuple(_page_number(page) for page in pages)
        with self._lock:
            try:
                artifacts = ArtifactStore(self.paths.root, workspace_state=self, create=False)
            except (ArtifactStateError, WorkspaceStateError, ValueError, OSError) as exc:
                raise WorkspaceStateError(
                    f"{exc}; restore missing resources if needed, then inspect "
                    f"the affected pages again before ready: {list(numbers)}"
                ) from exc
            failures = {}
            first_error = None
            # One batch shares a canvas; mutable imports are measured again in the next batch.
            canvas = None
            canvas_error = None
            if numbers:
                try:
                    canvas = self.require_geometry()
                except (WorkspaceStateError, ValueError, OSError) as exc:
                    canvas_error = exc
            for number in numbers:
                try:
                    if canvas_error is not None:
                        raise canvas_error
                    artifacts.validate_render_receipt(number, _canvas=canvas)
                except (ArtifactStateError, WorkspaceStateError, ValueError, OSError) as exc:
                    if first_error is None:
                        first_error = exc
                    failures[number] = {"code": "render_validation_failed", "detail": str(exc),
                        **(getattr(exc, "diagnostics", None) or {})}
            if failures:
                page_issues = self.inspection_diagnostics(
                    [self.page(number) for number in failures], validation_errors=failures)
                raise WorkspaceStateError(
                    f"{first_error}; restore missing resources if needed, then inspect "
                    f"the affected pages again before ready: {list(failures)}",
                    diagnostics={"page_issues": page_issues},
                ) from first_error

    def mark_ready(self, page: int) -> PageSnapshot:
        """把当前已检查哈希对提升为 ready，不解释 Agent 自由文本。"""
        return self.mark_ready_pages((page,))[0]

    def mark_ready_pages(self, pages: Sequence[int]) -> tuple[PageSnapshot, ...]:
        """同一锁内校验全部成员后一次提交；已 READY 的成员也重验，不提供跳过检查入口。"""
        numbers = tuple(dict.fromkeys(_page_number(page) for page in pages))
        if not numbers:
            return ()
        with self._lock:
            state = self._read_state()
            records = _page_records(state)
            snapshots = [self._derive_snapshot(_required_page_record(state, number)) for number in numbers]
            incomplete = [page for page in snapshots if page.lifecycle not in {PageLifecycle.INSPECTED, PageLifecycle.READY}]
            if incomplete:
                raise WorkspaceStateError(f"page {incomplete[0].page} must inspect the current render before ready",
                    diagnostics={"page_issues": self.inspection_diagnostics(incomplete)})
            self.assert_ready_resources(numbers)
            changed = [page for page in snapshots if page.lifecycle != PageLifecycle.READY]
            for page in changed:
                records[str(page.page)].update(
                    ready_pair={"html_sha256": page.html_sha256, "png_sha256": page.png_sha256},
                    last_transition=PageLifecycle.READY.value,
                )
                self._append_event(state, "page_ready", page.page)
            if changed:
                self._write_state(state)
            return tuple(self._derive_snapshot(records[str(number)]) for number in numbers)

    def page(self, page: int) -> PageSnapshot:
        """读取单页记录，并依据当前文件哈希派生有效状态。"""
        number = _page_number(page)
        with self._lock:
            state = self._read_state()
            record = _required_page_record(state, number)
            return self._derive_snapshot(record)

    def pages(self) -> tuple[PageSnapshot, ...]:
        """返回所有活跃页面的有序有效快照(跳过已 retired 的结构修订遗留记录)。"""
        with self._lock:
            state = self._read_state()
            records = _page_records(state)
            return tuple(
                self._derive_snapshot(records[key])
                for key in sorted(records, key=int)
                if not records[key].get("retired")
            )

    def inspection_diagnostics(
        self, pages: Sequence[PageSnapshot], *,
        validation_errors: Mapping[int, Mapping[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        """失败时解释当前回执缺口；资源原因复用校验器结果，不再扫描一遍资源。"""
        with self._lock:
            state = self._read_state()
            config = self.presentation_config()
            issues = []
            for page in pages:
                record = _required_page_record(state, page.page)
                render = record.get("last_render")
                inspected_before = bool(record.get("inspected_once") or record.get("last_inspection"))
                reasons = []
                if not page.html_sha256:
                    reasons.append({"code": "source_missing", "detail": "页面 HTML 文件不存在。"})
                elif not page.authored_html_sha256:
                    reasons.append({"code": "source_not_authored", "detail": "HTML 尚未通过作者写入事务提交，骨架不代表已制作。"})
                elif (page.html_sha256 != page.authored_html_sha256
                      or (render and page.html_sha256 != page.rendered_html_sha256)
                      or (not render and inspected_before and record.get("last_transition") == PageLifecycle.AUTHORED.value)):
                    reasons.append({"code": "html_changed", "detail": "源码已改变；此前渲染和检查不能证明当前 HTML 有效。"})
                if not render:
                    reasons.append({"code": "render_missing", "detail": "当前源码尚无有效像素回执；请用 inspect 自动渲染并查看。"})
                else:
                    previous_config = render.get("presentation_config")
                    if previous_config != config:
                        reasons.append({"code": "presentation_language_changed",
                            "before": (previous_config or {}).get("presentation_language"),
                            "after": config["presentation_language"],
                            "detail": "渲染时的呈现语言配置与当前 config.json 不同；当前版本须重新 inspect，不能沿用旧版本回执。"})
                    if not page.png_sha256:
                        reasons.append({"code": "png_missing", "detail": "渲染 PNG 文件不存在。"})
                    elif page.png_sha256 != page.rendered_png_sha256:
                        reasons.append({"code": "png_changed", "detail": "当前 PNG 与渲染回执的哈希不同。"})
                if not inspected_before:
                    reasons.append({"code": "never_inspected", "detail": "尚无原作者已消费页面像素的检查记录；后台渲染或他人复核不代替作者检查。"})
                elif page.lifecycle == PageLifecycle.RENDERED:
                    reasons.append({"code": "inspection_required_after_render",
                        "detail": "此前版本已检查，但当前渲染尚未由原作者消费并确认；须检查当前 PNG。"})
                failure = (validation_errors or {}).get(page.page)
                if failure and not any(reason["code"] == failure["code"] for reason in reasons):
                    reasons.append(dict(failure))
                if reasons:
                    issues.append({"page": page.page, "page_uid": page.page_uid,
                        "html_path": page.html_path, "png_path": page.png_path,
                        "lifecycle": page.lifecycle.value, "previously_inspected": inspected_before,
                        "reasons": reasons})
            return issues

    def manifest(self) -> dict[str, object]:
        """生成供 WebUI、Renderer 与导出共同读取的工作区投影。"""
        canvas = self.require_geometry() if self.paths.path("base_css").is_file() else None
        return {
            "schema": "mural.workspace-manifest.v1",
            "deck_id": self.deck_id,
            "layout_version": self.paths.layout_version,
            "paths": dict(self.paths.paths),
            "canvas": canvas,
            "pages": [snapshot.to_dict() for snapshot in self.pages()],
        }

    def _new_page_record(
        self, state: Mapping[str, object], page: int
    ) -> dict[str, object]:
        """为新页生成稳定 UID 与 canonical 文件路径。"""
        suffix = uuid.uuid4().hex[:16]
        return {
            "page": page,
            "page_uid": f"pg_{page:02d}_{suffix}",
            "html_path": self.paths.relative(self.paths.page_html(page)),
            "png_path": self.paths.relative(self.paths.page_png(page)),
            "scaffold_html_sha256": "",
            "authored_html_sha256": "",
            "last_render": None,
            "last_inspection": None,
            "ready_pair": None,
            "last_transition": PageLifecycle.MISSING.value,
        }

    def _derive_snapshot(self, record: Mapping[str, object]) -> PageSnapshot:
        """比较当前文件与交易哈希，计算唯一有效生命周期。"""
        html_path = self.paths.resolve(str(record["html_path"]))
        png_path = self.paths.resolve(str(record["png_path"]))
        html_hash = file_sha256(html_path)
        png_hash = file_sha256(png_path)
        authored = str(record.get("authored_html_sha256") or "")
        render = record.get("last_render")
        config_stale = isinstance(render, dict) and render.get("presentation_config") != self.presentation_config()
        inspection = record.get("last_inspection")
        ready = record.get("ready_pair")
        rendered_html = str(render.get("html_sha256") or "") if isinstance(render, dict) else ""
        rendered_png = str(render.get("png_sha256") or "") if isinstance(render, dict) else ""
        inspected_html = (
            str(inspection.get("html_sha256") or "") if isinstance(inspection, dict) else ""
        )
        inspected_png = (
            str(inspection.get("png_sha256") or "") if isinstance(inspection, dict) else ""
        )
        stale_png = bool(
            png_hash
            and (
                not isinstance(render, dict)
                or rendered_html != html_hash
                or rendered_png != png_hash
                or config_stale
            )
        )
        if not html_hash:
            lifecycle = PageLifecycle.MISSING
        elif not authored or authored != html_hash:
            lifecycle = PageLifecycle.SCAFFOLDED
        elif (
            not isinstance(render, dict)
            or rendered_html != html_hash
            or not png_hash
            or rendered_png != png_hash
            or config_stale
        ):
            lifecycle = PageLifecycle.AUTHORED
        elif not isinstance(inspection, dict) or (
            inspected_html != html_hash or inspected_png != png_hash
        ):
            lifecycle = PageLifecycle.RENDERED
        elif isinstance(ready, dict) and (
            ready.get("html_sha256") == html_hash
            and ready.get("png_sha256") == png_hash
        ):
            lifecycle = PageLifecycle.READY
        else:
            lifecycle = PageLifecycle.INSPECTED
        return PageSnapshot(
            page=int(record["page"]),
            page_uid=str(record["page_uid"]),
            lifecycle=lifecycle,
            html_path=str(record["html_path"]),
            png_path=str(record["png_path"]),
            html_sha256=html_hash,
            png_sha256=png_hash,
            authored_html_sha256=authored,
            rendered_html_sha256=rendered_html,
            rendered_png_sha256=rendered_png,
            inspected_html_sha256=inspected_html,
            inspected_png_sha256=inspected_png,
            stale_png=stale_png,
            inspection_id=(
                str(inspection.get("inspection_id") or "")
                if isinstance(inspection, dict)
                else ""
            ),
        )

    def _append_event(
        self,
        state: dict[str, object],
        event_type: str,
        page: int,
        **fields: object,
    ) -> None:
        """追加一个只含机械事实的页面事件。"""
        sequence = int(state.get("sequence") or 0) + 1
        events = state.get("events") or []
        if not isinstance(events, list):
            raise WorkspaceStateError("workspace events must be an array")
        events.append(
            {
                "sequence": sequence,
                "type": str(event_type),
                "page": int(page),
                **copy.deepcopy(fields),
            }
        )
        state["sequence"] = sequence
        state["events"] = events

    def _read_state(self) -> dict[str, object]:
        """读取并校验持久工作区状态。"""
        if not self.path.is_file():
            digest = uuid.uuid5(
                uuid.NAMESPACE_URL, str(self.paths.root)
            ).hex[:16]
            return {
                "schema": WORKSPACE_STATE_SCHEMA,
                "deck_id": f"deck-{digest}",
                "layout_version": self.paths.layout_version,
                "paths_schema": WORKSPACE_PATHS_SCHEMA,
                "paths": dict(self.paths.paths),
                "sequence": 0,
                "pages": {},
                "events": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != WORKSPACE_STATE_SCHEMA:
            raise WorkspaceStateError(f"unsupported workspace state in {self.path}")
        raw_layout = value.get("layout_version")
        if raw_layout is None:
            raise WorkspaceStateError(
                f"workspace state has no layout_version: {self.path}"
            )
        state_layout = int(raw_layout)
        if state_layout != WORKSPACE_LAYOUT_VERSION:
            raise WorkspaceStateError(
                f"unsupported workspace layout_version={state_layout}; new v1 only "
                f"serves layout_version {WORKSPACE_LAYOUT_VERSION}"
            )
        stored_paths = value.get("paths")
        if value.get("paths_schema") != WORKSPACE_PATHS_SCHEMA:
            raise WorkspaceStateError("unsupported_workspace_layout: invalid paths_schema")
        if not isinstance(stored_paths, dict) or any(stored_paths.get(key) != relative for key, relative in self.paths.paths.items()):
            raise WorkspaceStateError(
                "workspace state path mapping differs from the canonical v2 layout"
            )
        _page_records(value)
        return value

    def _write_state(self, state: Mapping[str, object]) -> None:
        """在同目录完整写入后原子替换工作区状态。"""
        self.paths.assert_no_forbidden_markers()
        normalized = dict(state)
        normalized["layout_version"] = self.paths.layout_version
        normalized["paths_schema"] = WORKSPACE_PATHS_SCHEMA
        normalized["paths"] = dict(self.paths.paths)
        payload = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        pending = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.pending")
        try:
            with pending.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self.path)
        finally:
            pending.unlink(missing_ok=True)


def file_sha256(path: str | Path) -> str:
    """计算文件 SHA-256；文件缺失时返回空字符串。"""
    source = Path(path)
    if not source.is_file():
        return ""
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text_write(path: Path, value: str) -> None:
    """原子写入 UTF-8 文本派生产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _page_number(value: int) -> int:
    """把页码规范为正整数并拒绝布尔值。"""
    if isinstance(value, bool):
        raise ValueError("page number must be a positive integer")
    number = int(value)
    if number < 1:
        raise ValueError("page number must be a positive integer")
    return number


def _page_records(state: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """校验并返回页面记录映射。"""
    raw = state.get("pages") or {}
    if not isinstance(raw, dict) or not all(isinstance(item, dict) for item in raw.values()):
        raise WorkspaceStateError("workspace pages must be an object of objects")
    return raw


def _required_page_record(
    state: Mapping[str, object], page: int
) -> dict[str, object]:
    """读取已注册页面，缺失时给出明确错误。"""
    record = _page_records(state).get(str(page))
    if record is None:
        raise WorkspaceStateError(f"page {page} is not registered")
    return record
