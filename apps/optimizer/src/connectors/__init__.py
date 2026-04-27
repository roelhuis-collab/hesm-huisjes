"""
Third-party device-cloud and data-feed connectors.

Each module in this package wraps one external API (HomeWizard, ENTSO-E,
Open-Meteo, WeHeat, Resideo, Shelly, Growatt, ...) behind a small async
client. The optimizer cycle aggregates results from several connectors
and never speaks HTTP itself.

Common exception hierarchy lives in :mod:`.base` so callers can catch
:class:`ConnectorUnavailable` once and treat any flaky-cloud failure
uniformly.
"""

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)

__all__ = [
    "ConnectorAuthError",
    "ConnectorError",
    "ConnectorMalformed",
    "ConnectorUnavailable",
]
