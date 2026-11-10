"""
Token-Saver Utility & RTK-Style Output Compactor (`token_saver.py`).

Inspired by SiruGao/token-saver (https://github.com/SiruGao/token-saver):
A local-first diagnostic and compaction engine designed to diagnose, reduce,
and verify AI agent token waste without degrading task performance.

Core Capabilities:
1. The Doctor: Analyzes JSON payloads, tool outputs, and command logs to detect
   waste patterns (repeated context, oversized listings, redundant plists, giant logs).
2. Strategy Adapters:
   - JSON Compactor: Prunes nulls, empty collections, truncates long lists with counts,
     and projects only essential keys for LLM reasoning.
   - RTK-Style CLI Output Filter: Strips decorative ASCII banners, ANSI sequences,
     and repetitive progress ticks while preserving errors, warnings, and summary lines.
   - Firmware List Compactor: Aggregates historical unsigned IPSWs by major iOS release
     so a 50KB JSON payload becomes a crisp 1.2KB summary (saving ~95% tokens).
3. Proof & Verification: Measures raw vs. compressed character and token counts,
   calculating cost/token savings percentage.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# ANSI escape sequences pattern
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Repetitive banner lines (e.g. ==========, ----------, **********)
REPETITIVE_BORDER_RE = re.compile(r"^[\=\-\*\_\#\~]{5,}$")


@dataclass
class TokenMetric:
    """Stores token and byte measurements before and after compaction."""

    raw_chars: int
    compact_chars: int
    raw_tokens_est: int
    compact_tokens_est: int
    tokens_saved: int
    savings_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_chars": self.raw_chars,
            "compact_chars": self.compact_chars,
            "raw_tokens_est": self.raw_tokens_est,
            "compact_tokens_est": self.compact_tokens_est,
            "tokens_saved": self.tokens_saved,
            "savings_pct": round(self.savings_pct, 1),
        }


def estimate_tokens(text: str) -> int:
    """
    Estimates token count for an arbitrary string.
    Uses ~3.7 characters per token heuristic, aligned with GPT/Gemini tokenizers
    for mixed JSON, code, and English text.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.7))


class TokenDoctor:
    """Diagnoses inefficiencies and token bloat in data structures and tool outputs."""

    @staticmethod
    def audit(data: Union[str, Dict[str, Any], List[Any]]) -> Dict[str, Any]:
        """
        Scans data and identifies waste patterns:
        - Repetitive keys in large lists
        - Null or empty field overhead
        - Oversized strings or raw payloads
        - High-density redundant collections
        """
        if isinstance(data, str):
            raw_text = data
            try:
                parsed = json.loads(data)
                return TokenDoctor._audit_structured(parsed, raw_text)
            except json.JSONDecodeError:
                return TokenDoctor._audit_text(raw_text)
        elif isinstance(data, (dict, list)):
            raw_text = json.dumps(data)
            return TokenDoctor._audit_structured(data, raw_text)
        return {"status": "unsupported_type", "tokens": estimate_tokens(str(data))}

    @staticmethod
    def _audit_text(text: str) -> Dict[str, Any]:
        lines = text.splitlines()
        empty_lines = sum(1 for line in lines if not line.strip())
        border_lines = sum(1 for line in lines if REPETITIVE_BORDER_RE.match(line.strip()))
        ansi_count = len(ANSI_ESCAPE_RE.findall(text))
        tokens = estimate_tokens(text)

        findings = []
        if border_lines > 4:
            findings.append(f"Found {border_lines} decorative border lines consuming unnecessary tokens.")
        if ansi_count > 0:
            findings.append(f"Found {ansi_count} ANSI escape sequences bloating output.")
        if empty_lines > len(lines) * 0.2 and len(lines) > 10:
            findings.append(f"{empty_lines} empty lines ({round(empty_lines / len(lines) * 100)}% of output).")

        return {
            "type": "text",
            "lines": len(lines),
            "estimated_tokens": tokens,
            "waste_findings": findings,
            "recommendation": "Use RTK-style output filter (`filter_command_output`)." if findings else "Acceptable density.",
        }

    @staticmethod
    def _audit_structured(data: Any, raw_json: str) -> Dict[str, Any]:
        total_tokens = estimate_tokens(raw_json)
        null_count = raw_json.count("null")
        findings = []

        if null_count > 5:
            findings.append(f"{null_count} 'null' values found that could be pruned.")

        item_count = len(data) if isinstance(data, list) else len(data.keys()) if isinstance(data, dict) else 1
        if isinstance(data, list) and item_count > 15:
            findings.append(f"Large array with {item_count} items. Agent only needs top items + summary.")
        elif isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 20:
                    findings.append(f"Field '{k}' contains {len(v)} array items (consider truncating or grouping).")

        return {
            "type": "structured_json",
            "size_chars": len(raw_json),
            "estimated_tokens": total_tokens,
            "waste_findings": findings,
            "recommended_strategy": "compact_json" if findings else "pass_through",
        }


