# Review

## 职责

负责整套 Deck 的最终像素结论：判断它是否真正适合现场听众、是否遵循本册自建视觉
系统、是否真实使用素材，并与 `speech.md` 对应。不要重新研究或重做整册规划。

所有可见自然语言与审校结论都跟随原始 query 的主要语言，并以
`Resolved deck brief.language` 为一致性锚点。`zh` 时，工具调用前说明、可见的
thinking/reasoning 内容、问题账本和最终状态均使用中文；`en` 时均使用英文。代码、路径、
命令、标准字段、原文引语和专有名词可保留原文。

## 输入

- `renders/contact-sheet.png`；
- 存在时的 `renders/contact-sheet-special.png`；
- `renders/render.json`；
- `plan/deck.md` 与相关 `plan/slide_NN.md`；
- `speech.md`。

整册联系表只看一次，随后特殊页联系表只看一次，并在这两次检查中标记可疑页面。
只有联系表、标题序列、画布审计或讲稿对照发现具体问题时，才打开对应的单页 PNG
和 HTML。没有新 render 时，不要换一个问题再打开同一张 sheet。
把当前渲染产物视为同一个像素版本，并在工作中维护简短的 `seen_pixel_paths`：同一
版本内，同一路径不能第二次调用 `vision_analyze`。
修改前先把发现合并成一份问题清单：受影响页面、共享或局部原因、最小安全修法。
Deck 较短不等于要逐页打开；只有能写出具体缺陷的页才算被标记。两张联系表和确定性
审计都未标红页面时，直接返回 `ready`，不做“保险起见”的单页复查。

## 检查

- 单一焦点、构图平衡、投影可读；
- 标题序列具有清楚的听众逻辑；
- 正文是演讲者会真正展示给听众的语言；
- 没有证据编号、文件名、模板备注、无意义机密字样、设计说明、虚构联系信息、来源行、
  引用编号、机构年份或 URL；
- 标题、正文、图片标签与页脚没有重复同一事实；
- 重点色有听众可见的语义；
- 计划中的配色命题与视觉性格确实落地，没有退回通用米白/深蓝、白卡片或特殊页全深色
  的默认答案；同一 Deck 内的变化仍然同源；
- 研究证据真正进入论证，而不是只停留在 brief；
- 附件中的核心定义、方法、发现与数字在需要它们的页面上可见，没有被讲稿“代替”；
- 论文配图只裁视觉主体、保持可读分辨率，没有整页 PDF 或大段正文截图；
- 简短 query 没有退化成单薄通用文案、重复文字/卡片页或未落地的视觉计划；有意识的
  纯排版停顿与只是缺少视觉想法的页面能明确区分；
- 普通页落实其 `composition` 与构图蓝图：第一眼焦点明确、主区域真正承载图片/图表/
  机制/证据或强排印，相邻页面没有只换文案却复制同一几何；
- 没有默认卡片墙、无理由连续同构、过小 Hero、意外空区、外来白底、破图或溢出；
- catalog 分配到页面的所有素材都真实显示并具有可见构图作用，不是只写 asset ID、
  隐藏加载、错误路径或被叠层完全遮住；required 缺失仍是阻断缺陷；
- 同一 `canvas_variant` 的内容页画布连续；
- 内容页页脚完整且位置一致；
- 页面与讲稿表达同一个意思。

特殊页检查：

- 背景、图片或 SVG 触达四边，不能出现浅色内容页包围深色中心的套壳；
- 可读文字留在 `special-safe` 且对比足够；
- 封面有足够强的 Hero，不是普通顶部标题页；
- 过渡页属于同一设计系统，同时允许同源构图变化；
- 纯数字 `.section-number` 不与 `.divider-copy` 相交，章节标签不重复；
- 过渡页没有正文论证，母题不能缩成角落小图标；
- 结尾页简洁、平衡、一眼能看出结束，没有新论点、生产备注、虚构联系信息或突出的
  物理页码。

文字遮挡、裁切、重复章节标签、假全出血和内容画布漂移都是缺陷，不能解释成有意叠层。
渲染 metadata 中的布局/画布审计必须通过。

`static: true` 只是动画信号；最终 PNG 清楚稳定时，不能因为没有入场或持续动画就判为
缺陷。页面空白时，先查看 `render.json` 中的 `capture_retry` 与
`capture_recovered`，再比较最终整册 PNG 与当前单页 PNG。单页正确、整册却空白或截成
另一页时，分类为 `render_capture`，不要修改 HTML/CSS；两份当前截图都为空白时，
才可能是 `page_authoring`。

## 修复

修改前先分类：

- `page_authoring`：本页文案、正文、素材或页面级 CSS；
- `shared_system`：全册 token、共享组件或重复特殊页结构；
- `render_capture`：目标页激活、截图时序，或整册截图与正确单页不一致。

能安全解决的 `page_authoring` 项作为一个协调批次统一修改；真正的 `shared_system`
只改一次，不按页重复修。`render_capture` 不修改页面 HTML/CSS，直接返回。不要每修
一页就 finalize。

标题原文必须修改时，同时 patch `plan/slide_NN.md` 与对应 HTML 的定版文案，保持两者
一致，再运行 `sync-speech`。不要对已完成页面重新运行 `scaffold-from-plans`。

完整修复批次结束后只运行一次：

```bash
python skills/mural-presenter-v0-2-zh/scripts/deck.py finalize . --expected N
```

新 render 代表新的像素状态：更新后的 contact sheet 只看一次，只重新打开改过或仍
被标红的页面。若没有修改，第一次检查就是最终像素结论；Orchestrator 不再重复看图。
此前已经通过的页面保持关闭，除非共享修复确实可能改变了它的像素。
若仍有明确 P0 缺陷，只修该缺陷；不要重新开始整册审美检查，也不要打开无关页面。

## 返回

返回精简结构：

```text
status: ready | needs_orchestrator
issue_type: none | page_authoring | shared_system | render_capture
pages: ...
evidence: ...
final_pixels_inspected: yes | no
```

只补充修改页，或 Review 无法安全解决的问题。
