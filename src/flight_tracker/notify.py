"""Alert delivery. Stdlib only -- no SDKs, nothing to break.

Channels, easiest first:

  Discord  -- create a webhook in a channel's settings, paste the URL into the
              DISCORD_WEBHOOK_URL secret. No account linking, no API keys, no
              approval, instant push on phone. This is the recommended one.
  Email    -- any SMTP server. With Gmail you need an App Password (regular
              password won't work).
  Text     -- free via your carrier's email-to-SMS gateway: just add e.g.
              5551234567@vtext.com (Verizon), @txt.att.net (AT&T),
              @tmomail.net (T-Mobile) to EMAIL_TO. Genuine SMS APIs (Twilio)
              all cost money, so this is the free path.

Every channel is optional. With none configured we print to stdout, which
keeps `run` useful in a terminal and in Actions logs.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import List, Optional, Tuple

USER_AGENT = "flight-tracker/0.1 (+https://github.com)"


# Discord embed accent colours.
GREEN = 0x2ECC71   # a deal
RED = 0xE74C3C     # something is wrong
BLURPLE = 0x5865F2 # informational


@dataclass
class Message:
    title: str
    body: str = ""
    url: Optional[str] = None
    urgent: bool = False
    # Rendered as embed fields in Discord, and as an indented list everywhere
    # else. This is what keeps a ten-flight report from being one grey slab.
    fields: List[Tuple[str, str]] = field(default_factory=list)
    footer: Optional[str] = None
    color: Optional[int] = None

    def as_text(self) -> str:
        parts = [self.title]
        if self.body:
            parts += ["", self.body]
        for name, value in self.fields:
            parts += ["", name, "    " + value]
        if self.footer:
            parts += ["", self.footer]
        if self.url:
            parts += ["", self.url]
        return "\n".join(parts).strip()

    def accent(self) -> int:
        if self.color is not None:
            return self.color
        return RED if self.urgent else BLURPLE


class Channel:
    name = "channel"

    def send(self, message: Message) -> None:
        raise NotImplementedError


class ConsoleChannel(Channel):
    name = "console"

    def send(self, message: Message) -> None:
        print("\n" + "=" * 60)
        print(message.as_text())
        print("=" * 60 + "\n")


class DiscordChannel(Channel):
    name = "discord"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, message: Message) -> None:
        # An embed, not a wall of text. Discord gives embeds a coloured spine,
        # a real title, and proper field spacing; a fenced code block just
        # renders as a grey slab that reads like a dumped file. The deeplink
        # goes on the embed's `url`, which makes the title itself tappable --
        # no 400-character base64 URL in the body, and no shortener service.
        embed = {"title": message.title[:256], "color": message.accent()}
        if message.body:
            embed["description"] = message.body[:4096]
        if message.url:
            embed["url"] = message.url
        if message.fields:
            embed["fields"] = [
                {"name": name[:256], "value": value[:1024], "inline": False}
                for name, value in message.fields[:25]
            ]
        if message.footer:
            embed["footer"] = {"text": message.footer[:2048]}

        body = {
            "embeds": [embed],
            "allowed_mentions": {"parse": ["everyone"]},
        }
        if message.urgent:
            body["content"] = "@here"
        payload = json.dumps(body).encode()
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 300:
                raise RuntimeError("discord returned HTTP %s" % response.status)


class EmailChannel(Channel):
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        user: Optional[str],
        password: Optional[str],
        sender: str,
        recipients: List[str],
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender
        self.recipients = recipients

    def send(self, message: Message) -> None:
        email = EmailMessage()
        # SMS gateways truncate hard and ignore most headers; keep it terse.
        email["Subject"] = message.title[:120]
        email["From"] = self.sender
        email["To"] = ", ".join(self.recipients)
        email.set_content(message.as_text())

        context = ssl.create_default_context()
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
        try:
            if self.port != 465:
                server.starttls(context=context)
            if self.user and self.password:
                server.login(self.user, self.password)
            server.send_message(email)
        finally:
            server.quit()


class Notifier:
    def __init__(self, channels: List[Channel]) -> None:
        self.channels = channels or [ConsoleChannel()]

    @classmethod
    def from_env(cls, force_console: bool = False) -> "Notifier":
        channels: List[Channel] = []

        webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        if webhook:
            channels.append(DiscordChannel(webhook))

        recipients = [
            r.strip() for r in os.getenv("EMAIL_TO", "").split(",") if r.strip()
        ]
        host = os.getenv("SMTP_HOST", "").strip()
        if recipients and host:
            channels.append(
                EmailChannel(
                    host=host,
                    port=int(os.getenv("SMTP_PORT", "587")),
                    user=os.getenv("SMTP_USER") or None,
                    password=os.getenv("SMTP_PASS") or None,
                    sender=os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER") or "",
                    recipients=recipients,
                )
            )

        if force_console or not channels:
            channels.append(ConsoleChannel())
        return cls(channels)

    @property
    def channel_names(self) -> List[str]:
        return [c.name for c in self.channels]

    def send(self, message: Message) -> List[Tuple[str, bool, Optional[str]]]:
        """Deliver to every channel. One channel failing must not stop the rest."""
        results: List[Tuple[str, bool, Optional[str]]] = []
        for channel in self.channels:
            try:
                channel.send(message)
                results.append((channel.name, True, None))
            except Exception as exc:
                results.append((channel.name, False, str(exc)))
                print("notify: channel %s failed: %s" % (channel.name, exc))
        return results
