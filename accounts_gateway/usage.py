"""Per-user daily usage counters.

Counts live in memory for speed and are flushed to the database every minute,
so they survive a restart (a user's day is reloaded the first time we see
them). Only requests that cost money (uncached upstream calls) are counted.
"""
import threading
import time
from datetime import datetime, timezone


def today(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%d")


class UsageTracker:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self._lock = threading.RLock()
        self._counts = {}        # (day, uid, feature) -> total used
        self._pending = {}       # (day, uid, feature) -> not yet flushed
        self._loaded = set()     # (day, uid)
        self._thread = None
        self._stop = threading.Event()

    # -- internals
    def _load(self, day, uid):
        if (day, uid) in self._loaded:
            return
        try:
            for feature, n in self.store.usage_for_user(day, uid).items():
                self._counts[(day, uid, feature)] = max(
                    self._counts.get((day, uid, feature), 0), int(n))
        except Exception:
            pass                                 # DB hiccup: start from what we know
        self._loaded.add((day, uid))

    # -- public
    def used(self, uid, feature):
        day = today(self.clock)
        with self._lock:
            self._load(day, uid)
            return self._counts.get((day, uid, feature), 0)

    def snapshot(self, uid):
        day = today(self.clock)
        with self._lock:
            self._load(day, uid)
            return {f: n for (d, u, f), n in self._counts.items() if d == day and u == uid}

    def consume(self, uid, feature, limit):
        """Atomically use one unit. Returns (allowed, used_after)."""
        day = today(self.clock)
        with self._lock:
            self._load(day, uid)
            key = (day, uid, feature)
            used = self._counts.get(key, 0)
            if limit is not None and used >= limit:
                return False, used
            self._counts[key] = used + 1
            self._pending[key] = self._pending.get(key, 0) + 1
            return True, used + 1

    def refund(self, uid, feature):
        day = today(self.clock)
        with self._lock:
            key = (day, uid, feature)
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1
                self._pending[key] = self._pending.get(key, 0) - 1

    def flush(self):
        with self._lock:
            pending = {k: v for k, v in self._pending.items() if v}
            self._pending.clear()
        by_day = {}
        for (day, uid, feature), n in pending.items():
            by_day.setdefault(day, []).append((uid, feature, n))
        for day, rows in by_day.items():
            try:
                self.store.add_usage(day, rows)
            except Exception:
                with self._lock:                 # put it back; try again next time
                    for uid, feature, n in rows:
                        k = (day, uid, feature)
                        self._pending[k] = self._pending.get(k, 0) + n

    def start(self, interval=60):
        if self._thread:
            return
        def loop():
            while not self._stop.wait(interval):
                self.flush()
        self._thread = threading.Thread(target=loop, daemon=True, name="usage-flush")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.flush()
