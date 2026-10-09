# 证据素材与图像制作

按当前需求读相应章节；本文件帮助把语义视觉需要落实为可用素材，不要求每项都走搜索、生成、抠图和逐图检查。

## 从证据到视觉，而不是为留白找装饰

先消费已有 knowledge.md 中的事实、来源和候选，不为获取一张图重开主题研究。一个真实对象、文档、场景或数据关系能够
直接承担标题时，让它有足够尺度；没有表达作用的配图不做。附件出现了某张图，不等于整册都必须使用附件图。

| 本页任务 | 合适的来源与方法 |
| --- | --- |
| 保留用户给出的证据 | 原 Figure、照片、文档或提取的数据，保留来源与边界 |
| 真实身份、产品、作品、地点、事件 | 可识别、可信、可追踪的真实图片 |
| 允许的概念、隐喻、情境或统一艺术系列 | 生图；不伪装为纪实、史料或实验结果 |
| 数量、顺序、因果、层次与精确图层 | 页面作者的数据图/关系图；关键数值、单位和标签可核对 |
| 没有图像价值的命题 | 有主张的排印或直接关系表达，不填装饰图标 |

用户指定的真实对象不能静默降级成通用剪影或生成替身。真实视觉可通过色调、遮罩与排印融入主题，
但不能破坏识别性。复杂产品、场景与质感插画不默认交给大型手写 SVG；精确数据与简明几何不受此限制。
生成图遵守整册同一个艺术方向，把 deck.md 的调色来源、视觉性格、媒介、光线和材质转成画面要求；生成图不包含可读文字、日期、徽标或水印。真实图片可以通过裁切、CSS 色调、scrim、双色调或叠字融入视觉系统，但不能因此失去身份、事实细节或必要标签。
当栅格图不能增加信息时，优先使用有主张的排印或代码关系图；不要为填空添加装饰图。

## 判断候选是否适用

先看主体身份、来源、许可和素材类型，再看目标裁切后的清晰度与构图。
海报不冒充剧照，文件名不证明身份；搜索摘要也不证明实际图片包含所需对象。
一张高分辨率图可以服务多页，前提是各目标裁切都成立；一条 catalog 记录保留多个消费者，避免重复下载。
每页/每个明确视觉用途最多保留 3 个可信候选；已有 1 个满足身份、清晰度、裁切、来源和该用途必需可见内容的可用项后停止搜索。用户指定措辞、对象或细节必须在原件实际像素中可见，网页正文和邻近排版引文不能替代；缺少关键内容的真实图片只作辅助，不算必需原件已交付。
只 inspect 终选候选和发生变化的裁切图，不为所有备选逐张检查。每个素材最多 1 次裁切或替换重试；
仍不满足时登记 `unavailable` 或交给 Orchestrator 改视觉路线，不用近义词搜索和重复下载继续消耗上下文。
承载身份、场景或证据的图片应在页面里成为构图锚点，而不是缩成装饰缩略图。选材时记录 crop-safe region 与 text-safe region：主体怎样保持可识别，标题或解释文字放在哪里不会遮住证据。

图幅比例不是机械门。竖图用于横向 full-bleed 时，要确认保留下来的窗口仍显示关键主体与文字静区；
不成立则使用 split/contain 或换素材，不能仅凭尺寸、铺满倍率或工具成功判断素材可用。
full_bleed 的父容器需真正覆盖画布；若 source 是竖图但 object-position 完全无效，
应查 wrapper 的 margin、高度链、选择器、overflow 和 bbox，不先判断“源图也一样宽”。

## 显式登记，Catalog 只供阅读

```text
python <logical_root>/scripts/image.py register . --id cart-hero --path assets/cart-hero.jpg --kind real --slides 1,3 --status pending --source '真实来源页' --download '真实图片直链' --purpose '对象身份与开场焦点' --crop '已核验的主体范围' --display large
```

