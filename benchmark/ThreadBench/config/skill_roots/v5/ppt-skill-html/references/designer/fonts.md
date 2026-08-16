<!-- Reference owner: Designer. Allowed consumers: Designer only. -->

# 字体系统:角色化多字体排版("设计感 & 字体丰富"主杠杆)

优秀中文 deck 的封面 / hero **常同屏用 3–4 种字体、各司其职**,而不是通篇一款——这是"字体丰富、有设计感"的核心手法。`base-template.css` 已备好角色 token,**全部字体本地已装、断网也稳渲**,直接用;**需要更丰富的展示体 / niche 字体时可再引 CDN Google Fonts(在线优先 + 本地缓存兜底,render.py 已托管,见 §规约的「CDN 字体」条)**。

**先看 case 再选字**:下表是角色 vocabulary,不是题材触发器。字体由 `plan/design-brief.md` 的受众 / 场合 / 材料气质决定;题材只提供线索。严谨场合即使讲科技也可用直立宋体,轻松读书会即使有数据也可用圆润展示体;不要因为见到"数据/科技/文化"就反射式套某组字体。

## 角色 token(在 `base.css`,全本地已装,每栈以 Noto 收尾无 tofu)
| token | 字体 | 角色 |
|---|---|---|
| `--font-sans` | 思源黑 Noto Sans SC | **正文 / 数据 / 图表标注(永远用它,可读锚)** |
| `--font-serif` | 思源宋 Noto Serif SC | 编辑感标题 / 引言 / 小标签 |
| `--font-hei-heavy` | 得意黑 Smiley Sans | **超粗大字 hero**(配 `font-weight:900` 压场);斜体、**墨迹远超行盒**——多行 hero 别叠偏移套印重影(上行糊下行)、行距给足,见 design-rules §3 |
| `--font-brush` | 马善政毛笔 Ma Shan Zheng | 封面 / hero **艺术主标题**、金句(文旅 / 传统) |
| `--font-kai` | 霞鹜文楷 LXGW WenKai | 端庄标题 / 章节 / 可读楷体正文 |
| `--font-write` | **段宁硬笔楷(默认·工整清楚)** | 批注 / 引语 / 小字点缀 **首选**(端正不糊) |
| `--font-write-cursive` | 叶根友钢笔行书(草书) | 连笔艺术感,**仅**大字 hero / 金句;**别用于小字**(小字发糊看不清) |
| `--font-playful` | 站酷快乐体 ZCOOL KuaiLe | 活泼 / 消费 / 儿童 / 漫画标题 |
| `--font-round` | 软萌小果冻 Lovely Little Jelly | 圆润可爱(比快乐体更软)· 儿童 / 活泼品牌 |
| `--font-jotter` | 今年也要加油鸭 | 手写活泼 · 励志 / 校园 / 手账 |
| `--font-display-serif` | Fraunces | 编辑 / 杂志展示衬线标题 |
| `--font-grotesque` | Archivo | 现代无衬线展示 / accent |
| `--font-hand-en` | Patrick Hand / Caveat | 英文手写批注(工整可读) |
| `--font-hand-en-neat` | Architects Daughter | 英文手写(更工整)· 清楚 |
| `--font-hand-en-casual` | Indie Flower | 英文手写(随性但清楚)· 便签 / 涂鸦感 |
| `--font-mono` | IBM Plex Mono | 代码 / 数据标注等宽 |

## ★手写 / 书法分两档 —— 别把"草"的用在小字(治"手写太草看不清")
中文手写 / 书法体有**工整档**和**草书档**,按**字号 + 位置**选,别一律用草的:
- **工整档(可读,默认用这些)**:`--font-write` 段宁硬笔楷、`--font-kai` 霞鹜文楷、`--font-jotter` 加油鸭、`--font-round` 软萌小果冻。**批注 / 引语 / 标签 / 小字点缀 / 需要读清的地方 → 只用工整档。**
- **草书档(艺术连笔,克制用)**:`--font-write-cursive` 叶根友行书、`--font-brush` 马善政毛笔。**只用在大字号 hero / 封面主标题 / 金句(≥48px、且是"感受氛围"不是"逐字读"的场景)**;字号一小就连笔糊成一团、看不清。
- 一句话:**要读清 → 工整档;要氛围且够大 → 草书档。** 设计反馈"手写太草看不清"= 把草书档用到了该用工整档的小字位。

