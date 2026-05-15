"""
Multi-provider flight search with circuit breaker and cascading fallback.

Provider chain (in order):
  1. DuffelProvider   — primary (Duffel sandbox/live API)
  2. AmadeusProvider  — secondary (Amadeus Test API; set AMADEUS_API_KEY + AMADEUS_API_SECRET in .env)
  3. MockProvider     — always-on synthetic fallback

CircuitBreaker opens after FAILURE_THRESHOLD consecutive failures and stays
open for RECOVERY_TIMEOUT seconds before allowing a single probe attempt
(half-open). This prevents hammering a broken upstream on every request.

Note: Amadeus self-service developer portal is scheduled for decommission July 17 2026.
After that date, remove AmadeusProvider from the chain and substitute another provider.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared schema
# ---------------------------------------------------------------------------

class Flight(BaseModel):
    flight_id: str
    origin: str
    destination: str
    departure_time: str   # ISO-8601
    arrival_time: str     # ISO-8601
    price: float
    cabin_class: str      # economy | premium_economy | business | first
    airline: str
    stops: int = 0
    duration_hours: float = Field(default=0.0)
    source: str = "unknown"   # "duffel" | "amadeus" | "mock"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitState:
    CLOSED = "closed"       # healthy — requests flow normally
    OPEN = "open"           # unhealthy — requests short-circuit immediately
    HALF_OPEN = "half_open" # probe: one request allowed to test recovery


class CircuitBreaker:
    """
    Per-provider circuit breaker.

    State machine:
      CLOSED → OPEN after FAILURE_THRESHOLD consecutive failures
      OPEN → HALF_OPEN after RECOVERY_TIMEOUT seconds
      HALF_OPEN → CLOSED on success, → OPEN on failure
    """

    FAILURE_THRESHOLD = 3
    RECOVERY_TIMEOUT = 60.0  # seconds

    def __init__(self, name: str) -> None:
        self.name = name
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - (self._opened_at or 0) >= self.RECOVERY_TIMEOUT:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("Circuit %s → HALF_OPEN (probing)", self.name)
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                logger.info("Circuit %s → CLOSED (recovered)", self.name)
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN or self._failures >= self.FAILURE_THRESHOLD:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit %s → OPEN after %d failure(s)", self.name, self._failures
                )

    def is_available(self) -> bool:
        return self.state != CircuitState.OPEN

    def status(self) -> dict:
        return {
            "provider": self.name,
            "state": self.state,
            "consecutive_failures": self._failures,
        }


# ---------------------------------------------------------------------------
# Provider base
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Raised by a provider when search fails (triggers fallback)."""


class FlightProvider(ABC):
    name: str

    @abstractmethod
    def search(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        passengers: int,
        cabin_class: str,
    ) -> list[Flight]:
        """
        Search for flights. Must raise ProviderError on any failure so the
        chain can move to the next provider.
        """

    def is_configured(self) -> bool:
        """Return False if required credentials are missing — skip without trying."""
        return True


# ---------------------------------------------------------------------------
# Retry helper (used by real providers)
# ---------------------------------------------------------------------------

