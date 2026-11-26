import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime

from db_connection import ENV


class TeamsMsger:
    def __init__(self, env):
        self.webhookUrl_ = os.getenv("PCF_TEAMS_WEBHOOK_URL", "").strip()
        self.env_ = env
        self.logger_ = logging.getLogger("Main")

    def _build_card(self, body_text, title=None, title_style=None, body_style=None):
        card_body = []

        if title:
            title_block = {
                "type": "TextBlock",
                "text": title,
                "wrap": True,
                "weight": "Bolder",
                "size": "Large",
            }
            if title_style:
                title_block.update(title_style)
            card_body.append(
                {
                    "type": "Container",
                    "style": "emphasis",
                    "bleed": True,
                    "items": [title_block],
                }
            )

        body_block = {
            "type": "TextBlock",
            "text": body_text.replace("\n", "\n\n"),
            "wrap": True,
            "spacing": "Medium",
            "separator": bool(title),
        }
        if body_style:
            body_block.update(body_style)
        card_body.append(body_block)

        return {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "msteams": {"width": "Full"},
            "body": card_body,
        }

    def sendMessage(self, message, title=None, title_style=None, body_style=None):
        if self.env_ != ENV.PROD:
            self.logger_.info(f"*** Title: {title} ***")
            self.logger_.info(f"*** Message: {message} ***")
            return 0

        send_after = datetime.strptime("2026-11-17 16:00:00", "%Y-%m-%d %H:%M:%S")
        if datetime.now() < send_after:
            self.logger_.info(f"*** Title: {title} ***")
            self.logger_.info(f"*** Message: {message} ***")
            return 0

        if not self.webhookUrl_:
            self.logger_.error("PCF_TEAMS_WEBHOOK_URL is not configured")
            return 1

        payload = self._build_card(message, title, title_style, body_style)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhookUrl_,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                self.logger_.info(f"HTTP {resp.status}")
                if body:
                    self.logger_.info(body)
                return 0
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self.logger_.error(f"HTTP {e.code}: {e.reason}")
            self.logger_.error(body)
            return 1
