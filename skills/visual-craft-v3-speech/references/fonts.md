# 字体系统:角色化多字体排版("设计感 & 字体丰富"主杠杆)

优秀中文 deck 的封面 / hero **常同屏用 3–4 种字体、各司其职**,而不是通篇一款——这是"字体丰富、有设计感"的核心手法。`base-template.css` 已备好角色 token。创作环境从本地字体源渲染；最终交付由 `font_bundle.py` 把本册实际用到的可分发字体与字符裁剪成 WOFF2，写入 `assets/fonts/` 并注入 `@font-face`，因此浏览器断网、换机器仍与渲染 PNG 一致。

**先看 case 再选字**:下表是角色 vocabulary,不是题材触发器。字体由 `plan/design-brief.md` 的受众 / 场合 / 材料气质决定;题材只提供线索。严谨场合即使讲科技也可用直立宋体,轻松读书会即使有数据也可用圆润展示体;不要因为见到"数据/科技/文化"就反射式套某组字体。

## 角色 token(在 `base.css`,构建时映射为 Deck 自带字体)
| token | 字体 | 角色 |
|---|---|---|
| `--font-sans` | 思源黑 Noto Sans SC | **正文 / 表格值 / 图表轴与标注(可读锚)** |
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
| `--font-mono` | IBM Plex Mono | 代码 / 坐标 / ID / 技术眉签等宽 |
| `--font-number` | 由 Style Lock 映射到现有字体 token | Hero 数字 / KPI / 章节序号的数字主声部 |

## 数字字形按场景选

不要把“出现数字”等同于“使用等宽体”。每册在 Style Lock 里定一个 `numeric_voice`,再把 `--font-number` 映射到合适角色:

- 严谨报告、教学、通用商务:`--font-sans` 或 `--font-grotesque`,清楚稳健;
- 编辑、奢华、文化叙事:`--font-display-serif` 或 `--font-serif`,让大数字具有刊物感;
- 青年、体育、海报、娱乐:`--font-hei-heavy` / `--font-grotesque` / `--font-playful`,让数字成为图形;
- 工程、终端、代码、测量读数:只有这类语义才优先 `--font-mono`。

同一册保持主数字声部稳定,但不同数字角色仍要分工:Hero/KPI/章节号用 `--font-number`;表格、图表轴和解释性数值用 `--font-sans` + `tabular-nums`;代码、坐标、ID 和技术眉签才用 `--font-mono`。

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
- **技术 / 数据 / 科技**:`--font-hei-heavy` 得意黑大字 + `--font-sans` 正文与图表值 + `--font-grotesque` Archivo 数字主声部;只有代码 / 坐标 / ID 用 `--font-mono`。
- **电影 / 漫画 / 活泼 / 儿童**:`--font-playful` 站酷快乐体标题 + `--font-sans` 正文 + `--font-hand-en` Patrick Hand 批注。
- **文旅 / 传统 / 文化 / 人文**:`--font-brush` 马善政毛笔主标题(大字) + `--font-kai` 文楷正文 + `--font-write` 段宁硬笔楷点缀(小字批注用工整档;仅大字引题才用 `--font-write-cursive` 行书)。
- **编辑 / 杂志**:`--font-display-serif` Fraunces 标题 + `--font-serif` / Spectral 正文 + `--font-grotesque` accent。

## 规约
- **正文 / 表格 / 图表标注使用 `--font-sans`**(黑体,可读);Hero/KPI/章节数字走 `--font-number`;代码与技术编号走 `--font-mono`。书法 / 手写 / 展示体只点封面 / hero / 金句 / 批注 / 章节,不用于长正文。
- **按 register 定款数(与 design-rules §2、design-styles 一致)**:**表达型**(封面 / 漫画 / 电影 / 文旅 / 杂志)鼓励 **3–4 款各司其职**、别只"衬线+无衬"两款单调——至少给封面 / hero / 章节配第 3 款展示 / 书法体;**克制 / 严谨型**(学术 / 报告 / 政务)收到 **2–3 款**(衬线 + 无衬,点缀体也走克制、别上高调花体)。共通:每款**各有职责、别乱**(一款主标题展示 + 黑体正文 + 一款标签体,足矣),层次优先靠**字号 / 字重 / 字距**拉开。
- 写法:直接 `font-family: var(--font-brush);`(token 已含正确本地栈 + Noto 兜底);手写栈也可 `"Ma Shan Zheng", "Noto Serif SC", cursive`(**Noto 永远兜底,缺字不豆腐**)。
- **页面只用角色 token，不手写网络 `@import` 或外部字体 URL。** `font_bundle.py` 会把允许分发的字体改写为唯一 Deck family；授权不明的本地字体会确定性映射到气质接近的可分发字体，并用同一结果重渲 PNG。想扩充交付字体，必须同时提供许可明确的字体源并登记到 `font_bundle.py`，不能只在服务器 `fc-cache` 后假定用户浏览器也有。
