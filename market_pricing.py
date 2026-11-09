#!/usr/bin/env python3
"""
market_pricing.py — Dynamic Secondary Market Valuation & Agent Stimulation Engine

Replaces flat, static hardware cost assumptions ($799.00) with dynamic, live-researched
market replacement valuations (e.g. Swappa, Back Market, eBay Refurbished) and generates
active directives that stimulate autonomous agents to perform online pricing research.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import state_path

REPO_ROOT = Path(__file__).resolve().parent
# Real ledger in the private state dir (never committed). The example is reference only.
MARKET_PRICES_FILE = state_path("market_prices.json")
MARKET_PRICES_EXAMPLE = REPO_ROOT / "market_prices.example.json"

# Hardware Product Type to Commercial Friendly Name
PRODUCT_TYPE_MAP = {
    "iPhone10,1": "iPhone 8",
    "iPhone10,4": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,6": "iPhone X",
    "iPhone11,8": "iPhone XR",
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd generation)",
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,6": "iPhone SE (3rd generation)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
}

# Baseline default fallbacks when no live quotes are present
HEURISTIC_FALLBACKS: Dict[str, Dict[str, Any]] = {
    "iPhone10,4": {
        "friendly_name": "iPhone 8",
        "refurb_replacement_usd": 75.0,
        "retail_upgrade_usd": 799.0,
        "source": "Market Heuristic (Fallback)",
    },
    "iPhone12,8": {
        "friendly_name": "iPhone SE (2nd generation)",
        "refurb_replacement_usd": 95.0,
        "retail_upgrade_usd": 799.0,
        "source": "Market Heuristic (Fallback)",
    },
    "iPhone13,1": {
        "friendly_name": "iPhone 12 mini",
        "refurb_replacement_usd": 155.0,
        "retail_upgrade_usd": 799.0,
        "source": "Market Heuristic (Fallback)",
    },
}

DEFAULT_STALE_DAYS = 14


def get_friendly_name(product_type: str) -> str:
    """Translates ProductType (e.g. 'iPhone12,8') into friendly commercial model name."""
    clean = product_type.strip()
    return PRODUCT_TYPE_MAP.get(clean, clean)


def load_market_prices(prices_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Loads the market pricing ledger. When no real ledger exists yet, returns an empty
    model set so callers fall back to the clearly-labelled HEURISTIC_FALLBACKS rather than
    the shipped example (whose 'LIVE_VERIFIED' rows go stale and would masquerade as live).
    """
    target = prices_file or MARKET_PRICES_FILE
    if not target or not target.exists():
        return {"version": "1.0", "updated_at": "", "models": {}}
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "models" in data:
                return data
            return {"version": "1.0", "updated_at": "", "models": {}}
    except Exception:
        return {"version": "1.0", "updated_at": "", "models": {}}


def save_market_prices(data: Dict[str, Any], prices_file: Optional[Path] = None) -> None:
    """Saves updated market pricing records to disk."""
    target = prices_file or MARKET_PRICES_FILE
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_quote_stale(record: Dict[str, Any], max_days: int = DEFAULT_STALE_DAYS) -> Tuple[bool, str]:
    """Evaluates whether a pricing quotation is stale or requires live research."""
    if not record:
        return True, "No record exists"
    if record.get("confidence") != "LIVE_VERIFIED":
        return True, "Unverified fallback heuristic"
    ts_str = record.get("last_updated")
    if not ts_str:
        return True, "Missing timestamp"
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        if age.days > max_days:
            return True, f"Quote is {age.days} days old (> {max_days}d threshold)"
        return False, f"Fresh ({age.days}d old)"
    except Exception as e:
        return True, f"Invalid timestamp format: {e}"


