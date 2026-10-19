"""The event schema from the brief, as a Pydantic model shared by the Ingest API and the load script."""

from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ActorType = Literal["user", "service", "api_key"]
Action = Literal[
    "user.login", "user.logout", "user.password_reset", "user.mfa_enabled",
    "user.invite", "user.role_change", "user.delete", "api_key.create",
    "api_key.revoke", "invoice.create", "invoice.update", "invoice.delete",
    "invoice.export", "payment.refund", "project.create", "project.update",
    "project.delete", "document.view", "document.download", "document.share",
    "settings.update", "webhook.create",
]
ResourceType = Literal["user", "api_key", "invoice", "payment", "project", "document", "settings", "webhook"]
Result = Literal["success", "failure"]

ACTOR_ID_PREFIX = {"user": "usr_", "service": "svc_", "api_key": "key_"}

# Allowance for callers whose clocks run ahead. Anything further in the future is a caller bug,
# and would drag the Summary window (which ends on the newest day in the data) into the future
# for every tenant.
MAX_FUTURE_SKEW = timedelta(hours=1)


class Event(BaseModel):
    # Unknown fields are an error, not silently dropped: an audit log shouldn't lose data quietly.
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    tenant_id: str = Field(pattern=r"^t_\d+$")
    actor_id: str = Field(min_length=1)
    actor_type: ActorType
    actor_email: Optional[str]  # no default: the key is required, but its value may be null
    action: Action
    resource_type: ResourceType
    resource_id: str = Field(min_length=1)
    result: Result
    ip: IPv4Address
    metadata: dict[str, Any]
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_iso_string(cls, value):
        # Pydantic would also accept numbers (unix time); the brief says ISO 8601 strings.
        if not isinstance(value, str):
            raise ValueError("occurred_at must be an ISO 8601 string, e.g. 2026-09-14T08:31:07.412Z")
        return value

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_to_utc(cls, value: datetime):
        if value.tzinfo is None:
            raise ValueError("occurred_at needs a timezone, e.g. a trailing Z")
        value = value.astimezone(timezone.utc)
        if value > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
            raise ValueError("occurred_at is more than 1 hour in the future")
        return value

    @model_validator(mode="after")
    def actor_fields_match_type(self):
        if self.actor_email is not None and self.actor_type != "user":
            raise ValueError("actor_email must be null unless actor_type is user")
        if not self.actor_id.startswith(ACTOR_ID_PREFIX[self.actor_type]):
            raise ValueError(f"actor_id for actor_type {self.actor_type} must start with {ACTOR_ID_PREFIX[self.actor_type]}")
        return self


class IngestBatch(BaseModel):
    events: list[Event] = Field(min_length=1, max_length=1000)
