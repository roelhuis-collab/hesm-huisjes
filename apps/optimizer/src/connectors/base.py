"""
Shared exception hierarchy and helpers for all device/data-cloud connectors.

The orchestrator (``main.py`` / ``_gather_state()``) catches
:class:`ConnectorUnavailable` to treat any flaky-cloud failure uniformly:
  log it, count it, fall back to last-known-good state, and trip the
  failsafe + FCM alert if the same connector stays down for >30 min.

Auth-related and schema-violation failures are kept distinct because they
have different escalation paths: a misconfigured token is a configuration
problem (alert immediately), a schema violation likely means the upstream
API changed (alert + open issue), while a single network blip is fine.
"""


class ConnectorError(Exception):
    """Base class for every connector failure. Caught at the cycle boundary."""


class ConnectorAuthError(ConnectorError):
    """Authentication failed: missing token, expired credentials, 401/403.

    Typically a configuration issue, not transient. Don't retry blindly.
    """


class ConnectorUnavailable(ConnectorError):
    """Transient: timeout, network blip, 5xx, rate limit.

    Safe to retry on the next 15-min cycle. The optimizer falls back to
    last-known state for this device and emits an alert if the failure
    persists across multiple cycles.
    """


class ConnectorMalformed(ConnectorError):
    """The upstream API returned 200 OK but the body did not match the
    expected schema. Usually means the vendor changed something —
    investigate, don't just retry.
    """
