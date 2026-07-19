[CmdletBinding()]
param(
  [string]$RunRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$FrontendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $FrontendRoot "..")).Path
$BackendRoot = Join-Path $RepoRoot "backend"
$PythonExe = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$NodeExe = (Get-Command node.exe -ErrorAction Stop).Source
$ViteCli = Join-Path $FrontendRoot "node_modules\vite\bin\vite.js"
$PlaywrightCli = Join-Path $FrontendRoot "node_modules\@playwright\test\cli.js"
$SourceConfig = Join-Path $RepoRoot "config\source_config.yaml"
$JianyingUiConfig = Join-Path $RepoRoot "config\jianying_ui_profile.yaml"

foreach ($requiredPath in @($PythonExe, $NodeExe, $ViteCli, $PlaywrightCli, $SourceConfig, $JianyingUiConfig)) {
  if (-not (Test-Path -LiteralPath $requiredPath)) {
    throw "Golden E2E dependency is missing: $requiredPath"
  }
}

if (-not $RunRoot) {
  $RunRoot = "D:\CodexData\Temp\auto-video-engine\golden-real-{0}-{1}" -f (
    Get-Date -Format "yyyyMMdd-HHmmss"
  ), $PID
}
$RunRoot = [System.IO.Path]::GetFullPath($RunRoot)
if (-not [System.IO.Path]::GetPathRoot($RunRoot).Equals("D:\", [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "RunRoot must be an absolute D: path. Refusing to write: $RunRoot"
}

$paths = [ordered]@{
  artifacts = Join-Path $RunRoot "artifacts"
  autoMaterial = Join-Path $RunRoot "auto-material"
  breakdowns = Join-Path $RunRoot "breakdowns"
  browser = Join-Path $RunRoot "browser"
  data = Join-Path $RunRoot "data"
  db = Join-Path $RunRoot "db"
  exports = Join-Path $RunRoot "exports"
  logs = Join-Path $RunRoot "logs"
  material = Join-Path $RunRoot "material-root"
  mediaFixtures = Join-Path $RunRoot "media-fixtures"
  npmCache = Join-Path $RunRoot "npm-cache"
  playwrightBrowsers = Join-Path $RunRoot "playwright-browsers"
  project = Join-Path $RunRoot "project-root"
  pycache = Join-Path $RunRoot "pycache"
  skills = Join-Path $RunRoot "skills"
  temp = Join-Path $RunRoot "temp"
}
New-Item -ItemType Directory -Path $RunRoot -Force | Out-Null
foreach ($path in $paths.Values) {
  New-Item -ItemType Directory -Path $path -Force | Out-Null
}

# Deterministic read-only capability fixture: one Skill is present while the
# remaining approved Skills are intentionally absent.  Provider keys stay
# absent so the golden run cannot accidentally make an external request.
$hyperframesSkillDir = Join-Path $paths.skills "hyperframes"
New-Item -ItemType Directory -Path $hyperframesSkillDir -Force | Out-Null
@(
  "---",
  "name: hyperframes",
  "version: 1.2.3-golden",
  "description: deterministic REQ-018 golden fixture",
  "---"
) | Set-Content -LiteralPath (Join-Path $hyperframesSkillDir "SKILL.md") -Encoding UTF8

function New-GoldenImageFixture {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][int]$Red,
    [Parameter(Mandatory = $true)][int]$Green,
    [Parameter(Mandatory = $true)][int]$Blue
  )
  Add-Type -AssemblyName System.Drawing
  $bitmap = [System.Drawing.Bitmap]::new(1080, 1920)
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  try {
    $graphics.Clear([System.Drawing.Color]::FromArgb($Red, $Green, $Blue))
    $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
  }
  finally {
    $graphics.Dispose()
    $bitmap.Dispose()
  }
}

$mediaFixturePaths = @(
  (Join-Path $paths.mediaFixtures "owned-blue.png"),
  (Join-Path $paths.mediaFixtures "owned-green.png"),
  (Join-Path $paths.mediaFixtures "owned-orange.png")
)
New-GoldenImageFixture -Path $mediaFixturePaths[0] -Red 24 -Green 78 -Blue 119
New-GoldenImageFixture -Path $mediaFixturePaths[1] -Red 39 -Green 107 -Blue 84
New-GoldenImageFixture -Path $mediaFixturePaths[2] -Red 167 -Green 74 -Blue 34

