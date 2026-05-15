"""
Core booking agent.

Uses Claude with tool use to:
  1. Parse the natural language trip request
  2. Search for flights via Duffel / mock
  3. Check each result against company policy
  4. Rank compliant options by user preferences
  5. Return a plain-language recommendation

Streaming variant yields intermediate reasoning steps for the WebSocket endpoint.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Generator
from typing import Any, Optional

import anthropic
from pydantic import BaseModel

from categorization import infer_trip_purpose
from config import settings
from policy import BookingInput, PolicyCheckResult, check_policy
from preferences import PreferenceProfile, get_preferences, rank_by_preferences
from providers import Flight
from travel_api import Flight, search_flights

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_flights",
        "description": (
            "Search for available flights between two airports on a given date. "
            "Returns a list of flight options with price, airline, cabin class, and schedule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "IATA airport code, e.g. ORD"},
                "destination": {"type": "string", "description": "IATA airport code, e.g. JFK"},
                "departure_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "passengers": {"type": "integer", "default": 1},
                "cabin_class": {
                    "type": "string",
                    "enum": ["economy", "premium_economy", "business", "first"],
                    "default": "economy",
                },
            },
            "required": ["origin", "destination", "departure_date"],
        },
    },
    {
        "name": "check_policy",
        "description": (
            "Check whether a specific flight booking complies with the company's travel policy. "
            "Returns compliance status, any violations, and the nearest compliant alternative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_id": {"type": "string"},
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "departure_time": {"type": "string"},
                "arrival_time": {"type": "string"},
                "price": {"type": "number"},
                "cabin_class": {"type": "string"},
                "airline": {"type": "string"},
                "stops": {"type": "integer", "default": 0},
                "duration_hours": {"type": "number"},
                "trip_total": {"type": "number"},
                "company_id": {"type": "string"},
            },
            "required": ["flight_id", "origin", "destination", "departure_time",
                         "arrival_time", "price", "cabin_class", "airline", "company_id"],
        },
    },
    {
        "name": "categorize_spend",
        "description": "Infer the trip purpose from the natural language request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "natural_language_request": {"type": "string"},
            },
            "required": ["natural_language_request"],
        },
    },
    {
        "name": "get_preferences",
        "description": "Retrieve the traveler's historical booking preferences (airline, seat type, cabin).",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "store_preference",
        "description": (
            "Record a booking choice to update the traveler's preference profile. "
            "Call this after the user confirms a booking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "flight_id": {"type": "string"},
                "airline": {"type": "string"},
                "cabin_class": {"type": "string"},
            },
            "required": ["user_id", "flight_id", "airline", "cabin_class"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    user_id: str,
    company_id: str,
) -> Any:
    """Execute a tool call and return a JSON-serialisable result."""
    if tool_name == "search_flights":
        flights = search_flights(
            origin=tool_input["origin"],
            destination=tool_input["destination"],
            departure_date=tool_input["departure_date"],
            passengers=tool_input.get("passengers", 1),
            cabin_class=tool_input.get("cabin_class", "economy"),
        )
        return [f.model_dump() for f in flights]

    elif tool_name == "check_policy":
        booking_input = BookingInput(
            flight_id=tool_input["flight_id"],
            origin=tool_input["origin"],
            destination=tool_input["destination"],
            departure_time=tool_input["departure_time"],
            arrival_time=tool_input["arrival_time"],
            price=tool_input["price"],
            cabin_class=tool_input["cabin_class"],
            airline=tool_input["airline"],
            stops=tool_input.get("stops", 0),
            duration_hours=tool_input.get("duration_hours"),
            trip_total=tool_input.get("trip_total"),
        )
        result = check_policy(booking_input, tool_input.get("company_id", company_id))
        return result.model_dump()

    elif tool_name == "categorize_spend":
        purpose = infer_trip_purpose(tool_input.get("natural_language_request", ""))
        return {"trip_purpose": purpose}

    elif tool_name == "get_preferences":
        uid = tool_input.get("user_id", user_id)
        profile = get_preferences(uid)
        return profile.model_dump()

    elif tool_name == "store_preference":
        # Lightweight in-agent update (full store_preference requires a Booking ORM object;
        # the complete update happens in /book endpoint after DB write)
        return {"status": "noted", "message": "Preference will be persisted on booking confirmation"}

    else:
        return {"error": f"Unknown tool: {tool_name}"}


# ---------------------------------------------------------------------------
# Agent response models
# ---------------------------------------------------------------------------

class AgentRecommendation(BaseModel):
    top_flight: Optional[dict[str, Any]]
    all_compliant: list[dict[str, Any]]
    violations: list[str]
    requires_approval: bool
    recommendation_text: str
    trip_purpose: str
    used_preferences: bool
    fallback_used: bool


# ---------------------------------------------------------------------------
# Core agent runner
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are TripEnforce, an AI-powered corporate travel booking assistant.

Your job — follow these steps in order:
1. Parse the request: extract origin/destination IATA codes, date, passenger count, and any stated preferences.
2. Call get_preferences(user_id) to load the traveler's learned profile BEFORE searching.
3. Call search_flights to find available options.
4. Call check_policy on each candidate flight to verify compliance. Always pass company_id.
5. Call categorize_spend to infer the trip purpose.
6. Return a structured recommendation.

Recommendation format:
- If manager approval is needed: start with "⚠️ APPROVAL REQUIRED — ..."
- Top pick: state airline, flight, price, cabin, stops, and departure time
- If preferences influenced the ranking: say "Ranked first based on your preference for [airline/cabin/morning flights]"
- If flights came from a fallback provider: note "Live schedules unavailable — showing estimated options"
- If no compliant options: explain the specific violation and show the nearest compliant alternative
- Keep it under 120 words. Professional, not chatty.

Rules:
- NEVER recommend a non-compliant flight without disclosing the violation.
- Chicago maps to ORD (or MDW for Southwest). New York: JFK, LGA, or EWR.
- Resolve relative dates (next Friday, this Sunday) from today: {today}.
- If the user has a preference profile with booking_count > 0, always mention how preferences influenced the result.
"""


