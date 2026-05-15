"""
FastAPI application — TripEnforce backend.

Endpoints:
  POST /trip                  — natural language trip request → agent recommendation
  POST /book/{flight_id}      — confirm a booking
  PUT  /policy/{company_id}   — admin: update policy rules
  GET  /spend/{company_id}    — spend summary
  GET  /flagged               — bookings pending manager approval
  POST /approve/{booking_id}  — approve a flagged booking
  POST /reject/{booking_id}   — reject a flagged booking
  GET  /admin                 — admin dashboard UI
  WS   /trip/stream           — streaming agent reasoning
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agent import AgentRecommendation, run_agent
from providers import get_provider_chain
from config import settings
from models import (
    Booking,
    BookingStatus,
    CabinClass,
    Company,
    SpendCategory,
    SpendRecord,
    User,
    create_tables,
    get_session,
    seed_database,
)
from policy import PolicyUpdateRequest, get_policy, update_policy
from preferences import get_preferences

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TripEnforce starting up — creating tables and seeding data…")
    create_tables()
    seed_database()
    yield
    logger.info("TripEnforce shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    description="AI-powered corporate travel booking agent with policy enforcement",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def serve_ui() -> FileResponse:
    return FileResponse("static/index.html")


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class TripRequest(BaseModel):
    request: str = Field(..., description="Natural language trip request", min_length=5)
    user_id: str
    company_id: str


class TripResponse(BaseModel):
    recommendation: AgentRecommendation
    user_id: str
    company_id: str


class BookRequest(BaseModel):
    user_id: str
    company_id: str
    origin: str
    destination: str
    departure_time: str
    arrival_time: str
    airline: str
    cabin_class: str
    stops: int = 0
    price: float
    trip_purpose: Optional[str] = None
    natural_language_request: Optional[str] = None


class BookResponse(BaseModel):
    booking_id: str
    status: str
    spend_record_id: str
    approval_required: bool
    message: str


class SpendSummary(BaseModel):
    company_id: str
    total_spend: float
    by_category: dict[str, float]
    by_employee: list[dict[str, Any]]
    by_trip_purpose: dict[str, float]
    record_count: int


class PolicyResponse(BaseModel):
    company_id: str
    rules: list[dict[str, Any]]
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_or_404(user_id: str, session: Session) -> User:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id!r} not found")
    return user


def _get_company_or_404(company_id: str, session: Session) -> Company:
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail=f"Company {company_id!r} not found")
    return company


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.app_version)


@app.get("/providers/health", tags=["meta"])
async def providers_health() -> dict:
    """Circuit breaker state for each flight provider."""
    return {"providers": get_provider_chain().health()}


@app.post("/trip", response_model=TripResponse, tags=["trips"])
async def plan_trip(body: TripRequest) -> TripResponse:
    """
    Main endpoint. Accepts a natural language trip request and returns
    a ranked list of compliant flight options with a plain-language recommendation.
    """
    logger.info("Trip request user=%s company=%s: %r", body.user_id, body.company_id, body.request)

    # Validate user + company exist
    with get_session() as session:
        _get_user_or_404(body.user_id, session)
        _get_company_or_404(body.company_id, session)

    recommendation = run_agent(
        request=body.request,
        user_id=body.user_id,
        company_id=body.company_id,
    )

    return TripResponse(
        recommendation=recommendation,
        user_id=body.user_id,
        company_id=body.company_id,
    )


@app.post("/book/{flight_id}", response_model=BookResponse, tags=["trips"])
async def confirm_booking(flight_id: str, body: BookRequest) -> BookResponse:
    """
    Confirm a booking. Stores the booking, records spend, and updates preferences.
    """
    from policy import BookingInput, check_policy

    with get_session() as session:
        user = _get_user_or_404(body.user_id, session)
        _get_company_or_404(body.company_id, session)

        # Run a final policy check at confirmation time
        booking_input = BookingInput(
            flight_id=flight_id,
            origin=body.origin,
            destination=body.destination,
            departure_time=body.departure_time,
            arrival_time=body.arrival_time,
            price=body.price,
            cabin_class=body.cabin_class,
            airline=body.airline,
            stops=body.stops,
        )
        policy_result = check_policy(booking_input, body.company_id)

        # Hard block on non-compliant, non-approval bookings
        hard_violations = [v for v in policy_result.violations if "approval" not in v.lower()]
        if hard_violations:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Booking blocked by policy",
                    "violations": hard_violations,
                    "nearest_compliant": policy_result.nearest_compliant,
                },
            )

        try:
            cabin = CabinClass(body.cabin_class)
        except ValueError:
            cabin = CabinClass.ECONOMY

        status = (
            BookingStatus.NEEDS_APPROVAL if policy_result.requires_approval
            else BookingStatus.CONFIRMED
        )

        booking = Booking(
            id=str(uuid.uuid4()),
            user_id=body.user_id,
            company_id=body.company_id,
            flight_id=flight_id,
            origin=body.origin,
            destination=body.destination,
            departure_time=body.departure_time,
            arrival_time=body.arrival_time,
            airline=body.airline,
            cabin_class=cabin,
            stops=body.stops,
            price=body.price,
            status=status,
            trip_purpose=body.trip_purpose,
            natural_language_request=body.natural_language_request,
            approval_required=policy_result.requires_approval,
        )
        session.add(booking)
        session.flush()  # get booking.id before creating spend

        # Inline spend record (avoids nested session)
        from models import SpendRecord, SpendCategory
        from categorization import categorize_booking, infer_trip_purpose
        purpose = infer_trip_purpose(body.natural_language_request or body.trip_purpose)
        spend = SpendRecord(
            booking_id=booking.id,
            user_id=body.user_id,
            company_id=body.company_id,
            amount=body.price,
            category=categorize_booking(booking),
            cost_center=user.cost_center,
            trip_purpose=purpose,
        )
        session.add(spend)
        session.flush()

        booking_id = booking.id
        spend_id = spend.id
        approval_required = policy_result.requires_approval
        booking_airline = booking.airline
        booking_cabin = booking.cabin_class

    # Update preferences in a fresh session
    from preferences import store_preference as _store_pref
    with get_session() as session:
        booking_obj = session.get(Booking, booking_id)
        if booking_obj:
            _store_pref(body.user_id, booking_obj)

    msg = (
        "Booking confirmed and submitted for manager approval."
        if approval_required
        else "Booking confirmed successfully."
    )

    logger.info("Booking confirmed id=%s user=%s approval=%s", booking_id, body.user_id, approval_required)

    return BookResponse(
        booking_id=booking_id,
        status=status.value,
        spend_record_id=spend_id,
        approval_required=approval_required,
        message=msg,
    )


@app.put("/policy/{company_id}", response_model=PolicyResponse, tags=["admin"])
async def update_company_policy(company_id: str, body: PolicyUpdateRequest) -> PolicyResponse:
    """Admin endpoint — replace the full ruleset for a company."""
    with get_session() as session:
        _get_company_or_404(company_id, session)

    try:
        update_policy(company_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rules = get_policy(company_id) or {}
    logger.info("Policy updated company=%s rules=%d", company_id, len(body.rules))
    return PolicyResponse(
        company_id=company_id,
        rules=rules.get("rules", []),
        message=f"Policy updated with {len(body.rules)} rule(s).",
    )


@app.get("/policy/{company_id}", response_model=PolicyResponse, tags=["admin"])
async def get_company_policy(company_id: str) -> PolicyResponse:
    """Return current policy rules for a company."""
    rules = get_policy(company_id)
    if rules is None:
        raise HTTPException(status_code=404, detail=f"No policy for company {company_id!r}")
    return PolicyResponse(
        company_id=company_id,
        rules=rules.get("rules", []),
        message="Current policy rules.",
    )


@app.get("/spend/{company_id}", response_model=SpendSummary, tags=["reporting"])
async def get_spend_summary(company_id: str) -> SpendSummary:
    """Return spend summary broken down by category, employee, and trip purpose."""
    with get_session() as session:
        _get_company_or_404(company_id, session)

        records = (
            session.query(SpendRecord, User)
            .join(User, SpendRecord.user_id == User.id)
            .filter(SpendRecord.company_id == company_id)
            .all()
        )

    total = sum(r.SpendRecord.amount for r in records)

    by_category: dict[str, float] = {}
    by_purpose: dict[str, float] = {}
    employee_spend: dict[str, dict[str, Any]] = {}

    for row in records:
        rec = row.SpendRecord
        user = row.User

        cat = rec.category.value
        by_category[cat] = by_category.get(cat, 0.0) + rec.amount

        purpose = rec.trip_purpose or "unknown"
        by_purpose[purpose] = by_purpose.get(purpose, 0.0) + rec.amount

        emp_key = user.id
        if emp_key not in employee_spend:
            employee_spend[emp_key] = {
                "user_id": user.id,
                "name": user.name,
                "email": user.email,
                "total": 0.0,
                "cost_center": user.cost_center,
            }
        employee_spend[emp_key]["total"] += rec.amount

    return SpendSummary(
        company_id=company_id,
        total_spend=round(total, 2),
        by_category={k: round(v, 2) for k, v in by_category.items()},
        by_employee=sorted(employee_spend.values(), key=lambda x: x["total"], reverse=True),
        by_trip_purpose={k: round(v, 2) for k, v in by_purpose.items()},
        record_count=len(records),
    )


# ---------------------------------------------------------------------------
# Admin — flagged bookings + approve/reject
# ---------------------------------------------------------------------------

@app.get("/flagged", tags=["admin"])
async def get_flagged_bookings() -> list[dict[str, Any]]:
    """Return all bookings pending manager approval."""
    with get_session() as session:
        rows = (
            session.query(Booking, User)
            .join(User, Booking.user_id == User.id)
            .filter(Booking.status == BookingStatus.NEEDS_APPROVAL)
            .order_by(Booking.created_at.desc())
            .all()
        )
        return [
            {
                "booking_id": row.Booking.id,
                "user_name": row.User.name,
                "user_email": row.User.email,
                "cost_center": row.User.cost_center,
                "origin": row.Booking.origin,
                "destination": row.Booking.destination,
                "airline": row.Booking.airline,
                "cabin_class": row.Booking.cabin_class.value,
                "price": row.Booking.price,
                "departure_time": row.Booking.departure_time,
                "trip_purpose": row.Booking.trip_purpose or "—",
                "created_at": row.Booking.created_at.isoformat() if row.Booking.created_at else None,
            }
            for row in rows
        ]


@app.post("/approve/{booking_id}", tags=["admin"])
async def approve_booking(booking_id: str) -> dict[str, Any]:
    """Approve a flagged booking — sets status to confirmed."""
    with get_session() as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.status != BookingStatus.NEEDS_APPROVAL:
            raise HTTPException(
                status_code=400,
                detail=f"Booking status is '{booking.status.value}', not pending approval",
            )
        booking.status = BookingStatus.CONFIRMED
        booking.approval_required = False
        session.flush()

    logger.info("Booking approved id=%s", booking_id)
    return {"booking_id": booking_id, "status": "confirmed", "message": "Booking approved."}


@app.post("/reject/{booking_id}", tags=["admin"])
async def reject_booking(booking_id: str) -> dict[str, Any]:
    """Reject a flagged booking — sets status to cancelled."""
    with get_session() as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        booking.status = BookingStatus.CANCELLED
        session.flush()

    logger.info("Booking rejected id=%s", booking_id)
    return {"booking_id": booking_id, "status": "cancelled", "message": "Booking rejected."}


@app.get("/admin", include_in_schema=False)
async def serve_admin() -> FileResponse:
    return FileResponse("static/admin.html")


# ---------------------------------------------------------------------------
# WebSocket streaming endpoint
# ---------------------------------------------------------------------------

@app.websocket("/trip/stream")
async def trip_stream(websocket: WebSocket) -> None:
    """
    Stream agent reasoning steps in real time.

    Client sends JSON: {"request": "...", "user_id": "...", "company_id": "..."}
    Server streams JSON step objects, closes on completion.
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send_json({"type": "error", "message": "Invalid JSON"})
            await websocket.close()
            return

        request = payload.get("request", "")
        user_id = payload.get("user_id", "")
        company_id = payload.get("company_id", "")

        if not all([request, user_id, company_id]):
            await websocket.send_json(
                {"type": "error", "message": "request, user_id, and company_id are required"}
            )
            await websocket.close()
            return

        queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()

        def _step_callback(step: dict[str, Any]) -> None:
            queue.put_nowait(step)

        loop = asyncio.get_event_loop()

        # Run the blocking agent in a thread pool so it doesn't block the event loop
        agent_task = loop.run_in_executor(
            None,
            lambda: run_agent(request, user_id, company_id, stream_callback=_step_callback),
        )

        # Stream steps as they arrive, drain queue after agent finishes
        while not agent_task.done():
            try:
                step = await asyncio.wait_for(queue.get(), timeout=0.1)
                await websocket.send_json(step)
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                agent_task.cancel()
                return

        # Drain any remaining steps
        while not queue.empty():
            step = queue.get_nowait()
            await websocket.send_json(step)

        result: AgentRecommendation = await agent_task
        await websocket.send_json({"type": "done", "result": result.model_dump()})

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.exception("WebSocket error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
