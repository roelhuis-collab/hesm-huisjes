"""
Tests for src/ai/claude.py — system prompt builder + streaming helper.

We don't talk to the real Anthropic API in CI. ``set_client_for_testing``
substitutes a tiny fake whose ``messages.stream(...)`` async-context
yields predetermined text chunks, mirroring the SDK shape we actually
consume (just ``stream.text_stream``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, time, timedelta
from typing import Any

import pytest
from src.ai import claude as ai_claude
from src.optimizer.learning import (
    ActivationStatus,
    DailyPattern,
    LearnedProfile,
    ThermalSignature,
)
from src.optimizer.policy import Strategy, default_policy
from src.state import firestore as fs
from src.state.models import Decision, SystemState

# Reuse the in-memory Firestore fixture from conftest
pytestmark = pytest.mark.usefixtures("fake_db")


# ---------------------------------------------------------------------------
# Fake AsyncAnthropic client
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, chunks: list[str], capture: dict[str, Any]) -> None:
        self._chunks = chunks
        self._capture = capture

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for c in self._chunks:
                yield c

        return gen()


class _FakeStreamCtx:
    def __init__(self, chunks: list[str], capture: dict[str, Any], **kwargs: Any) -> None:
        self._chunks = chunks
        self._capture = capture
        self._capture.update(kwargs)

    async def __aenter__(self) -> _FakeStream:
        return _FakeStream(self._chunks, self._capture)

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeMessages:
    def __init__(self, chunks: list[str], capture: dict[str, Any]) -> None:
        self._chunks = chunks
        self._capture = capture

    def stream(self, **kwargs: Any) -> _FakeStreamCtx:
        return _FakeStreamCtx(self._chunks, self._capture, **kwargs)


class FakeAnthropicClient:
    """Captures the kwargs passed to ``messages.stream`` for assertions."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self.last_kwargs: dict[str, Any] = {}
        self.messages = _FakeMessages(chunks or ["Hallo ", "Roel."], self.last_kwargs)


@pytest.fixture
def fake_client() -> Iterator[FakeAnthropicClient]:
    client = FakeAnthropicClient()
    ai_claude.set_client_for_testing(client)
    yield client
    ai_claude.set_client_for_testing(None)


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


def _seed_state(boiler: float = 52.0, indoor: float = 20.5) -> None:
    """Write one fresh SystemState snapshot into the fake Firestore."""
    fs.save_state_snapshot(
        SystemState(
            timestamp=datetime.now(),
            pv_power=4500, house_load=800, hp_power=900, dompelaar_on=False,
            boiler_temp=boiler, buffer_temp=38, indoor_temp=indoor, outdoor_temp=12,
            cop=4.2, grid_import=-200.0, price_eur_kwh=0.085,
        )
    )


def _seed_decision(reason: str, tag: str = "BOOST") -> None:
    fs.save_decision(
        Decision(
            timestamp=datetime.now(),
            tag=tag,                       # type: ignore[arg-type]
            action="boiler_charge",
            reason=reason,
            rationale="…",
            boiler_target_temp=60.0,
            dompelaar_on=False,
            heat_pump_allowed=True,
        )
    )


def test_system_prompt_includes_persona_and_limits() -> None:
    prompt = ai_claude.build_system_prompt()
    # Persona-level facts
    assert "WeHeat Blackbird P80" in prompt
    assert "parquet" in prompt
    # Layer-1 hard limits
    assert "50" in prompt   # floor max
    assert "45" in prompt   # legionella floor


def test_system_prompt_includes_active_strategy() -> None:
    prompt = ai_claude.build_system_prompt()
    # Default policy uses MAX_SAVING preset
    assert "max_saving" in prompt
    assert "cost=" in prompt and "comfort=" in prompt


def test_system_prompt_handles_empty_state() -> None:
    """When the optimizer hasn't run yet, the prompt should say so."""
    prompt = ai_claude.build_system_prompt()
    assert "No state snapshot available" in prompt
    assert "optimizer has not run" in prompt


def test_system_prompt_includes_live_state_when_present() -> None:
    _seed_state(boiler=58.0, indoor=21.3)
    prompt = ai_claude.build_system_prompt()
    assert "58 °C" in prompt           # boiler temp
    assert "21.3 °C" in prompt         # indoor
    assert "Dompelaar: uit" in prompt
    assert "€0.085/kWh" in prompt


def test_system_prompt_lists_recent_decisions_newest_first() -> None:
    # Insert in chronological order; helper sorts newest-first
    base = datetime.now() - timedelta(hours=3)
    fs.save_decision(Decision(
        timestamp=base, tag="COAST", action="x",
        reason="oudere reden", rationale="…",
        boiler_target_temp=55, dompelaar_on=False, heat_pump_allowed=False,
    ))
    fs.save_decision(Decision(
        timestamp=datetime.now(), tag="BOOST", action="x",
        reason="nieuwste reden", rationale="…",
        boiler_target_temp=60, dompelaar_on=False, heat_pump_allowed=True,
    ))

    prompt = ai_claude.build_system_prompt()
    # Both reasons must be present
    assert "nieuwste reden" in prompt
    assert "oudere reden" in prompt
    # Newest must appear first
    assert prompt.index("nieuwste reden") < prompt.index("oudere reden")


