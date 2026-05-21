# claude-code-export

> 导出、归档、迁移、续聊 **Claude Code CLI** 会话。

[English](README.md) · [中文](README.zh-CN.md)

一个纯 Python 单文件工具，把 Claude Code 写在 `~/.claude/projects/...` 下的
JSONL transcript 转成三种东西：

- **人能读的归档** —— HTML / Markdown / JSON / CSV 四种渲染，外加项目根的
  `CLAUDE.md` 与助手 `Write` / `Edit` 过的每个文件，
- **能移植的备份** —— 把 bundle 拷到另一台机器上，`claude --resume` 直接接着聊，
- **跨账号续聊的种子（seed）** —— 一份 Markdown，粘到全新对话的第一条消息里，
  哪怕换账号、换机器，新对话也能从上次停下的地方接着干。

零第三方依赖（纯 Python 3.9+ 标准库），一个 .py 文件，一个 CLI。

姊妹项目：[claude-cowork-export](https://github.com/PeriChu/claude-cowork-export)
针对 Claude 桌面端的 Cowork chat。本项目针对 CLI 工具。

---

## 目录

- [安装](#安装)
- [快速上手](#快速上手)
- [命令](#命令)
  - [`list`](#list)
  - [`export`](#export)
  - [`import`](#import)
  - [`seed`](#seed)
- [Bundle 结构](#bundle-结构)
- [常见工作流](#常见工作流)
- [跨平台说明](#跨平台说明)
- [安全：`--include-auth` 注意事项](#安全)
- [故障排查](#故障排查)
- [分支](#分支)
- [License](#license)

---

## 安装

需要 Python 3.9 或更高。推荐使用 [pipx](https://pipx.pypa.io/)：

```bash
# macOS / Linux
pipx install "git+https://github.com/PeriChu/claude-code-export.git"

# Windows（PowerShell）—— 同样命令，但装的是 Windows 分支
pipx install "git+https://github.com/PeriChu/claude-code-export.git@windows"
```

装好后会注册两个等价的命令入口：

```bash
claude-code-export ...   # 主名
code-export ...          # 短别名
```

也可以不装，直接当脚本跑：

```bash
git clone https://github.com/PeriChu/claude-code-export.git
cd claude-code-export
python3 claude_code_export.py --help
```

---

## 快速上手

```bash
# 列出磁盘上所有会话，按项目分组
claude-code-export list

# 导出最近一次会话到 ./exports/
claude-code-export export latest --output ./exports

# 导出所有会话
claude-code-export export all --output ./exports

# 把 bundle 拷到另一台机器后，恢复回 ~/.claude/projects/
claude-code-export import ./exports/<session-id>

# 或者新账号场景：把整个会话压成一份可粘贴的续聊 prompt
claude-code-export seed ./exports/<session-id>
# → 输出 ./exports/<session-id>/seed-prompt.md
```

---

## 命令

CLI 有四个子命令。任何时候 `claude-code-export <cmd> --help` 都会列出当时的
完整 flag 清单。

### `list`

```
claude-code-export list [--code-root PATH] [--project PATH]
```

列出 `~/.claude/projects/` 下的所有 Claude Code 会话，按项目工作目录分组
（目录名是被编码过的；优先用 transcript 自带的 `cwd` 字段，回退到反向解码）。

| Flag | 默认值 | 作用 |
|---|---|---|
| `--code-root PATH` | `~/.claude/projects` | 改读其他位置——归档备份、外置盘等。 |
| `--project PATH` | _(无)_ | 只列出 `cwd` 以 `PATH` 开头的会话（前缀匹配）。 |

**示例：**

```bash
# 全部
claude-code-export list

# 只看某个项目
claude-code-export list --project ~/code/foo

# 备份树挂在外置盘
claude-code-export list --code-root /Volumes/Backup/.claude/projects
```

### `export`

```
claude-code-export export <selector> [-o DIR] [--formats LIST]
                                     [--no-files]
                                     [--include-auth] [--yes-i-know-this-is-risky]
                                     [--code-root PATH] [--project PATH]
```

把一个或多个会话导出成独立 bundle 目录（每个会话一个）。selector 决定导哪个：

| Selector | 含义 |
|---|---|
| `latest` | 最近一次活动的会话。 |
| `all` | 所有会话。 |
| `<prefix>` | UUID 以 `<prefix>` 开头的唯一会话；歧义时报错。 |

| Flag | 默认值 | 作用 |
|---|---|---|
| `-o`, `--output`, `--out` | `./exports` | bundle 输出目录，每个会话一个子目录。 |
| `--formats` | `html,md,json,csv` | 逗号分隔的子集。 |
| `--no-files` | _(关)_ | 不拷贝触碰文件和 `CLAUDE.md`，bundle 更小更快。 |
| `--include-auth` | _(关)_ | **高风险**。同时把 `~/.claude/.credentials.json` 装进 bundle，让新机器 `import` 后无需登录直接续聊。详见 [安全](#安全)。 |
| `--yes-i-know-this-is-risky` | _(关)_ | 跳过 `--include-auth` 的交互式 `I UNDERSTAND` 确认，仅供 CI 使用。 |

**示例：**

```bash
# 全部到 ./exports/
claude-code-export export all --output ./exports

# 只导一个会话，仅 HTML + JSON
claude-code-export export <session-id> --output ./exports --formats html,json

# 最小 bundle —— 只 transcript，不带触碰文件
claude-code-export export latest --no-files

# 带凭证的个人备份（仔细看一下警告再用）
claude-code-export export latest --output ./private-backup --include-auth
```

### `import`

```
claude-code-export import <bundle> [--cwd PATH] [--code-root PATH]
                                   [--skip-auth] [--dry-run] [--force]
```

把 bundle 还原回本机的 Claude Code 仓库，使 `claude --resume <session-id>` 能识别。
自动处理路径改写和跨平台分隔符转换；默认拒绝危险覆盖。

| Flag | 默认值 | 作用 |
|---|---|---|
| `<bundle>` | _(必填)_ | `export` 产出的 bundle 目录路径。 |
| `--cwd PATH` | _(读 manifest)_ | 目标机器上的工作目录。**跨平台时必填。** |
| `--code-root PATH` | `~/.claude/projects` | 改写目标 Claude Code projects 根目录。 |
| `--skip-auth` | _(关)_ | 即使 bundle 里有 `auth/` 也忽略；不动本机凭证。 |
| `--dry-run` | _(关)_ | 只打印 rewrite 计划，不实际写入。 |
| `--force` | _(关)_ | 允许覆盖已存在的 transcript / 还原文件 / 本机凭证。 |

**示例：**

```bash
# 同机器、同 cwd —— 直接从 bundle 重建
claude-code-export import ./exports/<session-id>

# 同机器、换 cwd
claude-code-export import ./exports/<session-id> --cwd ~/code/foo-renamed

# 跨平台（macOS bundle → Windows 机器）：必须指定 --cwd
claude-code-export import .\exports\<session-id> --cwd "D:\code\foo"

# 先看会做什么，再决定要不要写
claude-code-export import ./exports/<session-id> --dry-run

# import 之后续聊
claude --resume <session-id>
```

### `seed`

```
claude-code-export seed <bundle> [--mode brief|standard|full] [-o PATH]
```

从 bundle 渲染一份自包含的 Markdown 续聊 prompt。把它贴到一个**全新**的
Claude / Cowork 对话里当首条消息——无论是哪个账号、哪台机器——就能让新对话
从上一次停下的地方接着走。**不动鉴权、不依赖任何服务端状态**：新对话确实是全新的，
只是被前一次的上下文初始化了。

| Flag | 默认值 | 作用 |
|---|---|---|
| `<bundle>` | _(必填)_ | bundle 目录路径。 |
| `--mode brief\|standard\|full` | `standard` | 把多少历史装进 prompt。详见下表。 |
| `-o`, `--output`, `--out` | `<bundle>/seed-prompt.md` | prompt 输出位置。 |

**模式：**

| 模式 | 包含 | 28 轮会话的典型大小 |
|---|---|---|
| `brief` | 会话元数据 + 文件清单 + 最后 3 轮**原文**。 | ~50 KB |
| `standard` | 以上 + 每个 user prompt + 助手回复**压缩到 500 字**+ 工具调用摘要；最后一轮**原文**。 | ~70 KB |
| `full` | 全部**原文**，含思考块和工具结果。 | ~225 KB |

**示例：**

```bash
# 默认 —— 体积和保真度的平衡
claude-code-export seed ./exports/<session-id>

# 只要最尾巴几轮
claude-code-export seed ./exports/<session-id> --mode brief

# 一字不漏全装上
claude-code-export seed ./exports/<session-id> --mode full -o ./seed-full.md
```

---

## Bundle 结构

每次 `export` 都为每个会话产出一个独立目录：

```
exports/<session-id>/
├── manifest.json        # 版本 + 源平台 + cwd + auth 信息
├── transcript.jsonl     # 原始 JSONL，无损
├── session.html         # 渲染版阅读视图（Marked + highlight.js）
├── session.md           # GitHub 风格 Markdown
├── session.json         # 结构化逐块 dump（给 LLM 喂）
├── session.csv          # 扁平表格（给电子表格用）
├── README.md            # 自动生成的 bundle 摘要
├── CLAUDE.md            # 项目根的 CLAUDE.md（如果有）
├── assets/              # 助手 Write / Edit 过的文件
│   └── <相对原 cwd 的路径>
├── auth/                # 仅在用 --include-auth 时存在
│   └── credentials.json
└── seed-prompt.md       # 仅在跑过 `seed` 后存在
```

### manifest.json 字段说明

| 字段 | 类型 | 含义 |
|---|---|---|
| `bundle_version` | int | bundle 格式版本，当前为 `1`。 |
| `tool` | string | 恒为 `"claude-code-export"`。 |
| `tool_version` | string | 导出时的工具 semver。 |
| `exported_at` | ISO-8601 string | 导出时刻（UTC）。 |
| `source_platform` | string | `"darwin"` / `"win32"` / `"linux"`。 |
| `source_path_sep` | string | `"/"` 或 `"\\"`。`import` 用来翻译路径。 |
| `source_home` | string | 源机器的 `$HOME`（信息字段）。 |
| `source_cwd` | string | 会话所在的项目工作目录。 |
| `source_session_id` | string | Claude Code 会话 UUID。 |
| `source_project_dir_name` | string | 源端在 `~/.claude/projects/` 下的编码目录名。 |
| `source_git_branch` | string | 会话开始时的 git 分支（若有）。 |
| `source_cc_version` | string | 写 transcript 时的 Claude Code 版本。 |
| `auth` | object | `{"included": false}` 或 `--include-auth` 时的详细信息。 |

`import` 完全靠 `manifest.json` 驱动改写；缺 manifest 的旧 bundle 会被明确拒绝，
并指引如何重新导出。

---

## 常见工作流

### 1. 个人备份

```bash
# 周期性把所有会话 dump 到备份盘
claude-code-export export all --output /Volumes/Backup/cc-$(date +%F)
```

bundle 自包含、可直接在浏览器打开；`session.json` / `session.csv` 也很稳定，
可以 grep / diff / 喂给另一个 LLM。

### 2. 同账号迁机（同一个人，新设备）

```bash
# 旧机器
claude-code-export export latest --output ./bundle --include-auth

# 把 ./bundle 拷到新机器（任意方式，但**当成 SSH 私钥那样对待**——见 [安全](#安全)）

# 新机器
claude-code-export import ./bundle
claude --resume <session-id>
```

只要两边都用独立 Claude Code（不是被 Claude Desktop 托管），凭证文件就是平台无关的
纯 OAuth refresh token，可以跨机器复用。

### 3. 跨账号 / 跨机器续聊（seed 模式）

目的地是**另一个 Anthropic 账号**时，服务端状态没法迁移。用 seed 模式把上下文
内联进新对话即可：

```bash
# 源机器
claude-code-export export latest --output ./bundle
claude-code-export seed ./bundle/<session-id>
# → ./bundle/<session-id>/seed-prompt.md

# 拷 bundle 过去。在目标端：
# 1. 用新账号开一个全新的 Claude / Cowork 对话。
# 2. 把 seed-prompt.md 的内容粘进首条消息。
# 3. 等助手确认它吸收完上下文。
# 4. 想让新机器也有文件，可以同时跑 `import`。
```

### 4. 删掉 cwd 前先把项目历史归档

```bash
claude-code-export list --project ~/code/old-project
claude-code-export export all --project ~/code/old-project --output ./archive
# bundle 是独立的，现在可以安全删掉 ~/code/old-project。
```

---

## 跨平台说明

Claude Code 在所有 OS 上都用 `~/.claude/projects/`。工具会自动检测位置并处理
平台差异：

| 关注点 | 行为 |
|---|---|
| 路径分隔符 | `import` 按 `manifest.source_path_sep` 和目标平台改写 `/` ↔ `\`。 |
| 盘符 | 目标是 Windows 时 `--cwd` 必须含盘符（如 `D:\code\foo`）；macOS / Linux 拒绝盘符。 |
| 大小写敏感 | 源是 Windows 时前缀匹配走 `os.path.normcase`，源 `cwd` 的大小写漂移不影响匹配。 |
| 保留字符 | 目标是 Windows 且 `--cwd` 含 `<>:"|?*` 任一字符（盘符冒号除外）时拒绝。 |
| `LongPathsEnabled` | Windows 默认 260 字符路径上限；长 bundle 路径可能需要在注册表打开 `LongPathsEnabled`。 |
| 控制台编码 | windows 分支在启动时把 `sys.stdout` / `sys.stderr` 重配为 UTF-8，让 `cmd.exe` 下中文标题不乱码。 |

---

## 安全

`--include-auth` 会把你的 Anthropic 凭证打进 bundle。
**像对待 SSH 私钥一样对待这个 bundle。**

- 谁拿到 bundle，谁就能在你 rotate 之前以你的身份行事
  （另一台设备 `/login`、改密码、在账号页 revoke 设备等都算 rotate）。
- 工具会打印多行警告，并要求**交互式输入 `I UNDERSTAND`**，才会真的产出含
  auth 的 bundle。`--yes-i-know-this-is-risky` 只为 CI 跳过这个 prompt。
- 不带 `--include-auth` 的 bundle（默认）只含对话文本和工件，放未加密云盘
  也无所谓。
- bundle 里的 `auth/credentials.json` 在支持的文件系统上以 `0600` 权限写入。
- `import` 要覆盖现有 `~/.claude/.credentials.json` 必须显式 `--force`，
  避免误覆盖刚登的新账号。
- 如果源机器没有可移植的 credentials 文件（环境变量
  `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`，即 CC 被 Claude Desktop 托管），
  `--include-auth` 会立刻报错，提示你不带 auth 导出、目标机器自己登录即可。

---

## 故障排查

**`No Claude Code sessions found under ~/.claude/projects`**
要么 CLI 没装，要么还没从任何项目目录跑过 `claude`。先 `claude` 跑一次。

**`error: --include-auth requested but no portable credentials file was found`**
当前主机的 CC 被 Claude Desktop 托管（环境变量
`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`），凭证不可移植。去掉 `--include-auth`
重导，目标端正常登录即可。

**`error: cross-platform import (source=darwin, target=win32) requires --cwd`**
平台不同时路径约定不同。加上 `--cwd "<目标路径>"` 告诉工具新机器上的项目放哪。

**`error: --cwd '...' contains chars not allowed on Windows: '?'`**
源路径里有 Windows 保留字符，换一个目标路径。

**`error: <target>.jsonl already exists. Use --force to overwrite.`**
目标会话 UUID 已有 transcript。要么先删，要么明确加 `--force`。

**`Resume with: claude --resume <id>` 但 `claude --resume` 看不到**
检查 `--code-root`（默认 `~/.claude/projects`）和编码目录名是不是和 CLI 看的位置一致。
`import` 输出里有两个值，对照 `ls` 确认一遍。

**`seed-prompt.md` 太大粘不进一条消息**
用 `--mode brief` 出最小版，或者手动切分。模型支持的首条消息上下文一般远大于
standard 模式的体积。

---

## 分支

仓库刻意维护两条长期分支：

| 分支 | 面向 | 备注 |
|---|---|---|
| `macos`（默认） | macOS / Linux | 干净的跨平台基础。 |
| `windows` | Windows | 加上 UTF-8 stdout 重配 + 大小写不敏感路径比较；其它逻辑相同。 |

两条分支始终版本对齐。装哪条按目标平台决定；macOS 分支在 Linux 上也能用。
Windows 分支在 macOS 上也能跑，但没必要。

---

## License

MIT。详见 [LICENSE](LICENSE)。