def _with_retry(fn, max_attempts: int = 3, base_delay: float = 1.0):
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("Attempt %d/%d failed (%s). Retrying in %.1fs…", attempt, max_attempts, exc, delay)
            time.sleep(delay)
    raise ProviderError(f"All {max_attempts} attempts failed: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Duffel provider
# ---------------------------------------------------------------------------

_DUFFEL_HEADERS = {
    "Duffel-Version": "v2",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_CABIN_CLASS_MAP = {
    "economy": "economy",
    "premium_economy": "premium_economy",
    "business": "business",
    "first": "first",
}


def _parse_duffel_offer(offer: dict) -> Flight:
    slices = offer.get("slices", [])
    first_slice = slices[0] if slices else {}
    segments = first_slice.get("segments", [])
    first_seg = segments[0] if segments else {}
    last_seg = segments[-1] if segments else {}

    origin = first_seg.get("origin", {}).get("iata_code", "???")
    destination = last_seg.get("destination", {}).get("iata_code", "???")
    departure_time = first_seg.get("departing_at", "")
    arrival_time = last_seg.get("arriving_at", "")
    airline = first_seg.get("operating_carrier", {}).get("name", "Unknown")
    stops = max(0, len(segments) - 1)

    duration_hours = 0.0
    try:
        dep = datetime.fromisoformat(departure_time.replace("Z", "+00:00"))
        arr = datetime.fromisoformat(arrival_time.replace("Z", "+00:00"))
        duration_hours = round((arr - dep).total_seconds() / 3600, 2)
    except Exception:
        pass

    cabin_class = "economy"
    passengers = offer.get("passengers", [{}])
    if passengers:
        cabin_class = passengers[0].get("cabin_class", "economy")

    return Flight(
        flight_id=offer.get("id", str(uuid.uuid4())),
        origin=origin,
        destination=destination,
        departure_time=departure_time,
        arrival_time=arrival_time,
        price=float(offer.get("total_amount", "0")),
        cabin_class=_CABIN_CLASS_MAP.get(cabin_class, cabin_class),
        airline=airline,
        stops=stops,
        duration_hours=duration_hours,
        source="duffel",
    )


class DuffelProvider(FlightProvider):
    name = "duffel"

    def is_configured(self) -> bool:
        return bool(settings.duffel_api_key)

    def search(self, origin, destination, departure_date, passengers, cabin_class) -> list[Flight]:
        headers = {
            **_DUFFEL_HEADERS,
            "Authorization": f"Bearer {settings.duffel_api_key}",
        }
        payload = {
            "data": {
                "slices": [{"origin": origin.upper(), "destination": destination.upper(), "departure_date": departure_date}],
                "passengers": [{"type": "adult"} for _ in range(passengers)],
                "cabin_class": cabin_class,
                "max_connections": 1,
            }
        }

        def _call() -> list[Flight]:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(f"{settings.duffel_api_base}/air/offer_requests", headers=headers, json=payload)
                resp.raise_for_status()
                offer_request_id = resp.json()["data"]["id"]

                resp2 = client.get(
                    f"{settings.duffel_api_base}/air/offers",
                    headers=headers,
                    params={"offer_request_id": offer_request_id, "limit": 20},
                )
                resp2.raise_for_status()
                offers = resp2.json().get("data", [])

            logger.info("Duffel returned %d offers for %s→%s", len(offers), origin, destination)
            return [_parse_duffel_offer(o) for o in offers]

        return _with_retry(_call)


# ---------------------------------------------------------------------------
# Amadeus provider
# ---------------------------------------------------------------------------

class AmadeusProvider(FlightProvider):
    """
    Amadeus Flight Offers Search API v2.

    Auth: OAuth2 client_credentials — fetch a Bearer token, then use it per-request.
    This is intentionally different from Duffel's static key auth, demonstrating
    why the FlightProvider abstraction exists.

    To get sandbox credentials:
      1. Sign up at developers.amadeus.com
      2. Create an app under "My Apps"
      3. Copy API Key → AMADEUS_API_KEY and API Secret → AMADEUS_API_SECRET in .env

    Note: Amadeus self-service portal is scheduled for decommission July 2025.
    After that, the circuit breaker will open after 3 failed auth attempts and
    the chain will fall through to MockProvider automatically.
    """

    name = "amadeus"
    _TOKEN_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
    _SEARCH_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

    _CABIN_MAP = {
        "economy": "ECONOMY",
        "premium_economy": "PREMIUM_ECONOMY",
        "business": "BUSINESS",
        "first": "FIRST",
    }

    def is_configured(self) -> bool:
        return bool(settings.amadeus_api_key and settings.amadeus_api_secret)

    def _get_token(self, client: httpx.Client) -> str:
        resp = client.post(
            self._TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.amadeus_api_key,
                "client_secret": settings.amadeus_api_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _parse_offer(self, offer: dict) -> Flight:
        itineraries = offer.get("itineraries", [])
        first_it = itineraries[0] if itineraries else {}
        segments = first_it.get("segments", [])
        first_seg = segments[0] if segments else {}
        last_seg = segments[-1] if segments else {}

        origin = first_seg.get("departure", {}).get("iataCode", "???")
        destination = last_seg.get("arrival", {}).get("iataCode", "???")
        departure_time = first_seg.get("departure", {}).get("at", "")
        arrival_time = last_seg.get("arrival", {}).get("at", "")
        carrier_code = first_seg.get("carrierCode", "")
        flight_number = first_seg.get("number", "")
        airline = f"{carrier_code}{flight_number}" if carrier_code else "Unknown"
        stops = max(0, len(segments) - 1)

        duration_hours = 0.0
        try:
            dep = datetime.fromisoformat(departure_time)
            arr = datetime.fromisoformat(arrival_time)
            duration_hours = round((arr - dep).total_seconds() / 3600, 2)
        except Exception:
            pass

        cabin_class = "economy"
        traveler_pricings = offer.get("travelerPricings", [{}])
        if traveler_pricings:
            fare_details = traveler_pricings[0].get("fareDetailsBySegment", [{}])
            if fare_details:
                cabin_class = fare_details[0].get("cabin", "ECONOMY").lower()

        return Flight(
            flight_id=offer.get("id", str(uuid.uuid4())),
            origin=origin,
            destination=destination,
            departure_time=departure_time,
            arrival_time=arrival_time,
            price=float(offer.get("price", {}).get("grandTotal", "0")),
            cabin_class=cabin_class,
            airline=airline,
            stops=stops,
            duration_hours=duration_hours,
            source="amadeus",
        )

    def search(self, origin, destination, departure_date, passengers, cabin_class) -> list[Flight]:
        def _call() -> list[Flight]:
            with httpx.Client(timeout=20.0) as client:
                token = self._get_token(client)
                resp = client.get(
                    self._SEARCH_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "originLocationCode": origin.upper(),
                        "destinationLocationCode": destination.upper(),
                        "departureDate": departure_date,
                        "adults": passengers,
                        "travelClass": self._CABIN_MAP.get(cabin_class, "ECONOMY"),
                        "max": 20,
                        "currencyCode": "USD",
                    },
                )
                resp.raise_for_status()
                offers = resp.json().get("data", [])

            logger.info("Amadeus returned %d offers for %s→%s", len(offers), origin, destination)
            return [self._parse_offer(o) for o in offers]

        return _with_retry(_call)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

_AIRLINES = [
    "United Airlines",
    "Delta Air Lines",
    "American Airlines",
    "Southwest Airlines",
    "JetBlue Airways",
    "Alaska Airlines",
]

_CABIN_PRICES = {
    "economy": (120, 400),
    "premium_economy": (350, 700),
    "business": (800, 2500),
    "first": (2000, 6000),
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class MockProvider(FlightProvider):
    """
    Always-on synthetic fallback.
    Generates deterministic plausible flights so the agent always has data to work with.
    """

    name = "mock"

    def search(self, origin, destination, departure_date, passengers, cabin_class) -> list[Flight]:
        random.seed(f"{origin}{destination}{departure_date}")

        try:
            dep_base = datetime.strptime(departure_date, "%Y-%m-%d").replace(hour=6, minute=0)
        except ValueError:
            dep_base = datetime.utcnow().replace(hour=6, minute=0, microsecond=0)

        lo, hi = _CABIN_PRICES.get(cabin_class, (150, 500))
        flights: list[Flight] = []

        for i in range(5):
            airline = random.choice(_AIRLINES)
            dep_offset = timedelta(hours=i * 2 + random.randint(0, 1), minutes=random.choice([0, 15, 30, 45]))
            duration_h = round(random.uniform(1.5, 5.5), 2)
            dep_dt = dep_base + dep_offset
            arr_dt = dep_dt + timedelta(hours=duration_h)
            stops = 0 if random.random() < 0.6 else 1
            price = round(random.uniform(lo, hi) * passengers, 2)

            flights.append(Flight(
                flight_id=f"mock_{uuid.uuid4().hex[:12]}",
                origin=origin.upper(),
                destination=destination.upper(),
                departure_time=_iso(dep_dt),
                arrival_time=_iso(arr_dt),
                price=price,
                cabin_class=cabin_class,
                airline=airline,
                stops=stops,
                duration_hours=duration_h,
                source="mock",
            ))

        logger.info("Mock provider generated %d flights for %s→%s", len(flights), origin, destination)
        return flights


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------

class ProviderChain:
    """
    Tries providers in order. Each provider is guarded by its own CircuitBreaker.

    A provider is skipped if:
      - is_configured() returns False (missing credentials)
      - its circuit is OPEN

    Falls through to the next provider on ProviderError or empty results.
    The Mock provider is always last and never circuit-breaks.
    """

    def __init__(self, providers: list[FlightProvider]) -> None:
        self._providers = providers
        self._breakers: dict[str, CircuitBreaker] = {
            p.name: CircuitBreaker(p.name) for p in providers
        }

    def search(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        passengers: int,
        cabin_class: str,
    ) -> tuple[list[Flight], str]:
        """
        Returns (flights, provider_name_used).
        Raises RuntimeError only if ALL providers fail (should never happen — mock always works).
        """
        for provider in self._providers:
            if not provider.is_configured():
                logger.debug("Skipping %s — not configured", provider.name)
                continue

            breaker = self._breakers[provider.name]
            if not breaker.is_available():
                logger.warning("Skipping %s — circuit is OPEN", provider.name)
                continue

            try:
                flights = provider.search(origin, destination, departure_date, passengers, cabin_class)
                if not flights:
                    logger.info("%s returned 0 results — trying next provider", provider.name)
                    # Empty result is not a circuit-breaker event — API worked, just no flights
                    continue
                breaker.record_success()
                logger.info("Provider %s succeeded with %d flights", provider.name, len(flights))
                return flights, provider.name

            except ProviderError as exc:
                breaker.record_failure()
                logger.error(
                    "Provider %s failed (circuit=%s): %s",
                    provider.name, breaker.state, exc,
                )
                continue

        raise RuntimeError("All providers exhausted — no flights found")

    def health(self) -> list[dict]:
        return [
            {**b.status(), "configured": self._providers[i].is_configured()}
            for i, (name, b) in enumerate(self._breakers.items())
        ]


# ---------------------------------------------------------------------------
# Default chain singleton
# ---------------------------------------------------------------------------

_default_chain: Optional[ProviderChain] = None
_chain_lock = threading.Lock()


def get_provider_chain() -> ProviderChain:
    global _default_chain
    with _chain_lock:
        if _default_chain is None:
            _default_chain = ProviderChain([
                DuffelProvider(),
                AmadeusProvider(),
                MockProvider(),
            ])
    return _default_chain


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    passengers: int = 1,
    cabin_class: str = "economy",
) -> list[Flight]:
    """
    Search for flights using the provider chain.
    Returns results sorted by price ascending.
    """
    chain = get_provider_chain()
    flights, provider_used = chain.search(origin, destination, departure_date, passengers, cabin_class)
    logger.info("search_flights returning %d results from provider=%s", len(flights), provider_used)
    return sorted(flights, key=lambda f: f.price)
