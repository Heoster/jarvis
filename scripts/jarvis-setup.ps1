<#
.SYNOPSIS
    JARVIS - all-in-one setup for Windows (Linux/macOS: jarvis-setup.sh).

.DESCRIPTION
    One command takes you from a fresh clone to a verified install:

        powershell -ExecutionPolicy Bypass -File scripts\jarvis-setup.ps1

    It creates the virtualenv, installs the core and focus-area dependencies,
    the dev extras, the Chromium build Playwright drives, the OS-level tools the
    screen agent needs (OCR), copies .env, then proves the install by running
    the test suite, the lint gate, a wheel build and `jarvis doctor`.

    Everything optional degrades to a warning instead of aborting: JARVIS is
    designed to run with any subset of its capabilities.

.PARAMETER Python
    Interpreter to build the venv from. Default: `py -3`, else `python`.

.PARAMETER SkipBrowser
    Skip the Playwright Chromium download (~150 MB).

.PARAMETER SkipTests
    Skip the verification stage (pytest, ruff, wheel build).

.PARAMETER SkipSystem
    Never install OS packages (Tesseract OCR); report only.

.PARAMETER Model
    Also pull the default ollama model (~2 GB download).

.PARAMETER Web
    Also install the web UI extras (FastAPI, uvicorn, Gradio).

.PARAMETER NoDev
    Skip the dev extras (pytest, pytest-asyncio, ruff).

.PARAMETER Yes
    Do not prompt before installing OS packages.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\jarvis-setup.ps1 -Yes
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SkipBrowser,
    [switch]$SkipTests,
    [switch]$SkipSystem,
    [switch]$Model,
    [switch]$Web,
    [switch]$NoDev,
    [switch]$Yes
)

# NOTE ON STYLE: this script deliberately calls native commands directly rather
# than through a wrapper function. A PowerShell function returns *everything* it
# emits, so `Do-Pip ... | Out-Null` would swallow the exit code the wrapper
# returns and leave $LASTEXITCODE describing the wrong command. Direct calls keep
# every exit-code check below honest.

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"

# ── output helpers ───────────────────────────────────────────────────────────
$script:Warnings = @()

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "── $Text " -ForegroundColor Cyan
}
function Write-Info([string]$Text) { Write-Host "   $Text" }
function Write-Dim([string]$Text)  { Write-Host "   $Text" -ForegroundColor DarkGray }
function Write-Ok([string]$Text)   { Write-Host "   + $Text" -ForegroundColor Green }
function Write-Notice([string]$Text) {
    Write-Host "   ! $Text" -ForegroundColor Yellow
    $script:Warnings += $Text
}
function Stop-Fatal([string]$Text) {
    Write-Host ""
    Write-Host "   X $Text" -ForegroundColor Red
    exit 1
}
function Test-Command([string]$Name) {
    [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}
function Test-AnyPath([string[]]$Paths) {
    foreach ($P in $Paths) { if ($P -and (Test-Path $P)) { return $true } }
    return $false
}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "── JARVIS setup  $Root" -ForegroundColor White

# ── 1. interpreter ───────────────────────────────────────────────────────────
Write-Step "1/10  Python interpreter"

$Launcher = $null
$LauncherArgs = @()
if ($Python) {
    if (-not (Test-Command $Python)) { Stop-Fatal "'$Python' is not on PATH" }
    $Launcher = $Python
} elseif (Test-Command "py") {
    $Launcher = "py"
    $LauncherArgs = @("-3")
} elseif (Test-Command "python") {
    $Launcher = "python"
} else {
    Stop-Fatal "no Python on PATH - install Python 3.10+ from python.org and tick 'Add to PATH'"
}

$PyVersion = (& $Launcher @LauncherArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" | Out-String).Trim()
& $Launcher @LauncherArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-Fatal "Python 3.10+ required, found $PyVersion" }
Write-Ok "Python $PyVersion ($Launcher)"

# ── 2. virtualenv ────────────────────────────────────────────────────────────
Write-Step "2/10  Virtual environment"
if (Test-Path ".venv") {
    Write-Ok ".venv already exists - reusing it"
} else {
    & $Launcher @LauncherArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-Fatal "could not create .venv" }
    Write-Ok "created .venv"
}

