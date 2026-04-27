"""
Policy layer — Layer 1 (hard limits) + Layer 2 (strategy weights).

Layer 1 limits are NEVER violated by the optimizer or AI. They define the
safe envelope inside which all decisions must stay.

Layer 2 strategy weights determine which objective the optimizer prioritizes
when multiple safe options exist. Set by the user, modifiable any time.

Layer 3 (learning) lives separately in `learning.py` and feeds adjustments
into Layer 2 — but never overrides Layer 1.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Strategy(Enum):
    MAX_SAVING = "max_saving"
    COMFORT_FIRST = "comfort_first"
    MAX_SELF_CONSUMPTION = "max_self_consumption"
    ECO_GREEN_HOURS = "eco_green_hours"
    CUSTOM = "custom"


@dataclass
class TempBand:
    """Min/max temperature for a zone."""
    min_c: float
    max_c: float

    def clamp(self, value: float) -> float:
        return max(self.min_c, min(self.max_c, value))

    def contains(self, value: float) -> bool:
        return self.min_c <= value <= self.max_c


@dataclass
class SystemLimits:
    """Layer 1 — hard limits. Never violated, by anyone."""

    # Heating circuit flow temperature ceilings
    floor_max_flow_c: float = 50.0          # parquet limit
    bathroom_max_flow_c: float = 55.0       # higher allowed in badkamer
    radiator_max_flow_c: float = 50.0       # Jaga LTV is fine at this

    # DHW (boiler 500L)
    boiler_legionella_floor_c: float = 45.0  # absolute minimum
    boiler_max_c: float = 65.0               # tank rating

    # Indoor comfort bands per zone
    living_room: TempBand = field(default_factory=lambda: TempBand(19.5, 22.0))
    bedroom: TempBand = field(default_factory=lambda: TempBand(17.0, 20.0))
    bathroom: TempBand = field(default_factory=lambda: TempBand(20.0, 24.0))

    # Dompelaar safety
    dompelaar_max_price_eur_kwh: float = 0.10   # hard ceiling, no exceptions
    dompelaar_only_with_pv_above_w: float = 2500  # OR negative price

    # Heat pump
    hp_min_run_minutes: int = 15            # avoid short cycling

    def validate(self) -> list[str]:
        """Return list of validation errors. Empty = valid."""
        errors = []
        if self.floor_max_flow_c > 55:
            errors.append("floor_max_flow_c > 55°C riskeert parquet")
        if self.boiler_legionella_floor_c < 45:
            errors.append("boiler_legionella_floor_c < 45°C is niet veilig")
        if self.boiler_max_c > 70:
            errors.append("boiler_max_c > 70°C overschrijdt tank-spec")
        for name, band in [("living_room", self.living_room), ("bedroom", self.bedroom)]:
            if band.min_c >= band.max_c:
                errors.append(f"{name}: min_c >= max_c")
        return errors


@dataclass
class StrategyWeights:
    """Layer 2 — relative weights summed to 1.0."""
    cost: float = 0.55
    comfort: float = 0.25
    self_consumption: float = 0.15
    renewable_share: float = 0.05

    def normalize(self) -> "StrategyWeights":
        total = self.cost + self.comfort + self.self_consumption + self.renewable_share
        if total == 0:
            return StrategyWeights()
        return StrategyWeights(
            cost=self.cost / total,
            comfort=self.comfort / total,
            self_consumption=self.self_consumption / total,
            renewable_share=self.renewable_share / total,
        )

    @classmethod
    def from_preset(cls, strategy: Strategy) -> "StrategyWeights":
        """Predefined weight sets per strategy preset."""
        presets = {
            Strategy.MAX_SAVING: cls(cost=0.75, comfort=0.15, self_consumption=0.07, renewable_share=0.03),
            Strategy.COMFORT_FIRST: cls(cost=0.20, comfort=0.65, self_consumption=0.10, renewable_share=0.05),
            Strategy.MAX_SELF_CONSUMPTION: cls(cost=0.20, comfort=0.20, self_consumption=0.55, renewable_share=0.05),
            Strategy.ECO_GREEN_HOURS: cls(cost=0.30, comfort=0.20, self_consumption=0.20, renewable_share=0.30),
        }
        return presets.get(strategy, cls())


@dataclass
class Policy:
    """The complete user-controlled policy: limits + strategy."""

    limits: SystemLimits = field(default_factory=SystemLimits)
    strategy: Strategy = Strategy.MAX_SAVING
    custom_weights: StrategyWeights | None = None
    learning_enabled: bool = False              # set True after user activates Layer 3
    overrides: dict[str, Any] = field(default_factory=dict)  # temporary overrides
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def weights(self) -> StrategyWeights:
        """Effective weights — custom if strategy is CUSTOM, else preset."""
        if self.strategy == Strategy.CUSTOM and self.custom_weights:
            return self.custom_weights.normalize()
        return StrategyWeights.from_preset(self.strategy)

    def to_firestore(self) -> dict[str, Any]:
        """Serialize for Firestore write."""
        return {
            "limits": asdict(self.limits),
            "strategy": self.strategy.value,
            "custom_weights": asdict(self.custom_weights) if self.custom_weights else None,
            "learning_enabled": self.learning_enabled,
            "overrides": self.overrides,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> "Policy":
        """Reconstruct from Firestore document."""
        limits_data = data.get("limits", {})
        living = limits_data.pop("living_room", {})
        bedroom = limits_data.pop("bedroom", {})
        bathroom = limits_data.pop("bathroom", {})
        limits = SystemLimits(
            **limits_data,
            living_room=TempBand(**living) if living else TempBand(19.5, 22.0),
            bedroom=TempBand(**bedroom) if bedroom else TempBand(17.0, 20.0),
            bathroom=TempBand(**bathroom) if bathroom else TempBand(20.0, 24.0),
        )
        custom = data.get("custom_weights")
        return cls(
            limits=limits,
            strategy=Strategy(data.get("strategy", "max_saving")),
            custom_weights=StrategyWeights(**custom) if custom else None,
            learning_enabled=data.get("learning_enabled", False),
            overrides=data.get("overrides", {}),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(),
        )


def default_policy() -> Policy:
    """Sensible defaults for a new install — bias toward saving with comfort floor."""
    return Policy(
        limits=SystemLimits(),
        strategy=Strategy.MAX_SAVING,
        learning_enabled=False,
    )
