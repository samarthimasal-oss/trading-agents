"""Reproducibility manifests for research runs.

A result is only useful if the inputs behind it can be identified later.  This
module records compact hashes for the things that change a replay:

* the bar data actually replayed (content hash + cached CSV hash);
* the config content (hash only -- config.json may contain API keys);
* the code (git commit, dirty flag, and a hash of the source tree);
* the cost assumptions, split and run parameters;
* Python/NumPy/pandas versions.

Reports embed the manifest.  No absolute local paths, config values or secrets
are written: config is represented only by its SHA-256, and source paths are
relative to the repository root.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time

import numpy as np
import pandas as pd

SCHEMA_VERSION = 1
DATA_COLUMNS = ("time", "open", "high", "low", "close", "spread",
                "tick_volume", "volume")
SOURCE_DIRS = ("agents", "core", "execution", "market", "research",
               "strategies")
SOURCE_FILES = ("main.py", "runner.py")


# ------------------------------------------------------------------ hashing
def sha256_file(path: str) -> str:
    """SHA-256 of a file's bytes, streamed so large caches stay cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_value(value) -> str:
    """Stable SHA-256 of a JSON-like value, independent of key order."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def data_fingerprint(frame: pd.DataFrame) -> str:
    """Content hash of the OHLCV fields that drive a replay.

    Column order and unrelated columns are ignored.  Timestamps keep their
    full ISO-8601 representation, and floats use pandas' lossless text form.
    """
    columns = [column for column in DATA_COLUMNS
               if column in frame.columns]
    if not columns:
        raise ValueError("data frame has none of the OHLCV columns")
    selected = frame[columns].copy()
    if "time" in selected.columns:
        selected["time"] = pd.to_datetime(
            selected["time"], errors="raise").map(
                lambda value: value.isoformat())
    payload = selected.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------- summaries
def _relative(path: str, root: str | None) -> str:
    """Repository-relative POSIX path, or a basename for outside files."""
    if not root:
        return os.path.basename(path)
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        return os.path.basename(path)
    if rel.startswith(".."):
        return os.path.basename(path)
    return rel.replace("\\", "/")


def file_summary(path: str | None, *, root: str | None = None) -> dict:
    """Identify one input file without exposing its absolute location."""
    summary = {"source": _relative(path, root) if path else None,
               "exists": bool(path and os.path.isfile(path)),
               "bytes": None, "sha256": None}
    if summary["exists"]:
        summary["bytes"] = os.path.getsize(path)
        summary["sha256"] = sha256_file(path)
    return summary


def data_summary(frame: pd.DataFrame, *, source_path: str | None = None,
                 root: str | None = None) -> dict:
    """Bars, date range, content hash and optional source-file hash."""
    if frame is None or len(frame) == 0:
        raise ValueError("cannot summarise an empty data frame")
    times = (pd.to_datetime(frame["time"], errors="raise")
             if "time" in frame.columns else None)
    summary = {
        "bars": int(len(frame)),
        "first": times.iloc[0].isoformat() if times is not None else None,
        "last": times.iloc[-1].isoformat() if times is not None else None,
        "sha256": data_fingerprint(frame),
    }
    source = file_summary(source_path, root=root)
    summary["source"] = source["source"]
    summary["source_exists"] = source["exists"]
    summary["source_bytes"] = source["bytes"]
    summary["source_sha256"] = source["sha256"]
    return summary


def config_summary(config: dict) -> dict:
    """Hash the whole config but never serialise its values."""
    return {
        "sha256": sha256_value(config),
        "top_level_keys": sorted(str(key) for key in (config or {})),
    }


def environment_summary() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }


# --------------------------------------------------------------------- code
def _git_output(root: str, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True,
            timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _source_digest(root: str) -> tuple[str, int]:
    """Hash the Python source tree, including uncommitted changes."""
    digest = hashlib.sha256()
    paths: list[str] = []
    for name in SOURCE_DIRS:
        base = os.path.join(root, name)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                directory for directory in dirnames
                if directory != "__pycache__")
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    paths.append(os.path.join(dirpath, filename))
    for name in SOURCE_FILES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            paths.append(path)
    for path in sorted(set(paths)):
        digest.update(_relative(path, root).encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest(), len(set(paths))


def code_summary(root: str) -> dict:
    """Git identity plus a source hash that also covers dirty trees."""
    status = _git_output(root, "status", "--porcelain")
    source_sha, source_files = _source_digest(root)
    return {
        "commit": _git_output(root, "rev-parse", "HEAD"),
        "dirty": bool(status) if status is not None else None,
        "dirty_files": len(status.splitlines()) if status else 0,
        "source_sha256": source_sha,
        "source_files": source_files,
    }


# ------------------------------------------------------------------- report
def build_manifest(*, timeframe: str, symbols: list[str], data: dict,
                   config: dict, root: str | None = None,
                   costs: dict | None = None, split: dict | None = None,
                   parameters: dict | None = None,
                   inputs: dict | None = None) -> dict:
    """Build the manifest embedded in a research report."""
    base = os.path.abspath(root or os.getcwd())
    return {
        "schema": SCHEMA_VERSION,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timeframe": str(timeframe).upper(),
        "symbols": [str(symbol).upper() for symbol in symbols],
        "parameters": parameters or {},
        "config": config_summary(config),
        "code": code_summary(base),
        "environment": environment_summary(),
        "costs": costs,
        "costs_sha256": sha256_value(costs) if costs is not None else None,
        "split": split,
        "data": {str(symbol).upper(): summary
                 for symbol, summary in (data or {}).items()},
        "inputs": inputs or {},
    }
