# Codex Provider macOS 版

一个很小的 Codex 渠道切换工具。

它只做三件事：

- 添加第三方 API key 渠道；
- 添加 Codex/ChatGPT 登录账号渠道；
- 在这些渠道之间切换。

切换时会自动处理 Codex App 重启和历史 session 可见性。用户不需要理解这些内部细节。

## 安装

```bash
cd codex-provider-macos
./install.sh
```

之后直接运行：

```bash
codex-provider
```

不安装也可以：

```bash
./codex-provider
```

## 交互菜单

默认就是终端菜单：

```text
  ____          _           ____                 _     _
 / ___|___   __| | _____  _|  _ \ _ __ _____   _(_) __| | ___ _ __
| |   / _ \ / _` |/ _ \ \/ / |_) | '__/ _ \ \ / / |/ _` |/ _ \ '__|
| |__| (_) | (_| |  __/>  <|  __/| | | (_) \ V /| | (_| |  __/ |
 \____\___/ \__,_|\___/_/\_\_|   |_|  \___/ \_/ |_|\__,_|\___|_|
  https://github.com/r266-tech/codex-provider-macos
  Codex 渠道切换
  当前：personal (personal) [oauth]

➤ 1. 切换渠道      选择已保存的渠道
  2. 新增 API      添加第三方 API key
  3. 新增账号      添加 Codex 登录账号
  4. 当前状态      查看正在使用的渠道
  5. 退出          关闭工具

↑↓  |  Enter 确认  |  Q 退出
```

## 命令

添加第三方 API 渠道，并切过去：

```bash
codex-provider add-api https://www.msutools.cn sk-xxxxxx
```

指定名字：

```bash
codex-provider add-api https://www.msutools.cn sk-xxxxxx --name msutools
```

添加 Codex 登录账号渠道，并切过去：

```bash
codex-provider add-account work
codex login
```

切换渠道：

```bash
codex-provider use msutools
codex-provider use work
```

查看：

```bash
codex-provider list
codex-provider current
```

## 它会保留什么

切换时保留 Codex App 的工作区和项目配置，不会把当前 workspace 换掉。

工具只会在当前 macOS 用户的 Codex 本地目录里工作：

- `~/.codex`
- `~/Library/Application Support/Codex`

不要把真实 API key、`~/.codex` 内容或 App profile 提交到 GitHub。

## 开源

单文件 Python CLI，无第三方依赖，MIT 许可证。
