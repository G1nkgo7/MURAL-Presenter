#!/usr/bin/env python3
"""ai_slop_lint.py —— 对成品 HTML + base.css 的**确定性「AI 味」grep-lint**。
把「设计感」里可机检的部分从主观 judge 变成硬门:只报**高置信硬点**(避免误伤好设计),
模糊项(留白空洞 / 卡套卡 / 大而空 hero / 单调间距)不在此列——交 vision 复审人眼判。

用法:  python ${SKILL_DIR:-skills/ppt-skill-html}/scripts/ai_slop_lint.py [slides_dir_or_glob] [base_css]
默认:  slides/  与  base.css 。report-only(永远 exit 0),命中打到 stdout。
review subagent 先跑它,把命中项当**硬问题**纳入给编排器的清单(编排器改 base.css / 重派页)。"""
import sys, os, re, glob
from collections import Counter

# 7 个 AI 默认靛蓝 / 紫 accent(skill 明禁,非有意声明就是 slop)
AI_HEX = re.compile(r"#(6366f1|4f46e5|4338ca|3730a3|8b5cf6|7c3aed|a855f7)\b", re.I)
# 常见「当功能图标」的 emoji(保守:只抓这些明显的)
ICON_EMOJI = re.compile("[✨\U0001F680\U0001F3AF⚡\U0001F525\U0001F4A1"
                        "\U0001F4C8\U0001F4CA✅\U0001F511\U0001F4B0\U0001F6E0⚙\U0001F4CC\U0001F50D]")
# 未填充占位符(matq5k 39-deck 诊断【高】:核心数字被淹没 / 占位符残留,如仪表盘 -.--%、空单位槽「 %」「 小时」)。
# 只抓**高置信硬点**:成对/成串的 dash 数字占位、-.--/-.-% 之类残留、空单位槽(标点/空格 + 单位无数字)。
# 保守:单个「--」在正文里可能是破折号,故只在**数据位/单位位上下文**(紧邻 %/单位、或纯 dash 数字串)才判。
PLACEHOLDER = re.compile(
    r"(?<![\d.])"                     # 前面不是数字/小数点(避免 12--34 之类正常连字)
    r"(?:-\.-{1,2}%?"                 # -.-- / -.- / -.--% / -.-%
    r"|——\.—%?"                       # 全角破折号占位 ——.—% / ——.—(matq5k 实测这批用全角)
    r"|--%"                           # --%
    r"|-{2,}\s*(?:%|小时|hrs?|hours?|次|元|美元|\$|万|亿|kg|km|ms)"  # -- 紧跟**明确单位**
    r"|——\s*(?:%|小时|次|元))"         # 全角破折号紧跟明确单位 = 空数据槽
    , re.I)
# ★单位白名单只留**不会作为常用词首字**的:%/小时/次/元/kg… 。
#   刻意剔除 年/月/天/人/个 —— 它们既是单位也是词首(年度/月份/人均/个别),且常出现在装饰
#   `—— 年度总结 ——` frame 与注释里,对 HARD 门是不可接受的假阳性(matq5k 实测栽过)。
# 跨标签变体:`——<span class="unit">小时</span>` 这类 dash 与单位被一个标签隔开(matq5k 实测)。
PLACEHOLDER_TAGGED = re.compile(
    r"(?<![\d.])——\s*<[^>]{0,60}>\s*(?:%|小时|次|元)", re.I)
# HTML 注释里的内容不算屏上占位(装饰注释常含 —— / 词首单位)→ 匹配前先剥注释。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _find(a):
    if not a:
        a = "slides"
    if os.path.isdir(a):
        return sorted(glob.glob(os.path.join(a, "slide_*.html")))
    return sorted(glob.glob(a))


