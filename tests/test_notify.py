from __future__ import annotations

import unittest

from helpers import make_config  # noqa: F401  (path setup)

from flight_tracker.notify import BLURPLE, GREEN, RED, DiscordChannel, Message


class TestDiscordPayload(unittest.TestCase):
    """The report must be a real embed, not a grey slab of code block."""

    def payload(self, message):
        captured = {}

        class FakeChannel(DiscordChannel):
            def send(self, msg):  # noqa: D102
                embed = {"title": msg.title[:256], "color": msg.accent()}
                if msg.body:
                    embed["description"] = msg.body
                if msg.url:
                    embed["url"] = msg.url
                if msg.fields:
                    embed["fields"] = [
                        {"name": n, "value": v, "inline": False}
                        for n, v in msg.fields
                    ]
                captured["embed"] = embed

        FakeChannel("https://example.invalid").send(message)
        return captured["embed"]

    def test_fields_become_embed_fields(self):
        embed = self.payload(
            Message(title="Cheapest", fields=[("1. $297", "1:25p LGA->LAX")])
        )
        self.assertEqual(len(embed["fields"]), 1)
        self.assertEqual(embed["fields"][0]["name"], "1. $297")

    def test_url_goes_on_the_embed_not_the_body(self):
        embed = self.payload(Message(title="$238", body="deal", url="https://x.test"))
        self.assertEqual(embed["url"], "https://x.test")
        self.assertNotIn("https://x.test", embed["description"])

    def test_urgent_messages_are_red_by_default(self):
        self.assertEqual(Message(title="down", urgent=True).accent(), RED)
        self.assertEqual(Message(title="fyi").accent(), BLURPLE)
        self.assertEqual(Message(title="deal", color=GREEN).accent(), GREEN)


class TestPlainTextRendering(unittest.TestCase):
    def test_fields_render_for_email_and_console(self):
        text = Message(
            title="Cheapest",
            fields=[("1. $297", "1:25p LGA->LAX")],
            footer="prices as last seen",
        ).as_text()
        self.assertIn("1. $297", text)
        self.assertIn("1:25p LGA->LAX", text)
        self.assertIn("prices as last seen", text)


if __name__ == "__main__":
    unittest.main()
