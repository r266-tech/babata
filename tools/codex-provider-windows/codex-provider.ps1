[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

$Version = "0.1.0"
$ProjectUrl = $env:CODEX_PROVIDER_URL
if ([string]::IsNullOrWhiteSpace($ProjectUrl)) {
    $ProjectUrl = "https://github.com/r266-tech/codex-provider-windows"
}

$HomeDir = $env:CODEX_PROVIDER_HOME
if ([string]::IsNullOrWhiteSpace($HomeDir)) {
    $HomeDir = $env:USERPROFILE
}
if ([string]::IsNullOrWhiteSpace($HomeDir)) {
    throw "USERPROFILE 为空。请在正常 Windows 用户下运行。"
}

$CodexHome = $env:CODEX_HOME
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    $CodexHome = Join-Path $HomeDir ".codex"
}

$SlotsFile = Join-Path $CodexHome "provider-slots.json"
$AuthJson = Join-Path $CodexHome "auth.json"
$ConfigToml = Join-Path $CodexHome "config.toml"
$StateDb = Join-Path $CodexHome "state_5.sqlite"
$BackupRoot = Join-Path $CodexHome "provider-switch-backups"
$SessionDirs = @(
    (Join-Path $CodexHome "sessions"),
    (Join-Path $CodexHome "archived_sessions")
)

$OauthProvider = "openai"
$RelayProvider = "OpenAI"
$DefaultModel = "gpt-5.5"
$DefaultApiBaseUrl = "https://api.openai.com/v1"

$Banner = @"
  ____          _           ____                 _     _
 / ___|___   __| | _____  _|  _ \ _ __ _____   _(_) __| | ___ _ __
