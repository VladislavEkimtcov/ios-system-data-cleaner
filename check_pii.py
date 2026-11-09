#!/usr/bin/env python3
"""
PII / secret guard (`check_pii.py`).

Scans tracked source and docs for identifier-shaped strings that must never be committed:
real device UDIDs, Wi-Fi MACs, absolute home-directory paths, and any extra literals listed
in a local, uncommitted .pii-forbidden file. Synthetic fixtures and the shipped *.example.*
samples are intentionally allowed.

Exit code 0 = clean, 1 = potential leak found. Used by CI and by contributors:

    python3 check_pii.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Files/dirs that are allowed to contain synthetic identifier-shaped values.
SKIP_DIRS = {".git", "tests", "mvt_reports", "__pycache__", "tools", "simulator_apps"}
SKIP_FILES = {"check_pii.py"}
SKIP_SUFFIX_CONTAINS = (".example.",)

SCAN_EXTS = {".py", ".md", ".json", ".bat", ".cfg", ".toml", ".yml", ".yaml"}

PATTERNS = [
    ("40-hex UDID", re.compile(r"\b[0-9a-f]{40}\b")),
    ("modern UDID", re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b")),
    ("Wi-Fi MAC", re.compile(r"\b([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")),
    ("home path (unix)", re.compile(r"/Users/(?!<|USERNAME|you\b)[a-z][a-z0-9._-]+/")),
    ("home path (windows)", re.compile(r"C:\\\\Users\\\\(?![<%])[A-Za-z]")),
    # Personal literals (names, institutions, account handles) go one per line in a local,
    # uncommitted .pii-forbidden file so the guard itself never re-publishes them.
]

_EXTRA_FORBIDDEN = [
    line.strip() for line in (REPO_ROOT / ".pii-forbidden").read_text().splitlines()
    if line.strip() and not line.startswith("#")
] if (REPO_ROOT / ".pii-forbidden").exists() else []
for _lit in _EXTRA_FORBIDDEN:
    PATTERNS.append((f"forbidden literal", re.compile(re.escape(_lit), re.IGNORECASE)))


def _skip(path: Path) -> bool:
    if path.name in SKIP_FILES:
        return True
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if any(s in path.name for s in SKIP_SUFFIX_CONTAINS):
        return True
    return path.suffix not in SCAN_EXTS


def main() -> int:
    findings = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or _skip(path.relative_to(REPO_ROOT)):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "pii-allow" in line:
                continue
            for label, rx in PATTERNS:
                if rx.search(line):
                    findings.append((path.relative_to(REPO_ROOT), lineno, label, line.strip()[:120]))

    if findings:
        print("❌ Potential PII / secret-shaped strings found:\n")
        for rel, lineno, label, snippet in findings:
            print(f"  {rel}:{lineno}  [{label}]  {snippet}")
        print(f"\n{len(findings)} finding(s). If a value is intentionally synthetic, move it to a "
              "test fixture / *.example.* file or append a '# pii-allow' comment.")
        return 1

    print("✅ PII check clean: no device UDIDs, MACs, home paths, or personal tokens in tracked source.")
    return 0


if __name__ == "__main__":
    # Standalone on purpose (CI runs it before anything else); same fix as
    # common.ensure_utf8_console for legacy Windows consoles.
    if hasattr(sys.stdout, "reconfigure") and (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
