"""
Traveler preference learning with time-decayed scoring.

Learning model
--------------
Each booking contributes a weight = exp(-λ · days_ago) where λ = ln(2)/30,
giving recent bookings a half-life of 30 days. This means a booking from
last week counts roughly twice as much as one from five weeks ago.

Tracked signals
---------------
  airline          — weighted score per airline
  cabin_class      — weighted score per cabin
  departure_window — morning (6-11), afternoon (12-17), evening (18-23)
  price_avg        — exponentially smoothed average spend (α = 0.3)
  seat_type        — explicit preference set by user (window/aisle/none)

rank_by_preferences()
---------------------
Scores each flight on four axes, normalises to [-1, +1] per axis,
and returns flights sorted by (composite_score DESC, price ASC).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models import Booking, TravelerPreference, get_session
from providers import Flight

logger = logging.getLogger(__name__)

DECAY_HALF_LIFE_DAYS = 30.0
DECAY_LAMBDA = math.log(2) / DECAY_HALF_LIFE_DAYS
PRICE_SMOOTHING_ALPHA = 0.3   # EMA factor for average price


# ---------------------------------------------------------------------------
# Preference schema (public-facing)
# ---------------------------------------------------------------------------

class PreferenceProfile(BaseModel):
    preferred_airlines: list[str] = Field(default_factory=list)
    seat_type: Optional[str] = None          # "window" | "aisle" | None
    cabin_class: Optional[str] = None        # top-weighted cabin
    preferred_hotel_chains: list[str] = Field(default_factory=list)
    preferred_departure_window: Optional[str] = None  # "morning"|"afternoon"|"evening"
    avg_price: Optional[float] = None        # smoothed average booking price
    booking_count: int = 0

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Internal storage schema (what goes in preferences.data JSON)
# ---------------------------------------------------------------------------

class _PreferenceStore(BaseModel):
    """Full internal representation — stored in the DB JSON column."""
    # Weighted score maps (airline → cumulative decay-weighted count)
    airline_scores: dict[str, float] = Field(default_factory=dict)
    cabin_scores: dict[str, float] = Field(default_factory=dict)
    departure_window_scores: dict[str, float] = Field(default_factory=dict)

    # EMA of price paid
    avg_price: Optional[float] = None

    # Explicit preference (set directly, not learned)
    seat_type: Optional[str] = None
    preferred_hotel_chains: list[str] = Field(default_factory=list)

    # Booking history for audit / re-scoring
    history: list[dict[str, Any]] = Field(default_factory=list)

    booking_count: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decay_weight(booked_at_iso: str) -> float:
    """Return the time-decay weight for a booking timestamp."""
    try:
        booked_at = datetime.fromisoformat(booked_at_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if booked_at.tzinfo is None:
            booked_at = booked_at.replace(tzinfo=timezone.utc)
        days_ago = (now - booked_at).total_seconds() / 86400
        return math.exp(-DECAY_LAMBDA * max(0.0, days_ago))
    except Exception:
        return 1.0  # default to full weight if timestamp is unparseable


def _departure_window(departure_time_iso: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(departure_time_iso.replace("Z", "+00:00"))
        hour = dt.hour
        if 6 <= hour < 12:
            return "morning"
        elif 12 <= hour < 18:
            return "afternoon"
        elif 18 <= hour < 24:
            return "evening"
    except Exception:
        pass
    return None


def _top_by_score(scores: dict[str, float], n: int = 3) -> list[str]:
    return [k for k, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)][:n]


def _load_store(user_id: str, session: Session) -> tuple[TravelerPreference, _PreferenceStore]:
    pref = session.query(TravelerPreference).filter(TravelerPreference.user_id == user_id).first()
    if pref is None:
        pref = TravelerPreference(user_id=user_id, data={})
        session.add(pref)
        session.flush()
    try:
        store = _PreferenceStore(**pref.data) if pref.data else _PreferenceStore()
    except Exception:
        store = _PreferenceStore()
    return pref, store


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_preferences(user_id: str) -> PreferenceProfile:
    """Return the public preference profile for a user."""
    with get_session() as session:
        pref = session.query(TravelerPreference).filter(TravelerPreference.user_id == user_id).first()
        if pref is None or not pref.data:
            return PreferenceProfile()
        try:
            store = _PreferenceStore(**pref.data)
        except Exception:
            return PreferenceProfile()

    top_airline = _top_by_score(store.airline_scores, 3)
    top_cabin_list = _top_by_score(store.cabin_scores, 1)
    top_cabin = top_cabin_list[0] if top_cabin_list else None
    top_window_list = _top_by_score(store.departure_window_scores, 1)
    top_window = top_window_list[0] if top_window_list else None

    return PreferenceProfile(
        preferred_airlines=top_airline,
        seat_type=store.seat_type,
        cabin_class=top_cabin,
        preferred_hotel_chains=store.preferred_hotel_chains,
        preferred_departure_window=top_window,
        avg_price=round(store.avg_price, 2) if store.avg_price else None,
        booking_count=store.booking_count,
    )


def store_preference(user_id: str, booking: Booking) -> PreferenceProfile:
    """
    Update preference store from a confirmed booking using time-decayed scoring.
    """
    booked_at = (booking.created_at or datetime.utcnow()).isoformat()
    weight = _decay_weight(booked_at)
    window = _departure_window(booking.departure_time)

    with get_session() as session:
        pref, store = _load_store(user_id, session)

        # Update weighted scores
        store.airline_scores[booking.airline] = (
            store.airline_scores.get(booking.airline, 0.0) + weight
        )
        cabin = booking.cabin_class.value if hasattr(booking.cabin_class, "value") else str(booking.cabin_class)
        store.cabin_scores[cabin] = store.cabin_scores.get(cabin, 0.0) + weight

        if window:
            store.departure_window_scores[window] = (
                store.departure_window_scores.get(window, 0.0) + weight
            )

        # EMA price
        if store.avg_price is None:
            store.avg_price = booking.price
        else:
            store.avg_price = PRICE_SMOOTHING_ALPHA * booking.price + (1 - PRICE_SMOOTHING_ALPHA) * store.avg_price

        # Append history entry (keep last 50)
        store.history.append({
            "flight_id": booking.flight_id,
            "airline": booking.airline,
            "cabin_class": cabin,
            "price": booking.price,
            "origin": booking.origin,
            "destination": booking.destination,
            "departure_time": booking.departure_time,
            "booked_at": booked_at,
            "weight": round(weight, 4),
        })
        if len(store.history) > 50:
            store.history = store.history[-50:]

        store.booking_count += 1
        pref.data = store.model_dump()
        session.flush()

    profile = get_preferences(user_id)
    logger.info(
        "Preferences updated user=%s airlines=%s cabin=%s window=%s avg_price=%.0f",
        user_id,
        profile.preferred_airlines,
        profile.cabin_class,
        profile.preferred_departure_window,
        profile.avg_price or 0,
    )
    return profile


# ---------------------------------------------------------------------------
# Preference-aware ranking
# ---------------------------------------------------------------------------

def rank_by_preferences(flights: list[Flight], profile: PreferenceProfile) -> list[Flight]:
    """
    Re-rank flights by how well each matches the traveler's learned preferences.

    Scoring axes (each contributes to a composite score):
      +2.0  exact airline match (top preferred airline)
      +1.0  airline in preferred list (not #1)
      +1.0  cabin class matches preferred cabin
      +0.5  departure window matches preferred window
      +0.5  price within ±20% of historical average

    Flights are sorted by (composite_score DESC, price ASC).
    If no preferences exist, the original list is returned unchanged.
    """
    if not any([
        profile.preferred_airlines,
        profile.cabin_class,
        profile.preferred_departure_window,
        profile.avg_price,
    ]):
        return flights

    def _score(f: Flight) -> tuple[float, float]:
        score = 0.0

        # Airline preference
        if profile.preferred_airlines:
            if f.airline == profile.preferred_airlines[0]:
                score += 2.0
            elif f.airline in profile.preferred_airlines[1:]:
                score += 1.0

        # Cabin preference
        if profile.cabin_class and f.cabin_class == profile.cabin_class:
            score += 1.0

        # Departure window preference
        if profile.preferred_departure_window:
            window = _departure_window(f.departure_time)
            if window == profile.preferred_departure_window:
                score += 0.5

        # Price proximity to historical average
        if profile.avg_price and profile.avg_price > 0:
            ratio = f.price / profile.avg_price
            if 0.8 <= ratio <= 1.2:
                score += 0.5

        # Negate so higher score sorts first; price breaks ties
        return (-score, f.price)

    ranked = sorted(flights, key=_score)

    if logger.isEnabledFor(logging.DEBUG):
        for f in ranked[:3]:
            logger.debug("Ranked: %s %s $%.0f score=%.1f", f.airline, f.cabin_class, f.price, -_score(f)[0])

    return ranked
