"""
Firestore data layer.

This module is the only place that touches Firestore directly. The rest of
the codebase (optimizer cycle, learning check, push notifications, FastAPI
endpoints) calls these helpers and stays decoupled from the SDK.

Collections
-----------
``policy/current``           — singleton, the live ``Policy`` (Layer 1+2)
``activation/status``        — singleton, the user-facing Layer 3 activation flag
``learned_profile/current``  — singleton, the trained ``LearnedProfile``
``state_snapshots/{id}``     — quarter-hourly ``SystemState`` records
``decisions/{id}``           — every optimizer cycle's persisted ``Decision``
``fcm_tokens/{token}``       — registered push targets (doc id == token)

Datetime handling
-----------------
We serialize via ``model_dump(mode="json")`` so all timestamps are written as
ISO 8601 strings. ISO 8601 sorts lexicographically when using a fixed
timezone, so range queries on ``timestamp`` work correctly. This trades
native Firestore Timestamp typing for portability and easier mocking.

Cost
----
Quarter-hourly snapshots = ~35k docs/year, well under €5/month at Firestore's
free-tier overage rates. Revisit only if the bill spikes after 6 months
(see roadmap notes in CLAUDE.md).

Auth
----
In production: credentials come from Workload Identity Federation, the
service account's IAM permissions are sufficient, and ``initialize_app``
in ``main.py`` finds them automatically.
Locally: Application Default Credentials (``gcloud auth application-default
login``) work the same way.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.optimizer.learning import ActivationStatus, LearnedProfile
from src.optimizer.policy import Policy, default_policy
from src.state.models import (
    ActivationStatusDTO,
    Decision,
    FCMToken,
    LearnedProfileDTO,
    SystemState,
)

if TYPE_CHECKING:
    pass

# Collection / document paths -------------------------------------------------

POLICY_COLLECTION = "policy"
POLICY_DOC = "current"

ACTIVATION_COLLECTION = "activation"
ACTIVATION_DOC = "status"

LEARNED_PROFILE_COLLECTION = "learned_profile"
LEARNED_PROFILE_DOC = "current"

STATE_SNAPSHOTS_COLLECTION = "state_snapshots"
DECISIONS_COLLECTION = "decisions"
FCM_TOKENS_COLLECTION = "fcm_tokens"


# Client management -----------------------------------------------------------

# Tests substitute an in-memory fake here; production reads it as None and
# falls back to the real Firestore client.
_db_override: Any = None


def _db() -> Any:
    """Return the active Firestore client, allowing test substitution."""
    if _db_override is not None:
        return _db_override
    # Imported lazily so unit tests don't need google-cloud-firestore on path
    from firebase_admin import firestore

    return firestore.client()


def set_client_for_testing(fake: Any | None) -> None:
    """Inject a fake Firestore client. Pass ``None`` to revert."""
    global _db_override
    _db_override = fake


# Policy ----------------------------------------------------------------------


def get_policy() -> Policy:
    """Load the live policy. Returns ``default_policy()`` if none is stored yet."""
    snap = _db().collection(POLICY_COLLECTION).document(POLICY_DOC).get()
    if not snap.exists:
        return default_policy()
    data = snap.to_dict() or {}
    return Policy.from_firestore(data)


def save_policy(policy: Policy) -> None:
    """Overwrite the live policy."""
    policy.updated_at = datetime.now()
    _db().collection(POLICY_COLLECTION).document(POLICY_DOC).set(policy.to_firestore())


# Activation status -----------------------------------------------------------


def get_activation_status() -> ActivationStatus:
    """Load the user's Layer-3 activation status. Defaults to dormant."""
    snap = _db().collection(ACTIVATION_COLLECTION).document(ACTIVATION_DOC).get()
    if not snap.exists:
        return ActivationStatus()
    return ActivationStatusDTO.model_validate(snap.to_dict() or {}).to_dataclass()


def update_activation_status(status: ActivationStatus) -> None:
    """Persist the activation status (full overwrite, not merge)."""
    payload = ActivationStatusDTO.from_dataclass(status).model_dump(mode="json")
    _db().collection(ACTIVATION_COLLECTION).document(ACTIVATION_DOC).set(payload)


# Learned profile -------------------------------------------------------------


def get_learned_profile() -> LearnedProfile:
    """Load the trained profile. Returns an empty profile if none stored."""
    snap = _db().collection(LEARNED_PROFILE_COLLECTION).document(LEARNED_PROFILE_DOC).get()
    if not snap.exists:
        return LearnedProfile()
    return LearnedProfileDTO.model_validate(snap.to_dict() or {}).to_dataclass()


def save_learned_profile(profile: LearnedProfile) -> None:
    """Overwrite the trained profile (called nightly after training)."""
    payload = LearnedProfileDTO.from_dataclass(profile).model_dump(mode="json")
    _db().collection(LEARNED_PROFILE_COLLECTION).document(LEARNED_PROFILE_DOC).set(payload)


# State snapshots -------------------------------------------------------------


def save_state_snapshot(state: SystemState) -> None:
    """Persist one quarter-hourly snapshot. Doc id is auto-generated."""
    payload = state.model_dump(mode="json")
    _db().collection(STATE_SNAPSHOTS_COLLECTION).add(payload)


def count_state_samples(since: datetime) -> int:
    """Count snapshots with ``timestamp >= since``.

    Used by the daily learning-readiness check to compute data quality.
    """
    cutoff = since.isoformat()
    coll = _db().collection(STATE_SNAPSHOTS_COLLECTION).where("timestamp", ">=", cutoff)
    # Prefer aggregate count when available; fall back to streaming docs for
    # the in-memory fake used in tests.
    if hasattr(coll, "count"):
        agg = coll.count().get()
        # Real Firestore returns [[AggregationResult]] — extract the value.
        if agg and isinstance(agg, list) and agg and isinstance(agg[0], list) and agg[0]:
            return int(agg[0][0].value)
        if isinstance(agg, int):
            return agg
    return sum(1 for _ in coll.stream())


def get_data_start_date() -> datetime | None:
    """Return the timestamp of the earliest snapshot, or ``None`` if empty."""
    coll = _db().collection(STATE_SNAPSHOTS_COLLECTION).order_by("timestamp").limit(1)
    docs = list(coll.stream())
    if not docs:
        return None
    raw = docs[0].to_dict() or {}
    ts = raw.get("timestamp")
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        return datetime.fromisoformat(ts)
    return None


# Decisions -------------------------------------------------------------------


def save_decision(decision: Decision) -> None:
    """Persist one optimizer cycle's decision. Doc id is auto-generated."""
    _db().collection(DECISIONS_COLLECTION).add(decision.model_dump(mode="json"))


# FCM tokens ------------------------------------------------------------------


def get_user_fcm_tokens() -> list[str]:
    """Return all currently-valid FCM tokens (single-user system for now)."""
    coll = _db().collection(FCM_TOKENS_COLLECTION).where("valid", "==", True)
    out: list[str] = []
    for doc in coll.stream():
        data = doc.to_dict() or {}
        token = data.get("token")
        if isinstance(token, str):
            out.append(token)
    return out


def save_fcm_token(record: FCMToken) -> None:
    """Upsert an FCM registration. The token itself is the document id."""
    _db().collection(FCM_TOKENS_COLLECTION).document(record.token).set(
        record.model_dump(mode="json")
    )


def mark_fcm_token_invalid(token: str) -> None:
    """Soft-delete a token after FCM reports it as unregistered."""
    _db().collection(FCM_TOKENS_COLLECTION).document(token).set({"valid": False}, merge=True)