| |   / _ \ / _` |/ _ \ \/ / |_) | '__/ _ \ \ / / |/ _` |/ _ \ '__|
| |__| (_) | (_| |  __/>  <|  __/| | | (_) \ V /| | (_| |  __/ |
 \____\___/ \__,_|\___/_/\_\_|   |_|  \___/ \_/ |_|\__,_|\___|_|
"@

function Fail([string]$Message, [int]$Code = 2) {
    Write-Host "错误：$Message" -ForegroundColor Red
    exit $Code
}

function Ensure-Dir([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Write-JsonFile([string]$Path, $Value) {
    Ensure-Dir (Split-Path -Parent $Path)
    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json + "`r`n", [System.Text.UTF8Encoding]::new($false))
}

function Read-JsonFile([string]$Path, $Default) {
    if (-not (Test-Path $Path)) {
        return $Default
    }
    try {
        return Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json
    } catch {
        Fail "$Path 不是有效 JSON：$($_.Exception.Message)"
    }
}

function New-SlotsObject {
    [pscustomobject]@{
        version = 1
        current = $null
        slots = [pscustomobject]@{}
    }
}

function Get-Slots([bool]$Required = $true) {
    if (-not (Test-Path $SlotsFile)) {
        if ($Required) {
            Fail "还没有渠道。先运行 codex-provider add-api 或 codex-provider add-account。"
        }
        return New-SlotsObject
    }
    $data = Read-JsonFile $SlotsFile (New-SlotsObject)
    if ($null -eq $data.PSObject.Properties["slots"]) {
        $data | Add-Member -NotePropertyName slots -NotePropertyValue ([pscustomobject]@{})
    }
    return $data
}

function Save-Slots($Data) {
    Ensure-Dir $CodexHome
    Write-JsonFile $SlotsFile $Data
}

function Get-SlotKeys($SlotsObject) {
    if ($null -eq $SlotsObject) {
        return @()
    }
    return @($SlotsObject.PSObject.Properties.Name)
}

function Get-Slot($Data, [string]$Name) {
    $query = $Name.Trim().ToLowerInvariant()
    $keys = Get-SlotKeys $Data.slots
    if ($keys -contains $query) {
        return @($query, $Data.slots.$query)
    }
    $matches = @()
    foreach ($key in $keys) {
        $display = [string]$Data.slots.$key.display_name
        if ($key.Contains($query) -or $display.ToLowerInvariant().Contains($query)) {
            $matches += $key
        }
    }
    if ($matches.Count -eq 1) {
        $key = $matches[0]
        return @($key, $Data.slots.$key)
    }
    if ($matches.Count -eq 0) {
        Fail "找不到渠道：$Name"
    }
    Fail "渠道名不唯一：$($matches -join ', ')"
}

function Validate-Name([string]$Name) {
    $n = $Name.Trim().ToLowerInvariant()
    if ($n -notmatch '^[a-z0-9][a-z0-9_-]{0,30}$') {
        Fail "渠道名只能用小写字母、数字、_、-，最长 31 个字符：$Name"
    }
    return $n
}

function Normalize-BaseUrl([string]$Url) {
    $u = $Url.Trim()
    while ($u.EndsWith("/")) {
        $u = $u.Substring(0, $u.Length - 1)
    }
    if ($u.EndsWith("/v1", [System.StringComparison]::OrdinalIgnoreCase)) {
        $u = $u.Substring(0, $u.Length - 3)
    }
    if ($u -notmatch '^https?://') {
        Fail "URL 必须以 http:// 或 https:// 开头。"
    }
    return $u
}

function Get-NameFromUrl([string]$BaseUrl) {
    try {
        $host = ([uri]$BaseUrl).Host.ToLowerInvariant()
    } catch {
        Fail "无法从 URL 取渠道名，请加 --name。"
    }
    $host = $host -replace '^www\.', '' -replace '^api\.', ''
    $name = ($host -split '\.')[0]
    return Validate-Name $name
}

function Escape-TomlString([string]$Value) {
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

function Split-TomlHead([string]$Text) {
    $m = [regex]::Match($Text, "(?m)^\s*\[")
    if ($m.Success) {
        return @($Text.Substring(0, $m.Index), $Text.Substring($m.Index))
    }
    return @($Text, "")
}

function Remove-RelayProviderBlock([string]$Text) {
    return ([regex]::Replace(
        $Text,
        "(?is)\r?\n*\[model_providers\.OpenAI\][\s\S]*?(?=\r?\n\[|\z)",
        "`r`n"
    )).TrimEnd() + "`r`n"
}

function Set-TopLevelConfig([string]$Text, [hashtable]$Updates) {
    $parts = Split-TomlHead $Text
    $head = $parts[0]
    $tail = $parts[1]
    $keys = (($Updates.Keys | ForEach-Object { [regex]::Escape($_) }) -join "|")
    if (-not [string]::IsNullOrWhiteSpace($keys)) {
        $head = [regex]::Replace($head, "(?m)^\s*($keys)\s*=.*(?:\r?\n)?", "")
    }
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($key in @("model", "model_provider", "api_base_url")) {
        if ($Updates.ContainsKey($key)) {
            $lines.Add("$key = `"$(Escape-TomlString ([string]$Updates[$key]))`"")
        }
    }
    return (($lines -join "`r`n") + "`r`n" + $head.TrimStart("`r", "`n") + $tail)
}

function Update-Config([string]$Mode, [string]$BaseUrl = "", [string]$Model = $DefaultModel) {
    if (Test-Path $ConfigToml) {
        $text = [System.IO.File]::ReadAllText($ConfigToml)
    } else {
        $text = ""
    }
    $provider = if ($Mode -eq "relay") { $RelayProvider } else { $OauthProvider }
    $text = Set-TopLevelConfig $text @{
        model = $Model
        model_provider = $provider
        api_base_url = $DefaultApiBaseUrl
    }
    $text = Remove-RelayProviderBlock $text
    if ($Mode -eq "relay") {
        $block = @"

[model_providers.OpenAI]
name = "OpenAI"
base_url = "$(Escape-TomlString $BaseUrl)"
wire_api = "responses"
requires_openai_auth = true
"@
        $text = $text.TrimEnd() + "`r`n" + $block.TrimStart() + "`r`n"
    }
    Ensure-Dir $CodexHome
    [System.IO.File]::WriteAllText($ConfigToml, $text, [System.Text.UTF8Encoding]::new($false))
}

function Get-ProviderForSlot($Slot) {
    if ([string]$Slot.mode -eq "relay") {
        return $RelayProvider
    }
    return $OauthProvider
}

function Test-Relay([string]$BaseUrl, [string]$ApiKey) {
    $modelsUrl = "$BaseUrl/v1/models"
    $headers = @{
        Authorization = "Bearer $ApiKey"
        Accept = "application/json"
        "User-Agent" = "codex-provider-windows/0.1"
    }
    try {
        $resp = Invoke-RestMethod -Method Get -Uri $modelsUrl -Headers $headers -TimeoutSec 15
        if ($null -ne $resp.data) {
            return "/v1/models 可访问"
        }
        Fail "中转有响应，但返回格式不像 OpenAI 兼容接口。"
    } catch {
        $status = $null
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        if ($status -eq 401) { Fail "401 Unauthorized：API key 不正确。" 3 }
        if ($status -eq 403) { Fail "403 Forbidden：key 权限不足，或中转拒绝当前网络。" 3 }
        if ($status -eq 404) { Fail "404 Not Found：URL 可能不对，通常不要带 /v1。" 3 }
        Fail "无法访问 $modelsUrl：$($_.Exception.Message)" 3
    }
}

function Stop-Codex {
    Get-Process -Name "Codex" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

function Start-Codex {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Codex\Codex.exe")
        $candidates += (Join-Path $env:LOCALAPPDATA "Codex\Codex.exe")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $candidates += (Join-Path $env:ProgramFiles "Codex\Codex.exe")
    }
    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
        $candidates += (Join-Path $programFilesX86 "Codex\Codex.exe")
    }

    foreach ($path in $candidates) {
        if (Test-Path $path) {
            Start-Process $path
            return $true
        }
    }
    try {
        Start-Process "Codex" -ErrorAction Stop
        return $true
    } catch {
        Write-Host "未找到 Codex App 启动路径，请手动打开 Codex App。" -ForegroundColor Yellow
        return $false
    }
}

function New-BackupDir([string]$Label) {
    $safe = $Label -replace '[^A-Za-z0-9_.-]+', '-'
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $path = Join-Path $BackupRoot "$stamp-$safe"
    Ensure-Dir $path
    return $path
}

function Backup-File([string]$Path, [string]$BackupDir) {
    if (Test-Path $Path) {
        Copy-Item $Path (Join-Path $BackupDir (Split-Path -Leaf $Path)) -Force
    }
}

function Save-CurrentAuth($Data) {
    $current = [string]$Data.current
    if ([string]::IsNullOrWhiteSpace($current)) {
        return
    }
    $keys = Get-SlotKeys $Data.slots
    if ($keys -notcontains $current) {
        return
    }
    $slot = $Data.slots.$current
    if ([string]$slot.mode -ne "oauth") {
        return
    }
    if (Test-Path $AuthJson) {
        $target = [string]$slot.auth_file
        if (-not [string]::IsNullOrWhiteSpace($target)) {
            Ensure-Dir (Split-Path -Parent $target)
            Copy-Item $AuthJson $target -Force
        }
    }
}

function Ensure-Initialized {
    if (Test-Path $SlotsFile) {
        return
    }
    Ensure-Dir $CodexHome
    $authLocal = Join-Path $CodexHome "auth.local.json"
    if (Test-Path $AuthJson) {
        Copy-Item $AuthJson $authLocal -Force
    } elseif (-not (Test-Path $authLocal)) {
        [System.IO.File]::WriteAllText($authLocal, "{}" + "`r`n", [System.Text.UTF8Encoding]::new($false))
    }
    $data = New-SlotsObject
    $data.current = "local"
    $data.slots | Add-Member -NotePropertyName "local" -NotePropertyValue ([pscustomobject]@{
        display_name = "Codex - local"
        mode = "oauth"
        auth_file = $authLocal
    })
    Save-Slots $data
}

function Write-RelayAuth([string]$Path, [string]$ApiKey) {
    Write-JsonFile $Path ([ordered]@{
        OPENAI_API_KEY = $ApiKey
        auth_mode = "apikey"
    })
}

function Normalize-Rollouts([string]$TargetProvider, [string]$BackupDir) {
    $changed = 0
    foreach ($root in $SessionDirs) {
        if (-not (Test-Path $root)) {
            continue
        }
        Get-ChildItem -Path $root -Recurse -Filter "rollout-*.jsonl" -File -ErrorAction SilentlyContinue | ForEach-Object {
            $path = $_.FullName
            $text = [System.IO.File]::ReadAllText($path)
            $newText = $text
            if ($TargetProvider -eq $RelayProvider) {
                $newText = $newText.Replace('"model_provider":"openai"', '"model_provider":"OpenAI"')
            } else {
                $newText = $newText.Replace('"model_provider":"OpenAI"', '"model_provider":"openai"')
            }
            if ($newText -ne $text) {
                $lastWrite = $_.LastWriteTimeUtc
                $backupName = ($path -replace '[:\\\/]+', '_')
                Copy-Item $path (Join-Path $BackupDir "$backupName.bak") -Force
                [System.IO.File]::WriteAllText($path, $newText, [System.Text.UTF8Encoding]::new($false))
                [System.IO.File]::SetLastWriteTimeUtc($path, $lastWrite)
                $changed += 1
            }
        }
    }
    return $changed
}

function Normalize-StateDb([string]$TargetProvider, [string]$BackupDir) {
    if (-not (Test-Path $StateDb)) {
        return "无 state_5.sqlite"
    }
    $sqlite = Get-Command "sqlite3" -ErrorAction SilentlyContinue
    if ($null -eq $sqlite) {
        return "跳过 DB：未找到 sqlite3.exe"
    }
    Copy-Item $StateDb (Join-Path $BackupDir "state_5.sqlite") -Force
    $escaped = $TargetProvider.Replace("'", "''")
    $sql = "UPDATE threads SET model_provider='$escaped' WHERE model_provider IN ('openai','OpenAI') AND model_provider!='$escaped';"
    & $sqlite.Source $StateDb $sql | Out-Null
    if ($LASTEXITCODE -ne 0) {
        return "DB 聚合失败"
    }
    return "DB 已聚合"
}

function Normalize-Sessions([string]$TargetProvider) {
    $backup = New-BackupDir "sessions-$TargetProvider"
    $dbResult = Normalize-StateDb $TargetProvider $backup
    $rolloutChanged = Normalize-Rollouts $TargetProvider $backup
    Write-Host "  历史 session：$dbResult；rollout 文件 $rolloutChanged 个"
    Write-Host "  备份：$backup"
}

function Add-Api([string[]]$CommandArgs) {
    if ($CommandArgs.Count -lt 2) {
        Fail "用法：codex-provider add-api <base_url> <api_key> [--name name]"
    }
    $baseUrl = Normalize-BaseUrl $CommandArgs[0]
    $apiKey = $CommandArgs[1].Trim()
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        Fail "API key 不能为空。"
    }
    $name = ""
    $skipCheck = $false
    $noSwitch = $false
    for ($i = 2; $i -lt $CommandArgs.Count; $i++) {
        if ($CommandArgs[$i] -in @("--name", "--slot")) {
            $i += 1
            if ($i -ge $CommandArgs.Count) { Fail "--name 需要一个值" }
            $name = $CommandArgs[$i]
        } elseif ($CommandArgs[$i] -in @("--skip-check", "--skip-probe")) {
            $skipCheck = $true
        } elseif ($CommandArgs[$i] -eq "--no-switch") {
            $noSwitch = $true
        } else {
            Fail "未知参数：$($CommandArgs[$i])"
        }
    }
    if ([string]::IsNullOrWhiteSpace($name)) {
        $name = Get-NameFromUrl $baseUrl
    } else {
        $name = Validate-Name $name
    }
    Ensure-Initialized
    if (-not $skipCheck) {
        $probe = Test-Relay $baseUrl $apiKey
        Write-Host "检查通过：$probe" -ForegroundColor Green
    }
    $data = Get-Slots $true
    $existing = $data.slots.PSObject.Properties[$name]
    if ($null -ne $existing -and [string]$existing.Value.mode -ne "relay") {
        Fail "渠道 $name 已存在，但不是 API 渠道。"
    }
    $slot = [pscustomobject]@{
        display_name = "Codex - $name API"
        mode = "relay"
        auth_file = (Join-Path $CodexHome "auth.$name.json")
        base_url = $baseUrl
        model = $DefaultModel
        api_key = $apiKey
    }
    if ($null -eq $existing) {
        $data.slots | Add-Member -NotePropertyName $name -NotePropertyValue $slot
        Write-Host "已添加 API 渠道：$name"
    } else {
        $data.slots.PSObject.Properties.Remove($name)
        $data.slots | Add-Member -NotePropertyName $name -NotePropertyValue $slot
        Write-Host "已更新 API 渠道：$name"
    }
    Save-Slots $data
    if ($noSwitch) {
        Write-Host "稍后切换：codex-provider use $name"
        return
    }
    Use-Channel @($name)
}

function Add-Account([string[]]$CommandArgs) {
    if ($CommandArgs.Count -lt 1) {
        Fail "用法：codex-provider add-account <name>"
    }
    $name = Validate-Name $CommandArgs[0]
    $noSwitch = $CommandArgs -contains "--no-switch"
    Ensure-Initialized
    $data = Get-Slots $true
    $existing = $data.slots.PSObject.Properties[$name]
    if ($null -ne $existing -and [string]$existing.Value.mode -ne "oauth") {
        Fail "渠道 $name 已存在，但不是账号渠道。"
    }
    $slot = [pscustomobject]@{
        display_name = "Codex - $name"
        mode = "oauth"
        auth_file = (Join-Path $CodexHome "auth.$name.json")
    }
    if ($null -eq $existing) {
        $data.slots | Add-Member -NotePropertyName $name -NotePropertyValue $slot
        Write-Host "已添加账号渠道：$name"
    } else {
        $data.slots.PSObject.Properties.Remove($name)
        $data.slots | Add-Member -NotePropertyName $name -NotePropertyValue $slot
        Write-Host "已更新账号渠道：$name"
    }
    Save-Slots $data
    if ($noSwitch) {
        Write-Host "稍后切换：codex-provider use $name"
        return
    }
    Use-Channel @($name)
    Write-Host "下一步：运行 codex login，登录这个账号。"
}

function Use-Channel([string[]]$CommandArgs) {
    if ($CommandArgs.Count -lt 1) {
        Fail "用法：codex-provider use <name>"
    }
    Ensure-Initialized
    $data = Get-Slots $true
    $resolved = Get-Slot $data $CommandArgs[0]
    $name = $resolved[0]
    $slot = $resolved[1]

    Save-CurrentAuth $data
    Stop-Codex

    $backup = New-BackupDir "switch-$name"
    Backup-File $AuthJson $backup
    Backup-File $ConfigToml $backup

    if ([string]$slot.mode -eq "relay") {
        Write-RelayAuth ([string]$slot.auth_file) ([string]$slot.api_key)
        Copy-Item ([string]$slot.auth_file) $AuthJson -Force
        Update-Config "relay" ([string]$slot.base_url) ([string]$slot.model)
    } else {
        $targetAuth = [string]$slot.auth_file
        if (Test-Path $targetAuth) {
            Copy-Item $targetAuth $AuthJson -Force
        } else {
            [System.IO.File]::WriteAllText($AuthJson, "{}" + "`r`n", [System.Text.UTF8Encoding]::new($false))
        }
        Update-Config "oauth" "" $DefaultModel
    }

    Normalize-Sessions (Get-ProviderForSlot $slot)
    $data.current = $name
    Save-Slots $data
    Write-Host "已切换到：$($slot.display_name) ($name) [$($slot.mode)]" -ForegroundColor Green
    [void](Start-Codex)
}

function List-Channels {
    $data = Get-Slots $false
    $keys = Get-SlotKeys $data.slots
    if ($keys.Count -eq 0) {
        Write-Host "还没有渠道"
        Write-Host "  添加 API：  codex-provider add-api https://relay.example.com sk-..."
        Write-Host "  添加账号： codex-provider add-account work"
        return
    }
    foreach ($key in $keys) {
        $slot = $data.slots.$key
        $mark = if ($key -eq [string]$data.current) { "*" } else { " " }
        $extra = if ([string]$slot.mode -eq "relay") { [string]$slot.base_url } else { "账号登录" }
        Write-Host ("{0} {1,-16} [{2,-5}] {3,-28} {4}" -f $mark, $key, $slot.mode, $slot.display_name, $extra)
    }
}

function Current-Channel {
    $data = Get-Slots $false
    $current = [string]$data.current
    $keys = Get-SlotKeys $data.slots
    if ([string]::IsNullOrWhiteSpace($current) -or ($keys -notcontains $current)) {
        Write-Host "当前没有渠道"
        Write-Host "  运行：codex-provider"
        return
    }
    $slot = $data.slots.$current
    Write-Host "当前渠道：$($slot.display_name) ($current) [$($slot.mode)]"
    Write-Host "  渠道文件：$SlotsFile"
    Write-Host "  auth.json：$AuthJson"
    Write-Host "  config：   $ConfigToml"
    $running = if (Get-Process -Name "Codex" -ErrorAction SilentlyContinue) { "是" } else { "否" }
    Write-Host "  App 运行中：$running"
}

function Show-Help {
    Write-Host "用法："
    Write-Host "  codex-provider"
    Write-Host "  codex-provider add-api <base_url> <api_key> [--name name]"
    Write-Host "  codex-provider add-account <name>"
    Write-Host "  codex-provider use <name>"
    Write-Host "  codex-provider list"
    Write-Host "  codex-provider current"
}

function Read-LineDefault([string]$Prompt, [string]$Default = "") {
    if ([string]::IsNullOrWhiteSpace($Default)) {
        return (Read-Host $Prompt).Trim()
    }
    $value = (Read-Host "$Prompt [$Default]").Trim()
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value
}

function Add-ApiMenu {
    Clear-Host
    Write-Host "新增 API 渠道"
    Write-Host ""
    $baseUrl = Read-LineDefault "中转 base URL，不要带 /v1"
    $apiKey = Read-LineDefault "API key"
    $name = Read-LineDefault "渠道名，空着则自动从域名取"
    $args = @($baseUrl, $apiKey)
    if (-not [string]::IsNullOrWhiteSpace($name)) {
        $args += @("--name", $name)
    }
    Add-Api $args
    Pause-Menu
}

function Add-AccountMenu {
    Clear-Host
    Write-Host "新增账号渠道"
    Write-Host ""
    $name = Read-LineDefault "渠道名，例如 personal 或 work"
    Add-Account @($name)
    Pause-Menu
}

function SwitchMenu {
    $data = Get-Slots $false
    $keys = Get-SlotKeys $data.slots
    if ($keys.Count -eq 0) {
        Write-Host "还没有渠道。请先新增账号或 API。"
        Pause-Menu
        return
    }
    $options = @()
    foreach ($key in $keys) {
        $slot = $data.slots.$key
        $mark = if ($key -eq [string]$data.current) { "*" } else { " " }
        $extra = if ([string]$slot.mode -eq "relay") { [string]$slot.base_url } else { "账号登录" }
        $options += [pscustomobject]@{
            Label = ("{0} {1,-16} [{2}] {3}  {4}" -f $mark, $key, $slot.mode, $slot.display_name, $extra)
            Value = $key
        }
    }
    $choice = Select-Menu "切换渠道" "" $options
    if ($null -ne $choice) {
        Use-Channel @($choice)
        Pause-Menu
    }
}

function Pause-Menu {
    Write-Host ""
    Write-Host "按 Enter 继续..."
    [void][Console]::ReadLine()
}

function Select-Menu([string]$Title, [string]$Subtitle, $Options) {
    $selected = 0
    while ($true) {
        Clear-Host
        if ($Title -eq "Codex 渠道切换") {
            Write-Host $Banner
            Write-Host "  $ProjectUrl"
            Write-Host "  $Title"
            if (-not [string]::IsNullOrWhiteSpace($Subtitle)) {
                Write-Host "  $Subtitle"
            }
            Write-Host ""
        } else {
            Write-Host $Title
            if (-not [string]::IsNullOrWhiteSpace($Subtitle)) {
                Write-Host $Subtitle
            }
            Write-Host ""
        }
        for ($i = 0; $i -lt $Options.Count; $i++) {
            $marker = if ($i -eq $selected) { ">" } else { " " }
            Write-Host ("{0} {1}. {2}" -f $marker, ($i + 1), $Options[$i].Label)
        }
        Write-Host ""
        Write-Host "↑↓  |  Enter 确认  |  Q 退出"
        $key = [Console]::ReadKey($true)
        if ($key.Key -eq "UpArrow") {
            $selected = ($selected + $Options.Count - 1) % $Options.Count
        } elseif ($key.Key -eq "DownArrow") {
            $selected = ($selected + 1) % $Options.Count
        } elseif ($key.Key -eq "Enter") {
            return $Options[$selected].Value
        } elseif ($key.Key -eq "Q" -or $key.Key -eq "Escape") {
            return $null
        }
    }
}

function Show-Menu {
    while ($true) {
        $data = Get-Slots $false
        $current = [string]$data.current
        $keys = Get-SlotKeys $data.slots
        if (-not [string]::IsNullOrWhiteSpace($current) -and ($keys -contains $current)) {
            $slot = $data.slots.$current
            $currentLabel = "$($slot.display_name) ($current) [$($slot.mode)]"
        } else {
            $currentLabel = "未初始化"
        }
        $options = @(
            [pscustomobject]@{ Label = "切换渠道      选择已保存的渠道"; Value = "switch" },
            [pscustomobject]@{ Label = "新增 API      添加第三方 API key"; Value = "add-api" },
            [pscustomobject]@{ Label = "新增账号      添加 Codex 登录账号"; Value = "add-account" },
            [pscustomobject]@{ Label = "当前状态      查看正在使用的渠道"; Value = "current" },
            [pscustomobject]@{ Label = "退出          关闭工具"; Value = "quit" }
        )
        $choice = Select-Menu "Codex 渠道切换" "当前：$currentLabel" $options
        if ($null -eq $choice -or $choice -eq "quit") {
            Clear-Host
            return
        }
        if ($choice -eq "switch") { SwitchMenu }
        elseif ($choice -eq "add-api") { Add-ApiMenu }
        elseif ($choice -eq "add-account") { Add-AccountMenu }
        elseif ($choice -eq "current") { Clear-Host; Current-Channel; Pause-Menu }
    }
}

if ($CliArgs.Count -eq 0) {
    Show-Menu
    exit 0
}

$command = $CliArgs[0].ToLowerInvariant()
$rest = @()
if ($CliArgs.Count -gt 1) {
    $rest = $CliArgs[1..($CliArgs.Count - 1)]
}

switch ($command) {
    "add-api" { Add-Api $rest }
    "add-account" { Add-Account $rest }
    "use" { Use-Channel $rest }
    "list" { List-Channels }
    "current" { Current-Channel }
    "help" { Show-Help }
    "--help" { Show-Help }
    "-h" { Show-Help }
    "version" { Write-Host "codex-provider $Version"; Write-Host $ProjectUrl }
    default { Fail "未知命令：$command。运行 codex-provider help 查看用法。" }
}
