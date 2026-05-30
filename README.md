# claude-code-export

> Export, archive, migrate, and resume **Claude Code CLI** sessions.

[English](README.md) · [中文](README.zh-CN.md)

A single-file Python tool that takes the JSONL transcripts Claude Code writes
under `~/.claude/projects/...` and turns them into:

- a **readable bundle** — HTML, Markdown, JSON, CSV, plus the project's
  `CLAUDE.md` and every file the assistant `Write`d or `Edit`ed,
- a **portable archive** — drop the bundle on a different machine and have
  Claude Code pick the session right back up,
- a **continuation seed** — a single Markdown prompt you can paste into a
  brand-new chat (even under a different account) to keep working from
  where you left off.

Zero third-party dependencies (pure Python 3.9+ stdlib). One file. One CLI.

Sibling project: [claude-cowork-export](https://github.com/PeriChu/claude-cowork-export)
targets Claude Desktop's Cowork chats. This one targets the CLI.

---

## Table of contents

- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
  - [`list`](#list)
  - [`export`](#export)
  - [`import`](#import)
  - [`seed`](#seed)
- [Bundle layout](#bundle-layout)
- [Workflows](#workflows)
- [Cross-platform notes](#cross-platform-notes)
- [Security: `--include-auth` caveats](#security)
- [Troubleshooting](#troubleshooting)
- [Branches](#branches)
- [License](#license)

---

## Install

Requires Python 3.9 or newer. Recommended via [pipx](https://pipx.pypa.io/):

```bash
# macOS / Linux
pipx install "git+https://github.com/PeriChu/claude-code-export.git"

# Windows (PowerShell) — same command works, but install from the Windows branch
pipx install "git+https://github.com/PeriChu/claude-code-export.git@windows"
```

The tool registers two equivalent entry points:

```bash
claude-code-export ...   # primary name
code-export ...          # shorter alias
```

Or run it as a single script with no install:

```bash
git clone https://github.com/PeriChu/claude-code-export.git
cd claude-code-export
python3 claude_code_export.py --help
```

---

## Quickstart

```bash
# List every session on disk, grouped by project
claude-code-export list

# Export the most recent session into ./exports/
claude-code-export export latest --output ./exports

# Export every session
claude-code-export export all --output ./exports

# Move a bundle to another machine, then restore it
claude-code-export import ./exports/<session-id>

# Or, on a brand-new account: render a paste-able continuation prompt
claude-code-export seed ./exports/<session-id>
# → writes ./exports/<session-id>/seed-prompt.md
```

---

## Commands

The CLI has four subcommands. Run `claude-code-export <cmd> --help` for the
exact flag list at any time.

### `list`

```
claude-code-export list [--code-root PATH] [--project PATH]
```

Lists every Claude Code session under `~/.claude/projects/`, grouped by the
project's working directory (decoded from the encoded folder name, with the
transcript's own `cwd` record winning when present).

| Flag | Default | What it does |
|---|---|---|
| `--code-root PATH` | `~/.claude/projects` | Read from an alternate root — archived backups, mounted volumes, etc. |
| `--project PATH` | _(none)_ | Filter to sessions whose `cwd` starts with `PATH` (prefix match). |

**Examples:**

```bash
# Everything
claude-code-export list

# Only sessions for one project
claude-code-export list --project ~/code/foo

# A backup tree mounted somewhere unusual
claude-code-export list --code-root /Volumes/Backup/.claude/projects
```

### `export`

```
claude-code-export export <selector> [-o DIR] [--formats LIST]
                                     [--no-files]
                                     [--include-auth] [--yes-i-know-this-is-risky]
                                     [--code-root PATH] [--project PATH]
```

Exports one or more sessions into a self-contained bundle directory per
session. The selector picks which sessions:

| Selector | Picks |
|---|---|
| `latest` | The most recently active session. |
| `all` | Every session. |
| `<prefix>` | The unique session whose UUID starts with `<prefix>`. Errors if ambiguous. |

| Flag | Default | What it does |
|---|---|---|
| `-o`, `--output`, `--out` | `./exports` | Where to write the bundles. One subdir per session. |
| `--formats` | `html,md,json,csv` | Comma-separated subset of `html,md,json,csv`. |
| `--no-files` | _(off)_ | Skip copying touched files and `CLAUDE.md`. Faster, smaller bundle. |
| `--include-auth` | _(off)_ | **HIGH RISK.** Also copy `~/.claude/.credentials.json` into the bundle so `import` on a fresh machine can resume without re-logging in. See [Security](#security). |
| `--purge-source` | _(off)_ | **DESTRUCTIVE.** After each session's bundle is written and verified, delete that session's local copy from `~/.claude`. Your project/workspace files are never touched. Incompatible with `--no-files`. See [Deleting the local copy](#deleting-the-local-copy-after-export). |
| `--yes-i-know-this-is-risky` | _(off)_ | Skip the interactive confirmation prompts for `--include-auth` and `--purge-source`. CI only. |

**Examples:**

```bash
# Everything to ./exports/
claude-code-export export all --output ./exports

# Only one session, HTML + JSON only
claude-code-export export <session-id> --output ./exports --formats html,json

# Smallest possible bundle — transcript only, no touched files
claude-code-export export latest --no-files

# Personal backup with credentials included (read the warning carefully)
claude-code-export export latest --output ./private-backup --include-auth
```

### `import`

```
claude-code-export import <bundle> [--cwd PATH] [--code-root PATH]
                                   [--skip-auth] [--dry-run] [--force]
```

Restores a bundle into the local Claude Code store so that
`claude --resume <session-id>` picks it up. Handles path rewriting and
cross-platform separator translation; refuses risky overwrites by default.

| Flag | Default | What it does |
|---|---|---|
| `<bundle>` | _(required)_ | Path to a bundle directory produced by `export`. |
| `--cwd PATH` | _(manifest value)_ | Target working directory on this machine. **Required** when source and target platforms differ. |
| `--code-root PATH` | `~/.claude/projects` | Override the target Claude Code projects root. |
| `--skip-auth` | _(off)_ | Ignore `bundle/auth/` even if present. Do not touch local credentials. |
| `--dry-run` | _(off)_ | Print the rewrite plan and exit without writing anything. |
| `--force` | _(off)_ | Overwrite an existing transcript, restored files, or local credentials. |

**Examples:**

```bash
# Same machine, original cwd — just rebuild from a bundle
claude-code-export import ./exports/<session-id>

# Different cwd on the same machine
claude-code-export import ./exports/<session-id> --cwd ~/code/foo-renamed

# Cross-platform (macOS bundle → Windows machine): --cwd is required
claude-code-export import .\exports\<session-id> --cwd "D:\code\foo"

# See exactly what would happen, then bail
claude-code-export import ./exports/<session-id> --dry-run

# Resume after the import
claude --resume <session-id>
```

### `seed`

```
claude-code-export seed <bundle> [--mode brief|standard|full] [-o PATH]
```

Renders a self-contained Markdown prompt from a bundle. Paste it as the first
message of a fresh Claude / Cowork chat — under any account, on any machine —
to bootstrap the new conversation with the prior session's context. This does
not touch any auth and does not rely on any server-side state: the new chat
is genuinely new, just primed.

| Flag | Default | What it does |
|---|---|---|
| `<bundle>` | _(required)_ | Path to a bundle. |
| `--mode brief\|standard\|full` | `standard` | How much of the prior conversation to include. See modes table below. |
| `-o`, `--output`, `--out` | `<bundle>/seed-prompt.md` | Where to write the prompt. |

**Modes:**

| Mode | Includes | Typical size for a 28-turn session |
|---|---|---|
| `brief` | Session metadata + file inventory + last 3 turns verbatim. | ~50 KB |
| `standard` | All of the above + every user prompt + abridged assistant text (500-char truncation) + tool-call summaries; final turn verbatim. | ~70 KB |
| `full` | Everything verbatim, including reasoning blocks and tool outputs. | ~225 KB |

**Examples:**

```bash
# Default — best balance of size vs. fidelity
claude-code-export seed ./exports/<session-id>

# Only the tail of the conversation
claude-code-export seed ./exports/<session-id> --mode brief

# Reproduce every byte
claude-code-export seed ./exports/<session-id> --mode full -o ./seed-full.md
```

---

## Bundle layout

Every `export` produces one directory per session:

```
exports/<session-id>/
├── manifest.json        # version + source platform + cwd + auth info
├── transcript.jsonl     # raw source, lossless
├── session.html         # rendered reading view (Marked + highlight.js)
├── session.md           # GitHub-flavoured Markdown
├── session.json         # structured per-block dump (for LLMs)
├── session.csv          # flat per-block table (for spreadsheets)
├── README.md            # auto-generated bundle summary
├── CLAUDE.md            # project's CLAUDE.md, if one exists at the cwd
├── assets/              # files the assistant Wrote / Edited
│   └── <relative paths to original cwd>
├── auth/                # only when --include-auth was used
│   └── credentials.json
└── seed-prompt.md       # only after a `seed` invocation
```

### manifest.json schema

| Field | Type | Meaning |
|---|---|---|
| `bundle_version` | int | Bundle format version. Currently `1`. |
| `tool` | string | Always `"claude-code-export"`. |
| `tool_version` | string | Tool semver at export time. |
| `exported_at` | ISO-8601 string | UTC timestamp. |
| `source_platform` | string | `"darwin"` / `"win32"` / `"linux"`. |
| `source_path_sep` | string | `"/"` or `"\\"`. Used by `import` to translate paths. |
| `source_home` | string | Source machine's `$HOME` (informational). |
| `source_cwd` | string | The project working directory the session lived in. |
| `source_session_id` | string | The Claude Code session UUID. |
| `source_project_dir_name` | string | The encoded folder name under `~/.claude/projects/` on the source. |
| `source_git_branch` | string | Git branch at session start, if any. |
| `source_cc_version` | string | Claude Code version that wrote the transcript. |
| `auth` | object | `{"included": false}` or full details when `--include-auth`. |

`import` reads `manifest.json` to drive its rewrites; older bundles without a
manifest are rejected with a clear error pointing at how to re-export.

---

## Workflows

### 1. Personal backup

```bash
# Periodically dump every session to your backup drive
claude-code-export export all --output /Volumes/Backup/cc-$(date +%F)
```

The bundle is self-contained, fully readable in a browser, and the
`session.json` / `session.csv` are stable enough to grep, diff, and feed
back to another LLM.

### 2. Same-account migration (new device, same person)

```bash
# Old machine
claude-code-export export latest --output ./bundle --include-auth

# Transfer ./bundle to the new machine (any channel, but treat it like an SSH
# private key — see Security below)

# New machine
claude-code-export import ./bundle
claude --resume <session-id>
```

Auth is portable across machines when both run standalone Claude Code; the
credentials file is a plain OAuth refresh token. (See
[Security](#security) for what to be careful about.)

### 3. Cross-account / cross-machine continuation (seed mode)

When the destination is a different Anthropic account, server-side state
cannot be transferred. Use seed mode to inline the prior context into a
fresh conversation:

```bash
# Source machine
claude-code-export export latest --output ./bundle
claude-code-export seed ./bundle/<session-id>
# → ./bundle/<session-id>/seed-prompt.md

# Transfer the bundle. On the destination:
# 1. Open a brand-new Claude / Cowork chat under the new account.
# 2. Paste the contents of seed-prompt.md as your first message.
# 3. Wait for the assistant to confirm context absorption.
# 4. Optionally also run `import` so the new machine has the files restored.
```

### 4. Archive a project's history before deleting the cwd

```bash
claude-code-export list --project ~/code/old-project
claude-code-export export all --project ~/code/old-project --output ./archive
# Now safe to delete ~/code/old-project — the bundles are independent.
```

### 5. Export and free up the local store (`--purge-source`)

```bash
# Archive a session AND remove its local copy from ~/.claude in one step
claude-code-export export <session-id> --output ./archive --purge-source
```

See [Deleting the local copy](#deleting-the-local-copy-after-export) for the
exact safety behaviour.

---

## Deleting the local copy after export

`--purge-source` lets you offload a session: it is exported to a bundle and
then removed from `~/.claude`, in a single command.

- **What is deleted**: the session transcript
  (`~/.claude/projects/<encoded>/<session-id>.jsonl`) and the per-session
  scratch dir (`~/.claude/session-env/<session-id>/`), plus the encoded
  project folder if it becomes empty.
- **What is never touched**: your project / workspace files. The code the
  assistant wrote or edited in the cwd stays exactly where it is — purge
  only ever deletes under `~/.claude`.
- **Verified-before-delete**: a session is purged only after its bundle is
  written and passes a completeness check (`transcript.jsonl` +
  `manifest.json` present and non-empty). If export or verification fails,
  the source is left intact.
- **Forced confirmation**: the tool prints exactly which paths will be
  removed and waits for you to type `DELETE`. `--yes-i-know-this-is-risky`
  skips the prompt for CI only.
- **Incompatible with `--no-files`**: purging while the bundle omits touched
  files would make them unrecoverable, so the combination is rejected.
- **Reversible via import**: `claude-code-export import <bundle>` restores a
  purged session (see [`import`](#import)).

---

## Cross-platform notes

Claude Code uses `~/.claude/projects/` on every supported OS. The tool
auto-detects the right location and handles platform quirks:

| Concern | What happens |
|---|---|
| Path separators | `import` rewrites `/` ↔ `\` based on `manifest.source_path_sep` and the target platform. |
| Drive letters | Windows targets must have a drive in `--cwd` (e.g. `D:\code\foo`). macOS / Linux targets reject drive letters in `--cwd`. |
| Case sensitivity | When the source platform is Windows, prefix matches use `os.path.normcase` so an uppercase / lowercase drift in the source `cwd` still matches. |
| Reserved characters | `import --cwd` is rejected if the target is Windows and the path contains any of `<>:"|?*` (other than the drive-letter `:`). |
| `LongPathsEnabled` | Windows has a 260-char path limit by default. Long bundle paths may need the `LongPathsEnabled` registry flag. |
| Console encoding | The Windows branch reconfigures `sys.stdout` / `sys.stderr` to UTF-8 so non-ASCII session titles print correctly under `cmd.exe`. |

---

## Security

`--include-auth` makes the bundle carry your Anthropic credentials.
**Treat the resulting bundle exactly like you'd treat an SSH private key.**

- Anyone who obtains the bundle can act as your account until you
  rotate (`/login` on another device with the same credential set,
  password change, or device revoke from your account page).
- The tool prints a multi-line warning and requires you to type
  `I UNDERSTAND` interactively before producing such a bundle.
  `--yes-i-know-this-is-risky` skips the prompt for CI only.
- Bundles without `--include-auth` (the default) are safe to keep in
  unencrypted cloud storage — they contain conversation text and
  artefacts only.
- The bundle's `auth/credentials.json` is written with `0600` perms when
  the host filesystem supports it.
- `import` requires `--force` to overwrite an existing
  `~/.claude/.credentials.json` so you don't accidentally clobber a
  fresh login.
- If the source machine has no portable credentials file
  (`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`, i.e. Claude Code is running
  as a subprocess of Claude Desktop), `--include-auth` fails fast with a
  message telling you to skip it and sign in on the destination.

---

## Troubleshooting

**`No Claude Code sessions found under ~/.claude/projects`**
Either the CLI isn't installed, or you haven't run it yet from any
project directory. Run `claude` once, then re-check.

**`error: --include-auth requested but no portable credentials file was found`**
You're on a host where Claude Desktop manages credentials for the CLI
(see the env var `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`). Auth is
non-portable in that mode. Use the bundle without `--include-auth` and
sign in on the destination.

**`error: cross-platform import (source=darwin, target=win32) requires --cwd`**
Path conventions differ. Pass `--cwd "<destination-path>"` so the tool
knows where on the new machine the project should live.

**`error: --cwd '...' contains chars not allowed on Windows: '?'`**
Some macOS / Linux paths contain characters Windows reserves. Pick a
different target path.

**`error: <target>.jsonl already exists. Use --force to overwrite.`**
The destination session UUID already has a transcript on disk. Either
remove it, or pass `--force` if overwriting is intentional.

**`Resume with: claude --resume <id>` but `claude --resume` doesn't show it**
Check that `--code-root` (default `~/.claude/projects`) and the
encoded directory name actually match what the CLI looks at. The `import`
plan output shows both; verify with `ls`.

**`seed-prompt.md` is too big to paste in a single message**
Use `--mode brief` for the smallest version, or split manually.
Models accept very large first messages; the practical limit is
context-window-dependent and usually well above the standard-mode size.

---

## Branches

This repo intentionally maintains two long-lived branches:

| Branch | For | Notes |
|---|---|---|
| `macos` (default) | macOS / Linux | Clean cross-platform base. |
| `windows` | Windows | Adds UTF-8 stdout reconfiguration and case-insensitive path comparison. Functionally identical otherwise. |

Both stay version-synced. Install the branch matching your target
platform; the macOS branch also works on Linux. The Windows branch will
also work on macOS but isn't necessary there.

---

## License

MIT. See [LICENSE](LICENSE).
