"""In-memory caps so message/send cannot spend the OpenAI key without limit."""

import os
import time
from datetime import datetime, timezone

DAILY_LIMIT_ENV = "MAX_DAILY_MESSAGES"
IP_LIMIT_ENV = "MAX_MESSAGES_PER_IP_PER_HOUR"
DEFAULT_DAILY_LIMIT = 30
DEFAULT_IP_LIMIT = 5
RATE_LIMIT_CODE = -32029
DAILY_MESSAGE = "Límite diario de mensajes alcanzado. Inténtalo mañana."
IP_MESSAGE = "Demasiados mensajes desde esta dirección. Inténtalo dentro de una hora."
LIMITED_METHODS = frozenset({"message/send", "message/stream"})


def parse_limit(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    if value < 0:
        return default
    return value


class MessageQuota:
    """Global daily cap plus a sliding one-hour cap per IP.

    Counts live in this process. A restart (including a free host waking up)
    starts the counters again.
    """

    def __init__(
        self,
        daily_limit: int,
        per_ip_per_hour: int,
        now=None,
    ) -> None:
        self.daily_limit = daily_limit
        self.per_ip_per_hour = per_ip_per_hour
        self._now = now or time.time
        self._day = None
        self._daily_count = 0
        self._hits: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, now=None) -> "MessageQuota":
        return cls(
            daily_limit=parse_limit(DAILY_LIMIT_ENV, DEFAULT_DAILY_LIMIT),
            per_ip_per_hour=parse_limit(IP_LIMIT_ENV, DEFAULT_IP_LIMIT),
            now=now,
        )

    def _roll_day(self) -> None:
        day = datetime.fromtimestamp(self._now(), tz=timezone.utc).date()
        if day != self._day:
            self._day = day
            self._daily_count = 0

    def _prune(self, ip: str) -> list[float]:
        cutoff = self._now() - 3600
        return [stamp for stamp in self._hits.get(ip, []) if stamp >= cutoff]

    def reason(self, ip: str) -> str | None:
        """Why this IP cannot send, without counting the attempt."""
        self._roll_day()
        if self._daily_count >= self.daily_limit:
            return "daily"
        if len(self._prune(ip)) >= self.per_ip_per_hour:
            return "ip"
        return None

    def consume(self, ip: str) -> str | None:
        """Count one chat call. Return 'daily' or 'ip' when it is refused."""
        self._roll_day()
        if self._daily_count >= self.daily_limit:
            return "daily"
        hits = self._prune(ip)
        if len(hits) >= self.per_ip_per_hour:
            self._hits[ip] = hits
            return "ip"
        hits.append(self._now())
        self._hits[ip] = hits
        self._daily_count += 1
        return None


def message_for(reason: str) -> str:
    if reason == "ip":
        return IP_MESSAGE
    return DAILY_MESSAGE


def rpc_error(request_id, reason: str) -> dict:
    if not isinstance(request_id, str | int):
        request_id = None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": RATE_LIMIT_CODE, "message": message_for(reason)},
    }
