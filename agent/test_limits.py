"""Caps on chat calls, open card CORS, and the static agent card."""

import json
import os
import unittest
from pathlib import Path

os.environ["A2A_FAKE_LLM"] = "1"
os.environ.pop("OPENAI_API_KEY", None)

import app  # noqa: E402
from quota import DAILY_MESSAGE, IP_MESSAGE, RATE_LIMIT_CODE, MessageQuota  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def send(client, text="Hola", method="message/send", ip="203.0.113.8", request_id="1"):
    return client.post(
        "/",
        headers={"X-Forwarded-For": ip, "Content-Type": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m-" + str(request_id),
                    "parts": [{"kind": "text", "text": text}],
                }
            },
        },
    )


def client_for(daily, per_ip, now=None):
    quota = MessageQuota(daily, per_ip, now=now)
    return TestClient(app.create_app(quota)), quota


class QuotaUnitTests(unittest.TestCase):
    def test_daily_cap_resets_on_the_next_utc_day(self):
        clock = {"t": 1_700_000_000.0}
        quota = MessageQuota(1, 5, now=lambda: clock["t"])
        self.assertIsNone(quota.consume("1.1.1.1"))
        self.assertEqual(quota.consume("1.1.1.1"), "daily")
        clock["t"] += 86400
        self.assertIsNone(quota.consume("1.1.1.1"))

    def test_ip_cap_is_a_sliding_hour_and_does_not_block_others(self):
        clock = {"t": 1_700_000_000.0}
        quota = MessageQuota(30, 2, now=lambda: clock["t"])
        self.assertIsNone(quota.consume("10.0.0.1"))
        self.assertIsNone(quota.consume("10.0.0.1"))
        self.assertEqual(quota.consume("10.0.0.1"), "ip")
        self.assertIsNone(quota.consume("10.0.0.2"))
        clock["t"] += 3601
        self.assertIsNone(quota.consume("10.0.0.1"))

    def test_invalid_env_falls_back_to_defaults(self):
        os.environ["MAX_DAILY_MESSAGES"] = "nope"
        os.environ["MAX_MESSAGES_PER_IP_PER_HOUR"] = "-3"
        try:
            quota = MessageQuota.from_env()
            self.assertEqual(quota.daily_limit, 30)
            self.assertEqual(quota.per_ip_per_hour, 5)
        finally:
            os.environ.pop("MAX_DAILY_MESSAGES", None)
            os.environ.pop("MAX_MESSAGES_PER_IP_PER_HOUR", None)


