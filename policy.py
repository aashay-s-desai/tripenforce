"""
Policy enforcement engine.

Loads rules from Postgres and evaluates bookings against them.
Rules live in policies.rules JSON and are never hardcoded here.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models import Policy, get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas for rules stored in the JSON column
# ---------------------------------------------------------------------------

class CabinClassRule(BaseModel):
    id: str
    description: str
    type: str = "cabin_class"
    max_duration_hours: float
    allowed_classes: list[str]


class HotelRateRule(BaseModel):
    id: str
    description: str
    type: str = "hotel_rate"
    max_nightly_rate: float


class SpendLimitRule(BaseModel):
    id: str
    description: str
    type: str = "spend_limit"
    threshold: float
    action: str  # "require_approval" | "block"


class AirlineAllowlistRule(BaseModel):
    id: str
    description: str
    type: str = "airline_allowlist"
    airlines: list[str]  # empty = all allowed


class PolicyRules(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Booking input schema
# ---------------------------------------------------------------------------

class BookingInput(BaseModel):
    flight_id: str
    origin: str
    destination: str
    departure_time: str
    arrival_time: str
    price: float
    cabin_class: str  # economy | premium_economy | business | first
    airline: str
    stops: int = 0
    duration_hours: Optional[float] = None  # computed if not provided
    trip_total: Optional[float] = None  # total trip cost including return


# ---------------------------------------------------------------------------
# Policy check result
# ---------------------------------------------------------------------------

class PolicyCheckResult(BaseModel):
    compliant: bool
    violations: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    nearest_compliant: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _get_policy(company_id: str, session: Session) -> Optional[Policy]:
    return session.query(Policy).filter(Policy.company_id == company_id).first()


def _estimate_duration(departure: str, arrival: str) -> float:
    """
    Very rough duration estimate when not explicitly provided.
    Parses ISO-8601 strings; returns 3.0 hours as fallback.
    """
    try:
        from datetime import datetime
        fmt_options = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
        ]
        dep_dt: Optional[datetime] = None
        arr_dt: Optional[datetime] = None
        for fmt in fmt_options:
            try:
                dep_dt = datetime.strptime(departure[:19], fmt[:len(fmt)])
                arr_dt = datetime.strptime(arrival[:19], fmt[:len(fmt)])
                break
            except ValueError:
                continue
        if dep_dt and arr_dt:
            delta = arr_dt - dep_dt
            hours = delta.total_seconds() / 3600
            return max(0.5, hours)
    except Exception:
        pass
    return 3.0


def check_policy(booking: BookingInput, company_id: str) -> PolicyCheckResult:
    """
    Evaluate a booking against the company's stored policy rules.
    Returns a PolicyCheckResult with violations list and nearest_compliant suggestion.
    """
    with get_session() as session:
        policy = _get_policy(company_id, session)
        if not policy:
            logger.warning("No policy found for company_id=%s; defaulting to compliant", company_id)
            return PolicyCheckResult(compliant=True)

        rules: list[dict[str, Any]] = policy.rules.get("rules", [])

    violations: list[str] = []
    requires_approval = False
    nearest_compliant_patches: dict[str, Any] = {}

    duration = booking.duration_hours
    if duration is None:
        duration = _estimate_duration(booking.departure_time, booking.arrival_time)

    trip_total = booking.trip_total if booking.trip_total is not None else booking.price

    for rule in rules:
        rule_type = rule.get("type")

        if rule_type == "cabin_class":
            parsed = CabinClassRule(**rule)
            if duration <= parsed.max_duration_hours:
                if booking.cabin_class not in parsed.allowed_classes:
                    violations.append(
                        f"Policy requires {'/'.join(parsed.allowed_classes)} class "
                        f"for flights under {parsed.max_duration_hours}h "
                        f"(booked: {booking.cabin_class})"
                    )
                    nearest_compliant_patches["cabin_class"] = parsed.allowed_classes[0]

        elif rule_type == "hotel_rate":
            # hotel_rate checks are advisory here — enforced at hotel booking
            pass

        elif rule_type == "spend_limit":
            parsed = SpendLimitRule(**rule)
            if trip_total > parsed.threshold:
                if parsed.action == "require_approval":
                    requires_approval = True
                    violations.append(
                        f"Trip total ${trip_total:.2f} exceeds ${parsed.threshold:.0f} threshold "
                        f"— manager approval required"
                    )
                elif parsed.action == "block":
                    violations.append(
                        f"Trip total ${trip_total:.2f} exceeds hard limit ${parsed.threshold:.0f}"
                    )

        elif rule_type == "airline_allowlist":
            parsed = AirlineAllowlistRule(**rule)
            if parsed.airlines and booking.airline not in parsed.airlines:
                violations.append(
                    f"Airline {booking.airline!r} is not on the approved list: "
                    f"{', '.join(parsed.airlines)}"
                )

    # Build nearest_compliant suggestion when there are fixable violations
    nearest_compliant: Optional[dict[str, Any]] = None
    if violations and nearest_compliant_patches:
        patched = booking.model_dump()
        patched.update(nearest_compliant_patches)
        nearest_compliant = patched
        # If the only violation was cabin class, mark the patched version as compliant
        cabin_only = all("class" in v.lower() for v in violations if "approval" not in v.lower())
        if cabin_only:
            nearest_compliant["note"] = (
                "Same flight available in economy class — typically 30–50% cheaper"
            )

    # A booking that only requires_approval is still conditionally compliant
    hard_violations = [v for v in violations if "approval" not in v]
    compliant = len(hard_violations) == 0

    result = PolicyCheckResult(
        compliant=compliant,
        violations=violations,
        requires_approval=requires_approval,
        nearest_compliant=nearest_compliant,
    )
    logger.info(
        "Policy check company=%s flight=%s compliant=%s violations=%d",
        company_id, booking.flight_id, result.compliant, len(violations),
    )
    return result


# ---------------------------------------------------------------------------
# Admin: update policy rules
# ---------------------------------------------------------------------------

class PolicyUpdateRequest(BaseModel):
    rules: list[dict[str, Any]]


def update_policy(company_id: str, update: PolicyUpdateRequest) -> Policy:
    """
    Upsert policy rules for a company.
    Creates a new policy record if none exists.
    """
    with get_session() as session:
        policy = _get_policy(company_id, session)
        if policy is None:
            from models import Company
            company = session.get(Company, company_id)
            if company is None:
                raise ValueError(f"Company {company_id!r} not found")
            policy = Policy(company_id=company_id, rules={"rules": update.rules})
            session.add(policy)
        else:
            policy.rules = {"rules": update.rules}
        session.flush()  # context manager commits
        logger.info("Policy updated for company_id=%s (%d rules)", company_id, len(update.rules))
        return policy


def get_policy(company_id: str) -> Optional[dict[str, Any]]:
    """Return the raw rules dict for a company, or None."""
    with get_session() as session:
        policy = _get_policy(company_id, session)
        return policy.rules if policy else None