function Get-FreeTcpPort {
  $listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    0
  )
  try {
    $listener.Start()
    return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
  }
  finally {
    $listener.Stop()
  }
}

function Wait-HttpReady {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
    [int]$TimeoutSeconds = 45
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    if ($Process.HasExited) {
      throw "Process $($Process.Id) exited before $Url became ready."
    }
    try {
      $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
        return
      }
    }
    catch {
      Start-Sleep -Milliseconds 250
    }
  } while ((Get-Date) -lt $deadline)
  throw "Timed out waiting for $Url"
}

function Stop-OwnedProcess {
  param([AllowNull()][System.Diagnostics.Process]$Process)
  if ($null -eq $Process -or $Process.HasExited) {
    return
  }
  Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  try {
    Wait-Process -Id $Process.Id -Timeout 10 -ErrorAction SilentlyContinue
  }
  catch {
    # The process may already have exited between checks.
  }
}

function Start-GoldenBackend {
  param([Parameter(Mandatory = $true)][string]$LogSuffix)
  return Start-Process `
    -FilePath $PythonExe `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
    -WorkingDirectory $BackendRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $paths.logs "backend-$LogSuffix.out.log") `
    -RedirectStandardError (Join-Path $paths.logs "backend-$LogSuffix.err.log") `
    -PassThru
}

function Invoke-GoldenPlaywright {
  param(
    [Parameter(Mandatory = $true)][ValidateSet("empty", "seed", "restart")][string]$Phase,
    [Parameter(Mandatory = $true)][string[]]$Projects
  )
  $env:GOLDEN_E2E_PHASE = $Phase
  $arguments = @(
    $PlaywrightCli,
    "test",
    "e2e/real-backend-golden.spec.ts",
    "--config=playwright.real.config.ts"
  )
  foreach ($project in $Projects) {
    $arguments += "--project=$project"
  }
  Push-Location -LiteralPath $FrontendRoot
  try {
    & $NodeExe @arguments
    if ($LASTEXITCODE -ne 0) {
      throw "Playwright $Phase phase failed with exit code $LASTEXITCODE."
    }
  }
  finally {
    Pop-Location
  }
}