$PyExe = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $PyExe)) { Stop-Fatal "the venv python is missing: $PyExe" }
$RuffExe = Join-Path $Root ".venv\Scripts\ruff.exe"

# ── 3. core dependencies ─────────────────────────────────────────────────────
Write-Step "3/10  Core dependencies"
& $PyExe -m pip install --quiet --upgrade pip
# Installed as the project itself, so `jarvis` lands on PATH inside the venv.
& $PyExe -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) { Stop-Fatal "core install failed - check your network / index" }

$Version = (& $PyExe -m jarvis --version 2>$null | Out-String).Trim()
if (-not $Version) { $Version = "JARVIS installed" }
Write-Ok $Version

# ── 4. focus-area extras ─────────────────────────────────────────────────────
Write-Step "4/10  Focus-area extras (screen, browser, reading)"
& $PyExe -m pip install --quiet mss Pillow pytesseract pyautogui playwright selenium websockets trafilatura beautifulsoup4 tiktoken
if ($LASTEXITCODE -eq 0) {
    Write-Ok "screen + browser + reading extras installed"
} else {
    Write-Notice "some focus-area extras failed - JARVIS degrades gracefully without them"
}

if ($Web) {
    if (Test-Path "requirements-web.txt") {
        & $PyExe -m pip install --quiet -r requirements-web.txt
        if ($LASTEXITCODE -eq 0) { Write-Ok "web UI extras installed" }
        else { Write-Notice "web UI extras failed to install" }
    }
}

if (-not $NoDev) {
    & $PyExe -m pip install --quiet "pytest>=8.0" "pytest-asyncio>=0.23" "ruff>=0.4" build
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "dev extras installed (pytest, pytest-asyncio, ruff, build)"
    } else {
        Write-Notice "dev extras failed - the verification stage will be skipped"
        $SkipTests = $true
    }
}

# ── 5. browser engine ────────────────────────────────────────────────────────
Write-Step "5/10  Browser engine"
if ($SkipBrowser) {
    Write-Dim "skipped (-SkipBrowser)"
} else {
    & $PyExe -c "import playwright" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Notice "playwright is not installed - skipping the Chromium download"
    } else {
        Write-Info "downloading the Chromium build Playwright drives (~150 MB, once)"
        & $PyExe -m playwright install chromium
        if ($LASTEXITCODE -eq 0) { Write-Ok "Chromium ready" }
        else { Write-Notice "Chromium download failed - install Chrome or Edge manually, or re-run later" }
    }
}

# ── 6. OS-level tools ────────────────────────────────────────────────────────
# OCR is the one screen dependency that cannot be pip-installed. Windows needs
# no xdotool/wmctrl/scrot equivalent: the Win32 API covers input and capture.
Write-Step "6/10  OS-level tools (OCR)"

$ProgFiles = $env:ProgramFiles
$ProgFilesX86 = ${env:ProgramFiles(x86)}
$TesseractPaths = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe"),
    (Join-Path $ProgFiles "Tesseract-OCR\tesseract.exe")
)
if ($ProgFilesX86) { $TesseractPaths += (Join-Path $ProgFilesX86 "Tesseract-OCR\tesseract.exe") }

$HaveTesseract = (Test-Command "tesseract") -or (Test-AnyPath $TesseractPaths)
if ($HaveTesseract) {
    Write-Ok "Tesseract OCR found"
} else {
    Write-Notice "Tesseract OCR not found - the screen agent cannot read text without it"
    if ($SkipSystem) {
        Write-Info "winget install UB-Mannheim.TesseractOCR"
    } elseif (-not (Test-Command "winget")) {
        Write-Info "winget is unavailable - install Tesseract manually:"
        Write-Info "  https://github.com/UB-Mannheim/tesseract/wiki"
    } else {
        $Confirmed = $false
        if ($Yes) {
            $Confirmed = $true
        } else {
            $Answer = Read-Host "   install Tesseract OCR via winget? [y/N]"
            if ($Answer -match '^[yY]') { $Confirmed = $true }
        }
        if ($Confirmed) {
            winget install --id UB-Mannheim.TesseractOCR --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "Tesseract OCR installed (open a new terminal so PATH refreshes)"
            } else {
                Write-Notice "winget could not install Tesseract - install it manually"
            }
        } else {
            Write-Info "declined"
        }
    }
}

