"""
SQLAlchemy ORM models and database initialization for TripEnforce.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session

from config import settings


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CabinClass(str, enum.Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class BookingStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    NEEDS_APPROVAL = "needs_approval"


class SpendCategory(str, enum.Enum):
    AIRFARE = "airfare"
    LODGING = "lodging"
    GROUND = "ground"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    users: Mapped[list[User]] = relationship("User", back_populates="company")
    policies: Mapped[list[Policy]] = relationship("Policy", back_populates="company")
    bookings: Mapped[list[Booking]] = relationship("Booking", back_populates="company")
    spend_records: Mapped[list[SpendRecord]] = relationship("SpendRecord", back_populates="company")

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.name!r}>"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="employee")  # employee | manager | admin
    cost_center: Mapped[Optional[str]] = mapped_column(String(100))
    manager_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped[Company] = relationship("Company", back_populates="users")
    bookings: Mapped[list[Booking]] = relationship("Booking", back_populates="user")
    preferences: Mapped[list[TravelerPreference]] = relationship("TravelerPreference", back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)
    # JSON blob: {"rules": [...]}  — see policy.py for schema
    rules: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("company_id", name="uq_policy_company"),)

    company: Mapped[Company] = relationship("Company", back_populates="policies")

    def __repr__(self) -> str:
        return f"<Policy company_id={self.company_id}>"


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    company_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)

    # Flight identifiers
    flight_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    destination: Mapped[str] = mapped_column(String(10), nullable=False)
    departure_time: Mapped[str] = mapped_column(String(50), nullable=False)
    arrival_time: Mapped[str] = mapped_column(String(50), nullable=False)
    airline: Mapped[str] = mapped_column(String(100), nullable=False)
    cabin_class: Mapped[CabinClass] = mapped_column(Enum(CabinClass), nullable=False)
    stops: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float, nullable=False)

    # Booking metadata
    status: Mapped[BookingStatus] = mapped_column(Enum(BookingStatus), default=BookingStatus.CONFIRMED)
    trip_purpose: Mapped[Optional[str]] = mapped_column(String(255))
    natural_language_request: Mapped[Optional[str]] = mapped_column(Text)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="bookings")
    company: Mapped[Company] = relationship("Company", back_populates="bookings")
    spend_record: Mapped[Optional[SpendRecord]] = relationship("SpendRecord", back_populates="booking", uselist=False)

    def __repr__(self) -> str:
        return f"<Booking id={self.id} {self.origin}->{self.destination} ${self.price}>"


class TravelerPreference(Base):
    __tablename__ = "preferences"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, unique=True)
    # JSON blob with preference data — see preferences.py for schema
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="preferences")

    def __repr__(self) -> str:
        return f"<TravelerPreference user_id={self.user_id}>"


class SpendRecord(Base):
    __tablename__ = "spend_records"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    booking_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("bookings.id"), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    company_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[SpendCategory] = mapped_column(Enum(SpendCategory), nullable=False)
    cost_center: Mapped[Optional[str]] = mapped_column(String(100))
    trip_purpose: Mapped[Optional[str]] = mapped_column(String(255))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    booking: Mapped[Booking] = relationship("Booking", back_populates="spend_record")
    company: Mapped[Company] = relationship("Company", back_populates="spend_records")

    def __repr__(self) -> str:
        return f"<SpendRecord booking_id={self.booking_id} amount={self.amount} category={self.category}>"


# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.debug,
)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context-manager session factory. Commits on success, rolls back on error."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_tables() -> None:
    """Create all tables (idempotent — safe to call on every startup)."""
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

DEFAULT_POLICY_RULES = {
    "rules": [
        {
            "id": "economy_short_haul",
            "description": "Economy class only for flights under 6 hours",
            "type": "cabin_class",
            "max_duration_hours": 6,
            "allowed_classes": ["economy"],
        },
        {
            "id": "max_hotel_rate",
            "description": "Max nightly hotel rate $250",
            "type": "hotel_rate",
            "max_nightly_rate": 250.0,
        },
        {
            "id": "manager_approval_threshold",
            "description": "Trips over $1,000 total require manager approval",
            "type": "spend_limit",
            "threshold": 1000.0,
            "action": "require_approval",
        },
        {
            "id": "airline_allowlist",
            "description": "Approved airlines (empty = all airlines allowed)",
            "type": "airline_allowlist",
            "airlines": [],
        },
    ]
}


def seed_database() -> None:
    """Insert test company, users, and default policy if they don't exist."""
    with Session(engine) as session:
        # Check if already seeded
        existing = session.get(Company, "00000000-0000-0000-0000-000000000001")
        if existing:
            return

        company = Company(
            id="00000000-0000-0000-0000-000000000001",
            name="Acme Corp",
        )
        session.add(company)
        session.flush()

        manager = User(
            id="00000000-0000-0000-0000-000000000010",
            company_id=company.id,
            email="manager@acme.com",
            name="Alice Manager",
            role="manager",
            cost_center="ENG-001",
        )
        session.add(manager)
        session.flush()

        employee = User(
            id="00000000-0000-0000-0000-000000000011",
            company_id=company.id,
            email="bob@acme.com",
            name="Bob Employee",
            role="employee",
            cost_center="ENG-001",
            manager_id=manager.id,
        )
        session.add(employee)

        repeat_traveler = User(
            id="00000000-0000-0000-0000-000000000012",
            company_id=company.id,
            email="carol@acme.com",
            name="Carol Frequent",
            role="employee",
            cost_center="SALES-002",
            manager_id=manager.id,
        )
        session.add(repeat_traveler)

        policy = Policy(
            id="00000000-0000-0000-0000-000000000020",
            company_id=company.id,
            rules=DEFAULT_POLICY_RULES,
        )
        session.add(policy)

        # Seed Carol's preferences to power scenario 4
        carol_prefs = TravelerPreference(
            id="00000000-0000-0000-0000-000000000030",
            user_id=repeat_traveler.id,
            data={
                "preferred_airlines": ["United", "Delta"],
                "seat_type": "window",
                "cabin_class": "economy",
                "preferred_hotel_chains": ["Marriott"],
                "booking_count": 5,
            },
        )
        session.add(carol_prefs)

        session.commit()
        print("[seed] Database seeded with test company, users, and default policy.")
