"""Session discovery and I/O for Claude Code JSONL files."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .types import Message


def get_claude_dir() -> Path:
    """Return the Claude configuration directory."""
    return Path.home() / ".claude"


def get_projects_dir() -> Path:
    """Return the Claude projects directory."""
    return get_claude_dir() / "projects"


def find_project_dirs(project_filter: str | None = None) -> list[Path]:
    """Find project directories, optionally filtered by name."""
    projects = get_projects_dir()
    if not projects.exists():
        return []
    dirs = sorted(projects.iterdir())
    if project_filter:
        dirs = [d for d in dirs if project_filter.lower() in d.name.lower()]
    return [d for d in dirs if d.is_dir()]


def find_sessions(project_filter: str | None = None) -> list[dict]:
    """Find all JSONL session files with metadata."""
    sessions = []
    for proj_dir in find_project_dirs(project_filter):
        for f in sorted(proj_dir.glob("*.jsonl")):
            if ".jsonl.bak" in f.name or f.name.endswith(".bak"):
                continue
            size = f.stat().st_size
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            session_id = f.stem
            line_count = 0
            with open(f, "r") as fh:
                for _ in fh:
                    line_count += 1
            sessions.append({
                "path": f,
                "project": proj_dir.name,
                "session_id": session_id,
                "size": size,
                "mtime": mtime,
                "lines": line_count,
            })
    return sessions


def resolve_session(session_arg: str, project_filter: str | None = None) -> Path:
    """Resolve a session argument to a JSONL file path.

    Accepts: full path, UUID, or UUID prefix.
    """
    p = Path(session_arg)
    if p.exists() and p.suffix == ".jsonl":
        return p

    for sess in find_sessions(project_filter):
        if sess["session_id"] == session_arg:
            return sess["path"]
        if sess["session_id"].startswith(session_arg):
            return sess["path"]

    print(f"Error: Cannot find session '{session_arg}'", file=sys.stderr)
    print("Use 'cozempic list' to see available sessions.", file=sys.stderr)
    sys.exit(1)


def load_messages(path: Path) -> list[Message]:
    """Load JSONL file. Returns list of (line_index, message_dict, byte_size)."""
    messages: list[Message] = []
    with open(path, "r") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                messages.append((i, msg, len(line.encode("utf-8"))))
            except json.JSONDecodeError:
                messages.append((i, {"_raw": line, "_parse_error": True}, len(line.encode("utf-8"))))
    return messages


def save_messages(
    path: Path,
    messages: list[Message],
    create_backup: bool = True,
) -> Path | None:
    """Save messages back to JSONL, optionally creating a timestamped backup.

    Returns the backup path if created, else None.
    """
    backup_path = None
    if create_backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_suffix(f".{ts}.jsonl.bak")
        shutil.copy2(path, backup_path)

    with open(path, "w") as f:
        for _, msg, _ in messages:
            if msg.get("_parse_error"):
                f.write(msg["_raw"] + "\n")
            else:
                f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    return backup_path
