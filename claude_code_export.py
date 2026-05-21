#!/usr/bin/env python3
"""claude_code_export — export Claude Code CLI sessions to HTML / Markdown / JSON / CSV.

Claude Code stores each session as a JSONL transcript under
``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`` on every platform:

    macOS / Linux:  /Users/<you>/.claude/projects/...   (~/.claude/projects)
    Windows:        C:\\Users\\<you>\\.claude\\projects\\...

This is the Windows branch: it reconfigures the console to UTF-8 so Chinese
and emoji titles print correctly, and falls back to ``os.path.normcase`` for
the case-insensitive filesystem when matching touched-file paths against the
session cwd. The macOS branch lives at ../macos. This tool:

  * discovers every transcript on disk
  * groups them by the actual working directory (decoded from the encoded
    project folder name, with the transcript's own ``cwd`` record winning
    when present)
  * flattens the JSONL into HTML / Markdown / JSON / CSV bundles with the
    conversation grouped into per-turn collapsible blocks
  * snapshots files the assistant Wrote / Edited (falling back to the
    recorded content from the tool call when the live file is missing or
    unreadable)
  * bundles the project's ``CLAUDE.md`` (or ``.claude/CLAUDE.md``) when one
    exists, so the export carries the context the assistant was operating
    under

Usage:
    python claude_code_export.py list
    python claude_code_export.py list --project ~/code/foo
    python claude_code_export.py export latest
    python claude_code_export.py export <session-id-prefix>
    python claude_code_export.py export all --output ./exports
    python claude_code_export.py export latest --formats html,md
"""
from __future__ import annotations

import argparse
import csv
import html as html_mod
import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


HOME = Path.home()
CODE_ROOT = HOME / ".claude" / "projects"
DEFAULT_OUTPUT = Path.cwd() / "exports"
SUPPORTED_FORMATS = ("html", "md", "json", "csv")
TOOL_RESULT_TRUNCATE = 8000
BUNDLE_VERSION = 1
TOOL_VERSION = "0.3.0"
CREDENTIALS_PATH = HOME / ".claude" / ".credentials.json"
SEED_TEXT_TRUNCATE = 500
SEED_TOOL_INPUT_TRUNCATE = 200
SEED_TOOL_RESULT_TRUNCATE = 400


def _rel_to(abs_p: Path, base: Path) -> str | None:
    """Return abs_p relative to base (forward slashes), or None if not under base.

    On Windows we fall back to a case-insensitive comparison via
    ``os.path.normcase`` because the filesystem is case-insensitive but
    pathlib's ``relative_to`` is strict.
    """
    try:
        return str(abs_p.relative_to(base)).replace("\\", "/")
    except ValueError:
        if sys.platform == "win32":
            try:
                a_str = os.path.abspath(str(abs_p))
                b_str = os.path.abspath(str(base))
                a_norm = os.path.normcase(a_str)
                b_norm = os.path.normcase(b_str)
                if a_norm == b_norm:
                    return ""
                if a_norm.startswith(b_norm + os.sep):
                    return a_str[len(b_str) + 1:].replace("\\", "/")
            except OSError:
                pass
        return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _encode_cwd_dir(cwd: str) -> str:
    """Inverse of :func:`_decode_cwd_dir`. Encode a cwd into the directory
    name Claude Code uses under ``~/.claude/projects/``.

      ``/home/me/code/my-project``  →  ``-home-me-code-my-project``
      ``C:\\code\\my-project``                →  ``C--code-my-project``

    The transformation is host-platform-independent (host ``os.sep`` is not
    consulted) so we can encode a cross-platform target on import.
    """
    if not cwd:
        return ""
    return cwd.replace("\\", "-").replace("/", "-").replace(":", "-").replace("_", "-")


def _decode_cwd_dir(name: str) -> str:
    """Best-effort decode of Claude Code's encoded project directory name.

    Claude Code encodes the cwd by replacing the path separator with ``-`` and
    prepending one. Concrete examples:

      ``/home/me/code/my-project``  →  ``-home-me-code-my-project``
      ``C:\\code\\my-project``                →  ``C--code-my-project``

    Underscores in the original path are also replaced with ``-``, so the
    encoding is not perfectly reversible. We return a best-effort decode —
    the transcript's own ``cwd`` field always wins when present.
    """
    if not name:
        return ""
    if name.startswith("-"):
        # POSIX-style absolute path encoding: leading "-" stands for "/"
        return "/" + name[1:].replace("-", "/")
    # Windows-style encoding: first "-" after the drive letter stands for ":\\"
    if len(name) >= 2 and name[0].isalpha() and name[1:3] == "--":
        return f"{name[0]}:\\" + name[3:].replace("-", "\\")
    return name.replace("-", "/")


def _find_project_claude_md(cwd: str) -> Path | None:
    """Look for the project's CLAUDE.md (or .claude/CLAUDE.md). Returns the
    first existing path, or None."""
    if not cwd:
        return None
    cwd_p = Path(cwd).expanduser()
    for candidate in (cwd_p / "CLAUDE.md", cwd_p / ".claude" / "CLAUDE.md"):
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str                              # = session UUID (jsonl stem)
    title: str = ""                           # from ai-title record
    cwd: str = ""                             # authoritative cwd from transcript
    decoded_cwd: str = ""                     # fallback decoded from dir name
    git_branch: str = ""
    version: str = ""
    entrypoint: str = ""
    permission_mode: str = ""
    project_dir: Path | None = None           # ~/.claude/projects/<encoded>/
    transcript_path: Path | None = None
    created_at_ms: int = 0
    last_activity_ms: int = 0

    @property
    def display_when(self) -> str:
        ts = self.last_activity_ms or self.created_at_ms
        if not ts:
            return ""
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")

    @property
    def display_title(self) -> str:
        return self.title or f"(untitled session {self.task_id[:8]})"

    @property
    def effective_cwd(self) -> str:
        return self.cwd or self.decoded_cwd


def _peek_session_meta(jsonl_path: Path, scan_limit: int = 120) -> dict[str, Any]:
    """One-pass scan of a transcript's first N records to pick up display
    metadata (title, cwd, git_branch, version, entrypoint, permission_mode,
    first/last timestamps). Skipping `scan_limit` keeps `list` fast on very
    long transcripts."""
    out: dict[str, Any] = {
        "title": "", "cwd": "", "git_branch": "", "version": "",
        "entrypoint": "", "permission_mode": "",
        "started_at_ms": 0, "ended_at_ms": 0,
    }
    if not jsonl_path.exists():
        return out
    scanned = 0
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                if scanned >= scan_limit and all(
                    out[k] for k in ("title", "cwd", "git_branch", "version")
                ):
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                scanned += 1
                if not out["cwd"] and obj.get("cwd"):
                    out["cwd"] = obj["cwd"]
                if not out["git_branch"] and obj.get("gitBranch"):
                    out["git_branch"] = obj["gitBranch"]
                if not out["version"] and obj.get("version"):
                    out["version"] = obj["version"]
                if not out["entrypoint"] and obj.get("entrypoint"):
                    out["entrypoint"] = obj["entrypoint"]
                if not out["permission_mode"] and obj.get("permissionMode"):
                    out["permission_mode"] = obj["permissionMode"]
                if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                    out["title"] = obj["aiTitle"]
                ts = obj.get("timestamp")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ms = int(dt.timestamp() * 1000)
                        if not out["started_at_ms"]:
                            out["started_at_ms"] = ms
                        out["ended_at_ms"] = ms
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def discover_sessions(roots: list[Path] | None = None) -> list[Task]:
    """Walk ~/.claude/projects/<encoded>/*.jsonl and return Task records,
    newest first by last_activity_ms (which is the file mtime fallback
    when the transcript has no timestamps yet)."""
    if roots is None:
        roots = [CODE_ROOT] if CODE_ROOT.exists() else []
    out: list[Task] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for proj in sorted(root.iterdir()):
            if not proj.is_dir():
                continue
            decoded = _decode_cwd_dir(proj.name)
            for jf in proj.glob("*.jsonl"):
                if jf.stem in seen:
                    continue
                seen.add(jf.stem)
                meta = _peek_session_meta(jf)
                try:
                    mtime_ms = int(jf.stat().st_mtime * 1000)
                except OSError:
                    mtime_ms = 0
                out.append(Task(
                    task_id=jf.stem,
                    title=meta["title"],
                    cwd=meta["cwd"],
                    decoded_cwd=decoded,
                    git_branch=meta["git_branch"],
                    version=meta["version"],
                    entrypoint=meta["entrypoint"],
                    permission_mode=meta["permission_mode"],
                    project_dir=proj,
                    transcript_path=jf,
                    created_at_ms=meta["started_at_ms"] or mtime_ms,
                    last_activity_ms=meta["ended_at_ms"] or mtime_ms,
                ))
    out.sort(key=lambda t: t.last_activity_ms or t.created_at_ms, reverse=True)
    return out


def resolve_tasks(selector: str, tasks: list[Task]) -> list[Task]:
    if not tasks:
        return []
    if selector == "all":
        return tasks
    if selector == "latest":
        return [tasks[0]]
    matches = [t for t in tasks if t.task_id.startswith(selector)]
    if matches:
        return matches
    matches = [t for t in tasks if selector in t.task_id]
    return matches


def filter_by_project(tasks: list[Task], project_filter: str) -> list[Task]:
    """Keep only sessions whose effective_cwd matches the given project path
    (prefix match, case-insensitive on Windows)."""
    if not project_filter:
        return tasks
    target = str(Path(project_filter).expanduser().resolve())
    if sys.platform == "win32":
        norm = os.path.normcase(target)
        return [
            t for t in tasks
            if t.effective_cwd
            and os.path.normcase(str(Path(t.effective_cwd))).startswith(norm)
        ]
    return [
        t for t in tasks
        if t.effective_cwd and str(Path(t.effective_cwd)).startswith(target)
    ]


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

