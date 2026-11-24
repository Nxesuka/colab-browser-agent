# Lanceur automatique tout-en-un pour Windows (Python >= 3.11, .venv, deps, Chromium, .env, agent)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Test-PythonExe {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    try {
        & $Exe @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    foreach ($ver in @("-3.12", "-3.13", "-3.11")) {
        $cmd = Get-Command py -ErrorAction SilentlyContinue
        if ($cmd -and (Test-PythonExe -Exe "py" -PrefixArgs @($ver))) {
            $resolved = (& py $ver -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($resolved -and ($resolved -notlike "*WindowsApps*")) { return $resolved }
        }
    }
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd -and ($pyCmd.Source -notlike "*WindowsApps*") -and (Test-PythonExe -Exe $pyCmd.Source)) {
        return $pyCmd.Source
    }
    $searchPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "C:\Program Files\Python3*\python.exe"
    )
    foreach ($pattern in $searchPaths) {
        $candidates = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($c in $candidates) {
            if (Test-PythonExe -Exe $c.FullName) { return $c.FullName }
        }
    }
    return $null
}

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "  Demarrage automatique de l'agent navigateur" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan

$PythonExe = Find-Python
if (-not $PythonExe) {
    Write-Host "[1/5] Python >= 3.11 introuvable. Installation automatique de Python 3.12 via winget..." -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "ERREUR : winget est introuvable. Installez Python 3.12 manuellement depuis python.org." -ForegroundColor Red
        exit 1
    }
    winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    $PythonExe = Find-Python
    if (-not $PythonExe) {
        Write-Host "ERREUR : Python 3.12 a ete installe mais n'est pas encore detecte. Relancez LANCER_AGENT.bat." -ForegroundColor Red
        exit 1
    }
}
Write-Host "[1/5] Python OK : $PythonExe" -ForegroundColor Green

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[2/5] Creation de l'environnement virtuel (.venv)..." -ForegroundColor Yellow
    & $PythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit 1 }
} else {
    Write-Host "[2/5] Environnement virtuel .venv deja pret." -ForegroundColor Green
}

$ReqFile = Join-Path $ProjectRoot "requirements.txt"
$ReqHashFile = Join-Path $ProjectRoot ".venv\.req_hash"
$CurrentHash = (Get-FileHash -Path $ReqFile -Algorithm SHA256).Hash
$SavedHash = if (Test-Path $ReqHashFile) { (Get-Content -Path $ReqHashFile -Raw).Trim() } else { "" }

if ($CurrentHash -ne $SavedHash) {
    Write-Host "[3/5] Installation / mise a jour des dependances Python..." -ForegroundColor Yellow
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { exit 1 }
    & $VenvPython -m pip install -r $ReqFile
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Set-Content -Path $ReqHashFile -Value $CurrentHash -NoNewline
} else {
    Write-Host "[3/5] Dependances Python a jour." -ForegroundColor Green
}

$PlaywrightFlag = Join-Path $ProjectRoot ".venv\.playwright_ok"
$PlaywrightExe = Join-Path $ProjectRoot ".venv\Scripts\playwright.exe"
if (-not (Test-Path $PlaywrightFlag)) {
    Write-Host "[4/5] Installation du navigateur Chromium (Playwright)..." -ForegroundColor Yellow
    & $PlaywrightExe install chromium
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Set-Content -Path $PlaywrightFlag -Value "ok" -NoNewline
} else {
    Write-Host "[4/5] Navigateur Chromium deja installe." -ForegroundColor Green
}

$EnvFile = Join-Path $ProjectRoot ".env"
$EnvExample = Join-Path $ProjectRoot ".env.example"
if ((-not (Test-Path $EnvFile)) -and (Test-Path $EnvExample)) {
    Copy-Item -Path $EnvExample -Destination $EnvFile
    Write-Host "[5/5] Fichier .env cree depuis .env.example." -ForegroundColor Green
} else {
    Write-Host "[5/5] Configuration .env prete." -ForegroundColor Green
}

function Find-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $std = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $std) { return $std }
    return $null
}

$TsExe = Find-Tailscale
if (-not $TsExe) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "[6/6] Tailscale absent. Installation automatique via winget..." -ForegroundColor Yellow
        winget install --id Tailscale.Tailscale -e --source winget --accept-package-agreements --accept-source-agreements
        $TsExe = Find-Tailscale
    }
}
if ($TsExe) {
    & $TsExe status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[6/6] Connexion a Tailscale (suivez la fenetre de connexion si demandee)..." -ForegroundColor Yellow
        try { & $TsExe up } catch { Write-Host "[Info] Tailscale up interrompu ou non connecte." -ForegroundColor DarkYellow }
    } else {
        Write-Host "[6/6] Tailscale connecte." -ForegroundColor Green
    }
} else {
    Write-Host "[Info] Tailscale introuvable (necessaire pour joindre colab-gpu)." -ForegroundColor DarkYellow
}

$hasLo = (Get-Command soffice -ErrorAction SilentlyContinue) -or (Test-Path "C:\Program Files\LibreOffice\program\soffice.exe")
if (-not $hasLo) {
    Write-Host "[Info] LibreOffice non detecte (optionnel, utile pour la conversion PDF / .ods)." -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "Lancement de l'agent..." -ForegroundColor Cyan
& $VenvPython agent.py @args
exit $LASTEXITCODE
