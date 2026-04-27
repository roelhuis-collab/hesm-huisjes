"""
Layer 3 — Adaptive learning. Dormant until activated by user via push-prompt.

The learning layer extracts behavioral and physical patterns from accumulated
historical data, then feeds them back into the optimizer as soft suggestions
within Layer 1's hard envelope.

Activation timeline:
  Day 0–41 : data collection only, this module is bypassed entirely
  Day 42   : `jobs/learning_check.py` detects sufficient data, sends FCM push
  Day 42+N : user taps "Activate" in the app → policy.learning_enabled = True
  Day 42+N+1 onward: nightly retraining, suggestions integrated next morning

Even after activation: every learned pattern is shown in the UI, every
suggestion is overrulable by the user. Layer 1 limits remain inviolable.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

# Minimum data needed before activation can be offered to the user
MIN_DATA_DAYS = 42  # 6 weeks
MIN_DATA_QUALITY = 0.85  # fraction of expected samples actually present


@dataclass
class DailyPattern:
    """Patterns that recur on a daily basis."""
    typical_wake_time: time | None = None       # earliest indoor activity
    typical_leave_time: time | None = None      # last activity → coast can start
    typical_return_time: time | None = None     # pre-heat target
    typical_sleep_time: time | None = None      # heating reduction
    confidence: float = 0.0                      # 0–1, based on variance


@dataclass
class WeeklyPattern:
    """Variations across days of the week."""
    weekday_offset_c: float = 0.0               # vs weekend baseline
    saturday_pattern: DailyPattern = field(default_factory=DailyPattern)
    sunday_pattern: DailyPattern = field(default_factory=DailyPattern)
    weekday_pattern: DailyPattern = field(default_factory=DailyPattern)


@dataclass
class ThermalSignature:
    """Learned heat-loss characteristics of the building."""
    heat_loss_w_per_k: float = 0.0              # W per °C delta (indoor-outdoor)
    pv_to_indoor_lag_min: float = 0.0           # how fast does sun warm rooms
    boiler_decay_c_per_hour: float = 0.0        # standing losses
    floor_thermal_mass_kwh_per_c: float = 0.0   # how much energy to shift floor +1°C


@dataclass
class ForecastBias:
    """Corrections to external forecasts based on observed reality."""
    pv_forecast_multiplier: float = 1.0         # Solcast tends to under/overshoot
    heat_demand_multiplier: float = 1.0         # actual demand vs degree-day estimate


@dataclass
class LearnedProfile:
    """Everything the learning layer has figured out about this house."""
    daily: DailyPattern = field(default_factory=DailyPattern)
    weekly: WeeklyPattern = field(default_factory=WeeklyPattern)
    thermal: ThermalSignature = field(default_factory=ThermalSignature)
    forecast_bias: ForecastBias = field(default_factory=ForecastBias)
    last_trained: datetime | None = None
    samples_used: int = 0


@dataclass
class ActivationStatus:
    """Tracks whether and when the user activated learning."""
    is_active: bool = False
    activated_at: datetime | None = None
    push_sent_at: datetime | None = None         # when we asked the user
    push_dismissed_count: int = 0                # how often did they snooze
    data_start: datetime | None = None           # when we started collecting


def is_ready_for_activation(
    status: ActivationStatus,
    data_days: int,
    data_quality: float,
) -> bool:
    """
    Check whether we should send the activation push.

    Conditions:
      - learning is not yet active
      - at least MIN_DATA_DAYS of data accumulated
      - data quality (sample completeness) >= MIN_DATA_QUALITY
      - user hasn't dismissed the push more than 3 times (then back off for a month)
    """
    if status.is_active:
        return False
    if data_days < MIN_DATA_DAYS:
        return False
    if data_quality < MIN_DATA_QUALITY:
        return False
    if status.push_dismissed_count >= 3:  # noqa: SIM102 — kept nested for the comment context
        # Wait a month after 3 dismissals before re-prompting
        if status.push_sent_at and (datetime.now() - status.push_sent_at) < timedelta(days=30):
            return False
    # Don't re-send within 7 days of last prompt
    if status.push_sent_at and (datetime.now() - status.push_sent_at) < timedelta(days=7):  # noqa: SIM103
        return False
    return True


class LearningLayer:
    """
    Trains and serves the LearnedProfile.

    Before activation: methods return empty defaults. The optimizer treats
    these as "no suggestion" and falls back to pure rule-based behavior.

    After activation: nightly `train()` runs over the last 30 days of data
    and updates the profile. `suggest()` is called from the optimizer to
    get pattern-aware adjustments.
    """

    def __init__(self, status: ActivationStatus, profile: LearnedProfile | None = None):
        self.status = status
        self.profile = profile or LearnedProfile()

    @property
    def active(self) -> bool:
        return self.status.is_active

    def train(self, history: list[dict[str, Any]]) -> LearnedProfile:
        """
        Re-train the profile on recent historical data.

        Args:
            history: list of state snapshots from Firestore, sorted oldest-first.
                     Each dict has: timestamp, indoor_temp, outdoor_temp,
                     pv_power, hp_power, boiler_temp, motion (if any), etc.

        Returns:
            Updated LearnedProfile.
        """
        if not self.active:
            return self.profile  # dormant — return whatever's there

        if len(history) < 7 * 24 * 4:  # less than 1 week of quarter-hourly data
            return self.profile

        self.profile.daily = self._extract_daily_pattern(history)
        self.profile.weekly = self._extract_weekly_pattern(history)
        self.profile.thermal = self._extract_thermal_signature(history)
        self.profile.forecast_bias = self._extract_forecast_bias(history)
        self.profile.last_trained = datetime.now()
        self.profile.samples_used = len(history)
        return self.profile

    def suggest(
        self,
        current_time: datetime,
        outdoor_temp: float,
        pv_forecast: list[float],
    ) -> dict[str, Any]:
        """
        Return optimizer-relevant suggestions given current context.

        These are SOFT suggestions: the optimizer uses them as inputs to the
        objective function but Layer 1 limits and Layer 2 weights still rule.
        """
        if not self.active:
            return {}

        suggestions: dict[str, Any] = {}

        # Pre-heat hint based on learned return time
        if self.profile.daily.typical_return_time:
            return_dt = current_time.replace(
                hour=self.profile.daily.typical_return_time.hour,
                minute=self.profile.daily.typical_return_time.minute,
            )
            minutes_until_return = (return_dt - current_time).total_seconds() / 60
            if 30 <= minutes_until_return <= 90:
                suggestions["preheat_hint"] = {
                    "reason": "Roel komt meestal rond deze tijd thuis",
                    "confidence": self.profile.daily.confidence,
                }

        # Forecast bias correction
        if self.profile.forecast_bias.pv_forecast_multiplier != 1.0:
            corrected = [p * self.profile.forecast_bias.pv_forecast_multiplier for p in pv_forecast]
            suggestions["pv_forecast_corrected"] = corrected

        # Thermal mass guidance
        if self.profile.thermal.heat_loss_w_per_k > 0:
            indoor_setpoint = 20.5  # would come from policy
            heat_demand_w = self.profile.thermal.heat_loss_w_per_k * (indoor_setpoint - outdoor_temp)
            suggestions["estimated_heat_demand_w"] = max(0, heat_demand_w)

        return suggestions

    # --- Private extractors (stubs — full implementations after week 6) ---

    def _extract_daily_pattern(self, history: list[dict[str, Any]]) -> DailyPattern:
        """
        Find typical wake/leave/return/sleep times from indoor activity signals.

        Approach: cluster occupancy proxy events (motion, heat-pump short bursts,
        DHW draws, indoor-temp deltas) per quarter-hour-of-day and look for
        modal transitions.
        """
        # TODO post-activation: implement with simple histogram + peak detection
        return DailyPattern()

    def _extract_weekly_pattern(self, history: list[dict[str, Any]]) -> WeeklyPattern:
        """Differences in pattern between weekdays and weekend days."""
        # TODO post-activation
        return WeeklyPattern()

    def _extract_thermal_signature(self, history: list[dict[str, Any]]) -> ThermalSignature:
        """
        Linear regression of indoor-outdoor delta vs heat input over stable periods.
        Yields heat_loss_w_per_k and thermal mass estimates.
        """
        # TODO post-activation: numpy.linalg.lstsq on (delta_T, hp_power) → slope
        return ThermalSignature()

    def _extract_forecast_bias(self, history: list[dict[str, Any]]) -> ForecastBias:
        """
        Compare logged Solcast forecast vs actual PV; compute multiplier.
        Same for KNMI temperature forecast vs actual.
        """
        # TODO post-activation: simple mean ratio with outlier rejection
        return ForecastBias()


def empty_profile_for_dormant() -> LearnedProfile:
    """Returned by LearningLayer.train() while dormant. Optimizer treats as no-op."""
    return LearnedProfile()
