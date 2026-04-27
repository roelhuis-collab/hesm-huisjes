"""
Pydantic models for everything persisted in Firestore.

The optimizer's internal runtime types (e.g. ``policy.Policy``,
``learning.ActivationStatus``, ``learning.LearnedProfile``) remain dataclasses
so business logic stays free of Pydantic at the call sites. These DTOs are
the *persistence schema* and provide:

  * stable on-disk shapes (Firestore documents)
  * round-tripping helpers between dataclass <-> DTO <-> Firestore dict
  * validation at the storage boundary

Three groups of models live here:

  1. Snapshots & decisions — purely persistence, no runtime counterpart yet
     (``SystemState``, ``Decision``).
  2. Mirrors of the dataclasses in ``optimizer/learning.py`` so we can
     persist them without touching that module (``ActivationStatusDTO``,
     ``LearnedProfileDTO``, plus the inner ``DailyPattern`` etc.).
  3. Auxiliary persistence-only types (``FCMToken``).

The runtime dataclasses remain canonical. DTOs convert in both directions.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.optimizer.learning import (
    ActivationStatus,
    DailyPattern,
    ForecastBias,
    LearnedProfile,
    ThermalSignature,
    WeeklyPattern,
)

# ---------------------------------------------------------------------------
# State snapshot — written every 15 min by the optimizer
# ---------------------------------------------------------------------------


class SystemState(BaseModel):
    """A single quarter-hourly snapshot of the whole system.

    Field units are SI throughout: power in W, temperature in °C.
    The ``timestamp`` is the moment the snapshot was *captured*, not written.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    pv_power: float = Field(description="Instantaneous PV inverter output, W")
    house_load: float = Field(description="Net house electrical load, W (positive = consuming)")
    hp_power: float = Field(description="Heat pump electrical input, W")
    dompelaar_on: bool
    boiler_temp: float = Field(description="Top-of-tank DHW boiler temperature, °C")
    buffer_temp: float = Field(description="Heating buffer tank temperature, °C")
    indoor_temp: float = Field(description="Indoor reference temperature (living room), °C")
    outdoor_temp: float = Field(description="Outdoor temperature, °C")

    # Optional / computed-when-available
    cop: float | None = Field(default=None, description="Heat pump COP if reported")
    grid_import: float | None = Field(default=None, description="P1 net import, W (negative = export)")
    price_eur_kwh: float | None = Field(default=None, description="All-in spot price at this timestamp")


# ---------------------------------------------------------------------------
# Decision — every plan_next_quarter() output is persisted
# ---------------------------------------------------------------------------

DecisionTag = Literal["BOOST", "PV-DUMP", "COAST", "NORMAL", "NEG-PRICE", "OVERRIDE"]


