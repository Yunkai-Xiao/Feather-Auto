param(
    [switch]$SkipPythonInstall,
    [switch]$NoStart,
    [switch]$NoProfileCommand
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$venvDir = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$startScript = Join-Path $scriptDir "start-feather.ps1"
$startCmd = Join-Path $repoRoot "start-feather.cmd"

Set-Location $repoRoot

function Test-PythonCommand {
    param(
        [string]$Command,
        [string[]]$PrefixArgs = @()
    )

    try {
        & $Command @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @()

    foreach ($command in Get-Command python.exe -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Command = $command.Source; PrefixArgs = @() }
    }

    foreach ($command in Get-Command py.exe -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Command = $command.Source; PrefixArgs = @("-3") }
    }

    $knownPaths = @(
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe"
    )
    foreach ($path in $knownPaths) {
        if (Test-Path $path) {
            $candidates += [pscustomobject]@{ Command = $path; PrefixArgs = @() }
        }
    }

    foreach ($candidate in $candidates) {
        if (Test-PythonCommand -Command $candidate.Command -PrefixArgs $candidate.PrefixArgs) {
            return $candidate
        }
    }

    return $null
}

function Invoke-Python {
    param(
        [pscustomobject]$Python,
        [string[]]$Arguments
    )

    & $Python.Command @($Python.PrefixArgs) @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($Python.Command) $($Arguments -join ' ')"
    }
}

function Invoke-VenvPython {
    param([string[]]$Arguments)

    & $venvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Virtualenv command failed: $venvPython $($Arguments -join ' ')"
    }
}

function Install-PythonWithWinget {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Python 3.10+ was not found and winget is unavailable. Install Python from https://www.python.org/downloads/windows/ and rerun this script."
    }

    Write-Host "Installing Python 3.12 with winget..." -ForegroundColor Cyan
    & winget install --id Python.Python.3.12 --exact --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget Python install failed."
    }
}

function Install-ProfileCommand {
    $markerStart = "# >>> Feather Auto bootstrap >>>"
    $markerEnd = "# <<< Feather Auto bootstrap <<<"
    $escapedStart = [regex]::Escape($markerStart)
    $escapedEnd = [regex]::Escape($markerEnd)
    $pattern = "(?s)\r?\n?$escapedStart.*?$escapedEnd\r?\n?"

    $block = @"
$markerStart
function Start-Feather {
    & '$startScript' @args
}
function feather {
    & '$startScript' @args
}
$markerEnd
"@

    $profilePaths = @($PROFILE.CurrentUserAllHosts, $PROFILE.CurrentUserCurrentHost) | Select-Object -Unique
    foreach ($profilePath in $profilePaths) {
        $profileDir = Split-Path -Parent $profilePath
        New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

        $existing = ""
        if (Test-Path $profilePath) {
            $existing = Get-Content $profilePath -Raw
            $existing = [regex]::Replace($existing, $pattern, "")
        }

        $next = $existing.TrimEnd()
        if ($next) {
            $next += "`r`n`r`n"
        }
        $next += $block + "`r`n"
        Set-Content -Path $profilePath -Value $next -Encoding UTF8
        Write-Host "Profile: $profilePath"
    }
    Write-Host "Installed PowerShell commands: feather, Start-Feather" -ForegroundColor Green
}

Write-Host "Feather Auto setup" -ForegroundColor Cyan
Write-Host "Repo: $repoRoot"

$python = Find-Python
if (-not $python) {
    if ($SkipPythonInstall) {
        throw "Python 3.10+ was not found. Rerun without -SkipPythonInstall or install Python manually."
    }
    Install-PythonWithWinget
    $python = Find-Python
}
if (-not $python) {
    throw "Python 3.10+ was not found after installation. Open a new PowerShell and rerun this script."
}

Write-Host "Using Python: $($python.Command) $($python.PrefixArgs -join ' ')" -ForegroundColor Green

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment: $venvDir" -ForegroundColor Cyan
    Invoke-Python -Python $python -Arguments @("-m", "venv", $venvDir)
}

Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
Invoke-VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
Write-Host "Installing PaddlePaddle CPU runtime..." -ForegroundColor Cyan
Invoke-VenvPython -Arguments @(
    "-m",
    "pip",
    "install",
    "paddlepaddle==3.2.0",
    "-i",
    "https://www.paddlepaddle.org.cn/packages/stable/cpu/"
)
Invoke-VenvPython -Arguments @("-m", "pip", "install", "-e", $repoRoot)

Write-Host "Checking installed package..." -ForegroundColor Cyan
Invoke-VenvPython -Arguments @(
    "-X",
    "pycache_prefix=outputs\pycache_check",
    "-m",
    "py_compile",
    "feather_auto\cli.py",
    "feather_auto\dashboard_server.py",
    "feather_auto\download_task_slides.py",
    "feather_auto\review_task_slides.py"
)

if (-not $NoProfileCommand) {
    Install-ProfileCommand
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Start later by double-clicking: $startCmd"
Write-Host "Start later with: feather"
Write-Host "Or run directly: powershell -ExecutionPolicy Bypass -File `"$startScript`""

if (-not $NoStart) {
    & $startScript
}