def get_device_valuation(
    model: str,
    capacity_gb: Optional[int] = 64,
    prices_file: Optional[Path] = None,
    max_days: int = DEFAULT_STALE_DAYS,
) -> Dict[str, Any]:
    """
    Retrieves dynamic secondary-market valuation for an iPhone model.
    Returns valuation dict with pricing status, provenance, and agent stimulus triggers.
    """
    clean_model = model.strip()
    friendly = get_friendly_name(clean_model)
    ledger = load_market_prices(prices_file)
    models = ledger.get("models", {})

    record = models.get(clean_model)
    if record:
        stale, reason = is_quote_stale(record, max_days=max_days)
        refurb_usd = float(record.get("refurb_replacement_usd", 100.0))
        retail_usd = float(record.get("retail_upgrade_usd", 799.0))
        source = record.get("source", "Market Ledger")
        status = "LIVE_VERIFIED" if not stale else "STALE"
        confidence = record.get("confidence", "UNKNOWN")
    else:
        fallback = HEURISTIC_FALLBACKS.get(clean_model, {
            "friendly_name": friendly,
            "refurb_replacement_usd": 120.0,
            "retail_upgrade_usd": 799.0,
            "source": "Default Generation Heuristic",
        })
        refurb_usd = float(fallback["refurb_replacement_usd"])
        retail_usd = float(fallback["retail_upgrade_usd"])
        source = fallback["source"]
        status = "FALLBACK"
        confidence = "UNVERIFIED"
        stale, reason = True, "Unrecorded device model"

    needs_research = stale or status != "LIVE_VERIFIED"
    stimulus_prompt = generate_stimulus_banner(
        clean_model,
        friendly_name=friendly,
        capacity_gb=capacity_gb or 64,
        reason=reason,
        current_refurb=refurb_usd,
    ) if needs_research else ""

    return {
        "model": clean_model,
        "friendly_name": friendly,
        "capacity_gb": capacity_gb or 64,
        "refurb_replacement_usd": refurb_usd,
        "retail_upgrade_usd": retail_usd,
        "source": source,
        "pricing_status": status,
        "confidence": confidence,
        "is_stale": stale,
        "stale_reason": reason,
        "needs_research": needs_research,
        "stimulus_prompt": stimulus_prompt,
    }


