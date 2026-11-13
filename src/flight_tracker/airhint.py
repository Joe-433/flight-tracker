"""AirHint: a second opinion on whether a fare is likely to drop.

AirHint (airhint.com) predicts, for an airline + route + date + price, whether
to buy now or wait. It answers a plain GET with JSON:

    {"suggestion": "Wait", "confidence": 85, "max_saving": "40%",
     "price_range": [low, high, typical], ...}

It is advice layered on top of Google's prices, never a source of prices, and
every failure is silent: a report without a hint is still a report.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

BASE = "https://www.airhint.com/predict"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)


def predict(airline: str, origin: str, destination: str, date: str, price: float,
            currency: str = "USD", timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """AirHint's call for one fare, or None if it has nothing to say."""
    url = "%s/%s/%s/%s/%s?%s" % (
        BASE, urllib.parse.quote(airline), urllib.parse.quote(origin),
        urllib.parse.quote(destination), urllib.parse.quote(date),
        urllib.parse.urlencode({"cabin_class": "Y", "currency": currency,
                                "passengers": 1, "price": int(round(price))}),
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("suggestion"):
        return None
    return {k: data.get(k) for k in ("suggestion", "confidence", "max_saving", "price_range")}
