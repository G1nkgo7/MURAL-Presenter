# 字体清单(family name = CSS `font-family` 里要写的精确名)

本库随仓库自带、`bash install.sh` 装进 `~/.fonts`、经 fontconfig 供 headless chromium 使用(**零网络、无 `@import`、断网也稳渲**)。
当前双语 Clean Skill 的字体 token 位于 `../skills/ppt-skill-html-clean-{zh,en}/assets/base.css`；优先直接引用其中的 `--font-*` token。

## 正文基础(始终作正文 / 兜底,永远排在字体栈末尾)
| family name | 文件 | 说明 | 授权 |
|---|---|---|---|
| `Noto Sans SC` | NotoSansSC.ttf | 黑体,正文 / 数据 / 图表标注首选 | OFL ✅ |
| `Noto Serif SC` | 由仓库级 `scripts/install.sh` 安装 | 宋体,标题 / 引言 | OFL ✅ |

## 中文展示 / 书法体(封面 / hero / 大标题 / 章节页,按主题选)
| family name | 文件 | base.css token | 风格 / 适用主题 | 授权 |
|---|---|---|---|---|
| `Smiley Sans`(得意黑) | SmileySans-Oblique.ttf | `--font-hei-heavy` | 视觉粗重的单权重 400 Oblique 大字 hero · 科技/数据/力量感 | OFL ✅ |
| `Ma Shan Zheng`(马善政毛笔) | MaShanZheng-Regular.ttf | `--font-brush` | 毛笔楷 · 文旅/传统/人文 主标题·金句 | OFL ✅ |
| `Xiaolai`(小赖字体) | Xiaolai-Regular.ttf | `--font-write` / `--font-jotter` | 清晰硬笔手写 · 只用于真实批注/便签/课堂板书 | OFL ✅ |
| `LXGW WenKai`(霞鹜文楷) | LXGWWenKai-Regular.ttf / -Bold.ttf | `--font-kai` | 端庄楷体 · 人文、文化类章节与短引言 | OFL ✅ |
| `ZCOOL KuaiLe`(站酷快乐体) | ZCOOLKuaiLe-Regular.ttf | `--font-playful` | 圆润活泼 · 儿童/消费/漫画标题 | OFL ✅ |

## 英文展示 / 手写 / 等宽
| family name | 文件 | base.css token | 风格 | 授权 |
|---|---|---|---|---|
| `Fraunces` | Fraunces.ttf | `--font-display-serif` | 杂志展示衬线标题 | OFL ✅ |
| `Archivo` | Archivo.ttf | `--font-grotesque` | 现代无衬线展示 / accent | OFL ✅ |
| `Spectral` | Spectral_1.ttf / _2.ttf | `--font-serif`(兜底) | 编辑感衬线正文 | OFL ✅ |
| `IBM Plex Sans` | IBMPlexSans_1.ttf / _2.ttf | `--font-sans`(兜底) | 现代无衬线 | OFL ✅ |
| `Patrick Hand` | PatrickHand-Regular.ttf | `--font-hand-en` | 英文手写 · 漫画/手账批注 | OFL ✅ |
| `Caveat` | Caveat.ttf | `--font-hand-en`(兜底) | 英文手写 · 随性 | OFL ✅ |
| `Architects Daughter` | ArchitectsDaughter-Regular.ttf | — | 英文手写 · 工整 | OFL ✅ |
| `Indie Flower` | IndieFlower-Regular.ttf | — | 英文手写 · 随性 | OFL ✅ |

> `--font-mono`(IBM Plex Mono)本库未带,渲染时回退到系统等宽(仅用于代码/数据标注,影响小);需要精确等宽可自行补 `IBMPlexMono-*.ttf`。

## 用法规约
- 写法:`font-family: var(--font-brush);`(token 已含正确本地栈 + Noto 兜底);或手写栈 `"Ma Shan Zheng", "Noto Serif SC", cursive;` —— **展示体在前、Noto 永远兜底**(缺字不豆腐)。
- **正文 / 数据一律 `Noto Sans SC` / `Noto Serif SC`**(可读性);展示 / 书法 / 手写体**只点封面 / hero / 金句 / 批注 / 章节**,不用于长正文。
- 默认只使用标题、正文、数字 3 个角色；只有主题确实需要时才增加 1 个书法/手写/海报点缀角色。同屏通常不超过 3 个家族。中文眉签、部门名、页脚和元数据不得使用等宽字体或拉丁 ALL CAPS 疏字距。

## 授权
- 本目录只收录确认允许公开分发且文件完整可用的 OFL 字体。
- 不要提交商用字体、来源不明字体或仅限内部使用的字体；需要新增字体时，请同时记录授权来源。
