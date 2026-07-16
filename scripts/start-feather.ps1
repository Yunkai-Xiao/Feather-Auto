param(
    [switch]$NoOpen,
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"
$Port = 8001

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$url = "http://127.0.0.1:$Port/dashboard.html"
$stateUrl = "http://127.0.0.1:$Port/api/state"

function Test-FeatherPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $connected = $attempt.AsyncWaitHandle.WaitOne(500, $false)
        if ($connected) {
            $client.EndConnect($attempt)
        }
        return $connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-PythonExecutable {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $false
    }
    try {
        & $Path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-LauncherPython {
    if (Test-PythonExecutable $venvPython) {
        return $venvPython
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object {
        Test-PythonExecutable $_.Source
    } | Select-Object -First 1
    if ($command) {
        return $command.Source
    }
    throw "Python environment not found. Run scripts\setup-windows.ps1 first."
}

function Test-Dashboard {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-DashboardState {
    try {
        return Invoke-RestMethod -Uri $stateUrl -TimeoutSec 5
    } catch {
        return $null
    }
}

function Stop-LegacyDashboard {
    $legacyPort = 8000
    $legacyStateUrl = "http://127.0.0.1:$legacyPort/api/state"
    try {
        $state = Invoke-RestMethod -Uri $legacyStateUrl -TimeoutSec 2
        $runtimeRoot = [string]$state.runtime.repo_root
        $sameRepo = [string]::Equals(
            $runtimeRoot.TrimEnd('\'),
            $repoRoot.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        if (-not $sameRepo) {
            return
        }
        $processIds = @(Get-NetTCPConnection -LocalPort $legacyPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($processId in $processIds) {
            if ($processId) {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            }
        }
        if ($processIds.Count -gt 0) {
            Write-Host "Stopped legacy Feather dashboard on port $legacyPort." -ForegroundColor Yellow
            Start-Sleep -Milliseconds 800
        }
    } catch {
        return
    }
}

function Test-DashboardRuntime {
    $state = Get-DashboardState
    if ($null -eq $state -or $null -eq $state.runtime) {
        Write-Warning "Existing dashboard did not expose runtime health. Restarting it."
        return $false
    }

    $runtime = $state.runtime
    $runtimeRoot = [string]$runtime.repo_root
    $rootMatches = [string]::Equals($runtimeRoot.TrimEnd('\'), $repoRoot.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)
    $paddleReady = $runtime.paddleocr_available -eq $true
    $venvReady = $runtime.venv_python_exists -eq $true

    if (-not $rootMatches) {
        Write-Warning "Existing dashboard belongs to a different repo: $runtimeRoot"
    }
    if (-not $paddleReady) {
        Write-Warning "Existing dashboard cannot import PaddleOCR. Restarting with the repo virtualenv."
    }
    if (-not $venvReady) {
        Write-Warning "Repo virtualenv was not found: $venvPython"
    }

    return $rootMatches -and $paddleReady -and $venvReady
}

function Get-FeatherPortProcessIds {
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-FeatherPortProcesses {
    foreach ($processId in Get-FeatherPortProcessIds) {
        if ($processId) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
}

Stop-LegacyDashboard

$portOpen = Test-FeatherPort
$dashboardReady = Test-Dashboard

if ($ForceRestart -and $portOpen) {
    Stop-FeatherPortProcesses
    Start-Sleep -Milliseconds 800
    $portOpen = Test-FeatherPort
    $dashboardReady = Test-Dashboard
}

if ($dashboardReady -and -not (Test-DashboardRuntime)) {
    Stop-FeatherPortProcesses
    Start-Sleep -Milliseconds 800
    $portOpen = Test-FeatherPort
    $dashboardReady = Test-Dashboard
}

if ($portOpen -and -not $dashboardReady) {
    throw "Port $Port is already in use by another service. Stop that process or choose another port."
}

if (-not $dashboardReady) {
    $python = Find-LauncherPython
    $outputDir = Join-Path $repoRoot "outputs"
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    $stdout = Join-Path $outputDir "dashboard_server.stdout.log"
    $stderr = Join-Path $outputDir "dashboard_server.stderr.log"

    $previousSkipUpdate = $env:FEATHER_AUTO_SKIP_STARTUP_UPDATE
    $env:FEATHER_AUTO_SKIP_STARTUP_UPDATE = "1"
    try {
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList @("-m", "feather_auto.dashboard_server", "--port", "$Port") `
            -WorkingDirectory $repoRoot `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -WindowStyle Hidden `
            -PassThru
    } finally {
        if ($null -eq $previousSkipUpdate) {
            Remove-Item Env:\FEATHER_AUTO_SKIP_STARTUP_UPDATE -ErrorAction SilentlyContinue
        } else {
            $env:FEATHER_AUTO_SKIP_STARTUP_UPDATE = $previousSkipUpdate
        }
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and -not (Test-Dashboard)) {
        Start-Sleep -Milliseconds 400
    }

    if (-not (Test-Dashboard)) {
        throw "Feather dashboard did not start. Check $stderr"
    }

    if (-not (Test-DashboardRuntime)) {
        throw "Feather dashboard started, but runtime health failed. Check $stderr"
    }

    Write-Host "Started Feather dashboard (PID $($process.Id))." -ForegroundColor Green
} else {
    Write-Host "Feather dashboard is already running." -ForegroundColor Yellow
}

if (Test-Dashboard) {
    Write-Host "Dashboard ready: $url" -ForegroundColor Green
} else {
    Write-Warning "Port $Port is open, but $url did not return HTTP 200."
}

if (-not $NoOpen) {
    Start-Process $url
}