def run_agent(
    request: str,
    user_id: str,
    company_id: str,
    stream_callback=None,
) -> AgentRecommendation:
    """
    Run the booking agent synchronously.

    Args:
        request: Natural language trip request.
        user_id: ID of the requesting user.
        company_id: Company policy scope.
        stream_callback: Optional callable(step: dict) for streaming progress.

    Returns:
        AgentRecommendation with ranked compliant options and recommendation text.
    """
    from datetime import date
    today = date.today().isoformat()

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"User ID: {user_id}\n"
                f"Company ID: {company_id}\n\n"
                f"Trip request: {request}"
            ),
        }
    ]

    collected_flights: list[dict[str, Any]] = []
    policy_results: dict[str, PolicyCheckResult] = {}
    preference_profile: Optional[PreferenceProfile] = None
    fallback_used = False
    used_preferences = False
    trip_purpose = "business_travel"
    max_iterations = 10

    for iteration in range(max_iterations):
        logger.debug("Agent iteration %d/%d", iteration + 1, max_iterations)

        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT.format(today=today),
            tools=TOOLS,
            messages=messages,
        )

        if stream_callback:
            stream_callback({"type": "model_response", "stop_reason": response.stop_reason})

        # Collect text blocks into the conversation
        assistant_content: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            # Extract final text
            final_text = " ".join(
                b["text"] for b in assistant_content if b.get("type") == "text"
            ).strip()

            if not final_text:
                final_text = "I found several flight options. Please review the compliant results above."

            compliant_dicts = [
                f for f in collected_flights
                if policy_results.get(f.get("flight_id", ""), PolicyCheckResult(compliant=True)).compliant
            ]

            # Apply preference-based re-ranking to compliant results
            if preference_profile and compliant_dicts:
                try:
                    flight_objs = [Flight(**f) for f in compliant_dicts]
                    ranked_objs = rank_by_preferences(flight_objs, preference_profile)
                    compliant_dicts = [f.model_dump() for f in ranked_objs]
                    logger.info("Applied preference ranking for user=%s", user_id)
                except Exception as exc:
                    logger.warning("Preference ranking failed (using price order): %s", exc)

            violations = []
            requires_approval = False
            for r in policy_results.values():
                violations.extend(r.violations)
                if r.requires_approval:
                    requires_approval = True

            # De-duplicate violations
            violations = list(dict.fromkeys(violations))

            if stream_callback:
                stream_callback({"type": "final_recommendation", "text": final_text})

            return AgentRecommendation(
                top_flight=compliant_dicts[0] if compliant_dicts else None,
                all_compliant=compliant_dicts,
                violations=violations,
                requires_approval=requires_approval,
                recommendation_text=final_text,
                trip_purpose=trip_purpose,
                used_preferences=used_preferences,
                fallback_used=fallback_used,
            )

        if response.stop_reason != "tool_use":
            logger.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

        # Process tool calls
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input

            if stream_callback:
                stream_callback({"type": "tool_call", "tool": tool_name, "input": tool_input})

            logger.info("Tool call: %s(%s)", tool_name, json.dumps(tool_input, default=str)[:200])

            result = _dispatch_tool(tool_name, tool_input, user_id, company_id)

            # Side-effects for internal tracking
            if tool_name == "search_flights" and isinstance(result, list):
                collected_flights.extend(result)
                fallback_used = any(f.get("source") == "mock" for f in result)

            elif tool_name == "check_policy" and isinstance(result, dict):
                fid = tool_input.get("flight_id", "")
                policy_results[fid] = PolicyCheckResult(**result)

            elif tool_name == "get_preferences":
                used_preferences = True
                try:
                    preference_profile = PreferenceProfile(**result)
                except Exception:
                    pass

            elif tool_name == "categorize_spend":
                trip_purpose = result.get("trip_purpose", "business_travel")

            if stream_callback:
                stream_callback({"type": "tool_result", "tool": tool_name, "result": result})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    # Fallback if max iterations hit
    logger.error("Agent hit max iterations without end_turn")
    return AgentRecommendation(
        top_flight=None,
        all_compliant=[],
        violations=["Agent exceeded maximum reasoning steps"],
        requires_approval=False,
        recommendation_text="Unable to complete trip planning. Please try again or contact support.",
        trip_purpose=trip_purpose,
        used_preferences=used_preferences,
        fallback_used=fallback_used,
    )


# ---------------------------------------------------------------------------
# Streaming generator (for WebSocket endpoint)
# ---------------------------------------------------------------------------

def run_agent_streaming(
    request: str,
    user_id: str,
    company_id: str,
) -> Generator[dict[str, Any], None, AgentRecommendation]:
    """
    Generator version of run_agent that yields step dicts.
    Yields dicts with "type" key; final value is the AgentRecommendation.
    """
    steps: list[dict[str, Any]] = []

    def _cb(step: dict[str, Any]) -> None:
        steps.append(step)

    # We can't truly interleave a generator with the synchronous agent loop,
    # so we collect all steps and yield them after the run completes.
    # For true real-time streaming, use the WebSocket handler which runs this
    # in a thread and pushes steps via asyncio.Queue.
    result = run_agent(request, user_id, company_id, stream_callback=_cb)
    for step in steps:
        yield step
    return result
