"""
models.py — Pydantic models for request validation and response shaping.

Pydantic automatically validates incoming JSON against these models.
If a required field is missing or has the wrong type, FastAPI returns
a clear 422 error automatically — no manual validation needed.
"""

from typing import Any, Optional
from pydantic import BaseModel, HttpUrl, field_validator


class SubscriptionCreate(BaseModel):
    """Body for POST /api/subscriptions."""

    target_url: HttpUrl  # Pydantic validates this is a real HTTP/HTTPS URL
    event_filter: str = "*"  # e.g. "order.created", "user.*", or "*" for all
    secret: Optional[str] = None  # Shared secret for HMAC signing

    @field_validator("event_filter")
    @classmethod
    def validate_filter(cls, v: str) -> str:
        # Allow "type.subtype", "type.*", or plain "*"
        parts = v.split(".")
        if len(parts) > 2:
            raise ValueError("event_filter must be like 'order.created', 'order.*', or '*'")
        return v


class EventIngest(BaseModel):
    """Body for POST /api/events."""

    event_type: str  # e.g. "order.created"
    payload: dict[str, Any]  # Arbitrary JSON payload from the sender

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if not v or "." not in v:
            raise ValueError("event_type must be in 'category.action' format, e.g. 'order.created'")
        return v


class RetryRequest(BaseModel):
    """Body for POST /api/deliveries/{attempt_id}/retry."""

    # No fields needed — the attempt ID in the URL is enough.
    pass
