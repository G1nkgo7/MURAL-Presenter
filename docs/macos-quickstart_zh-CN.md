# 在 macOS 上运行 MURAL-Presenter

> 如需 Linux 或 Windows 说明，请查看[跨平台本地部署说明](local-deployment_zh-CN.md)。

这份说明面向拿到 GitHub URL 或 ZIP 包的普通用户，介绍如何在 Mac 本地启动网页服务。

仓库里有两个不同的网页：

| 网页 | 用途 | 本地地址 |
| --- | --- | --- |
| **SenseNova Present WebUI** | 可交互的演示文稿创作产品界面 | http://127.0.0.1:8001 |
| **MURAL 项目主页** | 论文、Blog、方法配图和项目介绍 | 以开发服务器输出的地址为准 |

如果你想体验产品界面，请优先启动 SenseNova Present WebUI。

## 1. 安装 Mac 基础环境

打开“终端”。如果尚未安装 Apple 命令行工具，执行：

~~~bash
xcode-select --install
~~~

如果终端中没有 brew 命令，先按照 [Homebrew 官网](https://brew.sh/)安装。安装完成后，请执行
安装器最后给出的 shell 环境配置命令，再继续下面的步骤。

安装 Git、Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

~~~bash
brew install git python@3.12 uv
python3.12 --version
uv --version
~~~

Studio 要求 Python 3.12 或更高版本。Homebrew 同时提供 Apple Silicon 和 Intel Mac 的安装包。

## 2. 获取项目

### 方式 A：通过 GitHub URL 克隆

~~~bash
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter
~~~

### 方式 B：使用 ZIP 包

在 GitHub 上选择 **Code → Download ZIP**，或者将别人发来的 ZIP 解压。在 Finder 中找到解压
后的文件夹，然后用终端进入该目录。GitHub 下载包通常是下面这个名字：

~~~bash
cd ~/Downloads/MURAL-Presenter-main
~~~

如果你解压到了其他目录，请换成实际路径。

## 3. 启动 SenseNova Present WebUI

从仓库根目录执行：

~~~bash
cd apps/studio/sensenova_present
cp .env.example .env
chmod +x start.sh scripts/*.sh
./start.sh --ui-only --check
./start.sh --ui-only --language zh --port 8001
~~~

第一次启动会自动创建本地虚拟环境并安装锁定版本的依赖。服务运行期间不要关闭当前终端，
然后在浏览器中打开：

http://127.0.0.1:8001

可以在另一个终端中检查服务是否正常：

~~~bash
curl http://127.0.0.1:8001/healthz
~~~

在运行服务的终端中按 **Control-C** 即可停止。项目、上传文件和本地状态保存在
apps/studio/sensenova_present/studio/data 下，不会提交到 Git。

### 8001 端口被占用

换一个端口即可：

~~~bash
./start.sh --ui-only --language zh --port 8010
~~~

然后打开 http://127.0.0.1:8010。

## 4. UI-only 模式能做什么

UI-only 是不需要账号密钥的公开预览模式。它能够启动 SenseNova Present 产品界面和本地状态层，
不要求用户提前准备私有模型服务或生成 Harness。

但它不等于完整的 PPT 生成后端。真正的内容生成、图片生成、网络检索和动态渲染仍需要兼容的
外部运行时及服务端点。公开仓库不会包含模型密钥、私有网关、模型权重、内部数据集或历史生成结果。

## 5. 启用完整生成

只有当你已经拿到兼容的 MURAL/SenseNova 运行时和模型服务配置时，才使用这一模式。

1. 打开 apps/studio/sensenova_present/.env。
2. 将 SENSENOVA_UI_ONLY 设置为 0。
3. 配置 PPTAGENT_CLEAN_PIPELINE_ROOT，以及所需 Skill 或 Harness 路径。
4. 配置模型、图片和检索服务的地址与密钥。
5. 不再传入 ui-only 参数：

~~~bash
./start.sh --edition full --language zh --port 8001
~~~

完整模式首次启动时还可能下载 Playwright Chromium 渲染器。不要提交 .env，也不要把包含真实密钥的
ZIP 包发给其他人。

## 6. 启动论文和 Blog 项目主页

这一网页是可选的，和创作 WebUI 相互独立。先安装 Node.js 22：

~~~bash
brew install node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"
node --version
~~~

从仓库根目录执行：

~~~bash
cd site
npm ci
cp .env.example .env.local
npm run dev
~~~

浏览器打开终端输出的地址。按 **Control-C** 停止。

如果需要模拟正式构建：

~~~bash
npm run build
npm run start -- --host 127.0.0.1 --port 4173
~~~

然后打开 http://127.0.0.1:4173。

## 7. 使用 Docker 启动

如果 Mac 已安装 Docker Desktop，也可以不在本机安装 Python 依赖，直接启动 UI-only WebUI：

~~~bash
cd apps/studio/sensenova_present
cp .env.example .env
docker compose up --build
~~~

打开 http://127.0.0.1:8001，前台运行时按 **Control-C** 停止。Docker 第一次构建会比原生
UI-only 方式更大，因为镜像还会准备渲染相关依赖。

## 8. 更新与常见问题

如果使用 Git 克隆：

~~~bash
git pull --ff-only
~~~

如果使用 ZIP，请在有新版本时重新下载。

- **Python 版本错误：**执行 python3.12 --version，并运行 brew install python@3.12。
- **找不到 uv：**执行 brew install uv，然后重新打开终端。
- **Permission denied：**重新执行 chmod +x start.sh scripts/*.sh。
- **Address already in use：**用 --port 换一个端口。
- **公司代理或证书错误：**先配置公司允许的代理和证书链，不要全局关闭 TLS 校验。
- **不要直接暴露到公网：**默认 v1 界面只监听 127.0.0.1，也没有启用生产级认证。只有配置好
  认证反向代理后才能考虑对外开放。

## 最短指令卡

如果 Mac 已经安装 Homebrew，只需要：

~~~bash
brew install git python@3.12 uv
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter/apps/studio/sensenova_present
cp .env.example .env
./start.sh --ui-only --language zh --port 8001
~~~

最后打开 http://127.0.0.1:8001。
