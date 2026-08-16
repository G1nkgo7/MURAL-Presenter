#!/usr/bin/env python3
"""ai_slop_lint.py —— 对成品 HTML + base.css 的**确定性「AI 味」grep-lint**。
把「设计感」里可机检的部分从主观 judge 变成硬门:只报**高置信硬点**(避免误伤好设计),
模糊项(留白空洞 / 卡套卡 / 大而空 hero / 单调间距)不在此列——交 vision 复审人眼判。

用法:  python ${SKILL_DIR:-skills/ppt-skill-html}/scripts/ai_slop_lint.py [slides_dir_or_glob] [base_css]
默认:  slides/  与  base.css 。设计命中 report-only(exit 0),但输入缺失时 exit 2,禁止空检查伪 PASS。
收尾 audience subagent 先跑它,把命中项当**硬问题**纳入给编排器的清单(编排器改 base.css / 重派页)。"""
import sys, os, re, glob
from collections import Counter
from urllib.parse import unquote, urlparse

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
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CJK = re.compile(r"[一-鿿]")
# 纯拉丁展示体 token(无中文字形):套在中文上会静默兜底成另一款字、同行两套字 = 违和。
_LATIN_DISPLAY_VAR = re.compile(
    r"var\(\s*--font-(?:grotesque|display-serif|hand-en|hand-en-neat|hand-en-casual)\s*\)", re.I)