def record_live_quote(
    model: str,
    refurb_usd: float,
    retail_usd: float = 799.0,
    source: str = "Swappa / Back Market / eBay",
    capacity_gb: int = 64,
    prices_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Records an authoritative, live-researched market valuation in the ledger."""
    clean_model = model.strip()
    friendly = get_friendly_name(clean_model)
    ledger = load_market_prices(prices_file)
    if "models" not in ledger:
        ledger["models"] = {}

    entry = {
        "friendly_name": friendly,
        "capacity_gb": capacity_gb,
        "refurb_replacement_usd": round(refurb_usd, 2),
        "retail_upgrade_usd": round(retail_usd, 2),
        "source": source.strip(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "confidence": "LIVE_VERIFIED",
    }
    ledger["models"][clean_model] = entry
    save_market_prices(ledger, prices_file)
    return entry


def generate_stimulus_banner(
    model: str,
    friendly_name: str,
    capacity_gb: int,
    reason: str,
    current_refurb: float,
) -> str:
    """Renders a high-visibility directive stimulating an AI agent to research live prices."""
    query = f"{friendly_name} {capacity_gb}GB refurbished price Swappa Back Market eBay"
    cmd_example = (
        f"python3 market_pricing.py set --model {model} "
        f"--refurb <USD> --retail 799.00 --source \"Swappa/Back Market $(date +%Y-%m)\""
    )
    banner = [
        "╔══════════════════════════════════════════════════════════════════════════════╗",
        "║           ⚡ [AGENT LIVE RESEARCH DIRECTIVE: MARKET VALUATION]               ║",
        "╠══════════════════════════════════════════════════════════════════════════════╣",
        f"║ Target Device:    {friendly_name} ({model}) - {capacity_gb}GB",
        f"║ Current Status:   {reason} (Heuristic: ${current_refurb:.2f})",
        "║ Action Required for Agent:",
        f"║   1. Run a live search with your web-search tool.",
        f"║      Query: \"{query}\"",
        "║   2. Record live verified quotation:",
        f"║      {cmd_example}",
        "║   3. Re-run 'python3 storage_monitor.py --diff' to publish calibrated ROI.",
        "╚══════════════════════════════════════════════════════════════════════════════╝",
    ]
    return "\n".join(banner)


def print_valuation(model: str, capacity_gb: int = 64) -> None:
    """Prints a single model's valuation (shared by the CLI and iphone_manager)."""
    val = get_device_valuation(model, capacity_gb=capacity_gb)
    print("\n" + "=" * 65)
    print(f"  MARKET VALUATION: {val['friendly_name']} ({val['model']})")
    print("=" * 65)
    print(f"  • Like-for-Like Refurbished:  ${val['refurb_replacement_usd']:.2f}")
    print(f"  • New Retail Upgrade Avoided: ${val['retail_upgrade_usd']:.2f}")
    print(f"  • Source Provenance:          {val['source']}")
    print(f"  • Status / Confidence:        {val['pricing_status']} ({val['confidence']})")
    print(f"  • Stale Status:               {val['stale_reason']}")
    print("=" * 65 + "\n")
    if val["needs_research"]:
        print(val["stimulus_prompt"] + "\n")


def print_ledger_list() -> None:
    """Prints the registered market quotes (shared by the CLI and iphone_manager)."""
    ledger = load_market_prices()
    models = ledger.get("models", {})
    print("\n" + "=" * 70)
    print("       SECONDARY MARKET VALUATION REGISTRY (LIVE CACHE)")
    print("=" * 70)
    if not models:
        print("  No model quotations currently recorded.")
    for mod, rec in sorted(models.items()):
        stale, reason = is_quote_stale(rec)
        status_tag = "[LIVE]" if not stale else "[STALE]"
        print(f"{status_tag} {rec.get('friendly_name', mod)} ({mod}, {rec.get('capacity_gb', 64)}GB):")
        print(f"     Refurb Cost: ${rec.get('refurb_replacement_usd', 0.0):.2f} | Retail Avoided: ${rec.get('retail_upgrade_usd', 799.0):.2f}")
        print(f"     Source:      {rec.get('source')} | Last Updated: {rec.get('last_updated', 'Never')[:16]}")
        print(f"     Condition:   {reason}\n")
    print("=" * 70 + "\n")


def print_staleness_audit(days: int = DEFAULT_STALE_DAYS) -> int:
    """Prints the staleness audit and returns the count of stale models."""
    ledger = load_market_prices()
    models = ledger.get("models", {})
    stale_count = 0
    print("\n" + "=" * 65)
    print("          MARKET QUOTATION STALENESS & HEALTH AUDIT")
    print("=" * 65)
    for mod, rec in models.items():
        stale, reason = is_quote_stale(rec, max_days=days)
        if stale:
            stale_count += 1
            print(f"[!] STALE: {rec.get('friendly_name')} ({mod}) -> {reason}")
            print(generate_stimulus_banner(mod, rec.get("friendly_name", mod), rec.get("capacity_gb", 64), reason, rec.get("refurb_replacement_usd", 100.0)))
            print()
        else:
            print(f"[OK] FRESH: {rec.get('friendly_name')} ({mod}) -> {reason}")
    print("=" * 65)
    if stale_count == 0:
        print(f"  All {len(models)} model market quotes are live and calibrated.\n")
    else:
        print(f"  {stale_count} models require agent live research.\n")
    return stale_count


def main():
    parser = argparse.ArgumentParser(description="Phone Revival: Dynamic Secondary Market Valuation Engine")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # get command
    p_get = subparsers.add_parser("get", help="Get valuation for a model identifier.")
    p_get.add_argument("model", help="Hardware model identifier (e.g. iPhone12,8, iPhone13,1)")
    p_get.add_argument("--capacity", type=int, default=64, help="Capacity in GB (default: 64)")

    # set command
    p_set = subparsers.add_parser("set", help="Record researched live market quotation.")
    p_set.add_argument("--model", required=True, help="Hardware model identifier (e.g. iPhone12,8)")
    p_set.add_argument("--refurb", required=True, type=float, help="Like-for-like refurbished replacement cost in USD")
    p_set.add_argument("--retail", type=float, default=799.0, help="New retail base upgrade avoided in USD (default: 799.0)")
    p_set.add_argument("--source", required=True, help="Provenance/source (e.g. 'Swappa / Back Market (2026-11)')")
    p_set.add_argument("--capacity", type=int, default=64, help="Capacity in GB (default: 64)")

    # list command
    subparsers.add_parser("list", help="List all registered device market quotes.")

    # check command
    p_check = subparsers.add_parser("check", help="Audit quotes against stale threshold and stimulate agent.")
    p_check.add_argument("--days", type=int, default=DEFAULT_STALE_DAYS, help="Stale threshold in days")

    # stimulate command
    p_stim = subparsers.add_parser("stimulate", help="Emit live agent research directive banner.")
    p_stim.add_argument("model", help="Hardware model identifier")
    p_stim.add_argument("--capacity", type=int, default=64, help="Capacity in GB")

    args = parser.parse_args()

    if args.command == "get":
        print_valuation(args.model, capacity_gb=args.capacity)
        return

    if args.command == "set":
        entry = record_live_quote(
            model=args.model,
            refurb_usd=args.refurb,
            retail_usd=args.retail,
            source=args.source,
            capacity_gb=args.capacity,
        )
        print(f"\n✅ Live quote recorded for {entry['friendly_name']} ({args.model}):")
        print(f"   Refurb Replacement: ${entry['refurb_replacement_usd']:.2f}")
        print(f"   Retail Avoidance:   ${entry['retail_upgrade_usd']:.2f}")
        print(f"   Source:             {entry['source']}\n")
        return

    if args.command == "list":
        print_ledger_list()
        return

    if args.command == "check":
        print_staleness_audit(days=args.days)
        return

    if args.command == "stimulate":
        friendly = get_friendly_name(args.model)
        val = get_device_valuation(args.model, capacity_gb=args.capacity)
        banner = generate_stimulus_banner(
            args.model,
            friendly,
            args.capacity,
            reason="Explicit agent research stimulation triggered",
            current_refurb=val["refurb_replacement_usd"],
        )
        print("\n" + banner + "\n")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
