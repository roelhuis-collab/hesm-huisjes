"""
Firebase Cloud Messaging push notifications.

Sends notifications to the user's registered devices (iPad + iPhone PWA).
Tokens are written by the dashboard when it gets push permission.
"""

import logging
import os
from typing import Any

from firebase_admin import messaging
from src.state.firestore import get_user_fcm_tokens, mark_fcm_token_invalid

log = logging.getLogger(__name__)

DEFAULT_LINK_BASE = os.environ.get("DASHBOARD_BASE_URL", "https://hesm-huisjes.netlify.app")


def send_push(
    title: str,
    body: str,
    deep_link: str | None = None,
    data: dict[str, str] | None = None,
) -> int:
    """
    Send a push to all registered devices for the user.

    Returns:
        Number of devices the message was successfully sent to.
    """
    tokens = get_user_fcm_tokens()
    if not tokens:
        log.warning("send_push: no FCM tokens registered, skipping")
        return 0

    payload_data = dict(data or {})
    if deep_link:
        payload_data["deep_link"] = deep_link
        payload_data["url"] = f"{DEFAULT_LINK_BASE}{deep_link}"

    success_count = 0

    for token in tokens:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=payload_data,
            token=token,
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        content_available=True,
                        category="HESM_NOTIFICATION",
                    )
                )
            ),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(
                    title=title,
                    body=body,
                    icon="/icons/icon-192.png",
                    badge="/icons/badge-72.png",
                ),
                fcm_options=messaging.WebpushFCMOptions(
                    link=f"{DEFAULT_LINK_BASE}{deep_link}" if deep_link else DEFAULT_LINK_BASE,
                ),
            ),
        )
        try:
            messaging.send(message)
            success_count += 1
        except messaging.UnregisteredError:
            log.info("send_push: stale token %s, removing", token[:16])
            mark_fcm_token_invalid(token)
        except Exception as e:
            log.error("send_push: failed for token %s: %s", token[:16], e)

    log.info("send_push: %d/%d delivered — %s", success_count, len(tokens), title)
    return success_count


def send_alert(level: str, message: str, deep_link: str | None = None) -> int:
    """Convenience for system alerts (failsafe trips, API outages, anomalies)."""
    icons = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}
    icon = icons.get(level, "")
    return send_push(
        title=f"{icon} HESM {level}".strip(),
        body=message,
        deep_link=deep_link or "/",
        data={"alert_level": level},
    )
