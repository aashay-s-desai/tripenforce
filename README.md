# TripEnforce

AI-powered corporate travel booking agent with policy enforcement.  
Built with FastAPI, Claude (tool use), Duffel API, and PostgreSQL.

**Live demo:** https://tripenforce.up.railway.app  
**Admin dashboard:** https://tripenforce.up.railway.app/admin

---

## Architecture

```
POST /trip
  └─ agent.py          ← Claude tool-use loop
       ├─ travel_api.py   ← Duffel API + mock fallback
       ├─ policy.py        ← DB-backed rule engine
       ├─ preferences.py   ← per-user preference ranking
       └─ categorization.py

POST /book/{flight_id}
  └─ policy re-check → Booking → SpendRecord → preference update

PUT  /policy/{company_id}   → update rules JSON
GET  /spend/{company_id}    → spend summary
WS   /trip/stream           → real-time agent reasoning
```

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker Desktop (for Postgres)
- Anthropic API key — [console.anthropic.com](https://console.anthropic.com)
- Duffel sandbox key — [app.duffel.com/join](https://app.duffel.com/join) (see below)

### 2. Clone and install

```bash
cd TripEnforce
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
DUFFEL_API_KEY=duffel_test_...
DATABASE_URL=postgresql://tripenforce:tripenforce@localhost:5432/tripenforce
```

### 4. Start Postgres

```bash
docker compose up -d
```

### 5. Run the server

```bash
uvicorn main:app --reload
```

On first boot it will:
- Create all tables
- Seed one test company (`Acme Corp`), three users, and default policy rules

API docs: https://tripenforce.up.railway.app/docs

---

## Getting Duffel Sandbox Credentials

1. Go to [app.duffel.com/join](https://app.duffel.com/join) and create a free account
2. In the dashboard, select **Test mode** (toggle in top-left)
3. Go to **Settings → Access tokens**
4. Create a token — it will start with `duffel_test_`
5. Paste it into `.env` as `DUFFEL_API_KEY`

The sandbox returns real flight schedules with fake payment processing. No credit card needed.

---

## Example curl Commands

### Plan a trip

```bash
curl -s -X POST https://tripenforce.up.railway.app/trip \
  -H "Content-Type: application/json" \
  -d '{
    "request": "Economy flight from Chicago to New York departing 2026-06-01",
    "user_id": "00000000-0000-0000-0000-000000000011",
    "company_id": "00000000-0000-0000-0000-000000000001"
  }' | jq .
```

### Confirm a booking

```bash
curl -s -X POST "https://tripenforce.up.railway.app/book/FLIGHT_ID_HERE" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "00000000-0000-0000-0000-000000000011",
    "company_id": "00000000-0000-0000-0000-000000000001",
    "origin": "ORD",
    "destination": "JFK",
    "departure_time": "2026-06-01T08:00:00",
    "arrival_time": "2026-06-01T11:30:00",
    "airline": "United Airlines",
    "cabin_class": "economy",
    "stops": 0,
    "price": 249.00,
    "natural_language_request": "Economy flight from Chicago to New York"
  }' | jq .
```

### View current policy

```bash
curl -s https://tripenforce.up.railway.app/policy/00000000-0000-0000-0000-000000000001 | jq .
```

### Update policy (admin)

```bash
curl -s -X PUT https://tripenforce.up.railway.app/policy/00000000-0000-0000-0000-000000000001 \
  -H "Content-Type: application/json" \
  -d '{
    "rules": [
      {
        "id": "economy_short_haul",
        "description": "Economy only under 6 hours",
        "type": "cabin_class",
        "max_duration_hours": 6,
        "allowed_classes": ["economy"]
      },
      {
        "id": "spend_limit",
        "description": "Trips over $1500 require approval",
        "type": "spend_limit",
        "threshold": 1500.0,
        "action": "require_approval"
      }
    ]
  }' | jq .
```

### Spend summary

```bash
curl -s https://tripenforce.up.railway.app/spend/00000000-0000-0000-0000-000000000001 | jq .
```

### WebSocket streaming (wscat)

```bash
npm install -g wscat
wscat -c wss://tripenforce.up.railway.app/trip/stream
# paste: {"request":"Economy ORD to JFK 2026-06-01","user_id":"00000000-0000-0000-0000-000000000011","company_id":"00000000-0000-0000-0000-000000000001"}
```

---

## Running the Demo

```bash
# All 5 scenarios
python demo.py

# Single scenario
python demo.py --scenario 2
```

---

## Default Policy Rules (seeded on startup)

| Rule | Details |
|------|---------|
| Economy short-haul | Economy only for flights < 6 hours |
| Hotel rate cap | Max $250/night |
| Spend threshold | Trips > $1,000 require manager approval |
| Airline allowlist | Empty = all airlines allowed |

---

## Likely Failure Points

| Issue | Cause | Fix |
|-------|-------|-----|
| `connection refused :5432` | Postgres not running | `docker compose up -d` |
| `401 Unauthorized` from Duffel | Invalid/expired API key | Regenerate in Duffel dashboard |
| Agent returns 0 compliant flights | All flights fail policy check | Check rules via `GET /policy/{id}` |
| `ANTHROPIC_API_KEY` missing | `.env` not loaded | Ensure `.env` exists and `pydantic-settings` is installed |
| Mock flights always returned | `DUFFEL_API_KEY` blank or Duffel down | Expected — mock is the designed fallback |
| Preferences not influencing rank | First booking for user | Preferences build up after 2+ bookings |

---

## Test User IDs

| ID | Name | Role |
|----|------|------|
| `00000000-0000-0000-0000-000000000010` | Alice Manager | manager |
| `00000000-0000-0000-0000-000000000011` | Bob Employee | employee |
| `00000000-0000-0000-0000-000000000012` | Carol Frequent | employee (has pre-seeded preferences) |

Company ID: `00000000-0000-0000-0000-000000000001`