class Decision(BaseModel):
    """One optimizer cycle's plan, persisted for audit + UI rendering."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    tag: DecisionTag
    action: str = Field(description="Short human-readable action, English (translated in UI)")
    reason: str = Field(description="One-sentence Dutch rationale shown in the dashboard")
    rationale: str = Field(description="Longer explanation including the numbers used")

    boiler_target_temp: float
    dompelaar_on: bool
    heat_pump_allowed: bool
    indoor_setpoint_offset: float = 0.0

    estimated_savings_eur: float | None = None
    strategy_used: str | None = Field(default=None, description="Strategy preset active for this cycle")
    learning_active: bool = False


# ---------------------------------------------------------------------------
# Activation status & learned profile mirrors
# ---------------------------------------------------------------------------


class DailyPatternDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    typical_wake_time: time | None = None
    typical_leave_time: time | None = None
    typical_return_time: time | None = None
    typical_sleep_time: time | None = None
    confidence: float = 0.0

    @classmethod
    def from_dataclass(cls, src: DailyPattern) -> DailyPatternDTO:
        return cls(
            typical_wake_time=src.typical_wake_time,
            typical_leave_time=src.typical_leave_time,
            typical_return_time=src.typical_return_time,
            typical_sleep_time=src.typical_sleep_time,
            confidence=src.confidence,
        )

    def to_dataclass(self) -> DailyPattern:
        return DailyPattern(
            typical_wake_time=self.typical_wake_time,
            typical_leave_time=self.typical_leave_time,
            typical_return_time=self.typical_return_time,
            typical_sleep_time=self.typical_sleep_time,
            confidence=self.confidence,
        )


class WeeklyPatternDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday_offset_c: float = 0.0
    saturday_pattern: DailyPatternDTO = Field(default_factory=DailyPatternDTO)
    sunday_pattern: DailyPatternDTO = Field(default_factory=DailyPatternDTO)
    weekday_pattern: DailyPatternDTO = Field(default_factory=DailyPatternDTO)

    @classmethod
    def from_dataclass(cls, src: WeeklyPattern) -> WeeklyPatternDTO:
        return cls(
            weekday_offset_c=src.weekday_offset_c,
            saturday_pattern=DailyPatternDTO.from_dataclass(src.saturday_pattern),
            sunday_pattern=DailyPatternDTO.from_dataclass(src.sunday_pattern),
            weekday_pattern=DailyPatternDTO.from_dataclass(src.weekday_pattern),
        )

    def to_dataclass(self) -> WeeklyPattern:
        return WeeklyPattern(
            weekday_offset_c=self.weekday_offset_c,
            saturday_pattern=self.saturday_pattern.to_dataclass(),
            sunday_pattern=self.sunday_pattern.to_dataclass(),
            weekday_pattern=self.weekday_pattern.to_dataclass(),
        )


class ThermalSignatureDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heat_loss_w_per_k: float = 0.0
    pv_to_indoor_lag_min: float = 0.0
    boiler_decay_c_per_hour: float = 0.0
    floor_thermal_mass_kwh_per_c: float = 0.0

    @classmethod
    def from_dataclass(cls, src: ThermalSignature) -> ThermalSignatureDTO:
        return cls(**src.__dict__)

    def to_dataclass(self) -> ThermalSignature:
        return ThermalSignature(**self.model_dump())


class ForecastBiasDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pv_forecast_multiplier: float = 1.0
    heat_demand_multiplier: float = 1.0

    @classmethod
    def from_dataclass(cls, src: ForecastBias) -> ForecastBiasDTO:
        return cls(**src.__dict__)

    def to_dataclass(self) -> ForecastBias:
        return ForecastBias(**self.model_dump())


class LearnedProfileDTO(BaseModel):
    """Persistence form of ``LearnedProfile`` from ``optimizer/learning.py``."""

    model_config = ConfigDict(extra="forbid")

    daily: DailyPatternDTO = Field(default_factory=DailyPatternDTO)
    weekly: WeeklyPatternDTO = Field(default_factory=WeeklyPatternDTO)
    thermal: ThermalSignatureDTO = Field(default_factory=ThermalSignatureDTO)
    forecast_bias: ForecastBiasDTO = Field(default_factory=ForecastBiasDTO)
    last_trained: datetime | None = None
    samples_used: int = 0

    @classmethod
    def from_dataclass(cls, src: LearnedProfile) -> LearnedProfileDTO:
        return cls(
            daily=DailyPatternDTO.from_dataclass(src.daily),
            weekly=WeeklyPatternDTO.from_dataclass(src.weekly),
            thermal=ThermalSignatureDTO.from_dataclass(src.thermal),
            forecast_bias=ForecastBiasDTO.from_dataclass(src.forecast_bias),
            last_trained=src.last_trained,
            samples_used=src.samples_used,
        )

    def to_dataclass(self) -> LearnedProfile:
        return LearnedProfile(
            daily=self.daily.to_dataclass(),
            weekly=self.weekly.to_dataclass(),
            thermal=self.thermal.to_dataclass(),
            forecast_bias=self.forecast_bias.to_dataclass(),
            last_trained=self.last_trained,
            samples_used=self.samples_used,
        )


class ActivationStatusDTO(BaseModel):
    """Persistence form of ``ActivationStatus`` from ``optimizer/learning.py``."""

    model_config = ConfigDict(extra="forbid")

    is_active: bool = False
    activated_at: datetime | None = None
    push_sent_at: datetime | None = None
    push_dismissed_count: int = 0
    data_start: datetime | None = None

    @classmethod
    def from_dataclass(cls, src: ActivationStatus) -> ActivationStatusDTO:
        return cls(**src.__dict__)

    def to_dataclass(self) -> ActivationStatus:
        return ActivationStatus(**self.model_dump())


# ---------------------------------------------------------------------------
# FCM tokens — multi-device push targets
# ---------------------------------------------------------------------------


class FCMToken(BaseModel):
    """A registered Firebase Cloud Messaging token for push delivery."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(description="The opaque FCM registration token (also doc id)")
    platform: Literal["ios", "android", "web"] = "web"
    user_id: str | None = Field(default=None, description="Firebase Auth uid that registered this token")
    label: str | None = Field(default=None, description="Optional device label, e.g. 'iPad Roel'")
    valid: bool = True
    created_at: datetime = Field(default_factory=datetime.now)
    last_used_at: datetime | None = None