# The terminal launcher drives a real browser window; Playwright's Chromium
# covers the headless path either way, so a miss here is informational.
$BrowserPaths = @(
    (Join-Path $ProgFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path $ProgFiles "Microsoft\Edge\Application\msedge.exe")
)
if ($ProgFilesX86) {
    $BrowserPaths += (Join-Path $ProgFilesX86 "Google\Chrome\Application\chrome.exe")
    $BrowserPaths += (Join-Path $ProgFilesX86 "Microsoft\Edge\Application\msedge.exe")
}
$HaveBrowser = (Test-AnyPath $BrowserPaths) -or (Test-Command "chrome") -or (Test-Command "msedge")
if ($HaveBrowser) {
    Write-Ok "a Chromium-family browser is present for the terminal launcher"
} else {
    Write-Notice "no Chrome/Edge found - Playwright's Chromium still works for headless driving"
}

# ── 7. local model (optional) ────────────────────────────────────────────────
Write-Step "7/10  Local model (optional)"
if (Test-Command "ollama") {
    Write-Ok "ollama found"
    if ($Model) {
        Write-Info "pulling hermes3:3b (~2 GB, once)"
        ollama pull hermes3:3b
        if ($LASTEXITCODE -ne 0) { Write-Notice "ollama pull failed - run it again later" }
    } else {
        Write-Dim "not pulling a model (pass -Model to pull hermes3:3b, ~2 GB)"
    }
} else {
    Write-Notice "ollama not installed - JARVIS will fall back to a free cloud provider"
    Write-Info "install: winget install Ollama.Ollama   (or download from ollama.com)"
}

# ── 8. environment file ──────────────────────────────────────────────────────
Write-Step "8/10  Environment file"
if (Test-Path ".env") {
    Write-Ok ".env already exists - leaving it alone"
} elseif (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    Write-Ok "created .env from .env.example (add your free API keys there)"
} else {
    Write-Notice ".env.example is missing - nothing to copy"
}

# ── 9. verify ────────────────────────────────────────────────────────────────
Write-Step "9/10  Verification"
if ($SkipTests) {
    Write-Dim "skipped (-SkipTests)"
} else {
    if (Test-Path "tests") {
        Write-Info "running the test suite"
        & $PyExe -m pytest tests/ -q
        if ($LASTEXITCODE -eq 0) { Write-Ok "test suite passed" }
        else { Write-Notice "the test suite reported failures - see the output above" }
    } else {
        Write-Dim "no tests/ directory - skipping pytest"
    }

    if (Test-Path $RuffExe) {
        Write-Info "running the lint gate (the same rules CI enforces)"
        & $RuffExe check --select F821,F822,F811,F823,F841,B023,B904,E713,E9 --output-format concise .
        if ($LASTEXITCODE -eq 0) { Write-Ok "lint gate clean" }
        else { Write-Notice "the lint gate found problems - see the output above" }
    }

    & $PyExe -c "import build" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "building the wheel + sdist"
        & $PyExe -m build --outdir dist
        if ($LASTEXITCODE -eq 0) {
            $Built = ((Get-ChildItem "dist" -ErrorAction SilentlyContinue).Name) -join " "
            Write-Ok "built: $Built"
        } else {
            Write-Notice "the wheel build failed - see the output above"
        }
    }
}

# ── 10. doctor ───────────────────────────────────────────────────────────────
Write-Step "10/10  Capability report"
& $PyExe -m jarvis doctor
if ($LASTEXITCODE -ne 0) { Write-Notice "jarvis doctor exited non-zero" }

# ── summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "── Done" -ForegroundColor White
if ($script:Warnings.Count -gt 0) {
    Write-Host "Things to look at:" -ForegroundColor Yellow
    foreach ($W in $script:Warnings) { Write-Host "   - $W" }
}

Write-Host ""
Write-Host "Next steps:"
Write-Host ""
Write-Host "   .venv\Scripts\Activate.ps1     (if blocked, see -ExecutionPolicy Bypass above)"
Write-Host "   python -m jarvis               start chatting"
Write-Host "   python -m jarvis doctor        re-check capabilities any time"
Write-Host ""
Write-Host "Free API keys go in .env - every one is optional, and the chain always"
Write-Host "ends in a stub so a reply is always produced."
Write-Host ""

exit 0
