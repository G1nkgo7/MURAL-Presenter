<!-- Reference owner: Designer. Allowed consumers: Designer only. -->

# fancy-effects · 可选视觉增强(仅表达型 · 克制优先)

> **这是「增益选项」,不是默认。** 只有**表达型**场合(封面 / hero / 漫画 / 电影 / 文旅 / 产品发布 / 品牌)才考虑;**党政 / 学术 / 报告 / 政务 / 法律 / 医疗一律不用**——它们靠排版秩序立庄重,加动效/炫背景=一眼跑偏。designer 在 `design-brief.md` 里判定本 deck 是否属表达型、是否启用。
>
> **★头号纪律(记忆教训:背景 push 极易过度矫正成 busy 低对比丑氛围图→判垃圾):克制 + 协调 + 对比 > 有没有炫背景。** 宁可干净纯色/极简母题底,也不要浑浊跑调的满屏粒子。加任何 fancy 前先问:它服务主题吗?够克制吗?文字还压得住吗?答不上就别加。

## 渲染事实(决定 fancy 怎么用)
- deck 渲染 = **静态截图**(约 2.6s 定帧)。**动画本身在 PNG 里看不到** → fancy 对静态图的价值 = **更有氛围的静态背景**(粒子场/渐变光晕/shader 噪声,截图时定帧可见),动画只在 `present.html` 播放时体现。
- 所以:**优先做「定帧就好看」的氛围背景**,别指望动画在评审图里加分;做的动画必须 **≤2s + `animation-fill-mode:forwards`**(2.6s 截图时已停在终态,不截到半截)。
- **优雅降级**:CDN 库(Three.js/GSAP)可能加载失败 → **关键内容绝不能藏在需 JS 才出现的层里**;背景是增强、缺了也要静态可读。库从 `cdn.jsdelivr.net` 引(render.py 在线优先+缓存+降级)。

## 三类可移植技法(借 dazzle fancy-cookbook,仅 entrance/background/interaction;跨页转场归 present.html 不在此)

### 1. 氛围背景(background · 对静态图最有用)
- **Canvas 2D 粒子/星野**(轻、稳、无需 Three.js):氛围粒子 **30–60 个**、速度 <0.2px/帧、alpha .25–.75;星野分三层(180/90/40,速度比 1:3:7,近层加光晕)做景深。**内容页把粒子 opacity 砍到 .25–.3、glow 减半**(背景化,别抢文字)。
- **Three.js 粒子场**(封面/hero 才值得上):封面震撼档 dark 3000 / medium 7000 / bright 12000+;**内容页一律降到背景档**(半透明遮罩+冷色单调)。
- **渐变光晕 / shader 噪声 / 径向暗角**替代 grid——**禁网格底纹**(方格纸/蓝图网格/透视网格地面/GridHelper 一律不用做背景)。
- **★scrim 护文字**:内容压在动态背景上,前景**必带底板**(径向 scrim center alpha .8–.9 / 卡片底板 .75–.92 + blur 5–8px),别裸压。低对比丑背景 = 头号硬伤。

### 2. 入场动效(entrance · 只在 present.html 体现,截图看终态)
- 标题/元素淡入上移、逐字/逐行 stagger、数字滚动到终值——**全部 ≤2s + fill-mode:forwards**。GSAP 或纯 CSS `@keyframes` 皆可。
- **数字滚动/计数**必须 fill-mode:forwards 停在真值(别截到中途的假数)。

### 3. 交互质感(interaction · 静态图里看的是「像可交互」的视觉暗示)
- hover 态/焦点态的视觉设计(截图取默认态);别做需鼠标才可读的关键信息。

## 硬门(踩到即改)
- ⛔ 非表达型 deck 用 fancy(党政/学术/报告…)。
- ⛔ busy / 低对比 / 浑浊 / 跑调的满屏氛围背景(克制+协调+对比优先)。
- ⛔ 关键内容藏在需 JS/CDN 才出现的层(降级后空白)。
- ⛔ 动画 >2s 或无 fill-mode:forwards(截图截到半截/假数)。
- ⛔ 全 deck 每页各生各的背景(要**一套背景配方**贯穿,和 design-brief 的 art-direction 一致)。
- ⛔ 网格底纹背景 / GridHelper。

## 谁决定
- **designer**:在 `design-brief.md` / `art-direction.md` 里判定是否表达型、定**一套**背景配方(媒介+密度+配色),写清「本 deck 用/不用 fancy」。
- **slide**:表达型页按 art-direction 落地背景+入场;守上面硬门 + 文字 scrim;渲染后看图确认背景没糊掉文字、没 busy。
