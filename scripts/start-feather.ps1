param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "restart")]
    [string]$Command = "start",
    [switch]$NoOpen,
    [switch]$ForceRestart,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$url = "http://127.0.0.1:$Port/dashboard.html"
$stateUrl = "http://127.0.0.1:$Port/api/state"
$restartRequested = $ForceRestart -or $Command -eq "restart"

function Find-FeatherGit {
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidatePaths = @()
    if ($env:ProgramFiles) {
        $candidatePaths += Join-Path $env:ProgramFiles "Git\cmd\git.exe"
        $candidatePaths += Join-Path $env:ProgramFiles "Git\bin\git.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidatePaths += Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe"
    }
    if ($env:LocalAppData) {
        $candidatePaths += Join-Path $env:LocalAppData "Programs\Git\cmd\git.exe"
    }
    if ($env:USERPROFILE) {
        $candidatePaths += Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
    }

    foreach ($candidatePath in $candidatePaths) {
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidatePath).Path
        }
    }

    if ($env:LocalAppData) {
        $githubDesktopRoot = Join-Path $env:LocalAppData "GitHubDesktop"
        if (Test-Path -LiteralPath $githubDesktopRoot -PathType Container) {
            $githubDesktopGit = Get-ChildItem -LiteralPath $githubDesktopRoot -Directory -Filter "app-*" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "resources\app\git\cmd\git.exe" } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
                Select-Object -First 1
            if ($githubDesktopGit) {
                return $githubDesktopGit
            }
        }
    }

    return $null
}

$gitExecutable = Find-FeatherGit

function Invoke-FeatherGit {
    param([string[]]$Arguments)

    if (-not $gitExecutable) {
        return [pscustomobject]@{
            ExitCode = 127
            Output = "git executable not found; install Git for Windows or add git.exe to PATH"
        }
    }

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $gitExecutable -C $repoRoot @Arguments 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output = (($output | Out-String).Trim())
        }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Update-FeatherBeforeRestart {
    $insideWorkTree = Invoke-FeatherGit -Arguments @("rev-parse", "--is-inside-work-tree")
    if ($insideWorkTree.ExitCode -ne 0 -or $insideWorkTree.Output -ne "true") {
        Write-Warning "Restart update skipped: $($insideWorkTree.Output)"
        return
    }

    $upstream = Invoke-FeatherGit -Arguments @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if ($upstream.ExitCode -ne 0) {
        Write-Warning "Restart update skipped because no Git upstream is configured."
        return
    }

    Write-Host "Fetching latest Feather changes..." -ForegroundColor Cyan
    $fetch = Invoke-FeatherGit -Arguments @("fetch", "--quiet", "--prune")
    if ($fetch.ExitCode -ne 0) {
        Write-Warning "Restart update fetch failed: $($fetch.Output)"
        return
    }

    $head = Invoke-FeatherGit -Arguments @("rev-parse", "HEAD")
    $remote = Invoke-FeatherGit -Arguments @("rev-parse", "@{u}")
    $base = Invoke-FeatherGit -Arguments @("merge-base", "HEAD", "@{u}")
    if ($head.ExitCode -ne 0 -or $remote.ExitCode -ne 0 -or $base.ExitCode -ne 0) {
        Write-Warning "Fetched latest changes, but could not compare the local branch with $($upstream.Output)."
        return
    }
    if ($head.Output -eq $remote.Output) {
        Write-Host "Feather is already up to date." -ForegroundColor Green
        return
    }

    $dirty = Invoke-FeatherGit -Arguments @("status", "--porcelain")
    if ($dirty.ExitCode -ne 0) {
        Write-Warning "Restart update could not inspect the worktree: $($dirty.Output)"
        return
    }
    if ($dirty.Output) {
        Write-Warning "Fetched latest changes, but did not update because local changes are present."
        return
    }

    if ($base.Output -ne $head.Output) {
        Write-Warning "Fetched latest changes, but did not update because the local branch has diverged from $($upstream.Output)."
        return
    }

    $merge = Invoke-FeatherGit -Arguments @("merge", "--ff-only", "--quiet", "@{u}")
    if ($merge.ExitCode -ne 0) {
        Write-Warning "Restart update failed: $($merge.Output)"
        return
    }
    Write-Host "Updated Feather to the latest $($upstream.Output)." -ForegroundColor Green
}

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

$portOpen = Test-FeatherPort
$dashboardReady = Test-Dashboard

if ($restartRequested) {
    Update-FeatherBeforeRestart
}

if ($restartRequested -and $portOpen) {
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
