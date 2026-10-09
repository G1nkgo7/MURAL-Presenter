# 公开工具与必要诊断

只在需要具体入口、参数或故障动作时读取本文件。创作判断留在主 Skill 和角色卡；工具 schema 与当前回执是参数的真源，不在文档中复制运行时状态机。

## 固定入口

运行上下文会给出当前固定 Skill 的 logical root。下文 `<logical_root>` 是占位符，调用前替换成该逻辑路径；不复制 Skill、不改用其他版本，也不把占位符原样提交。

文件内容用 `read_file`、`write_file` 和 `patch`。页面首次写入前的空容器、检查前的像素渲染、分页讲稿派生和整册交付由 Harness 自动完成，不需要模型运行生命周期命令。`terminal` 只用于当前角色仍公开的内容、素材或结构修订脚本及受控查询；不确定参数时查询一次 `--help`，随后按回执继续，不用 shell 包装或自造参数。

`read_file(query=...)` 用于定位关键词附近的文本，不代表完整阅读。需要上下文时用 offset/limit 继续相关范围。

## 附件与 MinerU

读取 MinerU 的 `document.md` 或内容清单时，当前片段直接关联的 figure/chart 会在同一个工具结果中作为图片输入返回。优先按 query 或 offset/limit 分段读取，不并行扫完整 PDF，也不为已随片段返回的图逐页调用 inspect。

图片本轮未加载时，用更窄的关键词或相邻范围继续读取；只有 OCR、公式、裁切或标签仍存在会改变结论的歧义时，才定向查看原页。

## Orchestrator 公开入口

| 需要做什么 | 入口 |
| --- | --- |
| 查看规划问题 | `python <logical_root>/scripts/orchestrator.py inspect-plans .` |
| 验证规划 | `python <logical_root>/scripts/orchestrator.py validate-plans .` |

这些不是每轮必跑的清单。共享设计由 Orchestrator 写入 `base.css`，单页内容由当前页面作者写入 HTML；自动准备的空页面不算 authored，后台交付也不替代像素验收。工作区 audit、初始化、讲稿派生和交付 finalize 是后台入口，不由模型调用；运行期浏览器缓存等内部诊断不作为页面质量问题。后台若返回真实机械缺口，沿该回执修复当前产物。

## 委派与继续

使用 `delegate_submit` 一次提交当前已经明确且可独立开工的 Research、Image 或 Slide 任务。任务目标写问题、消费者、来源/事实边界、所需产物和真正依赖；不复制整份计划、研究稿或运行记录。

Single 负责可独立完成的页面，Group 只用于必须共享坐标、底图、图例或连续表达的页面。把所有已就绪任务一起提交，不按并发槽手工分批，也不为展示并行制造任务。

页面返修交回原作者；需要继续、修复或显式接手时，使用当前回执给出的字段和工具 schema，不猜身份或责任范围。没有新决定可做时自然结束本轮，Harness 会等待真实后台事件，并在有结果、失败或需要决策时继续同一会话；不要输出轮询式状态更新。

## 页面、联系表与素材检查

统一使用 `inspect`：

```json
{"page":[2],"review_stage":"draft","question":"检查骨架、安全区与主要阅读顺序。"}
{"page":[2],"review_stage":"final","question":"验收本页是否支撑页面结论与投影阅读。"}
{"page":[1,2,3],"review_stage":"draft","question":"比较标题链、共享设计和跨页节奏。"}
{"path":"assets/figure.png","question":"核对主体、裁切、清晰度与用途。"}
```

`page` 始终是页码数组；`page` 与 `path` 二选一。question 说明用途和关注点，不预先宣布通过，也不要求忽略固定检查。页面 PNG 缺失或过期时，`inspect` 自动渲染当前 DOM/CSS；多页调用自动生成联系表，因此不需要独立的渲染步骤。

- `draft`：草稿、占位或联系表自检，不证明素材已消费或终稿成立。
- `final`：内容与关键素材稳定后的单页终稿检查。
- `blocking_recheck`：阻塞问题修复后的定向复检。

每页尽量在三次检查内收口，这是减少轻微偏好反复修稿的目标，不是页面生命周期的硬上限。正常修稿后须检查当前版本，必要的单页终稿验收和当前联系表复核不因累计次数被阻断。同一画面换 question 或 review_stage 不能绕过防循环保护；基于现有回执合并问题，修复后按疑点页定向复查。缓存重看不产生新验收。

外部视觉复核启用时，只在页面级 final/recheck 调用；素材 inspect 不调用外部 Critic。检查反馈是判断依据，不是修改命令；同一版本已有有效检查时不重复获取赞同。整册先看联系表，只有小字、细线、贴边、裁切或其他具体疑点才放大单页。

## 图片操作

公开网页视觉需要作为证据时，用 `web_screenshot` 获取真实视口图；query 只定位原文，不改写页面。返回图先 inspect，再沿用身份登记消费者，细节见[网页取证截图](../media/materials-and-images.md#公开网页取证截图)。它仅对 Orchestrator/Image 开放，不是普通正文抽取或禁网任务的替代入口。

下载、生成、登记、裁切与素材批次 finalize 仅由 Orchestrator/Image 执行，准确命令集中在[材料与图像](../media/materials-and-images.md)。每次 terminal 只提交一条完整脚本命令，逐条保留必需参数；多项独立操作用多次工具调用，不用分号串联。受控 ls 的组合语法不适用于脚本。沿工具实际返回的路径继续，不手写 Catalog，不把来源页当图片直链，也不通过重新生成整批修复路径、MIME 或登记问题。

## 写入与像素未变的诊断

patch 未命中时先读取当前短片段，再提交针对实际文本的修改；失败的 patch 没有改变文件。已检查页面开始一批修复时，在 `repair_reason` 中写清位置、阅读后果和修复目标。

修改图片位置但像素未变时，检查源图、媒体与父容器尺寸、margin、overflow、高度链和选择器是否命中；确认真实裁切窗口后再调 object-position。不要用重复微调代替诊断。

## 研究工具检索回执

`web_extract` 默认返回当前可用正文片段及可续读的 `source_ref`；沿该引用用 query 或 offset/limit 定向续读，不猜缓存路径。只有公开正文确实依赖 JavaScript 时才使用 `render_js:true`。

访问被明确拒绝、页面不存在、正文不可用或格式不支持时直接换来源。同一条件重试不能改变这类结果。暂时网络、限流或服务错误只在该来源具有独特价值时重试一次；替代来源已经支撑当前论证后立即收口。失败来源不作为事实依据。

同一 URL 的确定性失败会在本次运行中共享；同一站点连续拒绝多个页面后，该站点也会停止继续探测。回执出现 `research_convergence` 时用当前证据写稿和交接；`research_web_budget_reached` 表示当前委派范围过宽或已经过搜，不再换近义词继续调用，确有关键缺口时交回 Orchestrator 拆成更窄问题。

## 已有 Deck 的结构修改

移动、插入或删除页面使用公开 revision 流程，不手工改文件名、页序或派生演示。先运行：

```text
python <logical_root>/scripts/orchestrator.py revision-context .
```

再按照该回执和工具 schema 准备公开的 `revision-operations.json`、执行 `prepare-revision`、逐项应用并提交。只描述用户要求的实际操作；修改后更新受影响 brief、页面和讲稿，并复核相关跨页关系。普通新建、页面内容返修和收尾不启动结构修订流程。