# 斜体展示体 token(得意黑 Smiley Sans oblique):只配 hero 单一大字,别给成组数据数字。
_OBLIQUE_DISPLAY_VAR = re.compile(r"var\(\s*--font-(?:hei-heavy|display)\s*\)", re.I)
# ECharts 系列 type / init(源码级图表密度检测:ECharts 渲到 canvas,DOM 门看不到系列/图例)。
_ECHART_SERIES = re.compile(r"\btype\s*:\s*['\"](?:bar|line|pie|scatter|radar|candlestick|boxplot|funnel|gauge|graph|sankey|treemap)['\"]", re.I)
_ECHART_INIT = re.compile(r"echarts\.init\s*\(", re.I)
_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*(['\"])(.*?)\1", re.I | re.S)
_UNRESOLVED_ASSET = re.compile(r"\b(?:ASSET_PENDING|IMG_PENDING|IMAGE_PENDING)\b", re.I)


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
        if _UNRESOLVED_ASSET.search(ln_nc):
            hits.append((base, i, "unresolved-asset", "未绑定素材 token 残留(ASSET/IMG/IMAGE_PENDING)"))
        pm = PLACEHOLDER.search(ln_nc) or PLACEHOLDER_TAGGED.search(ln_nc)
        if pm:
            hits.append((base, i, "placeholder", "未填充占位符 " + re.sub(r"\s+", " ", pm.group(0).strip()) + "(空数据槽出屏 = 硬伤,填真值或删该槽)"))
        # 中英混排违和:同行既有「纯拉丁展示体 token」又有中文字符(剥 HTML+CSS 注释后)→ 拉丁展示体套中文、静默兜底 = 违和。
        ln_stripped = _CSS_COMMENT.sub(" ", ln_nc)
        if _LATIN_DISPLAY_VAR.search(ln_stripped):
            after = _LATIN_DISPLAY_VAR.sub(" ", ln_stripped)   # 去掉 token 本身再看有没有中文文本
            if len(_CJK.findall(after)) >= 2:
                hits.append((base, i, "mixed-script-font",
                             "纯拉丁展示体(Archivo/Fraunces/手写体)用在含中文的元素上——中文会静默兜底成另一款字、同行两套字 = 违和;中文改用含 CJK 字形的字体打头"))
        # 硬编码超大绝对字号:绕过 --fs 阶梯 → 跨页/页内字号不一致、易「字号差距过大」;clamp()/chart fontSize 不误伤。
        for fm in re.finditer(r"font-size:\s*(\d{2,3})px", ln_stripped):
            if int(fm.group(1)) >= 100:   # ≥100px 才算「跳档裸大字」;72–99 属正常 hero/标题区间(--fs-display=88),不误伤
                hits.append((base, i, "abs-bigfont",
                             "硬编码超大绝对字号 %spx(绕过 --fs 阶梯/无上界)——用 var(--fs-*) 或带上界的 clamp(),别裸写巨号" % fm.group(1)))
                break
    # 本地图片路径静态门:render 还会用 naturalWidth 做运行时兜底;这里先抓确定性文件缺失。
    for match in _IMG_SRC.finditer(_txt_nc):
        src = match.group(2).strip()
        if not src or src == "#":
            line_no = _txt_nc.count("\n", 0, match.start()) + 1
            hits.append((base, line_no, "broken-img-path", "img src 为空或 #"))
            continue
        parsed = urlparse(src)
        if parsed.scheme in {"http", "https", "data", "blob"}:
            continue
        raw_path = unquote(parsed.path)
        if parsed.scheme == "file":
            local_path = raw_path
        else:
            local_path = os.path.normpath(os.path.join(os.path.dirname(fp), raw_path))
        if not os.path.isfile(local_path):
            line_no = _txt_nc.count("\n", 0, match.start()) + 1
            hits.append((base, line_no, "broken-img-path", f"本地图片不存在: {src}"))

    # 数据页图内 hero 读数(SOFT 启发式):有 ECharts 图但图配置里无放大字号 → 疑「大数留标题、图内一片同字号」
    if "echarts" in txt.lower():
        big = re.findall(r"fontSize\s*[:=]\s*(\d{2,3})", txt)
        if not any(int(x) >= 36 for x in big):
            hits.append((base, 0, "no-chart-hero", "数据页图内无放大字号(≥36px)——若这页有值得强调的关键数就放大作 hero;纯趋势 / 结构 / 多指标图无单一焦点数则无需(别硬凑)"))
        # 图表拥挤(源码级密度门):系列 / 图例 / 饼片 / 一页多图过密 → 读者分不清系列、标签挤(SOFT,busy 图可能有意、vision 定夺)。
        n_series = len(_ECHART_SERIES.findall(txt))
        n_init = len(_ECHART_INIT.findall(txt))
        if n_series >= 6:
            hits.append((base, 0, "chart-crowded", "单页 ECharts 系列 ~%d 个(≥6 偏密)——系列 ≤5 / 拆两页 / 长尾并「其他」/ 换 small-multiples,别一图塞满" % n_series))
        lg = re.search(r"legend\s*:\s*\{[^{}]*?data\s*:\s*\[([^\]]*)\]", txt, re.S)
        if lg:
            n_leg = len([x for x in lg.group(1).split(",") if x.strip()])
            if n_leg > 6:
                hits.append((base, 0, "chart-crowded", "图例项 ~%d 个(>6)——图例 ≤6 / 直接线尾 endLabel 标名 / 并项,别堆一长条图例" % n_leg))
        for pm2 in re.finditer(r"type\s*:\s*['\"]pie['\"]", txt, re.I):
            seg = txt[pm2.end():pm2.end() + 900]
            dm = re.search(r"data\s*:\s*\[(.*?)\]", seg, re.S)
            if dm and dm.group(1).count("{") > 7:
                hits.append((base, 0, "chart-crowded", "饼 / 环 ~%d 片(>7)——饼 ≤7 片、长尾并「其他」,或改排序条(近似份额饼里看不出差)" % dm.group(1).count("{")))
                break
        if n_init >= 2:
            hits.append((base, 0, "two-charts", "一页 echarts.init ~%d 次(疑一页多图)——§4 一页最多一个图表,拆页或合图(点睛大数字 + 单主图除外)" % n_init))
    return hits


def lint_css(fp):
    hits = []
    try:
        txt = open(fp, encoding="utf-8", errors="ignore").read()
    except OSError:
        return hits
    base = os.path.basename(fp)

    # base.css 框架完整性(确定性硬门):designer 复制 base-template.css 时若把固定框架
    # 结构类整段剥掉、只留 :root token,则每页链接 base.css 后 .slide-title/.slide-body/
    # .slide-footer 全无样式 → 标题带塌顶、页脚缺失、版式各页现造(实测 se_v25_r5 栽:
    # base.css 被砍到 101 行纯 token,6/14 页标题塌)。这些类缺任一即为剥空,零歧义。
    _txt_nc0 = _CSS_COMMENT.sub(" ", txt)
    _missing = [c for c in (".slide-title", ".slide-body", ".slide-footer")
                if not re.search(re.escape(c) + r"\s*[,{]", _txt_nc0)]
    if _missing:
        hits.append((base, 0, "base-css-stripped",
                     "base.css 缺固定框架类 %s——designer 复制 base-template.css 时把结构类剥掉了、"
                     "只剩 :root token;每页标题带/页脚/安全边距会失样式(塌顶、漂移)。"
                     "base.css 必须逐行保留 base-template.css 的全部结构类(.slide/.slide-title/"
                     ".slide-body/.slide-footer/.arch-*),只改 :root 里 TODO 的值" % ",".join(_missing)))

    def num(name):
        m = re.search(r"--%s:\s*(\d+(?:\.\d+)?)px" % name, txt)
        return float(m.group(1)) if m else None

    disp, body = num("fs-display"), num("fs-body")
    if disp and body and disp / body < 2.0:
        hits.append((base, 0, "flat-hierarchy",
                     "--fs-display:--fs-body=%.2f <2.0(字号层级太平,把大字拉开)" % (disp / body)))
    txt_nc = _CSS_COMMENT.sub(" ", txt)
    # 斜体数字违和:--font-display 被设成斜体展示体(得意黑/Smiley Sans),且数据数字选择器(.num/.stat/.kpi/.metric/.figure)指向斜体展示体 → 满屏斜数字。
    display_is_oblique = bool(re.search(r"--font-display\s*:\s*(?:var\(\s*--font-hei-heavy\s*\)|[\"']?Smiley\s*Sans)", txt_nc, re.I))
    for dm in re.finditer(r"(\.(?:num|stat|kpi|metric|figure|data-num|big-?num)[\w-]*)\s*\{([^{}]*)\}", txt_nc, re.I):
        fam = re.search(r"font-family\s*:\s*([^;]+)", dm.group(2))
        if not fam:
            continue
        f = fam.group(1)
        if _OBLIQUE_DISPLAY_VAR.search(f) or re.search(r"Smiley\s*Sans", f, re.I):
            if "font-display" in f and not display_is_oblique:
                continue   # 指向 --font-display 但它不是斜体 → 无违和
            ln_no = txt[:dm.start()].count("\n") + 1
            hits.append((base, ln_no, "oblique-data-num",
                         "数据数字选择器 %s 用了斜体展示体(得意黑/Smiley Sans)——成组数据数字会满屏斜、与直立正文违和;改直立 --font-number(等宽)或 --font-sans 900" % dm.group(1)))
    # 数字口径 token 被指向斜体展示体
    if re.search(r"--font-number\s*:\s*(?:var\(\s*--font-(?:hei-heavy|display)\s*\)|[\"']?Smiley\s*Sans)", txt_nc, re.I) and display_is_oblique:
        m2 = re.search(r"--font-number\s*:", txt_nc)
        hits.append((base, txt[:m2.start()].count("\n") + 1, "oblique-data-num",
                     "--font-number 指向斜体展示体——所有 KPI/图表数字都会变斜;数字口径应走直立等宽(--font-mono)"))
    # 中文结构文字选择器用纯拉丁展示体(base.css 级,补 HTML 同行启发式的盲区:页脚/标题的中文会兜底成另一款字)。
    for sm in re.finditer(r"(\.slide-title|\.slide-footer(?!\s*\.page-no))[\w\s,>.:-]*\{([^{}]*)\}", txt_nc, re.I):
        fam = re.search(r"font-family\s*:\s*([^;]+)", sm.group(2))
        if fam and _LATIN_DISPLAY_VAR.search(fam.group(1)):
            ln_no = txt[:sm.start()].count("\n") + 1
            hits.append((base, ln_no, "mixed-script-font",
                         "%s 用纯拉丁展示体(Archivo/Fraunces/手写体)——中文标题/页脚会静默兜底成另一款字 = 违和;中文结构文字用含 CJK 字形的字体打头" % sm.group(1).strip()))
    for m in AI_HEX.finditer(txt):
        hits.append((base, txt[:m.start()].count("\n") + 1, "ai-palette", "AI 靛蓝/紫 hex " + m.group(0)))
    for m in re.finditer(r"\b(Inter|Roboto|Arial)\b", txt, re.I):
        hits.append((base, txt[:m.start()].count("\n") + 1, "default-font", "默认字体 " + m.group(1)))
    return hits


# 严重度:HARD=高置信硬点(必修);SOFT=提示(grep 分不清正当用法,结合 vision 判、别机械返工)。
SEV = {
    "gradient-text": "HARD", "ai-palette": "HARD", "default-font": "HARD",
    "hotlink-img": "HARD", "emoji-icon": "HARD", "placeholder": "HARD",
    "broken-img-path": "HARD", "unresolved-asset": "HARD",
    "base-css-stripped": "HARD",   # base.css 剥掉框架结构类 → 全册塌:零歧义硬伤(se_v25_r5 实测)
    "side-tab": "SOFT", "flat-hierarchy": "SOFT",   # 左彩条可能是正当 takeaway 引用条;层级比值仅供参考
    "no-chart-hero": "SOFT",                         # 图内 hero 读数启发式(grep 不完全,vision 定夺)
    "oblique-data-num": "SOFT",   # 斜体数字违和:高置信,但保守留 SOFT 让 audience+vision 确认(极个别单数字 hero 可能有意)
    "mixed-script-font": "SOFT",  # 中英混排:同行启发式,可能是纯拉丁元素旁注中文注释,vision 核
    "abs-bigfont": "SOFT",        # 硬编码超大字号:多数是绕过阶梯,但 clamp 内 min/封面特例存在,vision 核
    "chart-crowded": "SOFT",      # 图表密度:busy 图可能有意,vision 定夺
    "two-charts": "SOFT",         # 一页多图:§4 禁,但点睛数字+单图/sparkline 例外,vision 核
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
    if warnings:
        for warning in warnings:
            print("AI-SLOP-LINT: " + warning)
        print("AI-SLOP-LINT: INCOMPLETE — required inputs missing; no PASS emitted")
        return 2
    hits = []
    for f in slides:
        hits += lint_html(f)
    if os.path.exists(css):
        hits += lint_css(css)
    hard = [h for h in hits if SEV.get(h[2]) == "HARD"]
    soft = [h for h in hits if SEV.get(h[2]) != "HARD"]
    if not hard and not soft:
        print("AI-SLOP-LINT: OK 0 命中(高置信硬点干净;留白/卡套/大而空等模糊项仍需 vision 核)")
        return 0
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
