param(
    [switch]$NoOpen,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$url = "http://127.0.0.1:$Port/dashboard.html"

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

function Find-LauncherPython {
    if (Test-Path $venvPython) {
        return $venvPython
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
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

if (-not (Test-FeatherPort)) {
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
