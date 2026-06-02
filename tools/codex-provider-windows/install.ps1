$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DestDir = Join-Path $HOME ".local\bin"

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Copy-Item (Join-Path $ScriptDir "codex-provider.ps1") (Join-Path $DestDir "codex-provider.ps1") -Force
Copy-Item (Join-Path $ScriptDir "codex-provider.cmd") (Join-Path $DestDir "codex-provider.cmd") -Force

Write-Host "已安装：$DestDir\codex-provider.cmd" -ForegroundColor Green
Write-Host ""
Write-Host "如果终端找不到 codex-provider，请把下面路径加入用户 PATH："
Write-Host "  $DestDir"
Write-Host ""
Write-Host "运行："
Write-Host "  codex-provider"
