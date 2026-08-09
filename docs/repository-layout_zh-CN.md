# 仓库结构说明

本文档定义 MURAL-Presenter 的公开实现结构。多数核心目录目前仍是接口约定；`apps/studio` 已额外
纳入经过公开边界清理的 SenseNova Present WebUI 兼容包，但不包含其外部生成运行时。

## 两条执行链路

### 离线数据构造

```text
版本化 Query 池 + 合成配置
            ↓
     query_synthesis
            ↓ Query specifications
      data_pipeline
            ↓ 标准化 case bundles
  inference / rollout runner
            ↓ 运行产物与轨迹
     quality_control
            ↓ 接受记录 + 拒绝原因
       可发布数据子集
```

### 在线交互创作

```text
apps/studio → services/api → orchestration → inference
                                      ↘ rendering ↔ quality_control
                                               ↓
                                      HTML / PPTX / PDF / 图片
```

两条链路可以共享 Schema 与推理适配器，但生命周期职责不同：离线链路负责产生、筛选可复现记录；
产品链路负责用户项目和可恢复运行。

## 目录职责

| 路径 | 负责内容 | 预期产物 |
| --- | --- | --- |
| `src/mural_presenter/query_synthesis/` | Domain、style、audience、speaker 池，约束采样与 Prompt 组装 | 带 seed 和 provenance 的版本化 Query specification |
| `src/mural_presenter/data_pipeline/` | 数据摄取、标准化、rollout manifest、筛选与数据集组装 | 可复现 case bundle 与 accepted-data manifest |
| `src/mural_presenter/orchestration/` | 生命周期状态、整册规划、页面组编译、委派和修改路由 | Deck/group plan 与可审计状态迁移 |
| `src/mural_presenter/inference/` | 模型适配、运行配置、单条/批量执行、重试和恢复 | 与 Provider 解耦的响应、trace、usage 与终止状态 |
| `src/mural_presenter/rendering/` | HTML 渲染、截图、contact sheet 与交付格式适配 | 渲染页、检查视图和导出文件 |
| `src/mural_presenter/quality_control/` | Schema、内容、视觉、整册和轨迹检查 | 结构化 QC 报告与接受决策 |
| `src/mural_presenter/schemas/` | 各阶段共享的版本化记录 | 不包含 Provider 私有字段的稳定序列化协议 |
| `configs/` | 无密钥、可提交审阅的配置 | 有名称、可追踪的实验与流水线设置 |
| `scripts/` | 面向使用者的命令入口 | 调用可导入模块的轻量封装 |
| `apps/studio/` | 交互式创作 Web UI | 项目、运行、复审、修改和导出界面 |
| `services/api/` | Studio 的服务端边界 | Project/run API 与流式生命周期事件 |
| `tests/` | 单元、集成和端到端验证 | 确定性测试与可再分发的小型 fixture |

## 边界规则

1. `schemas` 不依赖具体模型 Provider、Web 框架或存储后端。
2. `query_synthesis` 只产生任务规格，不隐式调用模型，也不自行接受自己的输出。
3. `data_pipeline` 负责记录 provenance 和编排数据阶段；模型执行属于 `inference`，接受或拒绝属于
   `quality_control`。
4. `quality_control` 输出发现与决策；若要修改 deck，必须产生一条新的 revision attempt，而不是静默覆盖。
5. 浏览器代码不保存 Provider 密钥，也不直接调用模型。兼容阶段由 Studio 的 FastAPI 服务端持有适配器；
   目标是将其抽取到 `services/api`，再由后者调用 `src/mural_presenter` 的公共能力。
6. `scripts` 只做参数解析和组件装配，可测试逻辑统一留在 `src/`。
7. 每个落盘记录都应包含 Schema 版本、稳定 ID、配置引用、必要的随机种子，以及足以复现或拒绝该记录的
   provenance。

## 单次运行的落盘约定

生成目录默认不进入 Git，但每个 run 应采用统一、可检查的结构：

```text
artifacts/runs/<run_id>/
├── run.json               # 身份、版本、配置、状态与时间戳
├── input/                 # 不可变 Query、附件与标准化 case bundle
├── state/                 # deck blueprint、group brief 与 revision state
├── traces/                # 事件流、模型/工具调用和 usage
├── deck/                  # 可编辑 HTML/CSS/assets 与导出元数据
├── renders/               # 单页图、contact sheet、PDF/PPTX 预览
└── qc/                    # 阶段报告、整册报告与接受决策
```

大型或受限输入只通过带校验和的 manifest 引用，不复制进 Git。未来可以把小型、确认可再分发的样例放入
`data/public/` 或 `examples/`，但不得包含凭证、私有地址或专有源材料。

## 两类 Web 页面

- `site/`：静态双语项目主页、Blog 与论文阅读页。
- `apps/studio/`：SenseNova Present 交互式产品 WebUI 的兼容迁移包。
- `services/api/`：目标中的版本化浏览器到运行时边界；等价路由当前仍位于兼容包的 FastAPI 服务端。

这样拆分后，论文网站可以继续保持轻量，而产品 UI 后续能够独立加入项目存储、流式运行、可视化编辑、复审和
导出能力，不会把产品运行时与宣传页面耦合在一起。
