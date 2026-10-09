# MURAL Presenter WebUI

配套 0919 `mural-presenter-v1` Skill/Harness 的静态演示工作台。
前端、服务端与推理桥接均保留；历史运行入口不对新任务开放。

## 启动

从仓库根目录运行：

```bash
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
cp webui/.env.example webui/.env
bash webui/start.sh
```

默认地址为 `http://127.0.0.1:8001`。Windows 使用 `webui/start.ps1`。
只检查安装而不启动服务：

```bash
python webui/scripts/launch.py --check --no-install --no-browser-install
```

启动器验证发布哈希，解析 `configs/runtime.local.json`，固定当前仓库的 Skill/Harness，
再启动 Studio。`MURAL_RUNTIME_CONFIG` 可指向已有的绝对配置路径。
新任务使用该配置；旧任务仍保留原有固定 Skill/状态，不自动迁移。

## 配置与数据

- `webui/.env`：本地 UI/模型环境配置，不能提交。
- `.secrets/.env`：已有部署的共享 Provider 配置，可用 `MURAL_SECRETS_ENV_PATH` 指定替代文件。
- `configs/runtime.local.json`：本机资源目录、并发与外部 MinerU 地址，不能提交。
- `webui/studio/data/`：数据库、上传、任务、生成结果、浏览器及共享对象，不能提交。
- `STUDIO_DATA_DIR`：可指定其他数据目录。切换目录不会自动迁移历史数据。

模型可通过界面配置。文字任务需要模型服务；PDF/Office/图片附件还需要配置 MinerU。
搜索、生图等服务按任务和模型能力配置。本次整理不宣称已验证所有模型或平台。
默认免登录的单用户模式适合本机使用；外网部署应由部署者配置访问控制。

## Docker

使用仓库根目录作为构建上下文：`docker compose -f webui/compose.yaml up --build`。
镜像包含 0919 运行代码与配套启动层，数据保存在独立 volume。
Docker 构建仍需网络下载依赖和 Chromium；本次本机验证不等同于容器端到端验证。

[版本来源](../docs/release-provenance.md) · [实验复现边界](../docs/reproduction.md)
