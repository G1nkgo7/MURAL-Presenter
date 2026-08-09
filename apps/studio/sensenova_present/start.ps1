param(
  [ValidateSet("zh", "en")][string]$Language = "zh",
  [string]$HostAddress = "127.0.0.1",
  [int]$Port = 8001,
  [ValidateSet("v1", "full")][string]$Edition = "v1",
  [switch]$UiOnly,
  [switch]$NoInstall,
  [switch]$NoBrowserInstall,
  [switch]$Reload,
  [switch]$Check
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# Optional direct PowerShell configuration. Environment variables or .env
# are also supported. Never commit real keys.
# $env:SENSENOVA_IMAGE_BASE_URL = "https://example.com/v1"
# $env:SENSENOVA_IMAGE_API_KEY = "sk-..."
# $env:SENSENOVA_SEARCH_BASE_URL = "https://google.serper.dev"
# $env:SENSENOVA_SEARCH_API_KEY = "..."

$PythonExe = $env:SENSENOVA_BOOTSTRAP_PYTHON
$PythonArgs = @()

if (-not $PythonExe) {
  $Python312 = Get-Command python3.12 -ErrorAction SilentlyContinue
  if ($Python312) {
    $PythonExe = $Python312.Source
  }
}

if (-not $PythonExe) {
  $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($PyLauncher) {
    & $PyLauncher.Source -3.12 -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
      $PythonExe = $PyLauncher.Source
      $PythonArgs = @("-3.12")
    }
  }
}

if (-not $PythonExe) {
  $UvCommand = Get-Command uv -ErrorAction SilentlyContinue
  if ($UvCommand) {
    $UvPython = (& $UvCommand.Source python find 3.12 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $UvPython) {
      $PythonExe = $UvPython.Trim()
    }
  }
}

if (-not $PythonExe) {
  $PythonFallback = Get-Command python -ErrorAction SilentlyContinue
  if ($PythonFallback) {
    $PythonExe = $PythonFallback.Source
  }
}

if (-not $PythonExe) {
  throw "Python 3.12+ is required. Install it directly or run: uv python install 3.12"
}

& $PythonExe @PythonArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required." }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "[SenseNova Present] Installing uv for the current user..."
  & $PythonExe @PythonArgs -m pip install --user uv
  $UserBase = (& $PythonExe @PythonArgs -m site --user-base).Trim()
  $env:Path = "$UserBase\Scripts;$env:Path"
}

$ArgsList = @(
  "$ProjectRoot\scripts\launch.py",
  "--language", $Language,
  "--host", $HostAddress,
  "--port", $Port,
  "--edition", $Edition
)
if ($NoInstall) { $ArgsList += "--no-install" }
if ($UiOnly) { $ArgsList += "--ui-only" }
if ($NoBrowserInstall) { $ArgsList += "--no-browser-install" }
if ($Reload) { $ArgsList += "--reload" }
if ($Check) { $ArgsList += "--check" }

& $PythonExe @PythonArgs @ArgsList
exit $LASTEXITCODE
