# MURAL Presenter v1 Phase 5 Report

> 2026-09-02 审计说明：本报告只证明可注入 Kernel 与导出 projection，目录内没有正式 Skill-backed
> generation backend；以 `V1.0-CAPABILITY-AUDIT.md` 为准。

完成日期：2026-09-02
状态：共享入口、Delivery 与 Training Export 完成，保持 Shadow

## 1. 交付范围

- `infer.py`
- `synthesize.py`
- `export/delivery.py`
- `export/training.py`
- `runtime.env`

## 2. 共享生成内核

`PresenterKernel` 对两种模式装配完全相同的 RunSpec、Owner、Manager、Artifact、QC 和 stable tools。
宿主注入同一个 generation backend、角色 runners、Renderer 和 vision analyzer。

`synthesize.py` 只把运行模式固定为 synthesis；它不复制 Orchestrator 或页面生产循环。单输入对照测试中，
inference/synthesis 的 Canvas、HTML SHA-256、PNG SHA-256 和 backend result 完全一致。

## 3. Delivery

- 交付前要求每页 ready、当前 HTML/PNG 同源、Renderer receipt 有效、视觉资源未改变且没有 blocking issue；
- 生成根目录 `present.html`、`result.json`、`delivery/manifest.json`；
- 可选生成多页 PDF 与全页 PNG PPTX；
- PDF DPI/物理页尺寸、PPTX slide width/height 和 HTML/CSS 都来自唯一 CanvasSpec；
- manifest 写入后再次计算所有输出和页面哈希，交付后页面改变会校验失败。

## 4. Training Export

- 每行只对应一个 Owner segment；messages/checkpoint 单独内容寻址，不拼接整册历史；
- base64 图片外置到 `_trace/training/blobs/`，checkpoint 只保存 blob ref；
- 首段 predecessor 必须为 null，self/forward/missing predecessor 和 open tool transaction 进入拒绝原因；
- Artifact hash、Delivery、blocking issue、segment size 和 run mode 共同决定 admission；
- inference 不执行训练导出；直接调用 TrainingExporter 时明确返回 `not_applicable`。

## 5. 验证与未切换部分

Phase 5 专项：`7 passed`，覆盖 HTML/PDF/PPTX、Canvas 物理尺寸、交付后变更、共享资源漂移、image blob、
lineage/open transaction、inference admission 和双模式同核。

当前 CLI 是 Shadow 宿主边界：真正生成需要宿主注入 backend；`--export-existing` 仅导出已 ready 工作区。
旧 WebUI、生产 batch/synthesis 入口和 release pointer 未切换。
