"""Alerts: always stored for the dashboard; optionally POSTed to NOTIFY_WEBHOOK_URL (e.g. ntfy.sh topic)."""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from qqq.models import utcnow
from qqq.store import Store

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, store: Store, webhook_url: Optional[str] = None):
        self.store = store
        self.webhook_url = webhook_url if webhook_url is not None else os.getenv("NOTIFY_WEBHOOK_URL", "")

    def alert(self, level: str, title: str, body: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        a = {"id": uuid.uuid4().hex[:12], "level": level, "title": title, "body": body,
             "data": data or {}, "at": utcnow().isoformat(), "delivered": False}
        if self.webhook_url:
            try:
                r = httpx.post(self.webhook_url, content=f"{title}\n{body}".encode(),
                               headers={"Title": title[:120], "Priority": "high" if level == "risk" else "default"},
                               timeout=5)
                a["delivered"] = r.status_code < 400
            except Exception as exc:
                logger.warning("webhook delivery failed: %s", exc)
        self.store.insert("alerts", a["id"], a)
        return a
