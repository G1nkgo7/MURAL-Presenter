#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MURAL_ROOT="$(cd "${HERE}/../../.." && pwd)"
GATE_REPO="${GATE_REPO:-$(cd "${MURAL_ROOT}/.." && pwd)/nova_vision_demo}"
GATE_TAG="sensenova-harness-vision-v1.1-20260815"
GATE_COMMIT="4a067c75e4db7ea5ba81c264a1dc0ddac24f411e"
: "${RUN_ROOT:?set RUN_ROOT to a dedicated absolute run directory}"

case "${RUN_ROOT}" in
  /*) ;;
  *) echo "RUN_ROOT must be absolute: ${RUN_ROOT}" >&2; exit 2 ;;
esac

# CCI mounts the shared AFS checkout with an owner different from the container
# user.  Both operations only need the object database, so address the exact
# Git directory directly.  This avoids worktree ownership checks without
# weakening Git's global safe.directory policy inside the container.
git_objects=(git --git-dir="${GATE_REPO}/.git")
resolved="$(env -u LD_LIBRARY_PATH "${git_objects[@]}" rev-parse "${GATE_TAG}^{commit}")"
if [[ "${resolved}" != "${GATE_COMMIT}" ]]; then
  echo "Gate tag mismatch: expected ${GATE_COMMIT}, got ${resolved}" >&2
  exit 3
fi

runtime="${RUN_ROOT%/}/gate-runtime"
provenance="${runtime}/source-provenance.json"
if [[ -d "${runtime}" ]]; then
  python3 - "${provenance}" "${GATE_TAG}" "${GATE_COMMIT}" <<'PY'
import json
import sys
from pathlib import Path

path, tag, commit = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"existing Gate runtime has no valid provenance: {exc}")
if data.get("tag") != tag or data.get("commit") != commit:
    raise SystemExit("existing Gate runtime provenance does not match frozen tag")
PY
  printf '%s\n' "${runtime}"
  exit 0
fi

mkdir -p "${RUN_ROOT}"
temporary="$(mktemp -d "${RUN_ROOT%/}/.gate-runtime.XXXXXX")"
cleanup() { [[ -d "${temporary}" ]] && rm -rf -- "${temporary}"; }
trap cleanup EXIT
# AFS is root-squashed in CCI.  The container may run as root, but archived
# ownership must not be restored onto AFS; files should belong to the mounted
# workspace identity instead.
env -u LD_LIBRARY_PATH "${git_objects[@]}" archive "${GATE_TAG}" \
  | tar --no-same-owner -xf - -C "${temporary}"
python3 - "${temporary}/source-provenance.json" "${GATE_TAG}" "${GATE_COMMIT}" "${GATE_REPO}" <<'PY'
import json
import sys
from pathlib import Path

path, tag, commit, repository = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
path.write_text(json.dumps({
    "repository": repository,
    "tag": tag,
    "commit": commit,
    "exported_from_git_archive": True,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
mv -- "${temporary}" "${runtime}"
trap - EXIT
printf '%s\n' "${runtime}"