$BackendPort = Get-FreeTcpPort
$FrontendPort = Get-FreeTcpPort
while ($FrontendPort -eq $BackendPort) {
  $FrontendPort = Get-FreeTcpPort
}
$BackendUrl = "http://127.0.0.1:$BackendPort"
$FrontendUrl = "http://127.0.0.1:$FrontendPort"
$dbUrlPath = (Join-Path $paths.db "golden.db").Replace("\", "/")

$environment = [ordered]@{
  TEMP = $paths.temp
  TMP = $paths.temp
  TMPDIR = $paths.temp
  PYTHONDONTWRITEBYTECODE = "1"
  PYTHONPYCACHEPREFIX = $paths.pycache
  CODEX_TEST_TEMP_ROOT = $paths.temp
  NPM_CONFIG_CACHE = $paths.npmCache
  PLAYWRIGHT_BROWSERS_PATH = $paths.playwrightBrowsers
  DATABASE_URL = "sqlite:///$dbUrlPath"
  DATA_DIR = $paths.data
  EXPORT_DIR = $paths.exports
  BREAKDOWN_DIR = $paths.breakdowns
  SOURCE_CONFIG_PATH = $SourceConfig
  MATERIAL_ROOT_DIR = $paths.material
  PROJECT_ROOT_DIR = $paths.project
  AUTO_MATERIAL_ROOT_DIR = $paths.autoMaterial
  CODEX_SKILLS_ROOT_DIR = $paths.skills
  JIANYING_ADAPTER_TYPE = "stub"
  JIANYING_DESKTOP_APP_PATH = (Join-Path $RunRoot "disabled-tools\jianying.exe")
  JIANYING_DOWNLOAD_DIR = (Join-Path $RunRoot "jianying-downloads")
  JIANYING_DESKTOP_UI_CONFIG_PATH = $JianyingUiConfig
  CANDIDATE_SCREENSHOT_DIR = (Join-Path $RunRoot "candidate-screenshots")
  BROWSER_HOTSPOT_PROFILE_DIR = $paths.browser
  BROWSER_HOTSPOT_CSV_PATH = (Join-Path $paths.exports "browser-hotspots.csv")
  BROWSER_HOTSPOT_HEADLESS = "true"
  AUTO_HOTSPOT_ON_STARTUP = "false"
  AI_PROVIDER = "fake"
  AI_BASE_URL = "http://127.0.0.1:9"
  AI_API_KEY = "golden-test-disabled"
  AI_MODEL = "fake-low-cost"
  TTS_PROVIDER = "windows_sapi"
  TTS_RATE = "10"
  PRODUCT_HUNT_TOKEN = "golden-test-disabled"
  VITE_BACKEND_URL = $BackendUrl
  GOLDEN_E2E_FRONTEND_URL = $FrontendUrl
  GOLDEN_E2E_ARTIFACT_ROOT = $paths.artifacts
  GOLDEN_E2E_MEDIA_PATHS = ($mediaFixturePaths | ConvertTo-Json -Compress)
}
foreach ($entry in $environment.GetEnumerator()) {
  Set-Item -LiteralPath "Env:$($entry.Key)" -Value $entry.Value
}

$backendProcess = $null
$frontendProcess = $null
$status = "failed"
$failure = ""
Write-Output "GOLDEN_E2E_RUN_ROOT=$RunRoot"
Write-Output "GOLDEN_E2E_BACKEND=$BackendUrl"
Write-Output "GOLDEN_E2E_FRONTEND=$FrontendUrl"

try {
  $backendProcess = Start-GoldenBackend -LogSuffix "seed"
  Wait-HttpReady -Url "$BackendUrl/api/health" -Process $backendProcess

  $frontendProcess = Start-Process `
    -FilePath $NodeExe `
    -ArgumentList @($ViteCli, "--host", "127.0.0.1", "--port", "$FrontendPort") `
    -WorkingDirectory $FrontendRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $paths.logs "frontend.out.log") `
    -RedirectStandardError (Join-Path $paths.logs "frontend.err.log") `
    -PassThru
  Wait-HttpReady -Url "$FrontendUrl/settings" -Process $frontendProcess

  Invoke-GoldenPlaywright -Phase "empty" -Projects @(
    "desktop-edge",
    "mobile-375x812-edge"
  )

  Invoke-GoldenPlaywright -Phase "seed" -Projects @("desktop-edge")

  Stop-OwnedProcess -Process $backendProcess
  $req003Seeder = Join-Path $BackendRoot "tests\fixtures\seed_req003_status_matrix.py"
  $req003Output = & $PythonExe $req003Seeder 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "REQ-003 status matrix seed failed with exit code $LASTEXITCODE.`n$req003Output"
  }
  $req003Output | Set-Content -LiteralPath (Join-Path $paths.logs "req003-status-matrix.json") -Encoding UTF8
  $backendProcess = Start-GoldenBackend -LogSuffix "restart"
  Wait-HttpReady -Url "$BackendUrl/api/health" -Process $backendProcess

  Invoke-GoldenPlaywright -Phase "restart" -Projects @(
    "desktop-edge",
    "mobile-375x812-edge"
  )
  $status = "passed"
}
catch {
  $failure = $_.Exception.Message
  throw
}
finally {
  Stop-OwnedProcess -Process $frontendProcess
  Stop-OwnedProcess -Process $backendProcess
  $manifest = [ordered]@{
    status = $status
    failure = $failure
    run_root = $RunRoot
    backend_url = $BackendUrl
    frontend_url = $FrontendUrl
    database_path = (Join-Path $paths.db "golden.db")
    artifact_root = $paths.artifacts
    completed_at = (Get-Date).ToString("o")
  }
  $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RunRoot "run-manifest.json") -Encoding UTF8
}

Write-Output "Golden real-backend E2E passed. Evidence: $($paths.artifacts)"
