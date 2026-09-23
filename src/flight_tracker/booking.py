"""Direct booking links, for the airlines whose deep links actually work.

Every template here was opened in a browser and confirmed to land on a search
with the route and both dates already filled in. Carriers are absent from this
table for one of two reasons, and both mean the same thing -- no link is
better than a broken one:

  * The deep link format couldn't be confirmed, because the site answers
    automated requests with a bot challenge (American, JetBlue).
  * The parameters were confirmed NOT to work -- Alaska loads its booking form
    and reports the dates as missing.

Verified 2026-09-14. Airlines change these without warning, so a link that
stops working is expected wear, not a surprise.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

VERIFIED = {"WN", "DL", "F9"}


def airline_url(
    carrier: Optional[str],
    origin: Optional[str],
    destination: Optional[str],
    out_date: str,
    ret_date: str,
    adults: int = 1,
) -> Optional[str]:
    """A direct booking search, or None when we can't build one we trust."""
    if not carrier or not origin or not destination:
        return None
    carrier = carrier.strip().upper()
    if carrier not in VERIFIED:
        return None

    if carrier == "WN":
        return "https://www.southwest.com/air/booking/select.html?" + urlencode(
            {
                "adultPassengersCount": adults,
                "departureDate": out_date,
                "destinationAirportCode": destination,
                "fareType": "USD",
                "originationAirportCode": origin,
                "passengerType": "ADULT",
                "returnDate": ret_date,
                "tripType": "roundtrip",
            }
        )

    if carrier == "DL":
        return "https://www.delta.com/flight-search/book-a-flight?" + urlencode(
            {
                "tripType": "ROUND_TRIP",
                "originCity": origin,
                "destinationCity": destination,
                "departureDate": out_date,
                "returnDate": ret_date,
                "paxCount": adults,
            }
        )

    # F9
    return "https://booking.flyfrontier.com/Flight/InternalSelect?" + urlencode(
        {
            "o1": origin,
            "d1": destination,
            "dd1": out_date,
            "o2": destination,
            "d2": origin,
            "dd2": ret_date,
            "ADT": adults,
            "mon": "true",
            "rt": "true",
        }
    )


def carrier_of(flight_no: Optional[str]) -> Optional[str]:
    """"WN 2536 / 4598" -> "WN"."""
    if not flight_no:
        return None
    code = flight_no.split(" ", 1)[0].strip().upper()
    return code or None
