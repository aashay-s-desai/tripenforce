"""
travel_api — backward-compatible re-export.

All logic lives in providers.py. This module exists so existing imports
(agent.py, main.py) don't need to change.
"""
from providers import Flight, search_flights

__all__ = ["Flight", "search_flights"]
