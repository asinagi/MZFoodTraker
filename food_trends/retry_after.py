"""Provider minimum delay, including HTTP dates; never clamp a valid long delay."""
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def seconds(headers, now=None):
    try:
        raw = headers.get('Retry-After', '0')
        try:
            delay = float(raw)
        except ValueError:
            delay = (parsedate_to_datetime(raw) - (now or datetime.now(timezone.utc))).total_seconds()
        return max(0, delay) if math.isfinite(delay) else 60
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0