## 封面多字体配方(参考真实优秀 deck)
一张文旅封面的典型分工:
- **主标题大字**:`--font-hei-heavy` **得意黑** 或 `--font-sans` **900 超粗黑体**(压场)
- **艺术副题 / 引题**:`--font-brush` **马善政毛笔**(氛围)
- **印章 / 小标签 / 编号**:`--font-serif` 思源宋 或 `--font-kai` 文楷
- **手写批注**(小字要读清):`--font-write` 段宁硬笔楷(工整档);**大字艺术引题**才用 `--font-write-cursive` 行书 / `--font-brush` 毛笔
> 三四款同屏、各司其职,**富而不乱**。

## 按场景挑(title / body / accent 三档 = 5 场景搭配栈)
- **学术 / 科研 / 报告**:`--font-serif` 思源宋标题 + `--font-sans` 正文 + `--font-kai` 文楷点缀(克制)。
- **技术 / 数据 / 科技**:`--font-hei-heavy` 得意黑大字 + `--font-sans` 正文 + `--font-grotesque` Archivo / `--font-mono` 数据标注。
- **电影 / 漫画 / 活泼 / 儿童**:`--font-playful` 站酷快乐体标题 + `--font-sans` 正文 + `--font-hand-en` Patrick Hand 批注。
- **文旅 / 传统 / 文化 / 人文**:`--font-brush` 马善政毛笔主标题(大字) + `--font-kai` 文楷正文 + `--font-write` 段宁硬笔楷点缀(小字批注用工整档;仅大字引题才用 `--font-write-cursive` 行书)。
- **编辑 / 杂志**:`--font-display-serif` Fraunces 标题 + `--font-serif` / Spectral 正文 + `--font-grotesque` accent。

## 规约
- **正文 / 数据 / 图表标注永远 `--font-sans`**(黑体,可读);书法 / 手写 / 展示体**只点封面 / hero / 金句 / 批注 / 章节**,不用于长正文。
- **★斜体 / oblique 展示体(得意黑 `--font-hei-heavy`)只给封面 / hero 的「单一大字」**——**成组数据数字**(KPI / 图表标注 / 统计值 / `.num`)**一律直立**(`--font-number` 等宽 或 `--font-sans` 900),别把数字指向 `--font-display`(当它被设成得意黑时)。整排斜数字 + 直立正文 = 说不上来的违和;要冲击靠字重 / 描边,不靠斜体。
- **★纯拉丁展示体绝不打头中文**:`--font-grotesque`(Archivo)/ `--font-display-serif`(Fraunces)/ `--font-hand-en*` 无中文字形,**只用于纯拉丁文本**(眉签 / 页码 / 英文词 / 数字);中文标题 / 正文 / 页脚中文一律用含 CJK 的字体打头,**否则中文静默兜底、同一行两套字 = 违和**。**整页语言统一,别中英并行两套文本。**
- **按 register 定款数(与 design-rules §2、design-styles 一致)**:**表达型**(封面 / 漫画 / 电影 / 文旅 / 杂志)鼓励 **3–4 款各司其职**、别只"衬线+无衬"两款单调——至少给封面 / hero / 章节配第 3 款展示 / 书法体;**克制 / 严谨型**(学术 / 报告 / 政务)收到 **2–3 款**(衬线 + 无衬,点缀体也走克制、别上高调花体)。共通:每款**各有职责、别乱**(一款主标题展示 + 黑体正文 + 一款标签体,足矣),层次优先靠**字号 / 字重 / 字距**拉开。
- 写法:直接 `font-family: var(--font-brush);`(token 已含正确本地栈 + Noto 兜底);手写栈也可 `"Ma Shan Zheng", "Noto Serif SC", cursive`(**Noto 永远兜底,缺字不豆腐**)。
- **★CDN 字体(在线优先 + 离线兜底,放心用但守纪律)**:本地字体是**可靠默认**;需要更丰富的展示体 / niche 字体时,**可用 `<link>` 或 `@import` 引 CDN Google Fonts**——`render.py` 走「在线优先 + 本地缓存复用 + CDN 挂落系统栈」,断网也不吊死。三条纪律:① **每个字体栈仍以本地 `Noto Sans/Serif SC` 收尾**(缺字不豆腐、断网不塌);② **中文正文 / 数据 / 图表标注仍优先本地思源黑**(可读锚,别拿 CDN 字体做长正文);③ **CDN 字体是 designer 判断下的「增益选项」、不是默认必用**——变量越多越易违和,守本文的斜体 hero-only / 中英分治纪律(纯拉丁 CDN 展示体只配纯拉丁文本)。想固化某展示体:把 ttf 放 `ppt-agent/fonts/` 并 `fc-cache -f` 后按 family name 引用。
