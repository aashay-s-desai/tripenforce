"""
Spend categorization module.

Automatically tags each confirmed booking with:
  - category (airfare / lodging / ground)
  - cost center (from user profile)
  - trip purpose (inferred from natural language request context)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from models import Booking, SpendCategory, SpendRecord, User, get_session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Purpose inference
# ---------------------------------------------------------------------------

_PURPOSE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(conference|summit|meetup|seminar|workshop|convention)\b", re.I), "conference"),
    (re.compile(r"\b(client|customer|sales|pitch|proposal|demo)\b", re.I), "client_meeting"),
    (re.compile(r"\b(interview|hiring|recruit|onboard)\b", re.I), "recruiting"),
    (re.compile(r"\b(team|offsite|retreat|kickoff|all.?hands)\b", re.I), "team_event"),
    (re.compile(r"\b(training|course|certification|boot.?camp)\b", re.I), "training"),
    (re.compile(r"\b(vacation|holiday|personal|leisure)\b", re.I), "personal"),
]


def infer_trip_purpose(natural_language_request: Optional[str]) -> str:
    """Return a best-guess trip purpose label from the free-text request."""
    if not natural_language_request:
        return "business_travel"
    for pattern, label in _PURPOSE_PATTERNS:
        if pattern.search(natural_language_request):
            return label
    return "business_travel"


# ---------------------------------------------------------------------------
# Category detection
# ---------------------------------------------------------------------------

def categorize_booking(booking: Booking) -> SpendCategory:
    """
    Determine spend category from booking data.
    Currently all bookings through this endpoint are airfare.
    Hotel / ground bookings would follow the same pattern with different types.
    """
    return SpendCategory.AIRFARE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_spend(
    booking: Booking,
    natural_language_request: Optional[str] = None,
) -> SpendRecord:
    """
    Create a SpendRecord for a confirmed booking.
    Idempotent — if a record already exists for this booking it is returned as-is.
    """
    with get_session() as session:
        # Idempotency check
        existing = (
            session.query(SpendRecord)
            .filter(SpendRecord.booking_id == booking.id)
            .first()
        )
        if existing:
            logger.debug("SpendRecord already exists for booking_id=%s", booking.id)
            return existing

        user = session.get(User, booking.user_id)
        cost_center = user.cost_center if user else None

        purpose = infer_trip_purpose(natural_language_request or booking.natural_language_request)
        category = categorize_booking(booking)

        record = SpendRecord(
            booking_id=booking.id,
            user_id=booking.user_id,
            company_id=booking.company_id,
            amount=booking.price,
            category=category,
            cost_center=cost_center,
            trip_purpose=purpose,
        )
        session.add(record)
        session.flush()  # get id without double-committing (context manager commits)

        logger.info(
            "Spend recorded booking_id=%s amount=%.2f category=%s purpose=%s",
            booking.id, record.amount, record.category.value, record.trip_purpose,
        )
        return record
