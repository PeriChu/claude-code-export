# claude-code-export (Windows branch)

Export **Claude Code CLI** sessions — the JSONL transcripts under
`~/.claude/projects/...` — to clean HTML / Markdown / JSON / CSV bundles,
with files the assistant `Write`d / `Edit`ed and the project's `CLAUDE.md`
captured alongside.

Zero dependencies (pure Python stdlib, 3.9+). One file, one command.

> This is the **Windows branch**. It reconfigures stdout/stderr to UTF-8
> on Windows so Chinese / emoji titles print correctly, and uses
> case-insensitive path comparison where needed. The macOS / Linux build
> lives on the [`macos`](../../tree/macos) branch.

> Sibling project of [claude-cowork-export](https://github.com/PeriChu/claude-cowork-export),
> which targets Claude Desktop's Cowork chats. This one is for the CLI tool.

## Why

Claude Code drops a full JSONL transcript of every session into
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. The encoding mangles
the path so the directory name is hard to read, the JSONL is dense, and
copy-pasting code blocks out of a terminal is fiddly. This tool flattens each
session into:

- **`session.html`** — formatted reading view with light/dark mode, syntax
  highlighting, a TOC of user prompts, and per-turn collapsing: each user
  prompt + assistant final reply is visible, the thinking + tool calls in
  between are wrapped in one collapsed `<details>` block.
- **`session.md`** — GitHub-flavoured Markdown for archiving / sharing.
- **`session.json`** — structured per-block dump for feeding to another LLM.
- **`session.csv`** — flat per-block table for spreadsheets or batch ingestion.
- **`assets/`** — snapshot of files the assistant `Write`'d / `Edit`'d.
- **`CLAUDE.md`** — the project's `CLAUDE.md` if one was present at the cwd
  (or `<cwd>/.claude/CLAUDE.md`), so the export carries the context the
  assistant was operating under.
- **`transcript.jsonl`** — the lossless source.

## Install (Windows)

Requires Python 3.9+. Get it from the Microsoft Store, [python.org](https://python.org),
or `winget install Python.Python.3.12`.

Recommended (isolated, gives you a `claude-code-export` command on PATH):

```powershell
# install pipx if you don't have it
python -m pip install --user pipx
python -m pipx ensurepath
# (open a new shell so PATH picks up)

# install the tool from the windows branch
pipx install "git+https://github.com/PeriChu/claude-code-export.git@windows"
```

Or just clone and run the script directly — it has no third-party deps:

```powershell
git clone -b windows https://github.com/PeriChu/claude-code-export.git
cd claude-code-export
python claude_code_export.py --help
```

### Install (macOS / Linux)

The same script works there too — see the [`macos`](../../tree/macos) branch
for the canonical version, or install from this branch:

```bash
pipx install "git+https://github.com/PeriChu/claude-code-export.git@windows"
```

## Usage

```bash
# list all your Claude Code sessions, grouped by project, newest first
claude-code-export list

# show only sessions whose cwd lives under a specific project
claude-code-export list --project ~/code/foo

# export the most recent session (default formats: html, md, json, csv)
claude-code-export export latest

# export by session-id prefix
claude-code-export export <session-id> --output ./exports

# export every session
claude-code-export export all --output ./exports

# pick a subset of formats
claude-code-export export latest --formats html,json

# skip touched-file snapshots and CLAUDE.md (transcript-only bundle)
claude-code-export export latest --no-files

# point at a different ~/.claude/projects directory (e.g. a backup)
claude-code-export --code-root /Volumes/backup/.claude/projects list
```

## Output bundle layout

Each exported session gets its own folder under `--output`:

```
exports/<session-id>/
├── README.md            # bundle summary
├── session.html         # rendered reading view
├── session.md           # markdown export
├── session.json         # structured per-block dump
├── session.csv          # flat tabular dump
├── transcript.jsonl     # raw source (lossless)
├── CLAUDE.md            # project's CLAUDE.md (only if one existed)
└── assets/              # files the assistant Wrote/Edited
```

## Where Claude Code stores sessions

- All platforms: `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`
  - macOS / Linux: `/Users/<you>/.claude/projects/...` or `/home/<you>/.claude/projects/...`
  - Windows: `C:\Users\<you>\.claude\projects\...`
- The encoded folder name is `<cwd>` with `/` (or `\`) replaced by `-`. We
  decode it as a fallback for display when the transcript records lack a
  `cwd` field; otherwise the transcript's own `cwd` wins.

## Notes & limitations

- **Recorded-content fallback.** When a `Write`-d file isn't readable from
  disk anymore (deleted, moved, or — on macOS — blocked by TCC for a
  protected folder), the tool recovers it from the `Write` tool call's
  `input.content` field captured in the transcript itself. This is the
  version the assistant originally produced, which is often more faithful
  than the live filesystem.
- **`Edit`/`MultiEdit` only.** If a file was only ever modified via diffs
  and isn't readable from disk, we can't reconstruct it. The bundle's
  README notes which paths fell through.
- **Windows path quirks.** Windows is case-insensitive but Python's
  `Path.relative_to` isn't, so the Windows branch normalises with
  `os.path.normcase` when matching touched-file paths to the session cwd.
  Long paths (> 260 chars) may fail unless you've enabled
  `LongPathsEnabled` in the registry.
- **Console encoding (Windows).** The script reconfigures stdout/stderr to
  UTF-8 at startup. If you still see mojibake under legacy `cmd.exe`, run
  `chcp 65001` first, or just use Windows Terminal.
- **Resumed sessions across cwds.** If a session was started in repo A,
  then resumed via `claude --continue` in repo B, the transcript carries
  both cwds. We pick the first non-empty cwd — i.e. the *original* project
  — so the bundled `CLAUDE.md` and the `assets/` paths line up with where
  the work actually happened.
- **Tool-result truncation.** Tool outputs over 8000 chars are truncated in
  HTML / MD for readability. The full text is always preserved in
  `session.json` and `transcript.jsonl`.
- **No external dependencies.** The HTML uses `marked.js` and
  `highlight.js` from a CDN at runtime, so the output renders nicely
  offline once cached but needs network access on first open.

## License

MIT. See [LICENSE](LICENSE).
