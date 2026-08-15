# 素材与图片

## 素材提取

只提取演示可用的内容：

- 事实与主张；
- 带单位和时间周期的数字；
- 表格与图表数据；
- 引文及其出处；
- Figure/Table 说明文字、标签、数值与视觉主体语义；
- 冲突、缺失定义与不确定性。

保留足够精确的来源位置以便核实。

## Harness 附件路由

先读取 `_trace/attachment-manifest.json`，不要通过遍历目录猜附件用途：

| intent | 是否 Material | 消费者 | 规则 |
|---|---:|---|---|
| `direct_text` | 否 | Research / Orchestrator | Harness 原样写入 `research/material.md` |
| `visual_asset` | 否 | Image | `register-user` 后登记 `kind: user` |
| `style_reference` | 否 | Orchestrator | Vision 查看一次，只影响视觉合同 |
| `evidence` | 是 | Material | OCR/Vision 提取事实、文字、图表与边界 |
| `mixed` | 是 | Material + Image | 内容取证与像素复用分别交接 |

扩展名不决定图片是否需要 Material。普通照片、Logo、插图默认是视觉素材；截图、扫描件、
报表或用户要求“读取/分析/提取内容”的图片才是证据。用户显式 intent 优先。

## 工作顺序

1. 解析所提供的素材。
2. 确定尚待回答的问题。
3. 研究这些问题。
4. 把附件视觉整理成可复用主体候选或基于 OCR/提取稿的替代 brief。
5. 独立 Figure/照片经 `crop-material` 审计后复用；其余从可信来源重新获取、为非特定
   概念重新生成，或把数据与关系交给 Slide 忠实重绘。

这样可避免重复搜索与证据自相矛盾。

## 图片选择

在以下情况下接受某张图片：

- 支持预定要传达的信息；
- 分辨率满足预期裁剪需要；
- 视觉风格与整套幻灯片协调；
- 来源与使用方式已记录在案；
- 无可见水印或误留文字。

“文件像素够大”不等于“适合全出血”：老照片可能由低清视频帧、扫描或早期网页图放大而
来。按最终展示尺度判断有效细节；素材若需要明显上采样，或原生细节在联系表上已经发糊，
就在 catalog 标为 `quality_intent: archival` / `full_bleed_ready: false`。这类真实证据不必
删除，但应以有边界的编辑式构图、局部裁切、接触印相/胶片格、纹理底和大排印来承载，
不能当作现代高清摄影铺满整页。非事实氛围可以生成一张合题背景替代，但不能生成具名
历史人物或事件的仿纪实画面。

对于生成的图片，检查实际输出结果。切勿仅凭提示词就予以信任。

## 来源决策（由 Orchestrator 冻结，Image 执行）

Orchestrator 依据信息任务本身选择方法，而非图方便；选择直接写入现有
`primary_visual_medium`，不再新增逐页机会表、反证字段或额外规划文件：

| 信息任务 | 首选来源 | 示例 |
|---|---|---|
| 使用用户直供独立图片 | `bitmap-material` → `register-user` 受控注册 | 照片、Logo、插图 |
| 保留附件独立视觉 | `bitmap-material` → Material 定位 + `crop-material` | 独立 Figure、照片、插图 |
| 保留附件证据语义 | OCR/提取稿 grounding 后重新表达 | caption、关键标签、数值、方法关系 |
| 呈现真实身份或事实场景 | `bitmap-real` → 搜索真实图片 | 具名人物、艺术品、地点、建筑、产品、文档、事件 |
| 营造可控原创表达 | `bitmap-generated` → 图像生成 | 概念体验、未来愿景、未建成空间、氛围、隐喻、协调视觉系列 |
| 说明数据或关系 | 由 Slide 制作 SVG/CSS/HTML | 图表、流程、时间线、架构图、箭头、标注、图标系统 |

媒介优先级：真实身份/场景用真图，非特定概念/氛围用生成位图，只有信息结构本身是
数据、流程、架构、机制或关系时才优先 Canvas/CSS/HTML；SVG 仅在简单、短标签、矢量
几何确有价值时使用。SVG 不是因为“更容易生成”就可
替代照片、场景、实物或氛围图的默认方案。

若用户要求真实人物或真实场景，使用来源可靠的真实图片。不要为具名人物、艺术品、事件、产品或地点生成纪实性替代图。不要将图表或图示生成为栅格图片。Image 只能在所选
路线内部决定 query、prompt、候选与裁切；不能临场把生成改成搜索、把真图改成生成，或
把附件复用改成“找一张相似图”。失败时返回精确缺口，由 Orchestrator 决定是否改计划。
不得把整张附件页图、扫描页、页眉页脚或文字密集裁图复制进 `assets/`。Material 已
核对的独立 Figure、照片或插图可以由 Image 用 `crop-material` 裁取；脚本拒绝过大、
贴边、低分辨率和文字过重的区域。通过后登记 `kind: material`。整页 facsimile（文档
翻拍）即使看似完整 Figure，也必须附有 `facsimile_justification`（≥20 字说明为何整页
原貌本身是证据）；缺少理由时 `crop-material` 拒收，应缩小到独立主体区域。若无法安全复用，论文
Figure 可从官方论文页、项目页或作者页作为 `real` 重新下载；否则只使用 OCR/提取稿
确认的语义生成概念画面，或按已核实数据制作代码视觉。生成结果不能冒充原 Figure。

生成的图片应遵循整套幻灯片层面统一的美术方向，而将可读的文字、标签、日期与徽标
交由 HTML 处理。真实图片可施加 CSS 色调、遮罩、双色调或渐变叠加，使其融入整套
幻灯片而不失其识别性。位图不能改善信息时，对真实关系优先制作代码视觉，或使用有
意识的纯排版构图。只有克制本身能增强页面时才使用 none，不能因为请求没有附图就
默认不用视觉；也不要为占据空间添加装饰。

## 承载身份的图片

当页面主题为已知人物、艺术品、地点、产品、文档或实物时，图片承载的是身份而非装饰。

- 优先选用可识别、可溯源的图片，而非通用剪影或图标。
- 不要为真实人物生成看似纪实的肖像。
- 记录裁剪安全区，以确保主体在幻灯片中保持可识别。
- 明确的作者/人物介绍在有可信图片时应使用真实来源的肖像；通用 SVG 半身像不是等效替代。
- 当裁切仍成立时，一份高分辨率文件可以分配给多页；目录只保留一条记录和一个本地
  路径，不重复下载原图或生成派生切片。
- 若无合适图片，报告该缺口并让 Orchestrator 修改构图，而不是悄悄塞入占位图。

## 目录

在 `assets/catalog.md` 中记录：

```markdown
## example
- slides: 4, 7
- kind: real
- path: assets/example.png
- source: https://example.com/source-page
- purpose: 为具名主体提供身份性证据
- crop: 主体在右，左侧为标题安全区
- display: full-bleed | large | inline
- quality_intent: standard | archival
- expect_transparent: false
```

`kind` 使用 `real`、`generated`、`material` 或 `user`。`user` 的 source 写原始
`inputs/NAME`，path 必须由 `register-user` 生成；`material` 的 source 写原附件与页码，
path 必须是 `crop-material` 生成的 `assets/NAME.png`。Slide 收到目录与本地路径后不再
进行二次搜索，也不得回到 `inputs/**` 猜路径。
