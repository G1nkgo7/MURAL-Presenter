# 字体库(展示 / 手写体)

deck 渲染用的**扩展字体库**——在 `Noto Sans SC` / `Noto Serif SC`(正文)之外,提供有个性的**中/英展示 & 手写字体**,让 deck 排版更丰富、更有设计感。随仓库自带、**离线可用**。

## 作用与用法
- **正文永远用 `Noto Sans SC` / `Noto Serif SC`**(可读性 + 全字形覆盖,不豆腐)。
- 本库是**展示 / 手写体**，用于**封面 / hero / 大标题 / 章节页**。按 deck 主题选择，并保持正文、数据和图表使用可读的正文栈。
- 这些是**本地字体、无 CDN**——slide 里必须按 `FONTS.md` 的 **family name 精确命名**,如 `font-family: "LXGW WenKai", "Noto Serif SC", serif;`(展示体在前、Noto 兜底)。

## 安装(渲染节点)
```bash
bash install.sh    # 复制到 ~/.fonts + fc-cache;渲染用的 headless chromium 经 fontconfig 找到
fc-list | grep "LXGW WenKai"   # 验证:能看到即安装成功
```
仓库级 `../scripts/install.sh` 会统一安装字体。字体经 fontconfig(`~/.fonts`)加载，装完即用、无需让生产 Agent 修改 render 代码。

## 目录内容
- `*.ttf` / `*.otf` — 字体文件。
- `FONTS.md` — 清单:文件 → family name → 风格 / 适用主题 → **授权**。
- `install.sh` — 安装到 `~/.fonts` 并刷新 fontconfig 缓存(幂等,可反复跑)。

## 授权
本库只包含确认允许公开分发的 **OFL 字体**(Noto、小赖字体 Xiaolai、得意黑 Smiley Sans、霞鹜文楷 LXGW WenKai、站酷快乐体 ZCOOL、Ma Shan Zheng、Fraunces、Archivo、Caveat、Patrick Hand、IBM Plex、Spectral、Architects Daughter、Indie Flower 等)。

商用字体、来源不明字体及仅限内部使用的字体不随公开仓库分发。逐项清单见 `FONTS.md`。
