from __future__ import annotations

import unittest

from helpers import make_config  # noqa: F401  (path setup)

from flight_tracker.booking import airline_url, carrier_of


class TestCarrierOf(unittest.TestCase):
    def test_extracts_the_code(self):
        self.assertEqual(carrier_of("WN 2536 / 4598"), "WN")
        self.assertEqual(carrier_of("AA 171"), "AA")

    def test_missing(self):
        self.assertIsNone(carrier_of(None))
        self.assertIsNone(carrier_of(""))


class TestAirlineUrl(unittest.TestCase):
    def url(self, carrier):
        return airline_url(carrier, "LGA", "LAX", "2026-10-21", "2026-10-27")

    def test_verified_carriers_get_a_link(self):
        self.assertIn("southwest.com", self.url("WN") or "")
        self.assertIn("delta.com", self.url("DL") or "")
        self.assertIn("flyfrontier.com", self.url("F9") or "")

    def test_dates_and_route_are_carried(self):
        url = self.url("WN") or ""
        self.assertIn("originationAirportCode=LGA", url)
        self.assertIn("destinationAirportCode=LAX", url)
        self.assertIn("departureDate=2026-10-21", url)
        self.assertIn("returnDate=2026-10-27", url)

    def test_unverified_carriers_get_nothing(self):
        """No link beats a link that 404s or drops the dates."""
        for carrier in ("AA", "B6", "AS", "UA", "NK"):
            self.assertIsNone(self.url(carrier), carrier)

    def test_missing_inputs(self):
        self.assertIsNone(airline_url("WN", None, "LAX", "d", "r"))
        self.assertIsNone(airline_url(None, "LGA", "LAX", "d", "r"))

    def test_case_insensitive(self):
        self.assertIsNotNone(airline_url("wn", "LGA", "LAX", "d", "r"))
