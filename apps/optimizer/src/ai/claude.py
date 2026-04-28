"""
Conversational layer for HESM — answers user questions about live system
state and decisions using the Anthropic Claude API.

Why this exists
---------------
The optimizer makes a decision every 15 min and persists a one-line
``rationale`` for it. That's the *system's* explanation. This module is
the *user's* explanation — when Roel asks "why is the dompelaar on?", we
hand Claude the live snapshot, the recent decisions, and the policy, and
let it explain in plain Dutch.

Caching strategy
----------------
The system prompt is split into two byte-stable layers and one volatile
tail, with a single ``cache_control`` breakpoint at the end of the system
text. State changes every 15 min, so within a chat session (rapid
follow-up questions inside one cycle window) the system prompt is
byte-identical and subsequent requests read the cache at ~10% of the
write cost. Across cycles the cache is rewritten — that's fine; cache
breakeven is two reads.

Prefix order, stable → volatile (everything cached up to the breakpoint):

  1. Persona + house spec + Layer-1 limits  — almost never changes
  2. Layer-2 strategy weights               — changes when user updates
  3. Most-recent SystemState snapshot       — changes every 15 min
  4. Last 24 h of Decision rationales       — changes every 15 min

The user's actual messages stay in ``messages`` and are never cached.

Streaming
---------
We expose ``answer_with_context()`` as an async iterator yielding
Server-Sent Event chunks. The FastAPI endpoint wraps it in a
``StreamingResponse`` with ``text/event-stream`` so the dashboard can
render tokens as they arrive.

Model
-----
Defaults to ``claude-sonnet-4-6`` — Roel's choice, balances cost and
intelligence for this conversational use case. Configurable via
``HESM_CHAT_MODEL`` env if we want to A/B against Opus 4.7 later.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, Protocol

from src.optimizer.policy import Policy
from src.state.firestore import (
    get_policy,
    get_recent_decisions,
    get_recent_state_snapshot,
)
from src.state.models import Decision, SystemState

log = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


_PERSONA = """\
You are HESM, the in-house energy assistant for Roel Huisjes' home in \
Sittard, NL. You explain in Dutch what the system is doing and why, \
referring to the policy and the live state below.

# House
- Heat pump:   WeHeat Blackbird P80 (8 kW thermal, R290)
- DHW:         Inventum 500 L boiler tank, 3 kW dompelaar (immersion heater)
- Buffer:      WeHeat 100 L
- Thermostat:  Honeywell Lyric T6 wired
- PV:          Growatt MOD 9000 TL3-X, 26 panels, ~11 000 kWh/year
- Smart meter: HomeWizard Wi-Fi P1

# Heating circuit limits — never recommend exceeding these
- Floor circuit max flow: 50 °C (parquet)
- Bathroom max flow:      55 °C
- Radiator max flow:      50 °C
- Boiler legionella floor 45 °C, hard ceiling 65 °C
- Dompelaar only with PV surplus > 2.5 kW or negative spot price; \
hard price ceiling €0.10/kWh