@dataclass
class SessionMeta:
    task_id: str = ""
    title: str = ""
    cwd: str = ""
    decoded_cwd: str = ""
    git_branch: str = ""
    version: str = ""
    entrypoint: str = ""
    permission_mode: str = ""
    started_at: str = ""
    ended_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


@dataclass
class FlatMessage:
    index: int
    kind: str
    role: str
    timestamp: str
    uuid: str = ""
    parent_uuid: str = ""
    text: str = ""
    tool_name: str = ""
    tool_id: str = ""
    tool_input: Any = None
    is_error: bool = False
    attachment_type: str = ""
    attachment_payload: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


def load_transcript(jsonl_path: Path) -> tuple[SessionMeta, list[dict[str, Any]]]:
    """Read a Claude Code transcript jsonl. The schema is uniform across
    platforms: each line is a JSON object with one of the types we handle
    in flatten()."""
    meta = SessionMeta()
    raw: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw.append(obj)
            if not meta.task_id and obj.get("sessionId"):
                meta.task_id = obj["sessionId"]
            # First non-empty cwd wins. A resumed session can appear with
            # multiple cwds in the same transcript (started in repo A, later
            # continued from repo B). We want the original project so the
            # CLAUDE.md / asset paths line up with where the work actually
            # happened.
            if obj.get("cwd") and not meta.cwd:
                meta.cwd = obj["cwd"]
            if obj.get("gitBranch") and not meta.git_branch:
                meta.git_branch = obj["gitBranch"]
            if obj.get("version") and not meta.version:
                meta.version = obj["version"]
            if obj.get("entrypoint") and not meta.entrypoint:
                meta.entrypoint = obj["entrypoint"]
            if obj.get("permissionMode") and not meta.permission_mode:
                meta.permission_mode = obj["permissionMode"]
            ts = obj.get("timestamp")
            if ts:
                if not meta.started_at:
                    meta.started_at = ts
                meta.ended_at = ts
            if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                meta.title = obj["aiTitle"]
    return meta, raw


def merge_task_meta(meta: SessionMeta, task: Task) -> None:
    if not meta.task_id:
        meta.task_id = task.task_id
    if task.title and not meta.title:
        meta.title = task.title
    if task.cwd and not meta.cwd:
        meta.cwd = task.cwd
    if task.decoded_cwd and not meta.decoded_cwd:
        meta.decoded_cwd = task.decoded_cwd
    if not meta.started_at and task.created_at_ms:
        meta.started_at = datetime.fromtimestamp(
            task.created_at_ms / 1000, tz=timezone.utc
        ).isoformat()
    if task.last_activity_ms:
        meta.ended_at = datetime.fromtimestamp(
            task.last_activity_ms / 1000, tz=timezone.utc
        ).isoformat()


