# Run MURAL-Presenter locally

This guide covers macOS, Linux, and Windows for users receiving either the GitHub URL or a ZIP
archive.

The repository contains two web services:

| Service | Purpose | Default address |
| --- | --- | --- |
| **SenseNova Present WebUI** | Product interface for authoring, inspection, revision, and export | http://127.0.0.1:8001 |
| **MURAL project site** | Paper, blog, method figures, and project introduction | Printed by the Node development server |

Start with the SenseNova Present WebUI if you want to try the product interface.

## 1. Install prerequisites

All platforms need Git, uv, and Python 3.12. Using uv to manage Python avoids changing the system
Python installation.

### macOS

Install the Apple command-line tools when needed:

~~~bash
xcode-select --install
~~~

Install [Homebrew](https://brew.sh/) if brew is unavailable, then follow the shell configuration
line printed by its installer.

~~~bash
brew install git uv
uv python install 3.12
git --version
uv --version
~~~

### Linux

Ubuntu or Debian:

~~~bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
~~~

Fedora users can install the base packages with:

~~~bash
sudo dnf install -y git curl ca-certificates
~~~

Open a new terminal after installing uv, then run:

~~~bash
uv python install 3.12
git --version
uv --version
~~~

### Windows

Open PowerShell:

~~~powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
~~~

Open a new PowerShell window, then run:

~~~powershell
uv python install 3.12
git --version
uv --version
~~~

If winget is unavailable, use [Git for Windows](https://git-scm.com/downloads/win) and the
[official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## 2. Get the repository

Git works in macOS Terminal, a Linux shell, and Windows PowerShell:

~~~bash
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter
~~~

Alternatively, choose **Code → Download ZIP** on GitHub or expand the archive you received.
Enter the extracted MURAL-Presenter-main directory before continuing.

## 3. Start the SenseNova Present WebUI

### macOS or Linux

~~~bash
cd webui
cp .env.example .env
chmod +x start.sh scripts/*.sh
./start.sh --ui-only --check
./start.sh --ui-only --language en --port 8001
~~~

### Windows PowerShell

~~~powershell
Set-Location webui
Copy-Item .env.example .env
.\start.ps1 -UiOnly -Check
.\start.ps1 -UiOnly -Language en -Port 8001
~~~

If PowerShell blocks local scripts, use the batch entry point:

~~~bat
start.bat -UiOnly -Language en -Port 8001
~~~

Or bypass the policy for this invocation only:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -UiOnly -Language en -Port 8001
~~~

The first run creates a virtual environment and installs locked dependencies. Keep the terminal
open and visit http://127.0.0.1:8001.

## 4. Verify, stop, or change the port

macOS or Linux:

~~~bash
curl http://127.0.0.1:8001/healthz
~~~

Windows PowerShell:

~~~powershell
Invoke-RestMethod http://127.0.0.1:8001/healthz
~~~

A healthy response contains ok set to true. Press **Control-C** in the server terminal to stop it.

If port 8001 is occupied:

~~~bash
# macOS / Linux
./start.sh --ui-only --language en --port 8010
~~~

~~~powershell
# Windows
.\start.ps1 -UiOnly -Language en -Port 8010
~~~

Local projects, uploads, and state are stored under
webui/studio/data and are not committed to Git.

## 5. UI-only versus full generation

UI-only is the public, zero-credential preview. It starts the SenseNova Present interface and local
state layer without a model service.

The repository now includes the current `skills/mural-presenter` Skill and its paired
`harnesses/mural-presenter` runtime. Generation still requires a compatible model endpoint;
image generation and web search are optional service integrations. Model credentials, private
gateways, model weights, internal datasets, and generated runs are not included.

Before enabling full generation, edit webui/.env:

1. Set SENSENOVA_UI_ONLY=0.
2. Configure SENSENOVA_MODEL_BASE_URL, SENSENOVA_MODEL_NAME, and the credential if required.
3. Optionally configure image-generation and search services.

macOS or Linux:

~~~bash
./start.sh --language en --port 8001
~~~

Windows:

~~~powershell
.\start.ps1 -Language en -Port 8001
~~~

The first full-mode run may download a Playwright Chromium renderer. Never commit .env or distribute
an archive containing real credentials.

## 6. Run the paper and blog site

The project site is independent from the product WebUI. Install Node.js 22.13 or newer from the
[official Node.js download page](https://nodejs.org/en/download).

macOS can also use:

~~~bash
brew install node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"
~~~

Verify the installation:

~~~bash
node --version
npm --version
~~~

macOS or Linux:

~~~bash
cd site
npm ci
cp .env.example .env.local
npm run dev
~~~

Windows PowerShell:

~~~powershell
Set-Location site
npm ci
Copy-Item .env.example .env.local
npm run dev
~~~

Open the URL printed by the development server. For a production-style local check:

~~~bash
npm run build
npm run start -- --host 127.0.0.1 --port 4173
~~~

Then visit http://127.0.0.1:4173.

## 7. Docker

Use Docker Desktop on macOS or Windows, or Docker Engine with the Compose plugin on Linux.

macOS or Linux:

~~~bash
cd webui
cp .env.example .env
docker compose up --build
~~~

Windows PowerShell:

~~~powershell
Set-Location webui
Copy-Item .env.example .env
docker compose up --build
~~~

Visit http://127.0.0.1:8001 and press **Control-C** to stop the foreground process. The initial
image build is larger than the native UI-only setup because it prepares rendering dependencies.

## 8. Update and troubleshoot

Git users can update with:

~~~bash
git pull --ff-only
~~~

ZIP users should download a new archive. Back up studio/data and the local .env before replacing a
working copy.

- **uv not found:** reopen the terminal or reinstall it using the official guide.
- **Python version error:** run uv python install 3.12.
- **Permission denied:** on macOS/Linux run chmod +x start.sh scripts/*.sh.
- **PowerShell blocks scripts:** use start.bat or the one-time Bypass command above.
- **Address already in use:** choose another port such as 8010.
- **Dependency downloads fail:** configure the approved proxy and certificate chain; do not disable
  TLS verification globally.
- **Generation is unavailable:** verify that UI-only is disabled, the bundled MURAL paths are intact,
  and the model endpoint is configured.
- **Do not expose the preview publicly:** default v1 mode binds to 127.0.0.1 without production
  authentication.

## Minimal command cards

### macOS

~~~bash
brew install git uv
uv python install 3.12
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter/webui
cp .env.example .env
./start.sh --ui-only --language en --port 8001
~~~

### Linux

~~~bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter/webui
cp .env.example .env
chmod +x start.sh scripts/*.sh
./start.sh --ui-only --language en --port 8001
~~~

### Windows PowerShell

~~~powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
uv python install 3.12
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
Set-Location MURAL-Presenter\webui
Copy-Item .env.example .env
.\start.ps1 -UiOnly -Language en -Port 8001
~~~

Finally, visit http://127.0.0.1:8001.
