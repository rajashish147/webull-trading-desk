param(
    [switch]$Dev,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Executable (exit code $LASTEXITCODE)."
    }
}

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Install Python 3.11-3.14 with 'Add Python to PATH', then reopen PowerShell. See docs/WINDOWS_SETUP.md."
}
$version = & $Python -c "import sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor))"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^3\.(11|12|13|14)$') {
    throw "Python 3.11-3.14 is required. Choose an interpreter with -Python 'C:\path\to\python.exe'."
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    if (Test-Path (Join-Path $PSScriptRoot ".venv")) {
        throw "An incomplete .venv already exists. Review it before removing or replacing it."
    }
    Invoke-Checked -Executable $Python -Arguments @("-m", "venv", ".venv")
}
$venvVersion = & $venvPython -c "import sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor))"
if ($LASTEXITCODE -ne 0 -or $venvVersion -notmatch '^3\.(11|12|13|14)$') {
    throw "The existing .venv uses an unsupported Python version; it was not overwritten."
}

Invoke-Checked -Executable $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
$requirements = if ($Dev) { "requirements-dev.txt" } else { "requirements.txt" }
Invoke-Checked -Executable $venvPython -Arguments @("-m", "pip", "install", "-r", $requirements)

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") (Join-Path $PSScriptRoot ".env")
    Write-Host "Created a blank .env from .env.example."
} else {
    Write-Host "Existing .env preserved."
}

Write-Host ""
Write-Host "Dependencies installed. No credentials were read or printed, and no orders were placed."
Write-Host "1. Edit .env locally: sandbox App Key and App Secret, WEBULL_ENV=sandbox, live trading=false."
Write-Host "2. Run .\.venv\Scripts\python.exe manage.py init to choose the cash sandbox account and generate the local API key."
Write-Host "3. Run .\.venv\Scripts\python.exe manage.py serve, then open http://127.0.0.1:8000."
