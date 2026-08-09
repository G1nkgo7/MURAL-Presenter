#!/usr/bin/env bash
# Download Playwright Chromium shared-library dependencies into a local prefix.
# This avoids requiring root access on CCI instances where apt install is not
# available to the web demo process.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS_DIR="${PPTAGENT_CHROMIUM_DEPS:-$ROOT/studio/data/chromium-deps}"
ROOT_DIR="$DEPS_DIR/root"
DL_DIR="$DEPS_DIR/dl"
LIB_DIR="$ROOT_DIR/usr/lib/x86_64-linux-gnu"
BROWSER_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
BROWSER_EXE="${PPT_SKILL_BROWSER_EXE:-}"
ENGINE_PYTHON="${PPTAGENT_ENGINE_PYTHON:-${ENGINE_PYTHON:-}}"

if [ -z "$ENGINE_PYTHON" ]; then
  for py in \
    "$ROOT/distillation/.venv/bin/python" \
    "$ROOT/frozen/ppt-skill-html/current/harness/.venv/bin/python" \
    "/usr/bin/python3"
  do
    if [ -x "$py" ]; then
      ENGINE_PYTHON="$py"
      break
    fi
  done
fi

if [ -n "$BROWSER_EXE" ] && [ -x "$BROWSER_EXE" ]; then
  echo "using explicit Playwright Chromium: $BROWSER_EXE"
elif [ -n "$ENGINE_PYTHON" ] && [ -x "$ENGINE_PYTHON" ]; then
  echo "ensuring Playwright Chromium for $ENGINE_PYTHON"
  "$ENGINE_PYTHON" -m playwright install chromium >/dev/null
else
  echo "warning: no engine Python found; skipping playwright browser install" >&2
fi

has_core_libs() {
  [ -f "$LIB_DIR/libnspr4.so" ] \
    && [ -f "$LIB_DIR/libnss3.so" ] \
    && [ -f "$LIB_DIR/libXrender.so.1" ]
}

clean_core_runtime_libs() {
  # Do not let local dependency bootstrapping override core runtime libraries.
  find "$ROOT_DIR" -type f \( \
    -name 'libc.so*' -o \
    -name 'ld-linux*.so*' -o \
    -name 'libstdc++.so*' -o \
    -name 'libgcc_s.so*' \
  \) -delete 2>/dev/null || true
}

ensure_render_compat_link() {
  # Compatibility for the v1.2 skill's current render.py, which hard-codes this
  # conda package path into LD_LIBRARY_PATH. The link is user-local and does not
  # modify the upstream skill repo.
  local compat_lib_dir="${PPTAGENT_RENDER_COMPAT_LIB:-$HOME/miniconda3/pkgs/nss-3.121-h5c1ed2d_0/lib}"
  if [ ! -e "$compat_lib_dir" ]; then
    mkdir -p "$(dirname "$compat_lib_dir")"
    ln -s "$LIB_DIR" "$compat_lib_dir"
  fi
}

clean_core_runtime_libs

if has_core_libs; then
  ensure_render_compat_link
  echo "chromium deps already present: $DEPS_DIR"
  exit 0
fi

if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "apt-get and dpkg-deb are required to bootstrap Chromium deps" >&2
  exit 1
fi

DEPS_FILE="$(find "$BROWSER_CACHE" -path '*/chrome*/*/deb.deps' -print 2>/dev/null | head -1 || true)"
if [ -z "$DEPS_FILE" ]; then
  DEPS_FILE="$(find "$BROWSER_CACHE" -name deb.deps -print 2>/dev/null | head -1 || true)"
fi
if [ -z "$DEPS_FILE" ]; then
  echo "cannot find Playwright deb.deps under $BROWSER_CACHE" >&2
  exit 1
fi

mkdir -p "$DL_DIR" "$ROOT_DIR"

BASE_PKGS="$(
  python3 - "$DEPS_FILE" <<'PY'
import re
import sys

skip = {"ca-certificates", "libc6", "wget", "xdg-utils"}
out = []
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    # deb.deps can contain alternatives and version constraints:
    # "libcurl3-gnutls | libcurl3-nss | libcurl4 | libcurl3"
    name = line.split("|", 1)[0].strip()
    name = re.sub(r"\s*\(.*\)\s*$", "", name).strip()
    if name and name not in skip and name not in out:
        out.append(name)
print("\n".join(out))
PY
)"

if command -v apt-cache >/dev/null 2>&1; then
  RESOLVED_PKGS="$(
    # Pull transitive dependencies too. Do not include core runtime packages
    # that could override the host libc/libstdc++ through LD_LIBRARY_PATH.
    apt-cache depends --recurse --no-recommends --no-suggests \
      --no-conflicts --no-breaks --no-replaces --no-enhances $BASE_PKGS 2>/dev/null \
      | awk '/^[[:alnum:].+:-]+$/ {print $1} /^[[:space:]]*Depends:/ {print $2}' \
      | sed 's/<//;s/>//' \
      | grep -E '^(lib|fonts-)' \
      | grep -Ev '^(libc6|libc-bin|libgcc-s1|libstdc[+][+]6|gcc-[0-9]+-base|ca-certificates|wget|xdg-utils)$' \
      | sort -u
  )"
  PKGS="$(printf '%s\n%s\n' "$BASE_PKGS" "$RESOLVED_PKGS" | awk 'NF' | sort -u)"
else
  PKGS="$BASE_PKGS"
fi

echo "bootstrapping Chromium deps from $DEPS_FILE"
cd "$DL_DIR"
while IFS= read -r pkg; do
  [ -n "$pkg" ] || continue
  if ls "${pkg}"_*.deb >/dev/null 2>&1; then
    continue
  fi
  echo "download $pkg"
  if ! apt-get download "$pkg" >/dev/null; then
    echo "warning: could not download $pkg; continuing" >&2
  fi
done <<< "$PKGS"

for deb in ./*.deb; do
  [ -e "$deb" ] || continue
  dpkg-deb -x "$deb" "$ROOT_DIR"
done

clean_core_runtime_libs

if ! has_core_libs; then
  echo "Chromium deps bootstrap incomplete: missing libnspr4.so, libnss3.so, or libXrender.so.1 in $LIB_DIR" >&2
  exit 1
fi

ensure_render_compat_link

date -u '+%Y-%m-%dT%H:%M:%SZ' > "$DEPS_DIR/.ready"
echo "chromium deps ready: $DEPS_DIR"
