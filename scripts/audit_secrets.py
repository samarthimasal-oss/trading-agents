"""Scan git history and tracked files for common credential patterns.

Usage:
    python scripts/audit_secrets.py

Exit code 0 means no pattern matched and no sensitive file is tracked.  The
script prints only the pattern name and commit prefix; matched values are never
echoed.
"""
from __future__ import annotations

import os
import re
import subprocess

PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "stripe_live_key": re.compile(r"sk_live_[A-Za-z0-9]{20,}"),
    "telegram_bot_token": re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "private_key": re.compile(r"BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY"),
}

SENSITIVE_EXACT = {
    "config.json", "ea_token.txt", "token.txt", "dashboard_token.txt",
}
SENSITIVE_PREFIXES = ("state/", "backups/")
SENSITIVE_SUFFIXES = (".env", ".log", ".sqlite", ".sqlite3", ".pkl")


def scan_diff(text: str) -> list[dict]:
    """Return pattern hits on added diff lines, without the matched value."""
    hits: list[dict] = []
    commit = None
    for line in text.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:12]
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(line):
                hits.append({"commit": commit, "pattern": name})
    return hits


def scan_text(text: str) -> list[str]:
    """Return pattern names found in plain text, without the matched value."""
    return [name for name, pattern in PATTERNS.items()
            if pattern.search(text)]


def local_logs(root: str = ".") -> list[str]:
    """Local ``.log`` files, excluding dependencies and VCS internals."""
    found = []
    skip = {".git", "node_modules", "vendors", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip]
        for filename in filenames:
            if filename.endswith(".log"):
                found.append(os.path.join(dirpath, filename))
    return sorted(found)


def sensitive_tracked(paths: list[str]) -> list[str]:
    """Tracked paths that must never be in the repository."""
    found = []
    for raw in paths:
        path = str(raw).replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if (path in SENSITIVE_EXACT or name in SENSITIVE_EXACT
                or path.startswith(SENSITIVE_PREFIXES)
                or name.startswith("config.json.bak")
                or name.startswith(".env.")
                or name.endswith(SENSITIVE_SUFFIXES)):
            found.append(path)
    return sorted(set(found))


def _run(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or "git command failed")
    return completed.stdout


def main() -> int:
    history = _run("log", "-p", "--all", "--", ".")
    hits = scan_diff(history)
    tracked_paths = _run("ls-files").splitlines()
    tracked = sensitive_tracked(tracked_paths)
    commits = _run("rev-list", "--all", "--count").strip()

    log_hits = []
    logs = local_logs(".")
    for path in logs:
        try:
            if os.path.getsize(path) > 10 * 1024 * 1024:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                for pattern in scan_text(handle.read()):
                    log_hits.append({"file": path, "pattern": pattern})
        except OSError:
            continue

    print(f"scanned {commits} commits and {len(tracked_paths)} tracked files")
    if hits:
        print(f"credential-pattern hits in git history: {len(hits)}")
        for hit in hits:
            print(f"  {hit['commit']}  {hit['pattern']}")
    else:
        print("credential-pattern hits in git history: 0")
    if tracked:
        print(f"tracked sensitive files: {len(tracked)}")
        for path in tracked:
            print(f"  {path}")
    else:
        print("tracked sensitive files: 0")
    print(f"local logs scanned: {len(logs)}")
    if log_hits:
        print(f"credential-pattern hits in logs: {len(log_hits)}")
        for hit in log_hits:
            print(f"  {hit['file']}  {hit['pattern']}")
    else:
        print("credential-pattern hits in logs: 0")
    return 1 if hits or tracked or log_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
