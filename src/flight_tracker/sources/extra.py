"""Second and third opinions on one-way prices: Skiplagged and AirHint.

Google Flights is the tracker's main source, but it misses fares:

  Skiplagged  sells "hidden-city" tickets (book NYC -> GUA -> Rochester, get
              off at Newark) and self-transfer combinations of separate
              tickets (Kiwi), neither of which Google shows. Its search API
              answers a plain GET with JSON; results stream in, so it is
              polled until complete.
  AirHint     runs a Skyscanner-backed search (create, then poll by id) that
              often prices the same flight lower through an online agency.

Both return itineraries in the same shape the trip search uses:

    {"segments": [{"from", "to", "dep", "arr"}], "price", "airlines",
     "flight_no", "url", "source", "hidden_city", "ticket_to",
     "self_transfer", "agent"}

with `dep`/`arr` as local "YYYY-MM-DDTHH:MM". A hidden-city itinerary is cut
at the stop where the traveller gets off; `ticket_to` names the airport the
ticket is actually for.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class SourceError(RuntimeError):
    """The source failed; the caller records it and keeps old prices."""


def _get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 40.0) -> Any:
    request = urllib.request.Request(
        url, headers=dict({"User-Agent": USER_AGENT, "Accept": "application/json"}, **(headers or {}))
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            # 202 with no body: "still searching". Not an error, not JSON.
            if response.status == 202 or not body.strip():
                return None
            return json.loads(body)
    except Exception as exc:
        raise SourceError("%s: %s" % (type(exc).__name__, exc)) from exc


def _local(stamp: str) -> str:
    """"2027-03-04T08:50:00-05:00" or "2027-03-04 08:50:00" -> "2027-03-04T08:50"."""
    return stamp.replace(" ", "T")[:16]


# Carriers that don't interline: a connection between one of these and any
# other airline is two separate tickets.
LOW_COST = {"F9", "NK", "Y4", "Q6", "DM", "G4", "MX", "F8", "WS", "P5", "VB", "SY", "TS"}


# ---------------------------------------------------------------------------
# Skiplagged
# ---------------------------------------------------------------------------


def skiplagged(frm: str, to: str, date: str, arrive_at: Set[str],
               polls: int = 6, wait: float = 3.0) -> List[Dict[str, Any]]:
    """One-way itineraries from Skiplagged, hidden-city ones cut short.

    `arrive_at` is the set of airports that count as arriving (e.g. JFK, LGA,
    EWR for "NYC"). An itinerary that lands at one of them before its last
    stop is a hidden-city fare and is cut there.
    """
    params = urllib.parse.urlencode({
        "from": frm, "to": to, "depart": date, "return": "", "format": "v3",
        "counts[adults]": 1, "counts[children]": 0,
    })
    url = "https://skiplagged.com/api/search.php?" + params
    data: Dict[str, Any] = {}
    for attempt in range(polls):
        data = _get_json(url) or {}
        if not data.get("incomplete"):
            break
        time.sleep(wait)
    if not isinstance(data, dict) or "flights" not in data:
        raise SourceError("no flights in Skiplagged's answer")

    names = {code: (info or {}).get("name", code) for code, info in (data.get("airlines") or {}).items()}
    link = "https://skiplagged.com/flights/%s/%s/%s" % (frm, to, date)
    out: List[Dict[str, Any]] = []
    for itinerary in (data.get("itineraries") or {}).get("outbound") or []:
        flight = (data.get("flights") or {}).get(itinerary.get("flight"))
        price = itinerary.get("one_way_price")
        if not flight or not isinstance(price, (int, float)) or price <= 0:
            continue
        segments, numbers, airlines = [], [], []
        hidden = False
        raw = flight.get("segments") or []
        for i, seg in enumerate(raw):
            segments.append({
                "from": seg["departure"]["airport"], "to": seg["arrival"]["airport"],
                "dep": _local(seg["departure"]["time"]), "arr": _local(seg["arrival"]["time"]),
            })
            numbers.append("%s %s" % (seg.get("airline", ""), seg.get("flight_number", "")))
            name = names.get(seg.get("airline"), seg.get("airline"))
            if name and name not in airlines:
                airlines.append(name)
            if seg["arrival"]["airport"] in arrive_at:
                hidden = i < len(raw) - 1
                break
        if not segments or segments[-1]["to"] not in arrive_at:
            continue
        sources: List[str] = []
        try:
            for entry in json.loads(itinerary["data"].split("|", 1)[1]).get("source") or []:
                sources.extend((entry.get("source") or {}).keys())
        except (KeyError, IndexError, ValueError, AttributeError):
            pass
        # Two airlines that don't sell each other's flights (a low-cost
        # carrier on either side) means two tickets, even when Skiplagged
        # doesn't mark it as a self-transfer.
        carriers = {seg.get("airline") for seg in raw[: len(segments)]}
        separate = len(carriers) > 1 and bool(carriers & LOW_COST)
        out.append({
            "segments": segments, "price": float(price) / 100.0, "airlines": airlines,
            "flight_no": " / ".join(numbers), "url": link, "source": "Skiplagged",
            "hidden_city": hidden, "ticket_to": raw[-1]["arrival"]["airport"] if hidden else None,
            "self_transfer": bool(itinerary.get("vi")) or separate,
            "agent": "Kiwi" if "Kiwi" in sources and itinerary.get("vi") else None,
        })
    return out


# ---------------------------------------------------------------------------
# AirHint (Skyscanner)
# ---------------------------------------------------------------------------

AIRHINT_HEADERS = {
    "Referer": "https://www.airhint.com/",
    "Origin": "https://www.airhint.com",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}


def airhint(frm: str, to: str, date: str, currency: str = "USD",
            polls: int = 6, wait: float = 4.0) -> List[Dict[str, Any]]:
    """One-way itineraries from AirHint's Skyscanner-backed search."""
    created = _get_json(
        "https://www.airhint.com/search/%s/%s/%s?%s" % (
            frm, to, date, urllib.parse.urlencode({"adults": 1, "currency": currency})),
        headers=AIRHINT_HEADERS,
    )
    search_id = (created or {}).get("search_id") if isinstance(created, dict) else None
    if not search_id:
        raise SourceError("AirHint didn't start a search: %r" % (created,))

    rows: List[Dict[str, Any]] = []
    previous = -1
    for attempt in range(polls):
        time.sleep(wait)
        answer = _get_json("https://www.airhint.com/search/%s" % search_id, headers=AIRHINT_HEADERS)
        if answer is None:
            continue  # still searching
        rows = answer if isinstance(answer, list) else []
        if rows and len(rows) == previous:
            break  # stopped growing
        previous = len(rows)

    out: List[Dict[str, Any]] = []
    for row in rows:
        price = row.get("total_price")
        flights = row.get("outbound_flights") or []
        airports = [row.get("origin")] + list(row.get("stops") or []) + [row.get("destination")]
        if not isinstance(price, (int, float)) or price <= 0 or not flights:
            continue
        if len(airports) != len(flights) + 1 or not all(airports):
            continue
        segments = [{
            "from": airports[i], "to": airports[i + 1],
            "dep": _local(f.get("departure_time") or ""), "arr": _local(f.get("arrival_time") or ""),
        } for i, f in enumerate(flights)]
        if not all(s["dep"] and s["arr"] for s in segments):
            continue
        carriers = row.get("carriers") or {}
        airlines = []
        for f in flights:
            code = f.get("airline")
            name = (carriers.get(code) or {}).get("name") or code
            if name and name not in airlines:
                airlines.append(name)
        agent = str(row.get("agent") or "").split("/", 1)[-1] or None
        numbers = [str(f.get("flight_number") or "") for f in flights]
        out.append({
            "segments": segments, "price": float(price), "airlines": airlines,
            "flight_no": " / ".join("%s %s" % (n[:2], n[2:]) for n in numbers if n),
            "url": row.get("book_url") or row.get("deep_link"),
            "source": "Skyscanner", "agent": agent,
            "hidden_city": False, "ticket_to": None,
            "self_transfer": bool(agent and agent.lower().startswith("kiwi")),
        })
    return out