kind 为 real / generated / material / user；生成图不填 download，附件图保留原件定位与真实页码。
required 的真实性和关键主体不能用近似替代蒙混；缺失项用 `--status unavailable --reason '原因'` 登记。
许可、署名、限制和多行交接用 `--notes`；主体区、文字静区用 `--subject-region`、`--text-safe-region`，不要另建说明 sidecar。
署名按具体条款记录是否必须可见及所需文字、位置或链接，供页面作者落实；来源记录本身不等于上屏义务，不默认给全部素材加来源脚注。
路径明确为 `assets/NAME.ext`。available 要有扩展名与编码一致的真实图片；pending 可指定尚未下载的目标路径，不据此宣称已可用。页面作者沿回执使用，不猜文件名，不把 `inputs/`、`inputs/extracted/` 或 `../inputs/` 写进 HTML `src` / CSS `url()`。附件原件只作为 `register-user` / `crop-material` 的 source，登记完成前不算可上屏路径。
同一素材沿用已有 ID 与真实路径补全消费者、来源和核验说明，不为同一路径另起别名或并发覆盖。生成工具已登记的素材沿其回执继续；业务名称写入 purpose。
finalize 从登记生成可读目录和联系表，不反向解析手写 catalog。需要提前交接时，先真实查看当前单图，再沿用原 ID/path 登记为 reviewed；图片变化后重新看图确认，不能让工具状态替代视觉判断。
`--slides` 只传明确页码，例如 `--slides 1,3`；没有消费者时省略该参数，不传 `--slides ""`。需要更正消费者时，用完整 register 参数重新提交正确页码，或登记 unavailable 并说明原因。

## 获取与附件操作

主 Agent 与 Image 使用同一 `scripts/image.py`。下表为参数示例，把逻辑根、路径和 ID 换成当前工具回执中的实际值：

| 需要 | image.py 的动作与参数 |
| --- | --- |
| 获取当前 Owner 已登记真实图片 | `fetch .`；也可用 `--asset ID` 定向获取 |
| 补全已生成素材的信息 | `register . --id 回执中的artifact_id --path 回执中的path --kind generated --slides 1,3` |
| 定向替换一个真实素材 | `fetch . --replace --asset ASSET_ID` |
| 导入用户图片 | `register-user . --source SOURCE_IMAGE_PATH --output assets/photo.png`；source 是 manifest 指向的 `inputs/` 下真实图片路径，不是 manifest 文件 |
| 裁取原 Figure | `crop-material . --source PAGE_PATH --output assets/figure.png --box x0,y0,x1,y1 --crop-policy preserve-complete-figure --preserve-internal-labels` |
| 裁取附件图或已生成/下载图片 | `crop . --source SOURCE_IMAGE_PATH --output assets/hero-wide.png --box 0.10,0.10,0.90,0.70` |
| 需要透明独立主体 | 查看 `extract-subject --help`，只用于边界适合分割的对象 |
| 核对真实透明像素 | `inspect . --asset assets/cutout.png --expect-transparent` |
| 汇总与验证 catalog | `finalize .` |

生图用已经提供的 image_generate 工具，沿用它返回的真实路径与 artifact_id；素材落盘不等于视觉验收，检查后用同 ID 补全 register 信息。
裁切 source 可用授权的 `inputs/` 或 `assets/` 图片，output 不覆盖原图；`register-user` 仍只登记用户附件，不把生成图改称用户原图。
独立需求可同轮并行；已经成功的图先复用，网络错误或登记失败只处理受影响项。
获取结果不一致时，核对实际路径和下载 URL；仅更新来源定位不证明新来源已核验，真实替换才使用 `fetch --replace`。不要把文件存在本身当作获取流程与视觉检查均已完成。

## 公开网页取证截图