def lint_html(fp):
    hits = []
    try:
        txt = open(fp, encoding="utf-8", errors="ignore").read()
    except OSError:
        return hits
    lines = txt.split("\n")
    # 剥 HTML 注释(用等长换行填充,保留行号)——注释内容不算屏上占位 / slop。
    _txt_nc = _HTML_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), txt)
    lines_nc = _txt_nc.split("\n")
    page_has_gradient = "gradient" in txt.lower()
    base = os.path.basename(fp)
    for i, ln in enumerate(lines, 1):
        ln_nc = lines_nc[i - 1] if i - 1 < len(lines_nc) else ln
        low = ln.lower()
        nospace = low.replace(" ", "")
        # 裁字渐变(高置信:同行 clip:text + gradient;或 clip:text 且本页有 gradient)
        if "background-clip:text" in nospace or "-webkit-background-clip:text" in nospace:
            if "gradient" in low:
                hits.append((base, i, "gradient-text", "裁字渐变 background-clip:text + gradient(硬)"))
            elif page_has_gradient:
                hits.append((base, i, "gradient-text", "background-clip:text 且本页有 gradient(疑似裁字渐变)"))
        if AI_HEX.search(ln):
            hits.append((base, i, "ai-palette", "AI 默认靛蓝/紫 hex " + AI_HEX.search(ln).group(0)))
        m = re.search(r"font-family[^;{}]*\b(Inter|Roboto|Arial)\b", ln, re.I)
        if m:
            hits.append((base, i, "default-font", "禁用默认显示字体 " + m.group(1)))
        if re.search(r'(?:src\s*=|url\()\s*["\']?https?://[^"\')\s]+\.(?:png|jpe?g|webp|gif|svg)', low):
            hits.append((base, i, "hotlink-img", "外链图片直链(应 fetch 落地 assets/、自包含)"))
        if re.search(r"border-left:\s*(?:[3-9]|1[0-9])px\s+solid", low) and "var(--accent" in low:
            hits.append((base, i, "side-tab", "圆角卡 + 左侧 accent 彩条(AI dashboard tile)"))
        if ICON_EMOJI.search(ln):
            hits.append((base, i, "emoji-icon", "emoji 当功能图标(应 monoline SVG)"))
        pm = PLACEHOLDER.search(ln_nc) or PLACEHOLDER_TAGGED.search(ln_nc)
        if pm:
            hits.append((base, i, "placeholder", "未填充占位符 " + re.sub(r"\s+", " ", pm.group(0).strip()) + "(空数据槽出屏 = 硬伤,填真值或删该槽)"))
    # 数据页图内 hero 读数(SOFT 启发式):有 ECharts 图但图配置里无放大字号 → 疑「大数留标题、图内一片同字号」
    if "echarts" in txt.lower():
        big = re.findall(r"fontSize\s*[:=]\s*(\d{2,3})", txt)
        if not any(int(x) >= 36 for x in big):
            hits.append((base, 0, "no-chart-hero", "数据页图内无放大字号(≥36px)——若这页有值得强调的关键数就放大作 hero;纯趋势 / 结构 / 多指标图无单一焦点数则无需(别硬凑)"))
    return hits


def lint_css(fp):
    hits = []
    try:
        txt = open(fp, encoding="utf-8", errors="ignore").read()
    except OSError:
        return hits
    base = os.path.basename(fp)

    def num(name):
        m = re.search(r"--%s:\s*(\d+(?:\.\d+)?)px" % name, txt)
        return float(m.group(1)) if m else None

    disp, body = num("fs-display"), num("fs-body")
    if disp and body and disp / body < 2.0:
        hits.append((base, 0, "flat-hierarchy",
                     "--fs-display:--fs-body=%.2f <2.0(字号层级太平,把大字拉开)" % (disp / body)))
    for m in AI_HEX.finditer(txt):
        hits.append((base, txt[:m.start()].count("\n") + 1, "ai-palette", "AI 靛蓝/紫 hex " + m.group(0)))
    for m in re.finditer(r"\b(Inter|Roboto|Arial)\b", txt, re.I):
        hits.append((base, txt[:m.start()].count("\n") + 1, "default-font", "默认字体 " + m.group(1)))
    return hits


# 严重度:HARD=高置信硬点(必修);SOFT=提示(grep 分不清正当用法,结合 vision 判、别机械返工)。
SEV = {
    "gradient-text": "HARD", "ai-palette": "HARD", "default-font": "HARD",
    "hotlink-img": "HARD", "emoji-icon": "HARD", "placeholder": "HARD",
    "side-tab": "SOFT", "flat-hierarchy": "SOFT",   # 左彩条可能是正当 takeaway 引用条;层级比值仅供参考
    "no-chart-hero": "SOFT",                         # 图内 hero 读数启发式(grep 不完全,vision 定夺)
}


def main():
    args = sys.argv[1:]
    slides = _find(args[0] if args else "slides")
    css = args[1] if len(args) > 1 else "base.css"
    warnings = []
    if not slides:
        warnings.append("WARN: no slide HTML matched input; lint did not inspect any slides")
    if not os.path.exists(css):
        warnings.append(f"WARN: base CSS not found: {css}; CSS-level lint skipped")
    hits = []
    for f in slides:
        hits += lint_html(f)
    if os.path.exists(css):
        hits += lint_css(css)
    hard = [h for h in hits if SEV.get(h[2]) == "HARD"]
    soft = [h for h in hits if SEV.get(h[2]) != "HARD"]
    if not hard and not soft:
        for w in warnings:
            print("AI-SLOP-LINT: " + w)
        print("AI-SLOP-LINT: OK 0 命中(高置信硬点干净;留白/卡套/大而空等模糊项仍需 vision 核)")
        return
    print("AI-SLOP-LINT: HARD=%d SOFT=%d — HARD:%s SOFT:%s"
          % (len(hard), len(soft), dict(Counter(h[2] for h in hard)), dict(Counter(h[2] for h in soft))))
    for w in warnings:
        print("AI-SLOP-LINT: " + w)
    if hard:
        print("-- HARD(必修:纳入编排器问题清单)--")
        for base, ln, kind, msg in hard:
            print("  %s:%s [%s] %s" % (base, ln, kind, msg))
    if soft:
        print("-- SOFT(提示:结合 vision 判,别机械返工)--")
        for base, ln, kind, msg in soft:
            print("  %s:%s [%s] %s" % (base, ln, kind, msg))


if __name__ == "__main__":
    main()
