# Codex Provider Windows 版

一个很小的 Codex 渠道切换工具。

它只做三件事：

- 添加第三方 API key 渠道；
- 添加 Codex/ChatGPT 登录账号渠道；
- 在这些渠道之间切换。

默认是中文终端菜单，朋友打开就能用。切换时会自动处理 Codex App 重启，并尽量聚合历史 session；工作区和项目配置不作为渠道的一部分切换。

## 直接运行

解压后在 PowerShell 或 CMD 里运行：

```powershell
.\codex-provider.cmd
```

会出现菜单：

```text
  ____          _           ____                 _     _
 / ___|___   __| | _____  _|  _ \ _ __ _____   _(_) __| | ___ _ __
| |   / _ \ / _` |/ _ \ \/ / |_) | '__/ _ \ \ / / |/ _` |/ _ \ '__|
| |__| (_) | (_| |  __/>  <|  __/| | | (_) \ V /| | (_| |  __/ |
 \____\___/ \__,_|\___/_/\_\_|   |_|  \___/ \_/ |_|\__,_|\___|_|
  https://github.com/r266-tech/codex-provider-windows
  Codex 渠道切换
  当前：未初始化

> 1. 切换渠道      选择已保存的渠道
  2. 新增 API      添加第三方 API key
  3. 新增账号      添加 Codex 登录账号
  4. 当前状态      查看正在使用的渠道
  5. 退出          关闭工具

↑↓  |  Enter 确认  |  Q 退出
```

## 安装到命令行

```powershell
.\install.ps1
```

如果 `%USERPROFILE%\.local\bin` 已经在 PATH 里，之后直接运行：

```powershell
codex-provider
```

## 命令

添加第三方 API 渠道，并切过去：

```powershell
codex-provider add-api https://www.msutools.cn sk-xxxxxx
```

指定名字：

```powershell
codex-provider add-api https://www.msutools.cn sk-xxxxxx --name msutools
```

添加 Codex 登录账号渠道，并切过去：

```powershell
codex-provider add-account work
codex login
```

切换渠道：

```powershell
codex-provider use msutools
codex-provider use work
```

查看：

```powershell
codex-provider list
codex-provider current
```

## 它会改什么

只动当前 Windows 用户的 Codex 本地目录：

- `%USERPROFILE%\.codex\provider-slots.json`
- `%USERPROFILE%\.codex\auth.json`
- `%USERPROFILE%\.codex\auth.<name>.json`
- `%USERPROFILE%\.codex\config.toml`
- `%USERPROFILE%\.codex\sessions`
- `%USERPROFILE%\.codex\archived_sessions`

配置只切换 Codex provider 相关项，不删除 `[projects."..."]` 工作区配置。

历史 session 聚合分两层：

- rollout JSONL：脚本直接处理；
- `state_5.sqlite`：如果电脑上有 `sqlite3.exe`，脚本会一起处理；没有就跳过 DB，不阻断渠道切换。

每次切换前会备份到：

```text
%USERPROFILE%\.codex\provider-switch-backups\
```

## 开源

PowerShell + CMD 包装，无第三方依赖，MIT 许可证。

不要把真实 API key、`%USERPROFILE%\.codex` 内容或备份目录提交到 GitHub。
