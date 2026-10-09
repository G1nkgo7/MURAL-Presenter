#!/usr/bin/env python3
"""Create and verify an immutable archive for one frozen MURAL release pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import time

try:
    from check_public_release import (
        ROOT,
        check_mural_snapshot,
    )
    from content_archive import atomic_json, verify_tar_zst, write_tar_zst
except ModuleNotFoundError:
    from tools.check_public_release import (
        ROOT,
        check_mural_snapshot,
    )
    from tools.content_archive import atomic_json, verify_tar_zst, write_tar_zst


# 冻结文件集合必须与运行准入摘要相同；公共文稿扫描的 runtime 排除规则不能用于源码打包。
source_tree_files = runpy.run_path(
    str(ROOT / "harnesses/mural-presenter-v1/runtime/release_guard.py")
)["source_tree_files"]


def release_members(release_path: Path, release: dict) -> list[str]:
    members = [release_path.relative_to(ROOT).as_posix()]
    members.extend(release.get("integration", {}).get("files", []))
    for key in ("skill", "harness"):
        root = (ROOT / str(release[key]["path"])).resolve()
        members.extend(path.relative_to(ROOT).as_posix() for path in source_tree_files(root))
    return sorted(set(members))


def verify_source_members(release_path: Path, release: dict, records: list[dict]) -> None:
    """成员字节自洽还不够：归档必须能重建冻结清单声明的三个源码摘要与集成摘要。"""
    hashes = {row["path"]: row["sha256"] for row in records}
    relative = release_path.relative_to(ROOT).as_posix()
    if hashes.get(relative) != hashlib.sha256(release_path.read_bytes()).hexdigest():
        raise ValueError("archive release manifest is missing or differs from the declared release")

    def digest(rows):
        return hashlib.sha256("".join(f"{value}  {name}\n" for name, value in
            sorted(rows.items(), key=lambda item: item[0].encode())).encode()).hexdigest()

    pair = {}
    for key in ("skill", "harness"):
        prefix = str(release[key]["path"]).rstrip("/") + "/"
        selected = {name: value for name, value in hashes.items() if name.startswith(prefix)}
        actual = digest({"./" + name[len(prefix):]: value for name, value in selected.items()})
        if actual != release[key]["tree_sha256"]:
            raise ValueError(f"release archive source coverage/hash mismatch: {key}")
        pair.update(selected)
    if digest(pair) != release["pair_tree_sha256"]:
        raise ValueError("release archive source coverage/hash mismatch: pair")
    integration = release["integration"]
    missing = set(integration["files"]) - hashes.keys()
    if missing or digest({name: hashes[name] for name in integration["files"]}) != integration["tree_sha256"]:
        raise ValueError("release archive source coverage/hash mismatch: integration")


def verify_bundle(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = Path(str(manifest["archive"]["path"]))
    report = verify_tar_zst(archive, manifest["archive"]["members"])
    if report["sha256"] != manifest["archive"]["sha256"]:
        raise SystemExit(
            "release archive digest drift: "
            f"expected {manifest['archive']['sha256']}, found {report['sha256']}"
        )
    release_path = Path(manifest["release_manifest"]).resolve()
    release_path.relative_to(ROOT.resolve())
    verify_source_members(release_path, json.loads(release_path.read_text()), manifest["archive"]["members"])
    return {**report, "source_hashes_verified": True}


def main() -> None:
    os.umask(0o022)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release-manifest",
        default=str(ROOT / "configs/releases/mural-paper-v1.json"),
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    release_path = Path(args.release_manifest).resolve()
    release_path.relative_to(ROOT.resolve())
    release = json.loads(release_path.read_text(encoding="utf-8"))
    output_dir = Path(
        args.output_dir
        or ROOT / "artifacts/releases" / str(release["release"])
    ).resolve()
    bundle_manifest = output_dir / "release-bundle-manifest.json"

    if args.verify_only:
        report = verify_bundle(bundle_manifest)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    errors = check_mural_snapshot(release_path)
    if errors:
        raise SystemExit("release snapshot is not frozen:\n- " + "\n- ".join(errors))

    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o755)
    archive = output_dir / f"{release['release']}.tar.zst"
    archive_record = write_tar_zst(
        ROOT,
        release_members(release_path, release),
        archive,
    )
    verified = verify_tar_zst(archive, archive_record["members"])
    if verified["sha256"] != archive_record["sha256"]:
        raise SystemExit("release archive changed between write and verification")
    verify_source_members(release_path, release, archive_record["members"])
    payload = {
        "schema": "mural.release-bundle.v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "release": release["release"],
        "release_manifest": str(release_path),
        "skill_tree_sha256": release["skill"]["tree_sha256"],
        "harness_tree_sha256": release["harness"]["tree_sha256"],
        "pair_tree_sha256": release["pair_tree_sha256"],
        "archive": archive_record,
        "verified": True,
    }
    atomic_json(bundle_manifest, payload)
    print(
        json.dumps(
            {
                "status": "frozen",
                "release": release["release"],
                "archive": str(archive),
                "archive_sha256": archive_record["sha256"],
                "member_count": archive_record["member_count"],
                "manifest": str(bundle_manifest),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