def flatten(raw: list[dict[str, Any]]) -> list[FlatMessage]:
    flat: list[FlatMessage] = []
    seen_uuid_kind: set[tuple[str, str]] = set()
    last_text_signature: tuple[str, str, str] | None = None

    def push(**kwargs):
        u = kwargs.get("uuid") or ""
        k = kwargs.get("kind") or ""
        if u and (u, k) in seen_uuid_kind:
            return
        nonlocal last_text_signature
        if k == "text":
            sig = (kwargs.get("role", ""), k, kwargs.get("text", "") or "")
            if sig[2] and sig == last_text_signature:
                return
            last_text_signature = sig
        else:
            last_text_signature = None
        if u:
            seen_uuid_kind.add((u, k))
        flat.append(FlatMessage(index=len(flat), **kwargs))

    for obj in raw:
        t = obj.get("type")
        if t in ("queue-operation", "ai-title", "last-prompt"):
            continue
        ts = obj.get("timestamp", "") or ""
        uuid = obj.get("uuid", "") or ""
        parent = obj.get("parentUuid", "") or ""
        if t == "attachment":
            att = obj.get("attachment", {}) or {}
            push(kind="attachment", role="system", timestamp=ts, uuid=uuid, parent_uuid=parent,
                 attachment_type=att.get("type", ""), attachment_payload=att)
            continue
        msg = obj.get("message") or {}
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            push(kind="text", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent, text=content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                push(kind="text", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent, text=block.get("text", ""))
            elif bt == "thinking":
                push(kind="thinking", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent, text=block.get("thinking", ""))
            elif bt == "tool_use":
                push(kind="tool_use", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent,
                     tool_name=block.get("name", ""), tool_id=block.get("id", ""),
                     tool_input=block.get("input"))
            elif bt == "tool_result":
                inner = block.get("content")
                text = ""
                if isinstance(inner, str):
                    text = inner
                elif isinstance(inner, list):
                    parts: list[str] = []
                    for x in inner:
                        if isinstance(x, dict):
                            if x.get("type") == "text":
                                parts.append(x.get("text", ""))
                            elif x.get("type") == "image":
                                parts.append("[image]")
                    text = "\n".join(parts)
                tur = obj.get("toolUseResult") or {}
                tur_meta = (
                    {k: v for k, v in tur.items() if k not in ("stdout", "stderr")}
                    if isinstance(tur, dict)
                    else {}
                )
                stderr = tur.get("stderr") if isinstance(tur, dict) else None
                extra = {}
                if stderr:
                    extra["stderr"] = stderr
                if tur_meta:
                    extra["result_meta"] = tur_meta
                push(kind="tool_result", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent,
                     text=text, tool_id=block.get("tool_use_id", ""),
                     is_error=bool(block.get("is_error")), extra=extra)
            elif bt == "image":
                push(kind="image", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent,
                     extra={"source": block.get("source")})
            else:
                push(kind=bt or "unknown", role=role, timestamp=ts, uuid=uuid, parent_uuid=parent,
                     extra={"raw": block})
    return flat


# ---------------------------------------------------------------------------
# Touched files (Write / Edit / NotebookEdit / MultiEdit)
# ---------------------------------------------------------------------------

@dataclass
class TouchedFile:
    absolute_path: str
    relative_path: str
    op: str
    message_uuid: str
    exists: bool = False
    size: int = 0
    recorded_content: str | None = None
    edit_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("recorded_content", None)
        d["has_recorded_content"] = self.recorded_content is not None
        return d


def collect_touched_files(flat: list[FlatMessage], cwd: str) -> list[TouchedFile]:
    cwd_p = Path(cwd).resolve() if cwd else None
    seen: dict[str, TouchedFile] = {}
    for m in flat:
        if m.kind != "tool_use":
            continue
        inp = m.tool_input or {}
        if not isinstance(inp, dict):
            continue
        path: str | None = None
        op = m.tool_name
        if m.tool_name in ("Write", "Edit", "MultiEdit"):
            path = inp.get("file_path")
        elif m.tool_name == "NotebookEdit":
            path = inp.get("notebook_path")
        if not path:
            continue
        try:
            abs_p = Path(path).resolve()
        except OSError:
            continue
        rel = ""
        if cwd_p:
            r = _rel_to(abs_p, cwd_p)
            if r is not None:
                rel = r
        key = os.path.normcase(str(abs_p)) if sys.platform == "win32" else str(abs_p)
        tf = seen.get(key)
        if tf is None:
            tf = TouchedFile(absolute_path=str(abs_p), relative_path=rel, op=op, message_uuid=m.uuid)
            seen[key] = tf
        elif op not in tf.op.split("+"):
            tf.op = f"{tf.op}+{op}"
        if m.tool_name == "Write":
            content = inp.get("content")
            if isinstance(content, str):
                tf.recorded_content = content
    out = list(seen.values())
    for tf in out:
        p = Path(tf.absolute_path)
        try:
            if p.exists() and p.is_file():
                tf.exists = True
                tf.size = p.stat().st_size
        except OSError:
            tf.exists = False
        tf.edit_only = (tf.recorded_content is None) and ("Write" not in tf.op.split("+"))
    return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _strip_uploaded_files_wrapper(text: str) -> str:
    if not text or "<uploaded_files>" not in text:
        return text
    start = text.find("<uploaded_files>")
    end = text.find("</uploaded_files>")
    if start == -1 or end == -1:
        return text
    return (text[:start] + text[end + len("</uploaded_files>"):]).strip()


def render_markdown(
    meta: SessionMeta,
    flat: list[FlatMessage],
    touched: list[TouchedFile],
    claude_md: Path | None,
    bundle_root: Path,
) -> str:
    out: list[str] = []
    title = meta.title or f"Claude Code session {meta.task_id[:8]}"
    out.append(f"# {title}")
    out.append("")
    info = [
        ("Session ID", meta.task_id, True),
        ("Working dir", meta.cwd or meta.decoded_cwd, True),
        ("Git branch", meta.git_branch, True),
        ("Started", _fmt_ts(meta.started_at), False),
        ("Ended", _fmt_ts(meta.ended_at), False),
        ("Entrypoint", meta.entrypoint, False),
        ("Permission mode", meta.permission_mode, False),
        ("Claude Code", meta.version, True),
    ]
    for label, value, mono in info:
        if not value:
            continue
        if mono:
            out.append(f"- **{label}:** `{value}`")
        else:
            out.append(f"- **{label}:** {value}")
    out.append("")

    if claude_md and claude_md.exists():
        try:
            rel = claude_md.relative_to(bundle_root)
        except ValueError:
            rel = Path(claude_md.name)
        out.append(f"- **Project CLAUDE.md:** [{claude_md.name}]({rel}) — "
                   f"{_human_size(claude_md.stat().st_size)}")
        out.append("")

    if touched:
        out.append("## Files written / edited via tool calls")
        out.append("")
        for tf in touched:
            shown = tf.relative_path or tf.absolute_path
            link = f"[{shown}](assets/{tf.relative_path})" if tf.exists and tf.relative_path else shown
            status = "" if tf.exists else " _(snapshot recovered from tool input)_"
            if not tf.exists and tf.recorded_content is None:
                status = " _(not on disk and no recorded content)_"
            out.append(f"- `{tf.op}` — {link}{status}")
        out.append("")

    out.append("## Transcript")
    out.append("")
    for m in flat:
        ts = _fmt_ts(m.timestamp)
        if m.kind == "text":
            who = m.role.capitalize()
            out.append(f"### {who} · {ts}")
            out.append("")
            out.append(_strip_uploaded_files_wrapper(m.text).rstrip() or "_(empty)_")
            out.append("")
        elif m.kind == "thinking":
            out.append(f"<details><summary>Thinking · {ts}</summary>")
            out.append("")
            out.append(m.text.rstrip())
            out.append("")
            out.append("</details>")
            out.append("")
        elif m.kind == "tool_use":
            inp = json.dumps(m.tool_input, ensure_ascii=False, indent=2) if m.tool_input is not None else ""
            out.append(f"#### Tool call: `{m.tool_name}` · {ts}")
            out.append("")
            out.append("```json")
            out.append(inp)
            out.append("```")
            out.append("")
        elif m.kind == "tool_result":
            tag = "Tool error" if m.is_error else "Tool result"
            out.append(f"<details><summary>{tag} · {ts}</summary>")
            out.append("")
            txt = m.text or ""
            note = ""
            if len(txt) > TOOL_RESULT_TRUNCATE:
                note = f"\n\n_…truncated, full text in JSON export ({len(txt)} chars)_"
                txt = txt[:TOOL_RESULT_TRUNCATE]
            out.append("```")
            out.append(txt.rstrip())
            out.append("```")
            if note:
                out.append(note)
            out.append("")
            out.append("</details>")
            out.append("")
        elif m.kind == "attachment":
            payload = m.attachment_payload or {}
            preview = json.dumps({k: v for k, v in payload.items() if k != "type"}, ensure_ascii=False)[:400]
            out.append(f"<details><summary>attachment · {m.attachment_type} · {ts}</summary>")
            out.append("")
            out.append("```json")
            out.append(preview)
            out.append("```")
            out.append("")
            out.append("</details>")
            out.append("")
        elif m.kind == "image":
            out.append(f"_(image attachment · {ts})_")
            out.append("")
        else:
            out.append(f"_({m.kind} · {ts})_")
            out.append("")
    return "\n".join(out)


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/highlight.min.js"></script>
<style>
:root {{
  --bg: #fafafa; --fg: #1f2328; --muted: #656d76; --border: #d0d7de;
  --user-bg: #ddf4ff; --user-bd: #b6e3ff;
  --asst-bg: #ffffff; --asst-bd: #d0d7de;
  --tool-bg: #fff8c5; --tool-bd: #eac54f;
  --result-bg: #dafbe1; --result-bd: #4ac26b;
  --result-err-bg: #ffebe9; --result-err-bd: #ff8182;
  --thinking-bg: #f3e8ff; --thinking-bd: #c8a2f5;
  --att-bg: #f6f8fa; --att-bd: #d0d7de;
  --code-bg: #0d1117; --code-fg: #e6edf3;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Helvetica Neue", Arial, sans-serif;
  color: var(--fg); background: var(--bg);
}}
.wrap {{ max-width: 1080px; margin: 32px auto; padding: 0 20px; }}
header.head {{
  background: #fff; border: 1px solid var(--border); border-radius: 10px;
  padding: 20px 24px; margin-bottom: 24px;
}}
header.head h1 {{ margin: 0 0 12px; font-size: 22px; }}
header.head dl {{
  display: grid; grid-template-columns: max-content 1fr;
  gap: 4px 16px; margin: 0; font-size: 13px; color: var(--muted);
}}
header.head dt {{ font-weight: 600; color: var(--fg); }}
header.head dd {{ margin: 0; word-break: break-all; }}
header.head dd.mono {{ font-family: ui-monospace, Menlo, Consolas, monospace; }}
.section {{
  background: #fff; border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 20px; margin-bottom: 24px;
}}
.section h2 {{ margin: 0 0 10px; font-size: 15px; }}
.section ul {{ margin: 0; padding-left: 22px; font-size: 13px; }}
.section li {{ margin: 3px 0; font-family: ui-monospace, Menlo, Consolas, monospace; }}
.section .op {{
  display: inline-block; padding: 1px 6px; margin-right: 6px;
  border-radius: 4px; background: #eee; font-size: 11px;
}}
.section .size {{ color: var(--muted); font-size: 12px; margin-left: 6px; }}
.toc {{
  background: #fff; border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 20px 14px; margin-bottom: 24px;
}}
.toc h2 {{ margin: 0 0 8px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }}
.toc ol {{ margin: 0; padding-left: 22px; font-size: 13px; }}
.toc a {{ color: #0969da; text-decoration: none; }}
.toc a:hover {{ text-decoration: underline; }}
section.turn {{
  border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px;
  margin-bottom: 22px; background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}}
section.turn .turn-head {{
  display: flex; align-items: baseline; gap: 12px;
  font-size: 12px; color: var(--muted);
  border-bottom: 1px dashed var(--border); padding-bottom: 8px; margin-bottom: 14px;
}}
section.turn .turn-num {{
  font-weight: 700; color: var(--fg); padding: 2px 8px;
  background: rgba(9,105,218,0.08); border-radius: 999px; font-size: 11px;
}}
section.turn .turn-preview {{
  flex: 1 1 auto; color: var(--fg); font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
section.turn .turn-ts {{ flex: 0 0 auto; font-variant-numeric: tabular-nums; }}
section.turn .msg {{ margin-bottom: 12px; }}
section.turn .msg:last-child {{ margin-bottom: 0; }}
details.process {{
  border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; margin: 8px 0 14px;
  background: rgba(0,0,0,0.02);
}}
details.process > summary {{ font-weight: 600; color: var(--muted); }}
details.process[open] > summary {{ color: var(--fg); }}
details.process .process-meta {{ font-weight: 400; color: var(--muted); margin-left: 4px; }}
details.process > .msg {{ margin-top: 10px; }}
details.preamble {{
  border: 1px dashed var(--border); border-radius: 8px;
  padding: 10px 14px; margin-bottom: 18px; color: var(--muted);
}}
.msg {{
  border: 1px solid; border-radius: 10px; padding: 14px 18px;
  margin-bottom: 16px; background: #fff; overflow: hidden;
}}
.msg.user {{ background: var(--user-bg); border-color: var(--user-bd); }}
.msg.assistant {{ background: var(--asst-bg); border-color: var(--asst-bd); }}
.msg.thinking {{ background: var(--thinking-bg); border-color: var(--thinking-bd); border-style: dashed; }}
.msg.tool_use {{ background: var(--tool-bg); border-color: var(--tool-bd); }}
.msg.tool_result {{ background: var(--result-bg); border-color: var(--result-bd); }}
.msg.tool_result.error {{ background: var(--result-err-bg); border-color: var(--result-err-bd); }}
.msg.attachment, .msg.image, .msg.unknown {{
  background: var(--att-bg); border-color: var(--att-bd);
  color: var(--muted); font-size: 13px;
}}
.msg-head {{
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted);
}}
.msg-head .role {{ font-weight: 700; }}
.msg-body {{ font-size: 15px; }}
.msg-body > *:first-child {{ margin-top: 0; }}
.msg-body > *:last-child {{ margin-bottom: 0; }}
.md p, .md ul, .md ol, .md blockquote {{ margin: 0.5em 0; }}
.md h1, .md h2, .md h3, .md h4 {{ margin: 0.6em 0 0.3em; }}
.md ul, .md ol {{ padding-left: 1.6em; }}
.md blockquote {{ border-left: 3px solid var(--border); padding: 0 12px; color: var(--muted); margin-left: 0; }}
.md pre {{
  background: var(--code-bg); color: var(--code-fg);
  padding: 12px 14px; border-radius: 6px; overflow-x: auto;
  max-height: 520px; margin: 0.5em 0;
}}
.md pre code {{ background: transparent; padding: 0; color: inherit; }}
.md :not(pre) > code {{ background: rgba(175,184,193,0.2); padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }}
.md table {{ border-collapse: collapse; margin: 0.6em 0; max-width: 100%; display: block; overflow-x: auto; }}
.md th, .md td {{ border: 1px solid var(--border); padding: 4px 8px; }}
.md th {{ background: #f6f8fa; }}
.md img {{ max-width: 100%; height: auto; }}
code, pre {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
details {{ margin: 6px 0; }}
details > summary {{ cursor: pointer; color: #444; font-weight: 600; user-select: none; }}
details[open] > summary {{ margin-bottom: 8px; }}
.tool-name {{
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  background: rgba(0,0,0,0.08); font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 13px;
}}
.truncated {{ color: var(--muted); font-style: italic; font-size: 12px; margin-top: 6px; }}
pre.raw {{
  background: var(--code-bg); color: var(--code-fg);
  padding: 12px 14px; border-radius: 6px; overflow-x: auto;
  max-height: 520px; margin: 0; white-space: pre-wrap; word-break: break-word;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --border: #30363d;
    --user-bg: #0c2d6b; --user-bd: #1f6feb;
    --asst-bg: #161b22; --asst-bd: #30363d;
    --tool-bg: #3d2e00; --tool-bd: #8b6914;
    --result-bg: #04260f; --result-bd: #2ea043;
    --result-err-bg: #3d0e0e; --result-err-bd: #f85149;
    --thinking-bg: #2d1b4e; --thinking-bd: #8957e5;
    --att-bg: #161b22; --att-bd: #30363d;
  }}
  body {{ background: var(--bg); }}
  header.head, .section, .toc, section.turn {{ background: #161b22; }}
  section.turn .turn-num {{ background: rgba(88,166,255,0.18); }}
  details.process {{ background: rgba(255,255,255,0.03); }}
  .md th {{ background: #161b22; }}
  .md :not(pre) > code {{ background: rgba(110,118,129,0.4); }}
  .toc a {{ color: #58a6ff; }}
}}
</style>
</head>
<body>
<div class="wrap">
{header}
{claude_md}
{touched}
{toc}
{messages}
</div>
<script>
(function () {{
  const opts = {{ gfm: true, breaks: true, headerIds: false, mangle: false }};
  if (window.marked && marked.setOptions) marked.setOptions(opts);
  document.querySelectorAll('.md').forEach(el => {{
    const raw = el.textContent;
    el.innerHTML = window.marked ? marked.parse(raw, opts) : raw;
  }});
  if (window.hljs) {{
    document.querySelectorAll('pre code').forEach(el => {{
      try {{ hljs.highlightElement(el); }} catch (e) {{}}
    }});
  }}
}})();
</script>
</body>
</html>
"""


def render_html(
    meta: SessionMeta,
    flat: list[FlatMessage],
    touched: list[TouchedFile],
    claude_md: Path | None,
    bundle_root: Path,
) -> str:
    esc = html_mod.escape
    title = meta.title or f"Claude Code session {meta.task_id[:8]}"

    rows: list[tuple[str, str, bool]] = [
        ("Session ID", meta.task_id, True),
        ("Working dir", meta.cwd or meta.decoded_cwd, True),
        ("Git branch", meta.git_branch, True),
        ("Started", _fmt_ts(meta.started_at), False),
        ("Ended", _fmt_ts(meta.ended_at), False),
        ("Entrypoint", meta.entrypoint, False),
        ("Permission mode", meta.permission_mode, False),
        ("Claude Code", meta.version, True),
    ]
    head_parts = [f"<header class='head'><h1>{esc(title)}</h1><dl>"]
    for label, value, mono in rows:
        if not value:
            continue
        cls = " class='mono'" if mono else ""
        head_parts.append(f"<dt>{esc(label)}</dt><dd{cls}>{esc(str(value))}</dd>")
    head_parts.append("</dl></header>")

    claude_md_html = ""
    if claude_md and claude_md.exists():
        try:
            rel = claude_md.relative_to(bundle_root)
        except ValueError:
            rel = Path(claude_md.name)
        href = "/".join(html_mod.escape(seg, quote=True) for seg in rel.parts)
        size = _human_size(claude_md.stat().st_size)
        claude_md_html = (
            "<div class='section'><h2>Project CLAUDE.md</h2><ul>"
            f"<li><a href=\"{href}\">{esc(claude_md.name)}</a>"
            f"<span class='size'>{esc(size)}</span></li></ul></div>"
        )

    touched_html = ""
    if touched:
        items = []
        for tf in touched:
            shown = esc(tf.relative_path or tf.absolute_path)
            if (tf.exists or tf.recorded_content is not None) and tf.relative_path:
                href = "assets/" + "/".join(html_mod.escape(seg, quote=True) for seg in tf.relative_path.split("/"))
                link = f"<a href=\"{href}\">{shown}</a>"
            else:
                link = shown
            if tf.exists:
                status = ""
            elif tf.recorded_content is not None:
                status = " <em>(recovered from tool input)</em>"
            else:
                status = " <em>(not on disk; no recorded content)</em>"
            items.append(f"<li><span class='op'>{esc(tf.op)}</span>{link}{status}</li>")
        touched_html = (
            "<div class='section'><h2>Files written / edited via tool calls</h2>"
            f"<ul>{''.join(items)}</ul></div>"
        )

    preamble, turns = _split_into_turns(flat)

    toc_items = []
    for i, turn in enumerate(turns):
        u = turn["user"]
        preview_src = _strip_uploaded_files_wrapper(u.text).strip().splitlines()
        preview = preview_src[0] if preview_src else "(empty)"
        preview = preview[:80] + ("…" if len(preview) > 80 else "")
        toc_items.append(f"<li><a href=\"#t{i}\">{esc(preview)}</a></li>")
    toc_html = ""
    if toc_items:
        toc_html = f"<div class='toc'><h2>User prompts</h2><ol>{''.join(toc_items)}</ol></div>"

    parts: list[str] = []

    if preamble:
        intro_pieces = [_render_block_html(m, esc) for m in preamble if _render_block_html(m, esc)]
        if intro_pieces:
            parts.append(
                "<details class='preamble'><summary>Pre-conversation events "
                f"({len(intro_pieces)})</summary>{''.join(intro_pieces)}</details>"
            )

    for i, turn in enumerate(turns):
        u: FlatMessage = turn["user"]
        body: list[FlatMessage] = turn["body"]
        hidden = [m for m in body if m.kind in ("thinking", "tool_use", "tool_result", "attachment", "image", "unknown")]
        visible_text = [m for m in body if m.kind == "text"]

        ts = esc(_fmt_ts(u.timestamp))
        user_text = _strip_uploaded_files_wrapper(u.text)
        prompt_preview = (user_text.strip().splitlines()[0] if user_text.strip() else "(empty)")
        prompt_preview = prompt_preview[:120] + ("…" if len(prompt_preview) > 120 else "")

        n_tool = sum(1 for m in hidden if m.kind == "tool_use")
        n_think = sum(1 for m in hidden if m.kind == "thinking")
        process_summary_bits = []
        if n_think:
            process_summary_bits.append(f"{n_think} thinking")
        if n_tool:
            process_summary_bits.append(f"{n_tool} tool call{'s' if n_tool != 1 else ''}")
        n_other = len(hidden) - n_tool - n_think
        if n_other > 0:
            process_summary_bits.append(f"{n_other} other")
        process_summary = " · ".join(process_summary_bits) if process_summary_bits else "no internal steps"

        parts.append(f"<section class='turn' id='t{i}'>")
        parts.append(
            f"<div class='turn-head'><span class='turn-num'>#{i + 1}</span>"
            f"<span class='turn-preview'>{esc(prompt_preview)}</span>"
            f"<span class='turn-ts'>{ts}</span></div>"
        )
        parts.append(
            f"<div class='msg user'><div class='msg-head'><span class='role'>User</span>"
            f"<span>{ts}</span></div><div class='msg-body'><div class='md'>{esc(user_text)}</div></div></div>"
        )
        if hidden:
            inner = "".join(_render_block_html(m, esc) for m in hidden)
            parts.append(
                f"<details class='process'><summary>Assistant reasoning &amp; tool calls "
                f"<span class='process-meta'>· {esc(process_summary)}</span></summary>{inner}</details>"
            )
        for m in visible_text:
            parts.append(_render_block_html(m, esc))
        parts.append("</section>")

    return HTML_TEMPLATE.format(
        title=esc(title),
        header="".join(head_parts),
        claude_md=claude_md_html,
        touched=touched_html,
        toc=toc_html,
        messages="".join(parts),
    )


def _split_into_turns(flat: list[FlatMessage]) -> tuple[list[FlatMessage], list[dict[str, Any]]]:
    preamble: list[FlatMessage] = []
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for m in flat:
        if m.kind == "text" and m.role == "user":
            if current is not None:
                turns.append(current)
            current = {"user": m, "body": []}
        elif current is None:
            preamble.append(m)
        else:
            current["body"].append(m)
    if current is not None:
        turns.append(current)
    return preamble, turns


def _render_block_html(m: FlatMessage, esc) -> str:
    ts = esc(_fmt_ts(m.timestamp))
    anchor = f"m{m.index}"
    if m.kind == "text":
        klass = "user" if m.role == "user" else "assistant"
        label = m.role.capitalize()
        text = _strip_uploaded_files_wrapper(m.text)
        body = f"<div class='md'>{esc(text)}</div>"
        return _msg_html(anchor, klass, label, ts, body)
    if m.kind == "thinking":
        body = f"<details open><summary>Reasoning</summary><div class='md'>{esc(m.text)}</div></details>"
        return _msg_html(anchor, "thinking", "thinking", ts, body)
    if m.kind == "tool_use":
        tn = esc(m.tool_name)
        inp = json.dumps(m.tool_input, ensure_ascii=False, indent=2) if m.tool_input is not None else ""
        body = (
            f"<div><span class='tool-name'>{tn}</span></div>"
            f"<details><summary>Input</summary>"
            f"<pre><code class='language-json'>{esc(inp)}</code></pre></details>"
        )
        return _msg_html(anchor, "tool_use", "tool call", ts, body)
    if m.kind == "tool_result":
        klass = "tool_result error" if m.is_error else "tool_result"
        label = "tool error" if m.is_error else "tool result"
        txt = m.text or ""
        note = ""
        if len(txt) > TOOL_RESULT_TRUNCATE:
            note = (
                f"<div class='truncated'>…truncated, full text in JSON export "
                f"({len(txt)} chars)</div>"
            )
            txt = txt[:TOOL_RESULT_TRUNCATE]
        body = f"<details open><summary>Output</summary><pre class='raw'>{esc(txt)}</pre>{note}</details>"
        return _msg_html(anchor, klass, label, ts, body)
    if m.kind == "attachment":
        atype = esc(m.attachment_type)
        payload = m.attachment_payload or {}
        preview = json.dumps({k: v for k, v in payload.items() if k != "type"}, ensure_ascii=False)
        if len(preview) > 600:
            preview = preview[:600] + " …"
        body = f"<details><summary>attachment · {atype}</summary><pre class='raw'>{esc(preview)}</pre></details>"
        return _msg_html(anchor, "attachment", "attachment", ts, body)
    if m.kind == "image":
        return _msg_html(anchor, "image", "image", ts, "<em>(image attachment)</em>")
    return _msg_html(anchor, "unknown", esc(m.kind), ts, "<em>unhandled block</em>")


def _msg_html(anchor: str, klass: str, label: str, ts: str, body: str) -> str:
    return (
        f"<div class='msg {klass}' id='{anchor}'>"
        f"<div class='msg-head'><span class='role'>{label}</span><span>{ts}</span></div>"
        f"<div class='msg-body'>{body}</div></div>"
    )


def render_json(
    meta: SessionMeta,
    flat: list[FlatMessage],
    touched: list[TouchedFile],
    claude_md: Path | None,
    bundle_root: Path,
) -> str:
    cmd_entry: dict[str, Any] | None = None
    if claude_md and claude_md.exists():
        try:
            rel = str(claude_md.relative_to(bundle_root))
        except ValueError:
            rel = claude_md.name
        cmd_entry = {
            "name": claude_md.name,
            "relative_path": rel,
            "size": claude_md.stat().st_size,
        }
    payload = {
        "meta": meta.to_dict(),
        "project_claude_md": cmd_entry,
        "files": [tf.to_dict() for tf in touched],
        "messages": [m.to_dict() for m in flat],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_csv(path: Path, flat: list[FlatMessage]) -> None:
    cols = [
        "index", "timestamp", "role", "kind",
        "tool_name", "tool_id", "is_error",
        "preview", "content", "tool_input_json",
        "uuid", "parent_uuid",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for m in flat:
            content = m.text
            if m.kind == "tool_use":
                content = json.dumps(m.tool_input, ensure_ascii=False) if m.tool_input is not None else ""
            elif m.kind == "attachment":
                content = json.dumps(m.attachment_payload, ensure_ascii=False) if m.attachment_payload is not None else ""
            preview_src = m.text if m.kind != "tool_use" else (m.tool_name or "")
            preview = (preview_src or "").strip().replace("\n", " ")[:200]
            w.writerow([
                m.index, m.timestamp, m.role, m.kind,
                m.tool_name, m.tool_id, "1" if m.is_error else "",
                preview, content,
                json.dumps(m.tool_input, ensure_ascii=False) if m.tool_input is not None else "",
                m.uuid, m.parent_uuid,
            ])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _confirm_auth_risk(non_interactive_ack: bool) -> None:
    msg = textwrap.dedent("""\
        ⚠️  --include-auth requested.
            The exported bundle will contain your Anthropic account credentials.
            Anyone who obtains this bundle can act as your account until you
            rotate tokens (log out everywhere / change password / revoke device).

            Intended uses:
              - migrating your own setup to a new device you control
              - personal backup stored in an encrypted vault (1Password / age / GPG)

            Do NOT:
              - share this bundle with anyone
              - upload to unencrypted cloud storage / chat / email
        """)
    print(msg, file=sys.stderr)
    if non_interactive_ack:
        print("  ack: --yes-i-know-this-is-risky given; proceeding non-interactively.",
              file=sys.stderr)
        return
    try:
        ans = input('Type "I UNDERSTAND" to proceed: ').strip()
    except EOFError:
        ans = ""
    if ans != "I UNDERSTAND":
        print("  abort: confirmation phrase not received.", file=sys.stderr)
        raise SystemExit(3)


def _resolve_auth_source() -> Path | None:
    """Find a portable CC credentials file on this machine.

    Standalone CC stores its OAuth refresh token at
    ``~/.claude/.credentials.json``. When CC is launched as a subprocess of
    Claude Desktop, credentials are passed via env and there is no portable
    file (``CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`` in that case) — return
    None so callers can surface a clear error.
    """
    candidates = [
        CREDENTIALS_PATH,
        HOME / ".config" / "claude-code" / "credentials.json",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def _copy_auth_for_export(target: Path, src: Path) -> dict[str, Any]:
    auth_dir = target / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    dest = auth_dir / "credentials.json"
    shutil.copy2(src, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return {
        "included": True,
        "files": ["auth/credentials.json"],
        "source_path": str(src),
        "encrypted": False,
    }


def _write_manifest(
    target: Path,
    task: Task,
    meta: SessionMeta,
    auth_info: dict[str, Any] | None,
) -> None:
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "tool": "claude-code-export",
        "tool_version": TOOL_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_platform": sys.platform,
        "source_path_sep": os.sep,
        "source_home": str(HOME),
        "source_cwd": meta.cwd or getattr(task, "decoded_cwd", "") or "",
        "source_session_id": task.task_id,
        "source_project_dir_name": (
            task.transcript_path.parent.name if task.transcript_path else ""
        ),
        "source_git_branch": meta.git_branch or "",
        "source_cc_version": meta.version or "",
        "auth": auth_info or {"included": False},
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def export_one(
    task: Task,
    output_root: Path,
    formats: Iterable[str],
    include_files: bool,
    auth_source: Path | None = None,
) -> Path | None:
    if not task.transcript_path or not task.transcript_path.exists():
        print(f"  warn: skipping {task.task_id} — no transcript file", file=sys.stderr)
        return None

    meta, raw = load_transcript(task.transcript_path)
    merge_task_meta(meta, task)
    flat = flatten(raw)
    touched = collect_touched_files(flat, meta.cwd) if include_files else []

    target = output_root / task.task_id
    target.mkdir(parents=True, exist_ok=True)

    shutil.copy2(task.transcript_path, target / "transcript.jsonl")

    claude_md_dest: Path | None = None
    if include_files:
        src_md = _find_project_claude_md(meta.cwd or meta.decoded_cwd)
        if src_md is not None:
            claude_md_dest = target / "CLAUDE.md"
            try:
                shutil.copy2(src_md, claude_md_dest)
            except OSError as e:
                print(f"  warn: failed to copy CLAUDE.md: {e}", file=sys.stderr)
                claude_md_dest = None

    if include_files:
        for tf in touched:
            src = Path(tf.absolute_path)
            asset_rel = tf.relative_path or src.name
            target_path = target / "assets" / asset_rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            copied = False
            if tf.exists:
                try:
                    shutil.copy2(src, target_path)
                    copied = True
                except OSError:
                    copied = False
            if not copied and tf.recorded_content is not None:
                try:
                    target_path.write_text(tf.recorded_content, encoding="utf-8")
                    copied = True
                except OSError as e:
                    print(f"  warn: failed to write recorded content for {asset_rel}: {e}", file=sys.stderr)
            if not copied:
                if tf.edit_only:
                    print(
                        f"  note: {asset_rel} only had Edit/MultiEdit calls and is not readable; "
                        "skipping (no recorded full content available)",
                        file=sys.stderr,
                    )
                else:
                    print(f"  warn: could not snapshot {asset_rel}", file=sys.stderr)

    formats = list(formats)
    if "html" in formats:
        (target / "session.html").write_text(
            render_html(meta, flat, touched, claude_md_dest, target),
            encoding="utf-8",
        )
    if "md" in formats:
        (target / "session.md").write_text(
            render_markdown(meta, flat, touched, claude_md_dest, target),
            encoding="utf-8",
        )
    if "json" in formats:
        (target / "session.json").write_text(
            render_json(meta, flat, touched, claude_md_dest, target),
            encoding="utf-8",
        )
    if "csv" in formats:
        write_csv(target / "session.csv", flat)

    auth_info: dict[str, Any] | None = None
    if auth_source is not None:
        auth_info = _copy_auth_for_export(target, auth_source)

    _write_manifest(target, task, meta, auth_info)
    _write_readme(target, task, meta, flat, touched, claude_md_dest, formats)
    return target


def _write_readme(
    target: Path,
    task: Task,
    meta: SessionMeta,
    flat: list[FlatMessage],
    touched: list[TouchedFile],
    claude_md: Path | None,
    formats: list[str],
) -> None:
    n_user = sum(1 for m in flat if m.kind == "text" and m.role == "user")
    n_asst = sum(1 for m in flat if m.kind == "text" and m.role == "assistant")
    n_tool = sum(1 for m in flat if m.kind == "tool_use")
    lines = [
        f"# {meta.title or task.task_id}",
        "",
        f"- Session ID: `{meta.task_id}`",
    ]
    if meta.cwd or meta.decoded_cwd:
        lines.append(f"- Working dir: `{meta.cwd or meta.decoded_cwd}`")
    if meta.git_branch:
        lines.append(f"- Git branch: `{meta.git_branch}`")
    if meta.entrypoint:
        lines.append(f"- Entrypoint: {meta.entrypoint}")
    if meta.permission_mode:
        lines.append(f"- Permission mode: {meta.permission_mode}")
    if meta.version:
        lines.append(f"- Claude Code: `{meta.version}`")
    if meta.started_at:
        lines.append(f"- Started: {_fmt_ts(meta.started_at)}")
    if meta.ended_at:
        lines.append(f"- Ended: {_fmt_ts(meta.ended_at)}")
    lines.append(
        f"- Messages: {len(flat)} blocks ({n_user} user, {n_asst} assistant, {n_tool} tool calls)"
    )
    if touched:
        lines.append(f"- Files written/edited: {len(touched)}")
    lines += ["", "## Files in this bundle", ""]
    if "html" in formats:
        lines.append("- `session.html` — formatted reading view (open in a browser)")
    if "md" in formats:
        lines.append("- `session.md` — Markdown export")
    if "json" in formats:
        lines.append("- `session.json` — structured export for LLM consumption")
    if "csv" in formats:
        lines.append("- `session.csv` — flat per-block table")
    lines.append("- `transcript.jsonl` — raw JSONL transcript (lossless source)")
    if claude_md is not None and claude_md.exists():
        lines.append("- `CLAUDE.md` — project's CLAUDE.md as it was at export time")
    if touched:
        lines.append("- `assets/` — files the assistant Wrote/Edited "
                     "(live copy if available, recorded-content fallback otherwise)")
    (target / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_list(args: argparse.Namespace) -> int:
    override = (
        [Path(args.code_root).expanduser().resolve()]
        if getattr(args, "code_root", None)
        else None
    )
    tasks = discover_sessions(override)
    project_filter = getattr(args, "project", None)
    if project_filter:
        tasks = filter_by_project(tasks, project_filter)
    if not tasks:
        root = override[0] if override else CODE_ROOT
        print(f"No Claude Code sessions found under {root}")
        if project_filter:
            print(f"(after filtering by project: {project_filter})", file=sys.stderr)
        return 0

    # Group by effective cwd; print one project header, then nested sessions.
    by_project: dict[str, list[Task]] = {}
    for t in tasks:
        by_project.setdefault(t.effective_cwd or "(unknown project)", []).append(t)

    # Order projects by the most-recent session's last_activity within each.
    def project_recency(item):
        cwd, ts_list = item
        return max(t.last_activity_ms or t.created_at_ms for t in ts_list)

    for cwd, group in sorted(by_project.items(), key=project_recency, reverse=True):
        print(cwd)
        for t in group:
            title = t.display_title
            if len(title) > 64:
                title = title[:61] + "…"
            when = t.display_when
            print(f"  {t.task_id}  {when}  {title}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    invalid = [f for f in formats if f not in SUPPORTED_FORMATS]
    if invalid:
        print(f"error: unknown format(s): {', '.join(invalid)}", file=sys.stderr)
        return 2

    override = (
        [Path(args.code_root).expanduser().resolve()]
        if getattr(args, "code_root", None)
        else None
    )
    tasks = discover_sessions(override)
    project_filter = getattr(args, "project", None)
    if project_filter:
        tasks = filter_by_project(tasks, project_filter)
    if not tasks:
        root = override[0] if override else CODE_ROOT
        print(f"No Claude Code sessions found under {root}", file=sys.stderr)
        return 1

    targets = resolve_tasks(args.session, tasks)
    if not targets:
        print(f"No session matched '{args.session}'", file=sys.stderr)
        return 1
    if len(targets) > 1 and args.session != "all":
        print(f"Ambiguous selector '{args.session}' matched {len(targets)} sessions:", file=sys.stderr)
        for t in targets:
            print(f"  {t.task_id}  {t.display_title}", file=sys.stderr)
        return 1

    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    auth_source: Path | None = None
    if getattr(args, "include_auth", False):
        auth_source = _resolve_auth_source()
        if auth_source is None:
            print(
                "error: --include-auth requested but no portable credentials file\n"
                "       was found. Checked:\n"
                f"         {CREDENTIALS_PATH}\n"
                f"         {HOME / '.config/claude-code/credentials.json'}\n"
                "       On hosts where CC runs as a subprocess of Claude Desktop\n"
                "       (CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1) credentials are\n"
                "       passed via env and have no portable form. Skip --include-auth\n"
                "       and sign in on the destination machine instead.",
                file=sys.stderr,
            )
            return 2
        _confirm_auth_risk(bool(getattr(args, "yes_i_know_this_is_risky", False)))

    exported = 0
    for task in targets:
        target = export_one(
            task,
            output_root,
            formats,
            include_files=not args.no_files,
            auth_source=auth_source,
        )
        if target:
            print(f"exported {task.task_id} → {target}")
            exported += 1
    if not exported:
        return 1
    return 0


WINDOWS_RESERVED_CHARS = set('<>:"|?*')


def _validate_path_for_windows(path: str) -> str | None:
    """Return an error string if path is unsafe on Windows, else None.
    The drive-letter colon at position 1 is tolerated."""
    if not path:
        return None
    head = path[:2]
    tail = path
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        tail = path[2:]
    bad = sorted({c for c in tail if c in WINDOWS_RESERVED_CHARS})
    if bad:
        return f"contains chars not allowed on Windows: {''.join(bad)!r}"
    return None


def _starts_with_cwd(path: str, src_cwd: str, src_platform: str) -> bool:
    """Prefix-match a path against src_cwd, applying case folding when the
    source platform is Windows (its filesystem is case-insensitive)."""
    if not path or not src_cwd:
        return False
    if len(path) < len(src_cwd):
        return False
    p, c = path, src_cwd
    if src_platform == "win32":
        p = p.lower()
        c = c.lower()
    if not p.startswith(c):
        return False
    # Require that the next char (if any) is a separator — avoid matching
    # /Users/foo as a prefix of /Users/foobar.
    if len(path) == len(src_cwd):
        return True
    nxt = path[len(src_cwd)]
    return nxt in ("/", "\\")


def _rewrite_path(
    path: str,
    src_cwd: str,
    dst_cwd: str,
    src_sep: str,
    dst_sep: str,
    src_platform: str,
) -> str:
    if not path:
        return path
    if not _starts_with_cwd(path, src_cwd, src_platform):
        return path
    tail = path[len(src_cwd):]
    if src_sep != dst_sep:
        tail = tail.replace(src_sep, dst_sep)
    return dst_cwd + tail


def _rewrite_jsonl(
    src_jsonl: Path,
    dst_jsonl: Path,
    src_cwd: str,
    dst_cwd: str,
    src_sep: str,
    dst_sep: str,
    src_platform: str,
) -> dict[str, int]:
    """Stream-rewrite src_jsonl → dst_jsonl, updating top-level cwd plus
    tool_use file_path / notebook_path that fall under the source cwd.
    Returns counters keyed by rewrite type."""
    counts = {"cwd": 0, "file_path": 0, "notebook_path": 0, "records": 0}
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with src_jsonl.open("r", encoding="utf-8") as fin, \
            dst_jsonl.open("w", encoding="utf-8") as fout:
        for line in fin:
            stripped = line.rstrip("\n")
            if not stripped.strip():
                fout.write(line)
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                fout.write(line)
                continue
            counts["records"] += 1
            if isinstance(obj.get("cwd"), str):
                new = _rewrite_path(obj["cwd"], src_cwd, dst_cwd, src_sep, dst_sep, src_platform)
                if new != obj["cwd"]:
                    counts["cwd"] += 1
                    obj["cwd"] = new
            msg = obj.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        inp = block.get("input")
                        if not isinstance(inp, dict):
                            continue
                        for key in ("file_path", "notebook_path"):
                            v = inp.get(key)
                            if isinstance(v, str):
                                new = _rewrite_path(v, src_cwd, dst_cwd, src_sep, dst_sep, src_platform)
                                if new != v:
                                    counts[key] += 1
                                    inp[key] = new
            fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return counts


def cmd_import(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser().resolve()
    if not bundle.is_dir():
        print(f"error: bundle directory does not exist: {bundle}", file=sys.stderr)
        return 2
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        print(
            f"error: not a valid bundle (missing manifest.json): {bundle}\n"
            f"       Bundles produced before tool version 0.2.0 lack a manifest and\n"
            f"       cannot be imported. Re-export with the current claude-code-export.",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: failed to parse manifest.json: {e}", file=sys.stderr)
        return 2
    if manifest.get("tool") != "claude-code-export":
        print(
            f"error: bundle was produced by {manifest.get('tool')!r}, expected "
            f"'claude-code-export'.",
            file=sys.stderr,
        )
        return 2
    if manifest.get("bundle_version", 1) > BUNDLE_VERSION:
        print(
            f"error: bundle uses bundle_version={manifest['bundle_version']} but "
            f"this tool only understands up to {BUNDLE_VERSION}. Upgrade the tool.",
            file=sys.stderr,
        )
        return 2

    src_platform = manifest.get("source_platform") or ""
    src_sep = manifest.get("source_path_sep") or ("\\" if src_platform == "win32" else "/")
    src_cwd = manifest.get("source_cwd") or ""
    session_id = manifest.get("source_session_id") or ""
    if not session_id or not src_cwd:
        print("error: manifest missing source_session_id or source_cwd", file=sys.stderr)
        return 2

    dst_platform = sys.platform
    dst_sep = "\\" if dst_platform == "win32" else "/"

    if args.cwd:
        dst_cwd = str(Path(args.cwd).expanduser())
        # Strip trailing separator for clean prefixing.
        dst_cwd = dst_cwd.rstrip("/").rstrip("\\") or dst_cwd
    elif src_platform == dst_platform:
        dst_cwd = src_cwd
    else:
        print(
            f"error: cross-platform import (source={src_platform}, target={dst_platform}) "
            "requires --cwd <path> on the target machine.",
            file=sys.stderr,
        )
        return 2

    if dst_platform == "win32":
        msg = _validate_path_for_windows(dst_cwd)
        if msg:
            print(f"error: --cwd {dst_cwd!r} {msg}", file=sys.stderr)
            return 2

    new_encoded = _encode_cwd_dir(dst_cwd)
    code_root = (
        Path(args.code_root).expanduser().resolve()
        if getattr(args, "code_root", None) else CODE_ROOT
    )
    target_jsonl = code_root / new_encoded / f"{session_id}.jsonl"
    src_jsonl = bundle / "transcript.jsonl"
    if not src_jsonl.exists():
        print(f"error: bundle is missing transcript.jsonl: {src_jsonl}", file=sys.stderr)
        return 2

    bundle_auth = bundle / "auth" / "credentials.json"
    bundle_claude_md = bundle / "CLAUDE.md"
    bundle_assets = bundle / "assets"
    file_plan: list[tuple[Path, Path]] = []
    if bundle_claude_md.exists():
        file_plan.append((bundle_claude_md, Path(dst_cwd) / "CLAUDE.md"))
    if bundle_assets.is_dir():
        for p in sorted(bundle_assets.rglob("*")):
            if p.is_file():
                rel = p.relative_to(bundle_assets)
                file_plan.append((p, Path(dst_cwd) / rel))

    print(f"Source bundle: {bundle}")
    print(f"  tool_version:    {manifest.get('tool_version')}")
    print(f"  exported_at:     {manifest.get('exported_at')}")
    print(f"  source_platform: {src_platform}")
    print(f"  source_cwd:      {src_cwd}")
    print(f"  session_id:      {session_id}")
    print(f"  manifest.auth.included: {manifest.get('auth', {}).get('included')}")
    print()
    print(f"Target ({dst_platform}):")
    print(f"  cwd:        {dst_cwd}")
    print(f"  encoded:    {new_encoded}")
    print(f"  jsonl:      {target_jsonl}")
    if src_sep != dst_sep:
        print(f"  separator rewrite: {src_sep!r} → {dst_sep!r}")
    if file_plan:
        print(f"  files to restore under cwd: {len(file_plan)}")
        for src, dst in file_plan[:5]:
            print(f"    {src.relative_to(bundle)} → {dst}")
        if len(file_plan) > 5:
            print(f"    ... +{len(file_plan) - 5} more")
    install_auth = bundle_auth.exists() and not args.skip_auth
    if install_auth:
        print(f"  auth:       {bundle_auth.relative_to(bundle)} → {CREDENTIALS_PATH}")
    elif bundle_auth.exists():
        print(f"  auth:       (skipped via --skip-auth; bundle contains credentials)")

    if args.dry_run:
        print()
        print("Dry-run: no files written.")
        return 0

    # Pre-flight: refuse to overwrite without --force.
    blockers: list[str] = []
    if target_jsonl.exists() and not args.force:
        blockers.append(f"{target_jsonl} already exists")
    if install_auth and CREDENTIALS_PATH.exists() and not args.force:
        blockers.append(f"{CREDENTIALS_PATH} already exists")
    if blockers:
        for b in blockers:
            print(f"error: {b}", file=sys.stderr)
        print("       Re-run with --force to overwrite.", file=sys.stderr)
        return 3

    print()
    counts = _rewrite_jsonl(
        src_jsonl, target_jsonl, src_cwd, dst_cwd, src_sep, dst_sep, src_platform,
    )
    print(
        f"wrote {target_jsonl}  "
        f"(records: {counts['records']}, "
        f"cwd rewrites: {counts['cwd']}, "
        f"file_path: {counts['file_path']}, "
        f"notebook_path: {counts['notebook_path']})"
    )

    if file_plan:
        Path(dst_cwd).mkdir(parents=True, exist_ok=True)
    restored = skipped = 0
    for src, dst in file_plan:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  warn: cannot create {dst.parent}: {e}", file=sys.stderr)
            continue
        if dst.exists() and not args.force:
            print(f"  skip (exists): {dst}")
            skipped += 1
            continue
        try:
            shutil.copy2(src, dst)
            restored += 1
        except OSError as e:
            print(f"  warn: failed to restore {dst}: {e}", file=sys.stderr)
    if file_plan:
        print(f"restored {restored} file(s) under cwd, skipped {skipped}.")

    if install_auth:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle_auth, CREDENTIALS_PATH)
        try:
            os.chmod(CREDENTIALS_PATH, 0o600)
        except OSError:
            pass
        print(f"installed auth: {CREDENTIALS_PATH}")

    print()
    print("Done. Resume with:")
    print(f"  claude --resume {session_id}")
    return 0


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [truncated, {len(text) - limit} more chars]"


def _demote_headers(text: str, by: int = 2) -> str:
    """Prepend ``by`` more ``#`` chars to any ATX-style heading line so the
    embedded conversation's headings don't compete with the seed's own
    structural headings when a reader scans the file."""
    if not text:
        return text
    out = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line); continue
        if in_fence:
            out.append(line); continue
        if line.startswith("#"):
            i = 0
            while i < len(line) and line[i] == "#":
                i += 1
            if 1 <= i <= 6 and (i == len(line) or line[i] in (" ", "\t")):
                out.append("#" * min(6, i + by) + line[i:])
                continue
        out.append(line)
    return "\n".join(out)


def _summarise_tool_use(m: FlatMessage) -> str:
    tn = m.tool_name or "?"
    inp = m.tool_input if isinstance(m.tool_input, dict) else {}
    bits = []
    for key in ("command", "file_path", "notebook_path", "pattern", "path", "url", "query"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            bits.append(f"{key}={_truncate(v, SEED_TOOL_INPUT_TRUNCATE)!r}")
            break
    return f"`{tn}`" + (f" ({', '.join(bits)})" if bits else "")


def _summarise_tool_result(m: FlatMessage) -> str:
    txt = (m.text or "").strip()
    if not txt:
        return "(empty)"
    return _truncate(txt, SEED_TOOL_RESULT_TRUNCATE)


def _list_bundle_files(bundle: Path) -> list[tuple[str, Path, int]]:
    """Return (category, relative-path, size) for files in the bundle that
    are conversation artefacts (assets, CLAUDE.md). Skips the rendered
    HTML/MD/JSON/CSV / transcript.jsonl / manifest / auth."""
    out: list[tuple[str, Path, int]] = []
    md = bundle / "CLAUDE.md"
    if md.exists() and md.is_file():
        out.append(("CLAUDE.md", md.relative_to(bundle), md.stat().st_size))
    assets = bundle / "assets"
    if assets.is_dir():
        for p in sorted(assets.rglob("*")):
            if p.is_file():
                try:
                    out.append(("asset", p.relative_to(bundle), p.stat().st_size))
                except OSError:
                    pass
    return out


def render_seed_prompt(
    meta: SessionMeta,
    flat: list[FlatMessage],
    bundle_files: list[tuple[str, Path, int]],
    mode: str,
    bundle: Path,
) -> str:
    if mode not in ("brief", "standard", "full"):
        raise ValueError(f"unknown seed mode: {mode}")
    preamble, turns = _split_into_turns(flat)
    n_user = len(turns)
    n_tool = sum(1 for m in flat if m.kind == "tool_use")
    n_think = sum(1 for m in flat if m.kind == "thinking")
    title = meta.title or f"Claude Code session {meta.task_id[:8]}"

    out: list[str] = []
    out.append(f"# Continuation of a previous Claude Code session")
    out.append("")
    out.append(
        "I'm resuming a previous Claude Code chat in a fresh conversation. "
        "Below is the context from the prior session — what we were working on, "
        "the relevant files, and where we left off. Please read it through, "
        "then confirm you've absorbed the context and are ready to continue."
    )
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Previous session metadata")
    out.append("")
    out.append(f"- **Title**: {title}")
    if meta.version:
        out.append(f"- **Claude Code version**: `{meta.version}`")
    if meta.cwd:
        out.append(f"- **Working dir** (on the original machine): `{meta.cwd}`")
    if meta.started_at:
        out.append(f"- **Started**: {_fmt_ts(meta.started_at)}")
    if meta.ended_at:
        out.append(f"- **Ended**: {_fmt_ts(meta.ended_at)}")
    if meta.git_branch:
        out.append(f"- **Git branch**: `{meta.git_branch}`")
    out.append(f"- **Activity**: {n_user} user prompt(s), {n_think} reasoning blocks, {n_tool} tool call(s)")
    out.append("")

    if bundle_files:
        out.append("## Files carried over from the previous session")
        out.append("")
        out.append(
            "The export bundle contains these files. They have either been "
            "restored to your working directory by the importer or are sitting "
            "next to this seed prompt as raw bytes — either way, treat them as "
            "the canonical state of those paths at the moment the previous "
            "session ended."
        )
        out.append("")
        for cat, rel, size in bundle_files:
            tag = "CLAUDE.md" if cat == "CLAUDE.md" else "asset"
            out.append(f"- `{rel}` ({_human_size(size)}) — {tag}")
        out.append("")

    if mode == "brief":
        keep_turns = turns[-3:] if len(turns) > 3 else turns
        out.append(f"## Last {len(keep_turns)} exchange(s) (verbatim)")
        out.append("")
        for i, turn in enumerate(keep_turns, 1):
            _render_turn_for_seed(out, turn, mode="full", index=n_user - len(keep_turns) + i)
    else:
        out.append("## Conversation summary")
        out.append("")
        if mode == "standard":
            out.append(
                "_Earlier turns are summarised; the final exchange is verbatim. "
                "Pass `--mode full` to the `seed` command if you need the entire "
                "conversation reproduced._"
            )
            out.append("")
        for i, turn in enumerate(turns[:-1] if turns else [], 1):
            _render_turn_for_seed(out, turn, mode=mode, index=i)
        if turns:
            out.append("### Final exchange (verbatim)")
            out.append("")
            _render_turn_for_seed(out, turns[-1], mode="full", index=n_user)

    out.append("---")
    out.append("")
    out.append("## Please continue from here")
    out.append("")
    out.append(
        "1. Confirm you've internalised the context above — note the files in "
        "scope, the working directory, and where the conversation left off."
    )
    out.append(
        "2. If you would have done something next in the previous session (a "
        "pending tool call, a follow-up question), surface it now so I can "
        "approve or correct it."
    )
    out.append(
        "3. Otherwise wait for my next instruction; I'll tell you what to "
        "tackle next."
    )
    out.append("")
    out.append("**Important**: any absolute paths in the context above point to the")
    out.append("**original** machine. If a path doesn't exist here, ask me how to")
    out.append("relocate it before reading or writing it.")
    out.append("")
    return "\n".join(out)


def _render_turn_for_seed(out: list[str], turn: dict[str, Any], mode: str, index: int) -> None:
    user_msg: FlatMessage = turn["user"]
    body: list[FlatMessage] = turn["body"]
    user_text = _strip_uploaded_files_wrapper(user_msg.text or "").strip()
    out.append(f"### Turn {index} · {_fmt_ts(user_msg.timestamp)}")
    out.append("")
    out.append("**User:**")
    out.append("")
    out.append("> " + user_text.replace("\n", "\n> ") if user_text else "> _(empty)_")
    out.append("")

    asst_text_blocks = [m for m in body if m.kind == "text" and m.role == "assistant"]
    thinking_blocks = [m for m in body if m.kind == "thinking"]
    tool_uses = [m for m in body if m.kind == "tool_use"]
    tool_results = {m.tool_id: m for m in body if m.kind == "tool_result"}

    if mode == "full":
        for m in body:
            if m.kind == "text" and m.role == "assistant":
                out.append("**Assistant:**")
                out.append("")
                out.append(_demote_headers(m.text) or "_(empty)_")
                out.append("")
            elif m.kind == "thinking":
                out.append("<details><summary>Reasoning</summary>")
                out.append("")
                out.append(m.text or "")
                out.append("")
                out.append("</details>")
                out.append("")
            elif m.kind == "tool_use":
                out.append(f"_Tool call:_ {_summarise_tool_use(m)}")
                tr = tool_results.get(m.tool_id)
                if tr:
                    if tr.is_error:
                        out.append("_Tool error:_")
                    else:
                        out.append("_Tool result:_")
                    out.append("")
                    out.append("```")
                    out.append(_summarise_tool_result(tr))
                    out.append("```")
                out.append("")
    elif mode == "standard":
        if asst_text_blocks:
            joined = "\n\n".join(b.text or "" for b in asst_text_blocks).strip()
            out.append("**Assistant** (abridged):")
            out.append("")
            out.append(_demote_headers(_truncate(joined, SEED_TEXT_TRUNCATE)) or "_(no textual reply)_")
            out.append("")
        if tool_uses:
            out.append(f"_Tool calls in this turn: {len(tool_uses)}_")
            for m in tool_uses[:6]:
                tr = tool_results.get(m.tool_id)
                marker = " ❌" if tr and tr.is_error else ""
                out.append(f"- {_summarise_tool_use(m)}{marker}")
            if len(tool_uses) > 6:
                out.append(f"- … +{len(tool_uses) - 6} more")
            out.append("")


def cmd_seed(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser().resolve()
    if not bundle.is_dir():
        print(f"error: bundle directory does not exist: {bundle}", file=sys.stderr)
        return 2
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        print(
            f"error: not a valid bundle (missing manifest.json): {bundle}\n"
            "       Bundles produced before tool 0.2.0 lack a manifest and cannot "
            "be seeded. Re-export first.",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: failed to parse manifest.json: {e}", file=sys.stderr)
        return 2
    if manifest.get("tool") != "claude-code-export":
        print(
            f"error: bundle was produced by {manifest.get('tool')!r}, expected "
            "'claude-code-export'.",
            file=sys.stderr,
        )
        return 2

    transcript = bundle / "transcript.jsonl"
    if not transcript.exists():
        print(f"error: bundle missing transcript.jsonl: {transcript}", file=sys.stderr)
        return 2

    meta, raw = load_transcript(transcript)
    meta.task_id = manifest.get("source_session_id") or meta.task_id or meta.cli_session_id
    if not meta.title:
        meta.title = ""  # may stay empty; renderer handles it
    if not meta.cwd:
        meta.cwd = manifest.get("source_cwd", "") or meta.cwd
    flat = flatten(raw)
    files = _list_bundle_files(bundle)

    mode = args.mode
    seed_md = render_seed_prompt(meta, flat, files, mode, bundle)

    out_path = (
        Path(args.output).expanduser().resolve()
        if args.output else (bundle / "seed-prompt.md")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(seed_md, encoding="utf-8")
    print(f"wrote {out_path}  ({len(seed_md)} chars, mode={mode})")
    print()
    print("Use:")
    print("  1. Open a fresh Claude / Cowork chat under the new account / machine.")
    print(f"  2. Paste the contents of {out_path} as your first message.")
    print("  3. Wait for the assistant to confirm context absorption.")
    print("  4. Continue working as usual.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude_code_export",
        description="Export Claude Code CLI sessions to HTML, Markdown, JSON, CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              claude_code_export.py list
              claude_code_export.py list --project ~/code/foo
              claude_code_export.py export latest
              claude_code_export.py export <session-id> --output ./exports
              claude_code_export.py export all --formats html,json
        """),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--code-root",
        default=None,
        metavar="PATH",
        help=(
            "override the auto-detected Claude Code sessions directory "
            f"(default: {CODE_ROOT}). Useful for archived backups or alt installs."
        ),
    )
    common.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="filter to sessions whose cwd matches PATH (prefix match).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "list", parents=[common], help="list available sessions, grouped by project"
    ).set_defaults(func=cmd_list)

    pe = sub.add_parser("export", parents=[common], help="export one or more sessions")
    pe.add_argument("session", help="session id (prefix), 'latest', or 'all'")
    pe.add_argument(
        "-o", "--output", "--out",
        dest="output",
        default=str(DEFAULT_OUTPUT),
        metavar="DIR",
        help=f"directory to write export bundles into (default: {DEFAULT_OUTPUT})",
    )
    pe.add_argument(
        "--formats",
        default=",".join(SUPPORTED_FORMATS),
        help=f"comma-separated subset of {','.join(SUPPORTED_FORMATS)} (default: all)",
    )
    pe.add_argument("--no-files", action="store_true",
                    help="skip copying touched files and the project CLAUDE.md")
    pe.add_argument(
        "--include-auth", action="store_true",
        help=(
            "HIGH RISK: also include your Anthropic credentials "
            "(~/.claude/.credentials.json) in the bundle so a matching import "
            "can resume sessions without re-logging in. Anyone with the bundle "
            "can act as your account. Interactive 'I UNDERSTAND' prompt by default."
        ),
    )
    pe.add_argument(
        "--yes-i-know-this-is-risky", action="store_true",
        help=(
            "skip the interactive 'I UNDERSTAND' prompt for --include-auth. "
            "Only meaningful together with --include-auth."
        ),
    )
    pe.set_defaults(func=cmd_export)

    pi = sub.add_parser(
        "import",
        help="restore a previously exported bundle into ~/.claude/projects/",
        description=(
            "Restore a bundle produced by `export` into the local Claude Code "
            "store. Cross-platform import requires --cwd to be supplied."
        ),
    )
    pi.add_argument("bundle", help="path to an exported bundle directory")
    pi.add_argument(
        "--cwd", default=None, metavar="PATH",
        help=(
            "target cwd on this machine. Required for cross-platform import "
            "(macOS↔Windows); optional otherwise (defaults to manifest.source_cwd)."
        ),
    )
    pi.add_argument(
        "--code-root", default=None, metavar="PATH",
        help=f"override target ~/.claude/projects/ root (default: {CODE_ROOT}).",
    )
    pi.add_argument(
        "--skip-auth", action="store_true",
        help="ignore bundle/auth/ even if present; do not touch local credentials.",
    )
    pi.add_argument(
        "--dry-run", action="store_true",
        help="print the rewrite plan and exit without writing.",
    )
    pi.add_argument(
        "--force", action="store_true",
        help="overwrite existing transcript / restored files / credentials.",
    )
    pi.set_defaults(func=cmd_import)

    ps = sub.add_parser(
        "seed",
        help="render seed-prompt.md from a bundle for cross-account continuation",
        description=(
            "Produce a self-contained Markdown prompt that can be pasted as the "
            "first message of a brand-new Claude conversation (potentially under "
            "a different account / machine) to continue the work from where the "
            "previous session left off. This does not touch any auth and does "
            "not rely on server-side state — the new conversation is a fresh "
            "session with the prior session's context inlined."
        ),
    )
    ps.add_argument("bundle", help="path to an exported bundle directory")
    ps.add_argument(
        "--mode", default="standard", choices=("brief", "standard", "full"),
        help=(
            "how much of the prior conversation to include. brief = last 3 turns; "
            "standard (default) = all user prompts + abridged assistant text + "
            "tool-call summaries + last turn verbatim; full = everything verbatim."
        ),
    )
    ps.add_argument(
        "-o", "--output", "--out", dest="output", default=None, metavar="PATH",
        help="where to write the seed prompt (default: <bundle>/seed-prompt.md)",
    )
    ps.set_defaults(func=cmd_seed)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
