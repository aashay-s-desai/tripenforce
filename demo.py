"""
Demo script — runs 5 example trip scenarios end to end against the live backend.

Scenarios:
  1. Compliant request — economy, under budget, straightforward
  2. Out-of-policy request — business class on a short flight
  3. Manager approval trigger — expensive trip over $1,000
  4. Repeat traveler — Carol's preferences should influence ranking
  5. API failure simulation — Duffel key temporarily blanked, mock kicks in

Usage:
    python demo.py
    python demo.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Seed IDs (match models.py seed_database)
# ---------------------------------------------------------------------------

COMPANY_ID = "00000000-0000-0000-0000-000000000001"
MANAGER_ID = "00000000-0000-0000-0000-000000000010"
EMPLOYEE_ID = "00000000-0000-0000-0000-000000000011"
REPEAT_TRAVELER_ID = "00000000-0000-0000-0000-000000000012"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEPARATOR = "─" * 70


def _print_header(n: int, title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  SCENARIO {n}: {title}")
    print(SEPARATOR)


def _pretty(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _post_trip(client: httpx.Client, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(f"{base_url}/trip", json=payload, timeout=120.0)
    if resp.status_code != 200:
        print(f"  [ERROR] HTTP {resp.status_code}: {resp.text[:500]}")
        return {}
    return resp.json()


def _print_recommendation(result: dict[str, Any]) -> None:
    rec = result.get("recommendation", {})

    print("\n  AGENT RECOMMENDATION:")
    print(textwrap.indent(textwrap.fill(rec.get("recommendation_text", "(no text)"), width=65), "    "))

    print(f"\n  Trip purpose  : {rec.get('trip_purpose', 'unknown')}")
    print(f"  Fallback used : {rec.get('fallback_used', False)}")
    print(f"  Used prefs    : {rec.get('used_preferences', False)}")
    print(f"  Needs approval: {rec.get('requires_approval', False)}")

    violations = rec.get("violations", [])
    if violations:
        print(f"\n  VIOLATIONS ({len(violations)}):")
        for v in violations:
            print(f"    • {v}")

    compliant = rec.get("all_compliant", [])
    print(f"\n  Compliant options: {len(compliant)}")
    for i, f in enumerate(compliant[:3], 1):
        print(
            f"    {i}. {f.get('airline', '?'):25s} "
            f"{f.get('cabin_class', '?'):15s} "
            f"${f.get('price', 0):.2f}  "
            f"source={f.get('source', '?')}"
        )
    if len(compliant) > 3:
        print(f"    … and {len(compliant) - 3} more")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_1_compliant(client: httpx.Client, base_url: str) -> None:
    _print_header(1, "Compliant Request — Economy, Under Budget")
    print("  Request: Round trip Chicago→New York, next Friday, economy, budget $400")

    payload = {
        "request": (
            "Book me a round trip from Chicago (ORD) to New York (JFK), "
            "departing 2026-05-22, returning 2026-05-24, budget $400 total, economy class."
        ),
        "user_id": EMPLOYEE_ID,
        "company_id": COMPANY_ID,
    }
    result = _post_trip(client, base_url, payload)
    if result:
        _print_recommendation(result)
        rec = result.get("recommendation", {})
        print(f"\n  Expected: compliant=True, violations=[], approval=False")
        print(f"  Actual  : compliant={not rec.get('violations')}, approval={rec.get('requires_approval')}")


def scenario_2_out_of_policy(client: httpx.Client, base_url: str) -> None:
    _print_header(2, "Out-of-Policy Request — Business Class on Short Flight")
    print("  Request: Business class Chicago→New York (< 3h flight)")

    payload = {
        "request": (
            "I need a business class flight from Chicago (ORD) to New York (JFK) "
            "departing 2026-05-22. Book the best option available."
        ),
        "user_id": EMPLOYEE_ID,
        "company_id": COMPANY_ID,
    }
    result = _post_trip(client, base_url, payload)
    if result:
        _print_recommendation(result)
        rec = result.get("recommendation", {})
        print(f"\n  Expected: violations present, nearest compliant in economy")
        print(f"  Actual  : violations={len(rec.get('violations', []))}, compliant_opts={len(rec.get('all_compliant', []))}")


def scenario_3_approval_required(client: httpx.Client, base_url: str) -> None:
    _print_header(3, "Manager Approval Trigger — Trip Over $1,000")
    print("  Request: San Francisco → London, $1,200+ trip")

    payload = {
        "request": (
            "Book me a flight from San Francisco (SFO) to London (LHR) "
            "departing 2026-06-01 for a client meeting. Economy class is fine."
        ),
        "user_id": EMPLOYEE_ID,
        "company_id": COMPANY_ID,
    }
    result = _post_trip(client, base_url, payload)
    if result:
        _print_recommendation(result)
        rec = result.get("recommendation", {})
        print(f"\n  Expected: requires_approval=True (long haul > $1000)")
        print(f"  Actual  : requires_approval={rec.get('requires_approval')}")


def scenario_4_repeat_traveler(client: httpx.Client, base_url: str) -> None:
    _print_header(4, "Repeat Traveler — Preferences Should Influence Ranking")
    print("  Carol has preferences: United/Delta, window seat, economy")
    print("  Request: Economy flight, should rank United/Delta first if available")

    payload = {
        "request": (
            "Find me an economy flight from Chicago (ORD) to Los Angeles (LAX) "
            "departing 2026-05-28. I prefer United or Delta."
        ),
        "user_id": REPEAT_TRAVELER_ID,
        "company_id": COMPANY_ID,
    }
    result = _post_trip(client, base_url, payload)
    if result:
        _print_recommendation(result)
        rec = result.get("recommendation", {})
        compliant = rec.get("all_compliant", [])
        top = compliant[0] if compliant else {}
        print(f"\n  Expected: used_preferences=True, top airline is United or Delta if available")
        print(f"  Actual  : used_preferences={rec.get('used_preferences')}, top_airline={top.get('airline', 'N/A')}")


def scenario_5_api_failure_fallback(client: httpx.Client, base_url: str) -> None:
    _print_header(5, "API Failure Simulation — Mock Fallback")
    print("  Temporarily removing Duffel key via env override…")
    print("  (The API key is blanked at the travel_api module level for this test)")

    import travel_api
    import config

    original_key = config.settings.duffel_api_key
    config.settings.duffel_api_key = ""  # blank the key so Duffel is skipped

    try:
        payload = {
            "request": (
                "I need an economy flight from Denver (DEN) to Seattle (SEA) "
                "departing 2026-05-25. Any airline is fine."
            ),
            "user_id": EMPLOYEE_ID,
            "company_id": COMPANY_ID,
        }
        result = _post_trip(client, base_url, payload)
        if result:
            _print_recommendation(result)
            rec = result.get("recommendation", {})
            print(f"\n  Expected: fallback_used=True (all flights source=mock)")
            print(f"  Actual  : fallback_used={rec.get('fallback_used')}")
    finally:
        config.settings.duffel_api_key = original_key
        print("  [Restored original Duffel API key]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TripEnforce demo script")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the running TripEnforce API",
    )
    parser.add_argument(
        "--scenario",
        type=int,
        choices=[1, 2, 3, 4, 5],
        help="Run a single scenario (default: all)",
    )
    args = parser.parse_args()

    print("\n" + "═" * 70)
    print("  TripEnforce — AI Corporate Travel Agent Demo")
    print("  Running against:", args.base_url)
    print("═" * 70)

    # Health check
    with httpx.Client() as client:
        try:
            resp = client.get(f"{args.base_url}/health", timeout=5.0)
            resp.raise_for_status()
            print(f"\n  [✓] Backend healthy: {resp.json()}")
        except Exception as exc:
            print(f"\n  [✗] Backend not reachable at {args.base_url}: {exc}")
            print("  Start the server first: uvicorn main:app --reload")
            sys.exit(1)

    scenarios = {
        1: scenario_1_compliant,
        2: scenario_2_out_of_policy,
        3: scenario_3_approval_required,
        4: scenario_4_repeat_traveler,
        5: scenario_5_api_failure_fallback,
    }

    to_run = [args.scenario] if args.scenario else list(scenarios.keys())

    with httpx.Client() as client:
        for n in to_run:
            start = time.monotonic()
            try:
                scenarios[n](client, args.base_url)
            except Exception as exc:
                print(f"\n  [ERROR] Scenario {n} raised: {exc}")
            elapsed = time.monotonic() - start
            print(f"\n  Completed in {elapsed:.1f}s")

    print(f"\n{SEPARATOR}")
    print("  Demo complete.")
    print(SEPARATOR + "\n")


if __name__ == "__main__":
    main()
