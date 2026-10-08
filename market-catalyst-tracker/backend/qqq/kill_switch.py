"""
Global kill switch. Persistent, audited, and ENGAGED by default:
a fresh install or a lost state file means "no trading" until a human
disengages it after reconciliation passes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from qqq.models import utcnow
from qqq.store import Store

_KEY = "kill_switch"


class KillSwitch:
    def __init__(self, store: Store):
        self.store = store

    def state(self) -> Dict[str, Any]:
        return self.store.kv_get(_KEY) or {"engaged": True, "reason": "default: engaged on first start",
                                           "by": "system", "at": None}

    def engaged(self) -> bool:
        try:
            return bool(self.state().get("engaged", True))
        except Exception:
            return True   # unreadable state → engaged

    def engage(self, reason: str, by: str) -> Dict[str, Any]:
        st = {"engaged": True, "reason": reason, "by": by, "at": utcnow().isoformat()}
        self.store.kv_set(_KEY, st)
        self.store.audit(by, "kill_switch.engage", st)
        return st

    def disengage(self, reason: str, by: str, reconciliation_ok: bool) -> Dict[str, Any]:
        if not reconciliation_ok:
            raise PermissionError("cannot disengage kill switch: reconciliation has not passed")
        st = {"engaged": False, "reason": reason, "by": by, "at": utcnow().isoformat()}
        self.store.kv_set(_KEY, st)
        self.store.audit(by, "kill_switch.disengage", st)
        return st