原网页视觉本身是证据、且来源允许联网时，Orchestrator/Image 可用 `web_screenshot({"url":"https://...","query":"页面中的原文短语"})` 获取真实视口截图；省略 query 则截取页首。它不是生图，也不重排原文或添加浏览器界面。
公开 PDF 使用同一工具返回原件页图：`url="https://.../original.pdf#page=4"` 指定从 1 开始的物理页，优先于 query；未指定页码时 query 在可提取原文中选页，均未提供则读第 1 页，不做 OCR。回执的 source_page 是原件物理页码，不是课件页码或印刷页码；页图按比例放入 1280×900 画布，必要时从原图定向裁切，不重绘原件。
返回图已落盘登记，先用 `inspect(path=...)` 核对真实主体、指定措辞、遮挡和裁切；再由返回的原素材 Owner 沿用 artifact_id/path，以 `--kind real --source` 和真实 source_url 补齐用途及消费者。跨 Owner 复用不转移修改权。不把截图获取成功当作证据要求已满足，截图日期也不当作原文发表日期。
仅截取当前 1280×900 视口，不扫描整站或整页；需要另一段时用更具体的原文 query 定位。已取得的同 URL/query 截图直接复用，不重复获取。403、登录页或关键内容不可得时交回具体缺口，由 Orchestrator 自主换可验证来源或未被用户固定的案例，不默认等待用户补充，也不伪造截图。
`web_screenshot_resource_busy` 是本地截图排队超时，尚未访问来源；不要当作网站不可访问或改画证据图。等已有渲染结束后有限重试同一 URL，仍不可用则准确交回资源缺口，不连续碰撞。

## 附件 Figure、截图与抠图

页图由 manifest/Research 定位，可按需物化，不预先渲染整份 PDF。
候选框四值使用 0–1 规范化 x0/y0/x1/y1，不接受像素坐标；确认主体及内部标签完整，排除无关标题、正文、页眉页脚与长 caption，不误裁必要事实限定或必须保留的可见署名。
裁图命令返回真实尺寸、来源、裁框和文字边界线索；仍须查看裁图，不按裁切比例、贴边或像素数判定用途与可用性。
结合目标展示尺寸看实际裁图后，用同 ID 的 register 补全用途与核验；命令成功不代表清晰度或主体完整性已通过。
论文 Figure/地图默认 preserve-complete-figure；照片/独立对象可用 subject-only 或合适的 auto-tighten；
文档原貌本身是证据才用 document-facsimile。来源页码与消费者页码是两回事。

检查 edge_text_fragments、clipped_visual_regions 等诊断时回到原页和裁图，不把 crop PASS 等同构图通过。
屏幕属于设备的一部分时，不能只裁屏幕而丢掉本次需要的键盘、边框或主体轮廓。
透明独立对象输出真实 Alpha；绘画、论文 Figure、地图和依赖环境的照片不默认抠图。
分割失败如实返回，矩形裁剪不是抠图；只有确认棋盘格已烘焙进像素时才做专用去除。
需要排除原页残余正文时用工具支持的显式排除区域，只改 Alpha、不补画，并复核没有误伤对象或内部标签。

## 可用性与停止判断

先看可读联系表筛查身份、用途、裁切、水印、假字和重复，再放大疑点图。
大批素材用联系表分块覆盖，不跳过后半批，也不把每次工具成功变成新一轮全量验图。
主体边缘、非语义白边/透明边、可用分辨率与叠字静区必须以实际像素为准。
记录可核对的 subject_region/text_safe_region；没有文字静区就说明没有，不对后续裁切作乐观承诺。

可定位问题合并为一批定向替换。若无新来源或改法，保留成功项并报告缺口；
不要为同主机限流换一串近义 URL，也不以重生成全部图片来修格式错误。
失败操作不改变已有可用结论；替换成功后只检查变化图及其消费者关系。
所有需求有 available/unavailable 决定即可交接；必需项缺失影响页面是否成立，由 Orchestrator 明确处理，不能假报满足。
