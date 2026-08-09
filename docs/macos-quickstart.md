# Run MURAL-Presenter on macOS

> Looking for Linux or Windows too? Use the
> [cross-platform local deployment guide](local-deployment.md).

This guide is for someone who receives either the GitHub URL or a ZIP archive and wants to run the
web services locally on a Mac.

MURAL-Presenter contains two different web surfaces:

| Surface | What it is | Local URL |
| --- | --- | --- |
| **SenseNova Present WebUI** | The interactive presentation-authoring product interface | http://127.0.0.1:8001 |
| **MURAL project site** | The paper, blog, method figures, and project introduction | URL printed by the development server |

If you want to try the product interface, start with the SenseNova Present WebUI.

## 1. Install macOS prerequisites

Open Terminal. Install the Apple command-line tools if they are not already present:

~~~bash
xcode-select --install
~~~

Install [Homebrew](https://brew.sh/) if the brew command is unavailable. Follow the shell setup
line printed by the installer before continuing.

Install Git, Python 3.12, and [uv](https://docs.astral.sh/uv/getting-started/installation/):

~~~bash
brew install git python@3.12 uv
python3.12 --version
uv --version
~~~

The Studio requires Python 3.12 or newer. Both Apple Silicon and Intel macOS packages are available
through Homebrew.

## 2. Get the repository

### Option A: clone the GitHub repository

~~~bash
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter
~~~

### Option B: use a ZIP archive

Download **Code → Download ZIP** from GitHub, or copy the ZIP you received. Expand it in Finder,
open Terminal, and enter the extracted directory. A GitHub archive normally uses this name:

~~~bash
cd ~/Downloads/MURAL-Presenter-main
~~~

Replace the path if the archive was extracted elsewhere.

## 3. Start the SenseNova Present WebUI

From the repository root:

~~~bash
cd apps/studio/sensenova_present
cp .env.example .env
chmod +x start.sh scripts/*.sh
./start.sh --ui-only --check
./start.sh --ui-only --language zh --port 8001
~~~

The first run creates a local virtual environment and installs the locked dependencies. Keep the
Terminal window open while the service is running, then open:

http://127.0.0.1:8001

Optional health check from another Terminal window:

~~~bash
curl http://127.0.0.1:8001/healthz
~~~

Press **Control-C** in the server Terminal to stop it. Local projects, uploads, and application
state are stored below apps/studio/sensenova_present/studio/data and are not committed to Git.

### If port 8001 is occupied

Choose another local port:

~~~bash
./start.sh --ui-only --language zh --port 8010
~~~

Then open http://127.0.0.1:8010.

## 4. What UI-only mode includes

UI-only mode is the public, zero-credential preview. It starts the SenseNova Present interface and
local state layer without requiring private model services or a generation Harness.

It does **not** make the full presentation-generation backend self-contained. Generation, image
creation, search, and dynamic rendering require compatible external runtimes and service endpoints.
The repository deliberately excludes model credentials, private gateways, model weights, internal
datasets, and generated runs.

## 5. Enable full generation

Only use this path if you have been given the compatible MURAL/SenseNova runtime and model-service
configuration.

1. Open apps/studio/sensenova_present/.env.
2. Set SENSENOVA_UI_ONLY=0.
3. Configure PPTAGENT_CLEAN_PIPELINE_ROOT and any required Skill or Harness roots.
4. Configure model, image, and search endpoints and credentials.
5. Start without the ui-only flag:

~~~bash
./start.sh --edition full --language zh --port 8001
~~~

The first full-mode run may also download a Playwright Chromium renderer. Never commit the .env
file or send a ZIP containing real credentials.

## 6. Run the paper and blog site

This is optional and independent of the authoring WebUI. Install Node.js 22:

~~~bash
brew install node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"
node --version
~~~

From the repository root:

~~~bash
cd site
npm ci
cp .env.example .env.local
npm run dev
~~~

Open the URL printed by the development server. Stop it with **Control-C**.

For a production-style local check:

~~~bash
npm run build
npm run start -- --host 127.0.0.1 --port 4173
~~~

Then open http://127.0.0.1:4173.

## 7. Docker alternative

If Docker Desktop is already installed, the UI-only WebUI can be started without installing local
Python packages:

~~~bash
cd apps/studio/sensenova_present
cp .env.example .env
docker compose up --build
~~~

Open http://127.0.0.1:8001 and use **Control-C** to stop the foreground process. The initial image
build is larger than the native UI-only setup because it prepares the rendering dependencies too.

## 8. Updating and troubleshooting

For a Git clone:

~~~bash
git pull --ff-only
~~~

ZIP users should download a new archive when a newer version is published.

- **Python version error:** run python3.12 --version and brew install python@3.12.
- **uv not found:** run brew install uv, then open a new Terminal.
- **Permission denied:** rerun chmod +x start.sh scripts/*.sh.
- **Address already in use:** choose another value with --port.
- **Corporate proxy or certificate errors:** configure the company-approved proxy and certificate
  chain before dependency installation; do not disable TLS verification globally.
- **Do not expose the preview publicly:** the default v1 UI binds to 127.0.0.1 and does not enable
  production authentication. Keep it local unless a proper authenticated reverse proxy is provided.

## Minimal command card

For a Mac that already has Homebrew:

~~~bash
brew install git python@3.12 uv
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter/apps/studio/sensenova_present
cp .env.example .env
./start.sh --ui-only --language zh --port 8001
~~~

Then open http://127.0.0.1:8001.
