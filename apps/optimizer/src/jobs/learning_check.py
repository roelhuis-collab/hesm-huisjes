"""
Daily learning-readiness check.

Runs once per day (Cloud Scheduler, 19:00 local). Checks whether the
learning layer is ready to be offered to the user, and if so triggers
a push notification with a deep-link to the activation flow.

This is the ONLY way Layer 3 ever gets activated — never automatically.
"""

import logging
from datetime import datetime, timedelta

from src.optimizer.learning import (
    ActivationStatus,
    is_ready_for_activation,
    MIN_DATA_DAYS,
    MIN_DATA_QUALITY,
)
from src.notifications.push import send_push
from src.state.firestore import (
    get_activation_status,
    update_activation_status,
    count_state_samples,
    get_data_start_date,
)

log = logging.getLogger(__name__)


def run_daily_check() -> dict:
    """
    Cloud Scheduler entrypoint.

    Returns a small status dict for logging / health checks.
    """
    status = get_activation_status()

    # Initialize data_start on first run
    if status.data_start is None:
        status.data_start = datetime.now()
        update_activation_status(status)
        log.info("learning_check: initialized data_start=%s", status.data_start)
        return {"status": "initialized", "data_days": 0}

    # If already active, nothing to do
    if status.is_active:
        return {"status": "already_active"}

    # Compute data accumulation
    days_collected = (datetime.now() - status.data_start).days
    expected_samples = days_collected * 24 * 4  # quarter-hourly
    actual_samples = count_state_samples(since=status.data_start)
    data_quality = (actual_samples / expected_samples) if expected_samples > 0 else 0.0

    log.info(
        "learning_check: days=%d quality=%.2f samples=%d/%d dismissed=%d",
        days_collected, data_quality, actual_samples, expected_samples,
        status.push_dismissed_count,
    )

    if not is_ready_for_activation(status, days_collected, data_quality):
        return {
            "status": "not_ready",
            "data_days": days_collected,
            "data_quality": round(data_quality, 3),
            "needs_days": max(0, MIN_DATA_DAYS - days_collected),
            "needs_quality": max(0.0, MIN_DATA_QUALITY - data_quality),
        }

    # Send the activation push
    send_activation_push(days_collected, actual_samples)
    status.push_sent_at = datetime.now()
    update_activation_status(status)

    return {
        "status": "push_sent",
        "data_days": days_collected,
        "data_quality": round(data_quality, 3),
        "samples": actual_samples,
    }


def send_activation_push(days_collected: int, samples: int) -> None:
    """
    Send the activation prompt to the user.

    The notification deep-links into the dashboard at /settings/learning,
    where the user sees a summary of what the system has observed and
    can choose to activate, snooze, or decline.
    """
    title = "Klaar om patronen te leren? 🧠"
    body = (
        f"Ik heb de afgelopen {days_collected} dagen je systeem in actie gezien "
        f"({samples:,} datapunten). Genoeg om vertrektijden, douche-routine en "
        f"warmtemodellen te modelleren. Klaar om dit te activeren?"
    )

    send_push(
        title=title,
        body=body,
        deep_link="/settings/learning",
        data={
            "type": "learning_activation_prompt",
            "days_collected": str(days_collected),
            "samples": str(samples),
        },
    )

    log.info("learning_check: activation push sent")


def handle_activation_response(accepted: bool) -> dict:
    """
    Called by the dashboard when the user responds to the activation prompt.

    Args:
        accepted: True = activate now, False = dismiss/snooze
    """
    status = get_activation_status()

    if accepted:
        status.is_active = True
        status.activated_at = datetime.now()
        update_activation_status(status)
        log.info("learning_check: ACTIVATED by user at %s", status.activated_at)
        return {"status": "activated"}

    status.push_dismissed_count += 1
    update_activation_status(status)
    log.info(
        "learning_check: dismissed (count=%d)",
        status.push_dismissed_count,
    )
    return {"status": "dismissed", "count": status.push_dismissed_count}
