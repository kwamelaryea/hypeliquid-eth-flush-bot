#!/usr/bin/env python3
"""Deterministic current-tree and full-Git-object privacy/secret scan."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
# Digests preserve deterministic owner-specific detection without storing or
# printing the sensitive identifiers themselves.
OWNER_WALLET_DIGEST = "e0c658eca29fb934e1b2494c1cd2360cd6f77e9733113cc6b4dd828d2e5db8c6"
ADDRESS_PATTERN = re.compile(r"(?i)\b0x[a-f0-9]{40}\b")
PRIVATE_PRODUCTION_ID_DIGEST = "c57f7e56e9fb1929c0abdd874063d12b3148893ebee22fa2dbe1a6ea57dd39cb"
IDENTIFIER_PATTERN = re.compile(r"(?i)\b[a-z0-9][a-z0-9-]{5,}\b")

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email address", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    ("private macOS path", re.compile(r"/" + r"Users/[A-Za-z0-9._-]+(?:/[^\s'\"<>]*)?")),
    ("production host", re.compile(r"(?i)\.fly" + r"\.dev\b")),
    ("private key material", re.compile(r"(?i)\b0x[a-f0-9]{64}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("token-like value", re.compile(
        r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"
    )),
    ("assigned secret literal", re.compile(
        r"(?i)\b(?:private_key|api_key|api_token|access_token|secret|password)\b\s*[:=]\s*['\"][^'\"$<{][^'\"]{11,}['\"]"
    )),
)


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(
        ["git", *args], cwd=root, input=input_bytes, stderr=subprocess.DEVNULL
    )


def is_git_repository(root: Path = ROOT) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def tracked_files(root: Path = ROOT) -> list[Path]:
    output = _git(root, "ls-files", "-z")
    return [root / item.decode() for item in output.split(b"\0") if item]


def _scan_text(text: str, source: str) -> list[str]:
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for address_match in ADDRESS_PATTERN.finditer(line):
            digest = hashlib.sha256(address_match.group(0).lower().encode()).hexdigest()
            if digest == OWNER_WALLET_DIGEST:
                findings.append(f"{source}:{line_number}: known owner wallet")
        for identifier_match in IDENTIFIER_PATTERN.finditer(line):
            digest = hashlib.sha256(identifier_match.group(0).lower().encode()).hexdigest()
            if digest == PRIVATE_PRODUCTION_ID_DIGEST:
                findings.append(f"{source}:{line_number}: known private production id")
        for rule_name, pattern in RULES:
            if pattern.search(line):
                findings.append(f"{source}:{line_number}: {rule_name}")
    return findings


def scan_paths(paths: Iterable[Path], root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if b"\0" in raw:
            continue
        try:
            shown_path = path.relative_to(root)
        except ValueError:
            shown_path = path
        findings.extend(_scan_text(raw.decode("utf-8", errors="replace"), str(shown_path)))
    return findings


def scan_git_history(root: Path = ROOT) -> tuple[list[str], int, int]:
    """Scan every local ref name plus every blob/commit/tag object.

    Findings identify only category, object digest prefix, and line number; the
    matching value and surrounding content are never emitted.
    """
    if not is_git_repository(root):
        return [], 0, 0

    findings: list[str] = []
    refs = [item for item in _git(root, "for-each-ref", "--format=%(refname)").decode(
        "utf-8", errors="replace"
    ).splitlines() if item]
    for index, ref_name in enumerate(refs, 1):
        findings.extend(_scan_text(ref_name, f"git-ref:{index}"))

    lines = _git(
        root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    ).decode("ascii", errors="replace").splitlines()
    objects_scanned = 0
    for entry in lines:
        parts = entry.split()
        if len(parts) != 2 or parts[1] not in {"blob", "commit", "tag"}:
            continue
        object_id, object_type = parts
        raw = _git(root, "cat-file", "-p", object_id)
        source = f"git-object:{object_type}:{object_id[:12]}"
        findings.extend(_scan_text(raw.decode("utf-8", errors="replace"), source))
        objects_scanned += 1
    return findings, len(refs), objects_scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="optional files to scan")
    parser.add_argument(
        "--working-tree-only", action="store_true",
        help="skip Git refs/object history (not sufficient for a publication gate)",
    )
    args = parser.parse_args(argv)
    paths = [path.resolve() for path in args.paths] if args.paths else tracked_files()
    findings = scan_paths(paths)
    refs_scanned = objects_scanned = 0
    if not args.paths and not args.working_tree_only and is_git_repository(ROOT):
        history_findings, refs_scanned, objects_scanned = scan_git_history(ROOT)
        findings.extend(history_findings)
    if findings:
        print("Privacy scan failed (matching values are intentionally redacted):")
        print("\n".join(findings))
        return 1
    print(
        f"Privacy scan passed ({len(paths)} tracked files, {refs_scanned} refs, "
        f"{objects_scanned} Git blob/metadata objects checked)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