class ServerLimitTests(unittest.TestCase):
    def test_message_send_stops_at_the_daily_cap(self):
        client, _quota = client_for(2, 10)
        first = send(client, request_id="a")
        second = send(client, request_id="b")
        third = send(client, request_id="c")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["result"]["status"]["state"], "completed")
        self.assertEqual(second.json()["result"]["artifacts"][0]["parts"][0]["text"], "echo: Hola")
        self.assertEqual(third.status_code, 200)
        self.assertEqual(third.json()["error"]["code"], RATE_LIMIT_CODE)
        self.assertEqual(third.json()["error"]["message"], DAILY_MESSAGE)
        self.assertEqual(third.json()["id"], "c")
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in third.headers})

    def test_per_ip_limit_leaves_other_addresses_free(self):
        client, _quota = client_for(30, 1)
        self.assertEqual(send(client, ip="198.51.100.2", request_id="1").status_code, 200)
        blocked = send(client, ip="198.51.100.2", request_id="2")
        other = send(client, ip="198.51.100.3", request_id="3")
        self.assertEqual(blocked.json()["error"]["message"], IP_MESSAGE)
        self.assertEqual(other.json()["result"]["status"]["state"], "completed")

    def test_message_stream_counts_toward_the_cap(self):
        client, _quota = client_for(1, 5)
        self.assertEqual(send(client, request_id="a").json()["result"]["status"]["state"], "completed")
        blocked = send(client, method="message/stream", request_id="s")
        self.assertEqual(blocked.json()["error"]["message"], DAILY_MESSAGE)

    def test_tasks_get_and_discovery_stay_free_after_the_cap(self):
        client, _quota = client_for(0, 5)
        card = client.get("/.well-known/agent-card.json", headers={"Origin": "https://example.com"})
        legacy = client.get("/.well-known/agent.json")
        health = client.get("/health")
        task = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "t", "method": "tasks/get", "params": {"id": "missing"}},
        )
        self.assertEqual(card.status_code, 200)
        self.assertEqual(card.headers["access-control-allow-origin"], "*")
        self.assertEqual(legacy.json(), card.json())
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertNotIn(DAILY_MESSAGE, json.dumps(task.json()))
        self.assertNotEqual(task.json().get("error", {}).get("code"), RATE_LIMIT_CODE)
        preflight = client.options("/.well-known/agent-card.json")
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight.headers["access-control-allow-origin"], "*")

    def test_plain_post_is_429_only_when_the_cap_is_spent(self):
        open_client, _quota = client_for(2, 5)
        under = open_client.post("/", content=b"hola", headers={"Content-Type": "text/plain"})
        self.assertNotEqual(under.status_code, 429)

        closed, _quota = client_for(0, 5)
        over = closed.post("/", content=b"hola", headers={"Content-Type": "text/plain"})
        self.assertEqual(over.status_code, 429)
        self.assertEqual(over.json()["error"], DAILY_MESSAGE)

    def test_static_card_matches_the_service(self):
        previous = os.environ.get("PUBLIC_URL")
        os.environ["PUBLIC_URL"] = "https://agent.agiq.top"
        try:
            expected = app.card_dict()
        finally:
            if previous is None:
                os.environ.pop("PUBLIC_URL", None)
            else:
                os.environ["PUBLIC_URL"] = previous
        card = json.loads((ROOT / ".well-known" / "agent-card.json").read_text())
        legacy = json.loads((ROOT / ".well-known" / "agent.json").read_text())
        self.assertEqual(card, legacy)
        self.assertEqual(card, expected)
        self.assertEqual(card["url"], "https://agent.agiq.top")
        self.assertEqual(card["provider"], {"organization": "MABC80", "url": "https://agiq.top"})
        self.assertEqual(card["documentationUrl"], "https://agiq.top")
        self.assertEqual(card["iconUrl"], "https://agiq.top/icon.svg")
        self.assertEqual(card["name"], "AGIQ Books")
        self.assertEqual(card["version"], "0.2.0")
        facts = json.loads((ROOT / "agentfacts.json").read_text())
        self.assertEqual(facts["agent_name"], card["name"])
        self.assertEqual(facts["version"], card["version"])
        self.assertEqual(facts["provider"], {"name": card["provider"]["organization"], "url": card["provider"]["url"]})
        self.assertEqual(facts["endpoints"]["a2a"], card["url"])
        self.assertEqual(
            facts["endpoints"]["agent_card"],
            card["url"] + "/.well-known/agent-card.json",
        )
        self.assertEqual(facts["capabilities"]["streaming"], card["capabilities"]["streaming"])
        self.assertEqual(facts["skills"][0]["id"], card["skills"][0]["id"])
        self.assertEqual(facts["skills"][0]["inputModes"], card["skills"][0]["inputModes"])
        self.assertEqual(facts["skills"][0]["outputModes"], card["skills"][0]["outputModes"])
        self.assertTrue((ROOT / ".nojekyll").is_file())
        page = (ROOT / "index.html").read_text()
        self.assertIn("AGIQ Books", page)
        self.assertNotIn("<form", page.lower())
        self.assertIn("https://agent.agiq.top/.well-known/agent-card.json", page)
        self.assertIn("https://agiq.top/.well-known/agent-card.json", page)
        self.assertNotIn("message/send", page)
        self.assertIn("https://agent.agiq.top/analytics/pageview", page)
        self.assertIn("<script", page.lower())
        blueprint = (ROOT / "render.yaml").read_text()
        self.assertIn("MAX_DAILY_MESSAGES", blueprint)
        self.assertIn('value: "30"', blueprint)
        self.assertIn("MAX_DAILY_ACTIONS", blueprint)
        self.assertIn('value: "1000"', blueprint)
        for key in (
            "RESEND_API_KEY",
            "EMAIL_FROM",
            "EMAIL_REPLY_TO",
            "REPORT_EMAIL_TO",
            "ADMIN_TOKEN",
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USER",
            "SMTP_PASSWORD",
        ):
            self.assertIn(key, blueprint)
        self.assertIn("mbueno74@hotmail.com", blueprint)


if __name__ == "__main__":
    unittest.main()