def test_system_prompt_reflects_custom_strategy() -> None:
    p = default_policy()
    p.strategy = Strategy.COMFORT_FIRST
    fs.save_policy(p)

    prompt = ai_claude.build_system_prompt()
    assert "comfort_first" in prompt


def test_system_prompt_snapshot_returns_debug_view() -> None:
    _seed_state()
    _seed_decision("test reden")

    snap = ai_claude.system_prompt_snapshot()
    assert snap["state"] is not None
    assert snap["state"]["boiler_temp"] == 52.0
    assert len(snap["decisions"]) == 1
    assert snap["decisions"][0]["reason"] == "test reden"
    assert snap["rendered_chars"] > 500


# ---------------------------------------------------------------------------
# answer_with_context — streaming + caching
# ---------------------------------------------------------------------------


def _decode_sse(chunks: list[bytes]) -> list[dict[str, Any]]:
    """Parse a list of SSE-encoded byte chunks into their JSON payloads."""
    out: list[dict[str, Any]] = []
    for c in chunks:
        text = c.decode("utf-8")
        assert text.startswith("data: "), f"not SSE: {text!r}"
        assert text.endswith("\n\n"), f"missing terminator: {text!r}"
        out.append(json.loads(text[len("data: "):-2]))
    return out


async def test_answer_with_context_streams_deltas_and_done(
    fake_client: FakeAnthropicClient,
) -> None:
    chunks: list[bytes] = []
    async for c in ai_claude.answer_with_context(
        [{"role": "user", "content": "Wat doet de pomp nu?"}]
    ):
        chunks.append(c)

    events = _decode_sse(chunks)
    # Two deltas for the two fake stream chunks + one final 'done'
    assert events == [
        {"type": "delta", "text": "Hallo "},
        {"type": "delta", "text": "Roel."},
        {"type": "done"},
    ]


async def test_answer_with_context_attaches_cache_control_to_system(
    fake_client: FakeAnthropicClient,
) -> None:
    async for _ in ai_claude.answer_with_context(
        [{"role": "user", "content": "test"}]
    ):
        pass

    sent = fake_client.last_kwargs
    assert sent["model"] == "claude-sonnet-4-6"
    assert isinstance(sent["system"], list)
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "WeHeat Blackbird P80" in sent["system"][0]["text"]
    assert sent["messages"] == [{"role": "user", "content": "test"}]


async def test_answer_with_context_respects_HESM_CHAT_MODEL_env(
    fake_client: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HESM_CHAT_MODEL", "claude-opus-4-7")
    async for _ in ai_claude.answer_with_context(
        [{"role": "user", "content": "?"}]
    ):
        pass
    assert fake_client.last_kwargs["model"] == "claude-opus-4-7"


async def test_answer_with_context_rejects_empty_messages(
    fake_client: FakeAnthropicClient,
) -> None:
    with pytest.raises(ValueError, match="messages must not be empty"):
        async for _ in ai_claude.answer_with_context([]):
            pass


async def test_answer_with_context_skips_empty_chunks() -> None:
    """Empty deltas are common in real streams; we must not emit them."""
    client = FakeAnthropicClient(chunks=["", "first", "", "second", ""])
    ai_claude.set_client_for_testing(client)
    try:
        chunks: list[bytes] = []
        async for c in ai_claude.answer_with_context(
            [{"role": "user", "content": "?"}]
        ):
            chunks.append(c)
        events = _decode_sse(chunks)
        # Only non-empty deltas + a single 'done'
        assert events == [
            {"type": "delta", "text": "first"},
            {"type": "delta", "text": "second"},
            {"type": "done"},
        ]
    finally:
        ai_claude.set_client_for_testing(None)


# ---------------------------------------------------------------------------
# Sanity — Layer-3 + activation reference doesn't blow up the prompt
# (Some future revision may include learned profile in the prompt;
#  for now we just guarantee construction succeeds with one present.)
# ---------------------------------------------------------------------------


def test_build_system_prompt_with_active_learning_does_not_error() -> None:
    fs.update_activation_status(ActivationStatus(
        is_active=True,
        activated_at=datetime.now(),
        data_start=datetime.now() - timedelta(days=42),
    ))
    fs.save_learned_profile(LearnedProfile(
        daily=DailyPattern(typical_return_time=time(17, 30), confidence=0.7),
        thermal=ThermalSignature(heat_loss_w_per_k=180.0),
        last_trained=datetime.now(),
        samples_used=4032,
    ))

    prompt = ai_claude.build_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0