# How to answer
- Answer in Dutch.
- Be concrete: cite the actual numbers from the state below.
- If something is uncertain or data is missing, say so.
- The optimizer's most recent decision is the source of truth for \
\"what is happening now\". Quote its reason verbatim if relevant.
- If the user asks for an action that would violate a limit, refuse \
and explain which limit and why.
"""


def _format_policy(policy: Policy) -> str:
    weights = policy.weights
    return (
        f"# Active strategy\n"
        f"- Preset:           {policy.strategy.value}\n"
        f"- Weights:          cost={weights.cost:.2f} comfort={weights.comfort:.2f} "
        f"self-cons={weights.self_consumption:.2f} "
        f"renewable={weights.renewable_share:.2f}\n"
        f"- Layer-3 active:   {policy.learning_enabled}\n"
        f"- Living-room band: {policy.limits.living_room.min_c:.1f}–"
        f"{policy.limits.living_room.max_c:.1f} °C\n"
        f"- Bedroom band:     {policy.limits.bedroom.min_c:.1f}–"
        f"{policy.limits.bedroom.max_c:.1f} °C\n"
    )


def _format_state(state: SystemState | None) -> str:
    if state is None:
        return (
            "# Current state\n"
            "No state snapshot available yet. The optimizer cycle hasn't \n"
            "produced a reading — say so plainly if the user asks about live values.\n"
        )

    grid = "n/a" if state.grid_import is None else f"{state.grid_import:.0f} W"
    cop = "n/a" if state.cop is None else f"{state.cop:.2f}"
    price = (
        "n/a" if state.price_eur_kwh is None else f"€{state.price_eur_kwh:.3f}/kWh"
    )
    return (
        f"# Current state (captured {state.timestamp.isoformat(timespec='minutes')})\n"
        f"- Indoor:    {state.indoor_temp:.1f} °C    Outdoor: {state.outdoor_temp:.1f} °C\n"
        f"- Boiler:    {state.boiler_temp:.0f} °C     Buffer:  {state.buffer_temp:.0f} °C\n"
        f"- PV:        {state.pv_power:.0f} W      House load: {state.house_load:.0f} W\n"
        f"- HP:        {state.hp_power:.0f} W      Dompelaar: "
        f"{'AAN' if state.dompelaar_on else 'uit'}\n"
        f"- Grid:      {grid}        Spot price: {price}    COP: {cop}\n"
    )


def _format_decisions(decisions: list[Decision], limit: int = 6) -> str:
    if not decisions:
        return "# Recent decisions\nNone — optimizer has not run yet.\n"

    lines = ["# Recent decisions (newest first)"]
    for d in decisions[:limit]:
        ts = d.timestamp.isoformat(timespec="minutes")
        lines.append(f"- {ts} [{d.tag}] {d.reason}")
    return "\n".join(lines) + "\n"


def build_system_prompt() -> str:
    """Compose the full system prompt from live Firestore state.

    All inputs are read from Firestore at call time. Within a 15-min cycle
    the values are stable, so back-to-back requests cache cleanly.
    """
    policy = get_policy()
    state = get_recent_state_snapshot()
    decisions = get_recent_decisions(hours=24)

    return "\n\n".join(
        [
            _PERSONA,
            _format_policy(policy),
            _format_state(state),
            _format_decisions(decisions),
        ]
    )


# ---------------------------------------------------------------------------
# Anthropic client + streaming
# ---------------------------------------------------------------------------


class _StreamLike(Protocol):
    """The minimal surface ``messages.stream(...).__aenter__`` returns."""

    text_stream: AsyncIterator[str]


class _StreamCtxLike(Protocol):
    async def __aenter__(self) -> _StreamLike: ...
    async def __aexit__(self, *args: Any) -> None: ...


class _MessagesLike(Protocol):
    def stream(self, **kwargs: Any) -> _StreamCtxLike: ...


class _AsyncClientLike(Protocol):
    messages: _MessagesLike


_client: _AsyncClientLike | None = None


def _get_client() -> _AsyncClientLike:
    """Return a process-wide Anthropic client, lazily initialized."""
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        # AsyncAnthropic structurally satisfies _AsyncClientLike but mypy
        # can't match Protocol-on-third-party-type — explicit cast is fine.
        _client = AsyncAnthropic()  # type: ignore[assignment]
    assert _client is not None
    return _client


def set_client_for_testing(fake: Any | None) -> None:
    """Inject a fake client for tests. Pass ``None`` to revert.

    Accepts ``Any`` because it's a test seam: the protocol matching that
    mypy does for class-Protocol pairs is too strict for our minimal fake.
    """
    global _client
    _client = fake


# ---------------------------------------------------------------------------
# Public surface — used by /chat in main.py
# ---------------------------------------------------------------------------


async def answer_with_context(messages: list[dict[str, Any]]) -> AsyncIterator[bytes]:
    """Stream a Claude reply, yielding SSE-formatted bytes.

    Each yielded chunk is a single ``data: {...}\\n\\n`` SSE event:

      * ``{"type": "delta", "text": "..."}``  — incremental text token
      * ``{"type": "done"}``                  — emitted after the final token

    Errors propagate as raised exceptions (FastAPI surfaces them as 500
    once they bubble out of the ``StreamingResponse``).
    """
    if not messages:
        raise ValueError("messages must not be empty")

    system = [
        {
            "type": "text",
            "text": build_system_prompt(),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    model = os.environ.get("HESM_CHAT_MODEL", DEFAULT_CHAT_MODEL)
    client = _get_client()

    log.info("chat: %d messages, model=%s", len(messages), model)

    async with client.messages.stream(
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
        system=system,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            if text:
                yield _sse_event({"type": "delta", "text": text})

    yield _sse_event({"type": "done"})


def _sse_event(payload: dict[str, Any]) -> bytes:
    """Encode a single SSE ``data:`` frame."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


# ---------------------------------------------------------------------------
# Helper exposed for tests + dashboards (debug endpoint, future)
# ---------------------------------------------------------------------------


def system_prompt_snapshot() -> dict[str, Any]:
    """Return a debuggable view of what the system prompt currently looks like.

    Used by tests and (eventually) a /debug endpoint so we can see exactly
    what context Claude is getting without firing a real chat request.
    """
    policy = get_policy()
    state = get_recent_state_snapshot()
    decisions = get_recent_decisions(hours=24)
    return {
        "policy": policy.to_firestore(),
        "state": state.model_dump(mode="json") if state else None,
        "decisions": [d.model_dump(mode="json") for d in decisions[:6]],
        "rendered_chars": len(build_system_prompt()),
        "model": os.environ.get("HESM_CHAT_MODEL", DEFAULT_CHAT_MODEL),
    }


# Suppress an unused-import warning — ``asdict`` is here for future use
# in /debug formatting; remove if unused after PR11.
_ = asdict
