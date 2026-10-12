"""从原始 Noto Sans SC 构建保留全部字形覆盖的 UI WOFF2 分片。"""
import hashlib
import io
import json
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "webui/studio/static"
OUTPUT = STATIC / "fonts"


def unicode_ranges(points):
    ranges = []
    for point in sorted(points):
        if ranges and point == ranges[-1][1] + 1:
            ranges[-1][1] = point
        else:
            ranges.append([point, point])
    return ",".join(f"U+{start:X}" if start == end else f"U+{start:X}-{end:X}"
                    for start, end in ranges)


def build():
    source = (ROOT / "fonts/NotoSansSC.ttf").read_bytes()
    with TTFont(io.BytesIO(source)) as font:
        coverage = set(font.getBestCmap())
    ui_sources = [STATIC / "app.js", STATIC / "app.css",
                  *(ROOT / "webui/studio/templates").glob("*.html")]
    common = {ord(char) for path in ui_sources for char in path.read_text()} & coverage
    # 常用汉字集中成片，避免一段中文标题触发多个稀疏的 Unicode 分片。
    frequent = {ord(char) for lead in range(0xB0, 0xD8) for trail in range(0xA1, 0xFF)
                for char in bytes([lead, trail]).decode("gb2312", errors="ignore")} & coverage - common
    remaining = sorted(coverage - common - frequent)
    groups = [sorted(common), sorted(frequent),
              *[remaining[i:i + 1024] for i in range(0, len(remaining), 1024)]]
    parts, rules = [], []
    for number, points in enumerate(groups):
        options = subset.Options()
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.recalc_timestamp = False
        with TTFont(io.BytesIO(source), recalcTimestamp=False) as font:
            subsetter = subset.Subsetter(options=options)
            subsetter.populate(unicodes=points)
            subsetter.subset(font)
            font.flavor = "woff2"
            buffer = io.BytesIO()
            font.save(buffer)
        payload = buffer.getvalue()
        name = f"mural-ui-cjk-{hashlib.sha256(payload).hexdigest()[:16]}.woff2"
        (OUTPUT / name).write_bytes(payload)
        ranges = unicode_ranges(points)
        rules.append(f'@font-face {{\n  font-family: "MURAL UI CJK";\n'
                     f'  src: url("{name}") format("woff2");\n'
                     '  font-weight: 100 900;\n  font-style: normal;\n  font-display: swap;\n'
                     f'  unicode-range: {ranges};\n}}')
        parts.append({"file": name, "bytes": len(payload), "characters": len(points),
                      "kind": "ui" if number == 0 else "frequent" if number == 1 else "coverage"})
        print(f"{number + 1}/{len(groups)}: {name} {len(payload)} bytes", flush=True)
    (OUTPUT / "ui-cjk.css").write_text("\n\n".join(rules) + "\n")
    (OUTPUT / "ui-cjk.json").write_text(json.dumps({
        "source": "fonts/NotoSansSC.ttf", "source_sha256": hashlib.sha256(source).hexdigest(),
        "characters": len(coverage), "parts": parts,
        "license": "SIL Open Font License 1.1; source copyright and license names retained in every subset",
    }, ensure_ascii=False, indent=2) + "\n")
    filenames = {part["file"] for part in parts}
    for old in OUTPUT.glob("mural-ui-cjk-*.woff2"):
        if old.name not in filenames:
            old.unlink()


if __name__ == "__main__":
    build()
