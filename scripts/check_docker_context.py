#!/usr/bin/env python3
"""Fail unless local/private artifacts are excluded from Docker's COPY context."""

from __future__ import annotations

import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_CONTEXT_PATHS = (
    ".git/config",
    ".github/workflows/ci.yml",
    ".env",
    ".env.local",
    ".venv/pyvenv.cfg",
    "data/trades.log",
    "logs/trading.log",
    "preview_server.py",
    "daily_review.sh",
    "test_action_items.py",
    "test_fixes.py",
    "weekly_review_proposal-private.md",
    "eval-review-private.html",
)


def _matches(path: str, pattern: str) -> bool:
    path = path.strip("/")
    pattern = pattern.strip("/")
    if not pattern:
        return False
    if "/" not in pattern:
        return any(fnmatch.fnmatch(part, pattern) for part in path.split("/"))
    return fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("/") + "/")


def docker_path_is_ignored(path: str, rules: list[str]) -> bool:
    ignored = False
    for raw_rule in rules:
        rule = raw_rule.strip()
        if not rule or rule.startswith("#"):
            continue
        negate = rule.startswith("!")
        if negate:
            rule = rule[1:]
        if _matches(path, rule):
            ignored = not negate
    return ignored


def check(root: Path = ROOT) -> list[str]:
    rules = (root / ".dockerignore").read_text().splitlines()
    failures = [path for path in SENSITIVE_CONTEXT_PATHS if not docker_path_is_ignored(path, rules)]
    dockerfile = (root / "Dockerfile").read_text()
    if "COPY --chown=bot:bot . ." in dockerfile or "COPY . ." in dockerfile:
        return failures
    # Keep the check useful if COPY changes: private paths still must be ignored.
    return failures


def main() -> int:
    failures = check()
    if failures:
        print("Docker context check failed for: " + ", ".join(failures))
        return 1
    print(f"Docker context check passed ({len(SENSITIVE_CONTEXT_PATHS)} sensitive path cases excluded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
