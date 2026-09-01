# ============================================================
#  VIDEO STUDIO - SETUP TU DONG (may moi)
#  Tu cai: Python, ffmpeg, Node, codex CLI (qua winget/npm)
#  roi tao venv + cai goi + config + kiem tra moi truong.
#  (Chu khong dau de tranh loi encoding tren may la.)
# ============================================================
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
function Say($m){ Write-Host ("  " + $m) }
function Head($m){ Write-Host ("`n============================================================`n  " + $m + "`n============================================================") }

$WG = @("--accept-source-agreements","--accept-package-agreements","--silent","-e")
$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

function Find-Python {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if ($c -and $c.Source -notmatch "WindowsApps") { return $c.Source }
  $pyc = Get-Command py -ErrorAction SilentlyContinue
  if ($pyc) { $p = (& py -3 -c "import sys;print(sys.executable)" 2>$null); if ($p) { return $p.Trim() } }
  foreach ($g in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe","$env:ProgramFiles\Python3*\python.exe")) {
    $f = Get-ChildItem $g -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
    if ($f) { return $f.FullName }
  }
  return $null
}

Head "1/6  Python 3.10+"
$py = Find-Python
if (-not $py -and $hasWinget) {
  Say "Chua co Python -> cai qua winget (im lang)..."
  winget install @WG --id Python.Python.3.13 | Out-Null
  Start-Sleep 3
  $py = Find-Python
}
if (-not $py) {
  Say "[!] Chua thay Python. HAY DONG cua so nay va CHAY LAI setup.cmd (de PATH cap nhat), hoac cai Python 3.10+ thu cong."
  Read-Host "Enter de thoat"; exit 1
}
Say ("Python: " + $py); & $py --version

Head "2/6  ffmpeg (cat ghep video)"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Say "ffmpeg: da co" }
elseif ($hasWinget) { Say "Cai ffmpeg qua winget..."; winget install @WG --id Gyan.FFmpeg | Out-Null; Say "Da cai ffmpeg (co hieu luc o cua so moi)." }
else { Say "[!] Khong co winget - cai ffmpeg thu cong roi them vao PATH." }

Head "3/6  Node.js (chay codex CLI)"
if (Get-Command node -ErrorAction SilentlyContinue) { Say ("Node: da co " + (node --version)) }
elseif ($hasWinget) { Say "Cai Node LTS qua winget..."; winget install @WG --id OpenJS.NodeJS.LTS | Out-Null; Say "Da cai Node (co hieu luc o cua so moi)." }
else { Say "[!] Khong co winget - cai Node thu cong tu https://nodejs.org" }

Head "4/6  codex CLI (@openai/codex)"
$npm = Get-Command npm -ErrorAction SilentlyContinue
$npmExe = $null
if ($npm) { $npmExe = $npm.Source } elseif (Test-Path "$env:ProgramFiles\nodejs\npm.cmd") { $npmExe = "$env:ProgramFiles\nodejs\npm.cmd" }
if (Get-Command codex -ErrorAction SilentlyContinue) { Say "codex: da co trong PATH" }
elseif ($npmExe) {
  Say "Cai @openai/codex qua npm (global)..."
  & $npmExe install -g "@openai/codex" 2>$null
  Say "Da cai codex. LUU Y: phai chay 'codex login' 1 lan truoc khi dung."
} else { Say "[!] Chua co npm/Node -> mo cua so moi roi chay lai, hoac cai codex thu cong." }

Head "5/6  Moi truong Python (venv + goi core)"
if (-not (Test-Path "$root\venv\Scripts\python.exe")) { Say "Tao venv..."; & $py -m venv "$root\venv" }
$vpy = "$root\venv\Scripts\python.exe"
Say "Cai goi core (flask, httpx, faster-whisper)..."
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -r "$root\requirements.txt"

Head "6/6  Config + kiem tra"
if (-not (Test-Path "$root\config.json")) { Copy-Item "$root\config.example.json" "$root\config.json"; Say "Da tao config.json (NHO dien genmax_api_key)." }
& $vpy "$root\check_env.py"

Head "XONG SETUP"
Say "Con phai lam TAY (1 lan):"
Say "  1. codex login   (dang nhap codex CLI). Neu vua cai codex, mo CUA SO MOI roi go: codex login"
Say "  2. Mo config.json dien genmax_api_key. Giu voice Adam - Dominant, Firm nhu mac dinh."
Say "  3. Neu vua cai Python/ffmpeg/Node lan dau: DONG cua so, mo lai roi chay start-studio.cmd."
Say ""
Say "Goi tuy chon (Remake tach nhac / blur):  venv\Scripts\python.exe -m pip install -r requirements-optional.txt"
Say "Chay tool:  start-studio.cmd  ->  http://127.0.0.1:7995"
Read-Host "Enter de thoat"