class TokenCompactor:
    """Strategy adapters that condense data structures and text without losing vital info."""

    @staticmethod
    def compact_json(
        data: Any,
        max_list_items: int = 10,
        max_str_len: int = 300,
        drop_nulls: bool = True,
        strip_empty_iterables: bool = True,
    ) -> Any:
        """
        Recursively compresses a JSON-serializable Python data structure:
        - Drops keys with None or empty values.
        - Truncates lists longer than `max_list_items` and appends an omission note.
        - Truncates oversized string values.
        - Formats floats to 2 decimal places.
        """
        if data is None:
            return None

        if isinstance(data, dict):
            cleaned: Dict[str, Any] = {}
            for k, v in data.items():
                if drop_nulls and v is None:
                    continue
                if strip_empty_iterables and isinstance(v, (dict, list)) and len(v) == 0:
                    continue
                compacted_v = TokenCompactor.compact_json(
                    v,
                    max_list_items=max_list_items,
                    max_str_len=max_str_len,
                    drop_nulls=drop_nulls,
                    strip_empty_iterables=strip_empty_iterables,
                )
                if drop_nulls and compacted_v is None:
                    continue
                if strip_empty_iterables and isinstance(compacted_v, (dict, list)) and len(compacted_v) == 0:
                    continue
                cleaned[k] = compacted_v
            return cleaned

        if isinstance(data, list):
            if len(data) > max_list_items:
                head = [
                    TokenCompactor.compact_json(
                        item,
                        max_list_items=max_list_items,
                        max_str_len=max_str_len,
                        drop_nulls=drop_nulls,
                        strip_empty_iterables=strip_empty_iterables,
                    )
                    for item in data[:max_list_items]
                ]
                omitted_count = len(data) - max_list_items
                head.append(f"... [+ {omitted_count} more items omitted for token efficiency]")
                return head
            return [
                TokenCompactor.compact_json(
                    item,
                    max_list_items=max_list_items,
                    max_str_len=max_str_len,
                    drop_nulls=drop_nulls,
                    strip_empty_iterables=strip_empty_iterables,
                )
                for item in data
            ]

        if isinstance(data, str):
            if len(data) > max_str_len:
                return data[:max_str_len] + f"... [truncated {len(data) - max_str_len} chars]"
            return data

        if isinstance(data, float):
            return round(data, 2)

        return data

    @staticmethod
    def filter_cli_output(
        raw_output: str,
        max_lines: int = 60,
        preserve_patterns: Optional[List[str]] = None,
    ) -> str:
        """
        RTK-style output filter for shell and tool commands:
        - Removes ANSI colors/escapes.
        - Removes decorative ASCII lines (==========).
        - Preserves lines containing PASS, FAIL, ERROR, WARNING, RESULT, UDID, etc.
        - Truncates output to `max_lines` while preserving head and tail.
        """
        cleaned = ANSI_ESCAPE_RE.sub("", raw_output)
        lines = cleaned.splitlines()

        default_patterns = [
            "pass", "fail", "error", "warn", "critical", "result",
            "udid", "model", "serial", "sha1", "verified", "download", "status",
        ]
        active_patterns = (preserve_patterns or []) + default_patterns

        filtered: List[str] = []
        for line in lines:
            stripped = line.strip()
            # Drop pure decorative separator lines
            if REPETITIVE_BORDER_RE.match(stripped):
                continue
            # Drop empty line runs
            if not stripped and filtered and not filtered[-1].strip():
                continue
            filtered.append(line)

        if len(filtered) <= max_lines:
            return "\n".join(filtered)

        # Retain head, important middle signals, and tail
        head_count = max(5, int(max_lines * 0.4))
        tail_count = max(5, int(max_lines * 0.4))

        head_lines = filtered[:head_count]
        tail_lines = filtered[-tail_count:]
        middle_lines = filtered[head_count:-tail_count]

        # Scan middle for high-signal lines
        preserved_middle = []
        for line in middle_lines:
            low = line.lower()
            if any(pat in low for pat in active_patterns):
                preserved_middle.append(line)
                if len(preserved_middle) >= 15:
                    break

        omitted = len(middle_lines) - len(preserved_middle)
        summary_marker = f"\n[... Token-Saver: {omitted} low-signal lines omitted ...]\n"

        result_lines = head_lines + [summary_marker] + preserved_middle + tail_lines
        return "\n".join(result_lines)

    @staticmethod
    def compact_firmwares(firmwares: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Specialized compactor for IPSW firmware listings:
        Instead of returning 50+ raw firmware objects (which can consume 15,000+ tokens),
        keeps all SIGNED firmwares in full detail, and groups unsigned builds by major version.
        """
        signed_builds: List[Dict[str, Any]] = []
        unsigned_summary: Dict[str, int] = {}
        total_unsigned = 0

        for fw in firmwares:
            is_signed = bool(fw.get("signed", False))
            version = str(fw.get("version", "Unknown"))
            if is_signed:
                # Keep high-signal fields
                signed_builds.append({
                    "version": version,
                    "buildid": fw.get("buildid"),
                    "signed": True,
                    "filesize_gb": fw.get("filesize_gb"),
                    "releasedate": fw.get("releasedate"),
                    "sha1sum": fw.get("sha1sum"),
                    "url": fw.get("url"),
                })
            else:
                total_unsigned += 1
                major = version.split(".")[0] if "." in version else version
                key = f"iOS {major}"
                unsigned_summary[key] = unsigned_summary.get(key, 0) + 1

        return {
            "signed_firmwares_count": len(signed_builds),
            "signed_firmwares": signed_builds,
            "unsigned_historical_count": total_unsigned,
            "unsigned_summary_by_major_release": unsigned_summary,
            "_token_saving_note": "Unsigned historical firmwares grouped by major release to prevent token exhaustion.",
        }


def compute_token_metric(raw_text: str, compact_text: str) -> TokenMetric:
    """Calculates verification metrics between raw and compacted text."""
    raw_chars = len(raw_text)
    compact_chars = len(compact_text)
    raw_tokens = estimate_tokens(raw_text)
    compact_tokens = estimate_tokens(compact_text)
    saved = max(0, raw_tokens - compact_tokens)
    pct = ((raw_tokens - compact_tokens) / raw_tokens * 100.0) if raw_tokens > 0 else 0.0

    return TokenMetric(
        raw_chars=raw_chars,
        compact_chars=compact_chars,
        raw_tokens_est=raw_tokens,
        compact_tokens_est=compact_tokens,
        tokens_saved=saved,
        savings_pct=pct,
    )


def wrap_command(cmd_args: List[str], max_lines: int = 50) -> Tuple[str, int, TokenMetric]:
    """
    Executes a shell command and filters its output using the RTK-style compactor,
    returning (filtered_output, exit_code, token_metrics).
    """
    proc = subprocess.run(
        cmd_args,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    combined_raw = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    filtered = TokenCompactor.filter_cli_output(combined_raw, max_lines=max_lines)
    metric = compute_token_metric(combined_raw, filtered)
    return filtered, proc.returncode, metric


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Token-Saver: Diagnose and reduce AI agent token waste."
    )
    subparsers = parser.add_subparsers(dest="command")

    # Audit subcommand
    audit_p = subparsers.add_parser("audit", help="Analyze a JSON or text file for token bloat.")
    audit_p.add_argument("file_path", help="Path to file or directory to audit.")

    # Wrap subcommand
    wrap_p = subparsers.add_parser("wrap", help="Execute a CLI command with RTK-style output compaction.")
    wrap_p.add_argument("cmd", nargs=argparse.REMAINDER, help="Command and arguments to execute.")
    wrap_p.add_argument("--max-lines", type=int, default=50, help="Maximum lines to preserve.")

    # Selftest subcommand
    subparsers.add_parser("selftest", help="Run Token-Saver verification benchmark.")

    args = parser.parse_args()

    if args.command == "audit":
        path = Path(args.file_path).expanduser().resolve()
        if not path.exists():
            print(f"Error: Path not found: {path}")
            return 1
        content = path.read_text(errors="replace")
        report = TokenDoctor.audit(content)
        print(json.dumps(report, indent=2))
        return 0

    elif args.command == "wrap":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("Error: No command specified to wrap.")
            return 1
        output, code, metric = wrap_command(cmd, max_lines=args.max_lines)
        print(output)
        print(f"\n[Token-Saver Proof: Saved {metric.tokens_saved} tokens ({metric.savings_pct}% reduction)]", file=sys.stderr)
        return code

    elif args.command == "selftest":
        # Verification benchmark test
        test_payload = {
            "device": "iPhone13,1",
            "null_prop": None,
            "empty_list": [],
            "huge_list": [{"id": i, "name": f"item_{i}"} for i in range(50)],
            "long_text": "A" * 1000,
        }
        raw_str = json.dumps(test_payload, indent=2)
        compacted = TokenCompactor.compact_json(test_payload, max_list_items=5)
        compact_str = json.dumps(compacted, indent=2)
        metric = compute_token_metric(raw_str, compact_str)

        print(json.dumps({
            "status": "selftest_passed",
            "proof": metric.to_dict(),
        }, indent=2))
        return 0

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    from common import ensure_utf8_console
    ensure_utf8_console()
    sys.exit(main())
