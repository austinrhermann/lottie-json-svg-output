<#
  install.ps1 — Windows installer. The macOS equivalent is install.sh.

    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall

  Puts the panel where After Effects looks for it and builds the private python
  environment it shells out to. Nothing here touches system python or any other
  Adobe extension. Re-running upgrades in place.

  Windows support is UNTESTED — the pipeline was built and validated on macOS.
  If something here is wrong, the panel itself is fine; it is this script and
  the CEP registry keys that want checking first.
#>

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$BundleId = "com.austin.lottie2svg"
$Src      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Support  = Join-Path $env:USERPROFILE ".lottie2svg"
$Venv     = Join-Path $Support "venv"
$Bin      = Join-Path $Support "bin"
$Cep      = Join-Path $env:APPDATA "Adobe\CEP\extensions"
$Dest     = Join-Path $Cep $BundleId

function Say($m) { Write-Host "  $m" }

if ($Uninstall) {
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    Say "removed $Dest"
    Say "the python environment at $Support was left alone; delete it by hand if you want it gone"
    exit 0
}

# ---------------------------------------------------------------- python --- #
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue) }
if (-not $py) {
    Write-Error "python not found. Install it from python.org (tick 'Add to PATH') and re-run."
}

Say "building the python environment in $Venv"
New-Item -ItemType Directory -Force -Path $Support | Out-Null
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { & $py.Source -m venv $Venv }
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet --upgrade lottie

# ------------------------------------------------------------------ panel --- #
Say "installing the panel into $Dest"
New-Item -ItemType Directory -Force -Path $Cep | Out-Null
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($item in @("CSXS", "index.html", "js", "jsx", "py", "README.md")) {
    $p = Join-Path $Src $item
    if (Test-Path $p) { Copy-Item -Recurse -Force $p $Dest }
}

# ---------------------------------------------- unsigned extensions (CEP) --- #
# Any locally built panel is unsigned, so every CEP version in play has to be
# told to load it. Harmless if a version is not installed.
foreach ($v in 9..13) {
    $key = "HKCU:\Software\Adobe\CSXS.$v"
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -Path $key -Name PlayerDebugMode -Value "1" -PropertyType String -Force | Out-Null
}
Say "allowed unsigned extensions (PlayerDebugMode)"

# -------------------------------------------------------------------- cli --- #
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$cmd = Join-Path $Bin "l2s.cmd"
"@echo off`r`n`"$VenvPy`" `"$Dest\py\l2s.py`" %*" | Set-Content -Encoding ASCII $cmd
Say "wrote the l2s command to $cmd"
if ($env:PATH -notlike "*$Bin*") { Say "add it to your PATH:  $Bin" }

Write-Host ""
Say "Done. Quit After Effects fully and reopen it, then:"
Say "Window > Extensions > SVG + Lottie Export"
