# MURAL-Presenter 跨平台本地部署说明

这份说明面向拿到 GitHub URL 或 ZIP 包的用户，覆盖 macOS、Linux 和 Windows。

仓库包含两个不同的网页服务：

| 服务 | 用途 | 默认地址 |
| --- | --- | --- |
| **SenseNova Present WebUI** | 演示文稿创作、检查、修改与导出的产品界面 | http://127.0.0.1:8001 |
| **MURAL 项目主页** | 论文、Blog、方法配图和项目介绍 | 由 Node 开发服务器输出 |

如果你想使用产品界面，请从 SenseNova Present WebUI 开始。

## 1. 安装基础环境

三个平台都需要 Git、uv 和 Python 3.12。推荐让 uv 管理 Python，这样不需要修改系统 Python。

### macOS

打开“终端”。如果尚未安装 Apple 命令行工具：

~~~bash
xcode-select --install
~~~

没有 brew 命令时，先按照 [Homebrew 官网](https://brew.sh/)安装，并执行安装器最后给出的
shell 环境配置命令。

~~~bash
brew install git uv
uv python install 3.12
git --version
uv --version
~~~

### Linux

Ubuntu 或 Debian：

~~~bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
~~~

Fedora 可将安装依赖的一行替换为：

~~~bash
sudo dnf install -y git curl ca-certificates
~~~

安装 uv 后重新打开终端，再执行：

~~~bash
uv python install 3.12
git --version
uv --version
~~~

### Windows

打开 PowerShell：

~~~powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
~~~

重新打开 PowerShell，再执行：

~~~powershell
uv python install 3.12
git --version
uv --version
~~~

如果系统没有 winget，可以分别从 [Git for Windows](https://git-scm.com/downloads/win)和
[uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)安装。

## 2. 获取项目

### 通过 GitHub URL

macOS、Linux 和 Windows PowerShell 都可以执行：

~~~bash
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter
~~~

### 通过 ZIP

在 GitHub 选择 **Code → Download ZIP**，或者解压别人发来的 ZIP。

- macOS：通常位于 ~/Downloads/MURAL-Presenter-main
- Linux：进入实际解压目录，例如 ~/Downloads/MURAL-Presenter-main
- Windows：通常位于 $HOME\Downloads\MURAL-Presenter-main

目录名不同时，以实际解压后的路径为准。

## 3. 启动 SenseNova Present WebUI

### macOS 或 Linux

从仓库根目录执行：

~~~bash
cd apps/studio/sensenova_present
cp .env.example .env
chmod +x start.sh scripts/*.sh
./start.sh --ui-only --check
./start.sh --ui-only --language zh --port 8001
~~~

### Windows PowerShell

从仓库根目录执行：

~~~powershell
Set-Location apps\studio\sensenova_present
Copy-Item .env.example .env
.\start.ps1 -UiOnly -Check
.\start.ps1 -UiOnly -Language zh -Port 8001
~~~

如果 PowerShell 阻止脚本执行，可以使用仓库提供的批处理入口：

~~~bat
start.bat -UiOnly -Language zh -Port 8001
~~~

也可以只为本次运行绕过策略：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -UiOnly -Language zh -Port 8001
~~~

第一次启动会创建本地虚拟环境并安装锁定版本的依赖。保持终端窗口打开，然后访问：

http://127.0.0.1:8001

各平台打开浏览器的命令：

~~~bash
# macOS
open http://127.0.0.1:8001

# Linux
xdg-open http://127.0.0.1:8001
~~~

~~~powershell
# Windows PowerShell
Start-Process http://127.0.0.1:8001
~~~

## 4. 检查、停止和更换端口

macOS 或 Linux 健康检查：

~~~bash
curl http://127.0.0.1:8001/healthz
~~~

Windows PowerShell：

~~~powershell
Invoke-RestMethod http://127.0.0.1:8001/healthz
~~~

正常响应中会包含 ok 为 true。在服务终端按 **Control-C** 停止。

如果 8001 被占用：

~~~bash
# macOS / Linux
./start.sh --ui-only --language zh --port 8010
~~~

~~~powershell
# Windows
.\start.ps1 -UiOnly -Language zh -Port 8010
~~~

然后访问 http://127.0.0.1:8010。

本地项目、上传文件和状态默认保存在
apps/studio/sensenova_present/studio/data，不会提交到 Git。

## 5. UI-only 与完整生成的区别

UI-only 是无需模型服务的公开预览模式，可以启动 SenseNova Present 产品界面和本地状态层。

仓库现在已经包含当前 `skills/mural-presenter` Skill 及其配套
`harnesses/mural-presenter` 运行时。内容生成仍需配置兼容的模型端点；图片生成和网络检索是
可选服务。公开仓库不会包含模型密钥、私有网关、模型权重、内部数据集或历史生成结果。

启用完整生成前，需要在 apps/studio/sensenova_present/.env 中：

1. 将 SENSENOVA_UI_ONLY 设置为 0。
2. 配置 SENSENOVA_MODEL_BASE_URL、SENSENOVA_MODEL_NAME，以及模型需要的密钥。
3. 按需配置图片生成与检索服务。

macOS 或 Linux：

~~~bash
./start.sh --language zh --port 8001
~~~

Windows：

~~~powershell
.\start.ps1 -Language zh -Port 8001
~~~

完整模式首次启动时可能下载 Playwright Chromium。不要提交 .env，也不要发送带真实密钥的 ZIP。

## 6. 启动论文与 Blog 项目主页

项目主页和产品 WebUI 相互独立，需要 Node.js 22.13 或更高版本。可从
[Node.js 官网](https://nodejs.org/en/download)安装当前 LTS。

macOS 也可以使用：

~~~bash
brew install node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"
~~~

安装后确认：

~~~bash
node --version
npm --version
~~~

macOS 或 Linux：

~~~bash
cd site
npm ci
cp .env.example .env.local
npm run dev
~~~

Windows PowerShell：

~~~powershell
Set-Location site
npm ci
Copy-Item .env.example .env.local
npm run dev
~~~

打开终端输出的地址。模拟正式构建时执行：

~~~bash
npm run build
npm run start -- --host 127.0.0.1 --port 4173
~~~

然后访问 http://127.0.0.1:4173。

## 7. Docker 方案

macOS 和 Windows 安装 Docker Desktop；Linux 安装 Docker Engine 与 Compose 插件。

macOS 或 Linux：

~~~bash
cd apps/studio/sensenova_present
cp .env.example .env
docker compose up --build
~~~

Windows PowerShell：

~~~powershell
Set-Location apps\studio\sensenova_present
Copy-Item .env.example .env
docker compose up --build
~~~

访问 http://127.0.0.1:8001，按 **Control-C** 停止前台服务。第一次构建会下载完整镜像依赖，
通常比原生 UI-only 启动更慢。

## 8. 更新项目

Git 克隆用户：

~~~bash
git pull --ff-only
~~~

ZIP 用户需要重新下载新版本。更新前请备份需要保留的 studio/data 与本地 .env。

## 9. 常见问题

- **找不到 uv：**重新打开终端；仍失败时按 uv 官方文档重新安装。
- **Python 版本错误：**执行 uv python install 3.12。
- **Permission denied：**macOS/Linux 执行 chmod +x start.sh scripts/*.sh。
- **PowerShell 禁止执行：**使用 start.bat，或使用上面的单次 Bypass 命令。
- **Address already in use：**使用 8010 等其他端口。
- **依赖下载失败：**检查网络、代理和公司证书链，不要全局关闭 TLS 校验。
- **无法生成 PPT：**确认已关闭 UI-only、仓库中的 MURAL Skill/Harness 路径完整，并已配置模型端点。
- **不要直接暴露到公网：**默认 v1 模式监听 127.0.0.1，未启用生产级认证。

## 三个平台最短指令

### macOS

~~~bash
brew install git uv
uv python install 3.12
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter/apps/studio/sensenova_present
cp .env.example .env
./start.sh --ui-only --language zh --port 8001
~~~

### Linux

~~~bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter/apps/studio/sensenova_present
cp .env.example .env
chmod +x start.sh scripts/*.sh
./start.sh --ui-only --language zh --port 8001
~~~

### Windows PowerShell

~~~powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
uv python install 3.12
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
Set-Location MURAL-Presenter\apps\studio\sensenova_present
Copy-Item .env.example .env
.\start.ps1 -UiOnly -Language zh -Port 8001
~~~

最后访问 http://127.0.0.1:8001。
