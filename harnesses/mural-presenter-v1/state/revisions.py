"""结构修订的唯一事务边界：Skill 改文件，内核同步身份、合同和失效回执。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .workspace import PageLifecycle, _atomic_text_write, file_sha256
from .artifacts import _html_resource_hashes
from runtime.shared_objects import workspace_skill_module

MARKER = "_state/structural-transaction.json"
STATE_FILES = ("deck.md", "config.json", "_state/workspace.json", "_state/artifacts.json",
               "_state/owners.json", "_state/issues.json", "_state/page-index.json",
               "_state/deck-graph.json", "_state/active-revision.json", "_state/revision-impact.md",
               "assets/catalog.md")
PAGE_FILES = (("briefs", "slide_*.md"), ("slides", "slide_*.html"), ("renders", "slide_*.png"))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _write(path: Path, value: dict) -> None:
    _atomic_text_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError(f"revision path escapes workspace: {relative}")
    return path


def _page_files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for directory, pattern in PAGE_FILES
            for path in (root / directory).glob(pattern)}


def recover_revision(root: Path) -> None:
    """持有 workspace lock 时回滚未提交事务；重复恢复具有相同结果。"""
    marker = _read(root / MARKER)
    if marker.get("status") != "prepared":
        return
    backup = str(marker.get("backup") or "")
    if not re.fullmatch(r"_state/structural-snapshots/[0-9a-f]{32}", backup):
        raise ValueError("invalid revision recovery snapshot")
    snapshot = _path(root, backup)
    files = marker["files"]
    # 必须先验证完整备份，再动 canonical 文件。
    for relative, digest in files.items():
        _path(root, relative)
        if digest and file_sha256(_path(snapshot, relative)) != digest:
            raise ValueError(f"revision snapshot is incomplete: {relative}")
    for relative in sorted(_page_files(root) | set(files)):
        target = _path(root, relative)
        if files.get(relative):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_path(snapshot, relative), target)
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
        else:
            target.unlink(missing_ok=True)
    _write(root / MARKER, {**marker, "status": "rolled_back"})


class RevisionTransaction:
    def __init__(self, workspace, artifacts, owners):
        self.workspace, self.artifacts, self.owners = workspace, artifacts, owners
        self.root = workspace.paths.root

    def run(self, execute, *, structural: bool = True):
        """execute 接收事务 token 和继承锁 fd；子进程退出前恢复不能抢跑。"""
        with self.workspace._lock:
            recover_revision(self.root)
            owner_state = self.owners._read_state()
            artifact_state = self.artifacts._read_state()
            if any(row["role"] != "orchestrator"
                   and row["status"] in {"pending", "running", "waiting", "interrupted"}
                   for row in owner_state.get("owners", {}).values()):
                raise ValueError("finish or cancel active Owners before a structural revision")
            if any(row.get("status") == "open" for row in artifact_state.get("transactions", {}).values()):
                raise ValueError("cannot revise while artifact leases are open")
            pointer = _read(self.root / "_state/active-revision.json")
            impact_relative = str(pointer.get("impact_path") or "")
            if not re.fullmatch(r"_state/revisions/[A-Za-z0-9_.-]+/impact.json", impact_relative):
                raise ValueError("prepare-revision must run before a structural revision")
            impact_path = _path(self.root, impact_relative)
            impact = _read(impact_path)
            if not structural:
                if impact.get("structural_pending"):
                    raise ValueError("revision still has pending structural operations")
                pages = self.workspace.pages()
                if not pages or any(page.lifecycle not in {PageLifecycle.INSPECTED, PageLifecycle.READY} for page in pages):
                    raise ValueError("revision commit requires current HTML/PNG inspections")
                self.workspace.assert_ready_resources(tuple(page.page for page in pages))
                from qc.routing import IssueLedger
                if IssueLedger(self.root).blocking_issues():
                    raise ValueError("revision commit has unresolved blocking issues")
            transaction_id = uuid.uuid4().hex
            # 恢复快照也是修订历史，不属于每条 Skill 命令结束时必须清空的临时输出。
            backup = f"_state/structural-snapshots/{transaction_id}"
            snapshot = self.root / backup
            files = {}
            for relative in sorted(set(STATE_FILES) | _page_files(self.root) | {impact_relative}):
                source = _path(self.root, relative)
                files[relative] = file_sha256(source)
                if files[relative]:
                    target = snapshot / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    with target.open("rb") as handle:
                        os.fsync(handle.fileno())
            marker = {"transaction_id": transaction_id, "status": "prepared", "backup": backup, "files": files}
            _write(self.root / MARKER, marker)
            before = self.workspace._read_state()
            snapshots = {page.page_uid: page for page in self.workspace.pages()}
            try:
                if not structural:
                    for assignment in impact.get("assignments", []):
                        assignment["status"] = "verified"
                    _write(impact_path, impact)
                result = execute(transaction_id, self.workspace._lock.handle.fileno())
                if structural and files["_state/page-index.json"] != file_sha256(self.root / "_state/page-index.json"):
                    self._sync(before, snapshots, artifact_state, owner_state, impact)
                elif not structural:
                    state = self.workspace._read_state()
                    state["deck_version"] = _read(self.root / "_state/deck-graph.json")["deck_version"]
                    self.workspace._write_state(state)
                _write(self.root / MARKER, {**marker, "status": "committed"})
                return result
            except BaseException:
                recover_revision(self.root)
                raise

    def _sync(self, before, snapshots, artifacts, owners, impact):
        index = _read(self.root / "_state/page-index.json")
        rows = sorted(index["pages"], key=lambda row: int(row["position"]))
        uids = [row["page_uid"] for row in rows]
        if not uids or len(uids) != len(set(uids)) or [row["position"] for row in rows] != list(range(1, len(rows) + 1)):
            raise ValueError("invalid revised page identity/order")
        old = {row["page_uid"]: row for row in before["pages"].values() if not row.get("retired")}
        if set(uids) - set(old) - set(impact.get("created_page_uids", [])):
            raise ValueError("revision introduced an unprepared page identity")
        after = copy.deepcopy(before)
        after["pages"] = {}
        positions = {uid: number for number, uid in enumerate(uids, 1)}
        changed = set(old) - set(uids)
        for number, uid in enumerate(uids, 1):
            record = copy.deepcopy(old.get(uid) or self.workspace._new_page_record(before, number))
            current_hash = file_sha256(self.workspace.paths.page_html(number))
            prior = snapshots.get(uid)
            if prior is None or prior.page != number or prior.html_sha256 != current_hash:
                changed.add(uid)
                authored = current_hash if prior and prior.html_sha256 == prior.authored_html_sha256 else ""
                record.update(authored_html_sha256=authored, last_render=None,
                              last_inspection=None, ready_pair=None,
                              last_transition="authored" if authored else "missing")
            record.update(page=number, page_uid=uid, html_path=f"slides/slide_{number:02d}.html", png_path=f"renders/slide_{number:02d}.png")
            after["pages"][str(number)] = record
        after.setdefault("retired_pages", {}).update({uid: old[uid] for uid in set(old) - set(uids)})
        after["deck_version"] = index["deck_version"]
        self.workspace._append_event(after, "page_order_revised", 0, page_uids=uids)
        self.workspace._write_state(after)

        archived = owners.setdefault("archives", [])
        affected = changed | set(impact.get("affected_pages", []))
        for group, owner in list(owners.get("owners", {}).items()):
            contract = {before["pages"][str(page)]["page_uid"] for page in owner["pages"]}
            if owner["role"] == "orchestrator":
                # 主 Agent 是当前修订执行者；页序变化只重映射其存续责任，不结束其上下文。
                owner["pages"] = sorted(positions[uid] for uid in contract if uid in positions)
                continue
            if contract & affected:
                archived.append({**owner, "status": "archived"})
                del owners["owners"][group]
        self.owners._write_state(owners)

        # 登记的消费者可能不完整；保留页实际使用的图片不能随删页退役。
        live_consumers = {}
        if any(row["kind"] == "image" for row in artifacts.get("artifacts", {}).values()):
            for number, uid in enumerate(uids, 1):
                prior = snapshots.get(uid)
                if prior is None and not self.workspace.paths.page_html(number).is_file():
                    continue
                receipt = artifacts.get("render_receipts", {}).get(str(prior.page), {}) if prior else {}
                for path in _html_resource_hashes(
                    self.root, self.workspace.paths.page_html(number),
                    loaded_resources=receipt.get("loaded_resources", []),
                    resolve_shared_font=self.artifacts._shared_font,
                ):
                    live_consumers.setdefault(path, set()).add(number)
        records = {}
        for key, record in artifacts.get("artifacts", {}).items():
            if record["kind"] == "image":
                metadata = record.get("image_metadata") or {}
                consumers = metadata.get("slides") or []
                if consumers:
                    remapped = sorted({positions[before["pages"][str(page)]["page_uid"]]
                        for page in consumers if str(page) in before["pages"]
                        and before["pages"][str(page)]["page_uid"] in positions}
                        | live_consumers.get(record["path"], set()))
                    # 退出当前登记，原记录保留在本事务快照；不删图片，也不冒充未分配素材。
                    if not remapped:
                        continue
                    if remapped != consumers:
                        record["image_metadata"] = {**metadata, "slides": remapped}
                        record["revision"] += 1
            if record["kind"] != "page_html":
                records[key] = record
                continue
            uid = before["pages"][str(record["page"])]["page_uid"]
            if uid not in positions:
                continue
            number = positions[uid]
            key = f"page-html-{number:02d}"
            record.update(artifact_id=key, page=number, path=f"slides/slide_{number:02d}.html")
            if uid in changed:
                record.update(sha256=after["pages"][str(number)]["authored_html_sha256"], revision=record["revision"] + 1)
                if not record["sha256"]:
                    record["status"] = "declared"
            records[key] = record
        artifacts["artifacts"] = records
        artifacts["render_receipts"] = {key: value for key, value in artifacts.get("render_receipts", {}).items()
                                        if before["pages"].get(key, {}).get("page_uid") not in changed}
        self.artifacts._append_event(artifacts, "page_order_revised", page_uids=uids)
        self.artifacts._refresh_catalog_hash(artifacts)
        self.artifacts._write_state(artifacts)
        if (self.root / "assets/catalog.md").is_file() or any(row["kind"] == "image" for row in records.values()):
            core = workspace_skill_module(self.root, "deck_core")
            core.write_image_catalog(self.root, core._registered_images(self.root))

        # Issue 属于原页面身份；删除页的历史保留，但不继续阻塞当前 Deck。
        from qc.routing import IssueLedger
        ledger = IssueLedger(self.root)
        state = ledger._read_state()
        for issue in state.get("issues", {}).values():
            old_pages = list(issue["pages"])
            issue["pages"] = sorted(positions[before["pages"][str(page)]["page_uid"]]
                                    for page in old_pages if before["pages"][str(page)]["page_uid"] in positions)
            if old_pages and not issue["pages"]:
                issue["status"] = "rejected"
                issue["resolution"] = f"pages retired by structural revision: {old_pages}"
        ledger._write_state(state)
