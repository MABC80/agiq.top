"""Classification, visit log, daily report, admin auth, and the page beacon."""

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

os.environ["A2A_FAKE_LLM"] = "1"
os.environ.pop("OPENAI_API_KEY", None)
for _name in (
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "EMAIL_REPLY_TO",
    "REPORT_EMAIL_TO",
    "SMTP_HOST",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "STRIPE_SECRET_KEY",
    "DATABASE_URL",
    "ADMIN_TOKEN",
):
    os.environ.pop(_name, None)

import app  # noqa: E402
from analytics import PAGE_ORIGIN, berlin_window, classify  # noqa: E402
from emailer import resend_payload  # noqa: E402
from bookstore import Bookstore  # noqa: E402
from db import Base, Visit  # noqa: E402
from payments import SandboxPaymentProvider  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class CaptureEmail:
    reply_to = "mbueno74@hotmail.com"

    def __init__(self) -> None:
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return "sent"


class ClassificationTests(unittest.TestCase):
    def test_known_bots_humans_and_agents(self):
        cases = [
            ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", False, None, "known_bot"),
            ("curl/8.7.1", False, None, "known_bot"),
            ("python-requests/2.32.3", False, None, "known_bot"),
            ("python-httpx/0.28.1", False, None, "known_bot"),
            ("a2aregistry/1.0", False, None, "known_bot"),
            ("AgentPeering/0.3", False, None, "known_bot"),
            ("NANDAFacts/1.0", False, None, "known_bot"),
            ("UptimeRobot/2.0", False, None, "known_bot"),
            (BROWSER, True, None, "human"),
            (BROWSER, False, None, "human"),
            ("python-httpx/0.28.1", False, "Shelf Agent", "ai_agent"),
            ("BookstoreAgent/1.2", False, None, "ai_agent"),
            ("mystery-client", False, None, "unknown"),
        ]
        for user_agent, beacon, identity, expected in cases:
            kind, reason = classify(user_agent, beacon=beacon, identity=identity)
            self.assertEqual(kind, expected, user_agent)
            self.assertTrue(reason)

    def test_order_mail_sets_reply_to_without_using_hotmail_as_the_sender(self):
        payload = resend_payload(
            sender="AGIQ Books <orders@agiq.top>",
            to="reader@example.com",
            subject="AGIQ Books order abc",
            text="paid",
            reply_to="mbueno74@hotmail.com",
        )
        self.assertEqual(payload["from"], "AGIQ Books <orders@agiq.top>")
        self.assertEqual(payload["reply_to"], "mbueno74@hotmail.com")
        self.assertNotIn("hotmail.com", payload["from"])


class PostgresShapeTests(unittest.TestCase):
    def test_tables_compile_for_postgres(self):
        dialect = postgresql.dialect()
        names = set()
        for table in Base.metadata.sorted_tables:
            sql = str(CreateTable(table).compile(dialect=dialect))
            self.assertIn("CREATE TABLE", sql)
            names.add(table.name)
        for required in ("customers", "carts", "orders", "invoices", "stock", "counters", "visits", "analytics_salts"):
            self.assertIn(required, names)


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.url = "sqlite:///" + str(Path(self._dir.name) / "books.sqlite")
        self.mail = CaptureEmail()
        self.store = Bookstore(
            database_url=self.url,
            payments=SandboxPaymentProvider(),
            email=self.mail,
            public_base="https://agent.agiq.top",
        )
        self.client = TestClient(app.create_app(database_url=self.url, store=self.store))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._dir.cleanup()
        os.environ.pop("ADMIN_TOKEN", None)
        os.environ.pop("REPORT_EMAIL_TO", None)
        os.environ.pop("PAGEVIEW_PER_HOUR", None)

    def _rows(self):
        with self.store.Session() as session:
            return session.scalars(select(Visit)).all()

    def test_requests_are_hashed_and_do_not_keep_message_text(self):
        secret = "super-secret-phrase-not-for-storage"
        card = self.client.get(
            "/.well-known/agent-card.json",
            headers={"User-Agent": "curl/8.7.1", "X-Forwarded-For": "203.0.113.50", "CF-IPCountry": "DE"},
        )
        self.assertEqual(card.status_code, 200)
        health = self.client.get("/health", headers={"User-Agent": "UptimeRobot/2.0"})
        self.assertEqual(health.status_code, 200)
        sent = self.client.post(
            "/",
            headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.50", "X-Agent-Name": "Shelf Agent"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": "m1",
                        "metadata": {"agentCard": "https://example.com/agent-card.json"},
                        "parts": [{"kind": "text", "text": secret}],
                    }
                },
            },
        )
        self.assertEqual(sent.status_code, 200)
        structured = self.client.post(
            "/",
            headers={"Content-Type": "application/json", "User-Agent": "python-httpx/0.28.1"},
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": "m2",
                        "parts": [{"kind": "data", "data": {"action": "list_books", "card_number": "4242424242424242"}}],
                    }
                },
            },
        )
        self.assertEqual(structured.status_code, 200)
        rows = self._rows()
        blob = json.dumps(
            [
                {
                    "kind": row.kind,
                    "ua": row.user_agent,
                    "hash": row.ip_hash,
                    "country": row.country,
                    "style": row.style,
                    "action": row.action_name,
                    "length": row.text_length,
                    "name": row.agent_name,
                    "card": row.agent_card_url,
                    "class": row.classification,
                }
                for row in rows
            ]
        )
        self.assertNotIn(secret, blob)
        self.assertNotIn("4242424242424242", blob)
        self.assertNotIn("203.0.113.50", blob)
        kinds = {row.kind for row in rows}
        self.assertIn("card_fetch", kinds)
        self.assertIn("monitor", kinds)
        self.assertIn("a2a_message", kinds)
        card_row = next(row for row in rows if row.kind == "card_fetch")
        self.assertEqual(card_row.classification, "known_bot")
        self.assertEqual(card_row.country, "DE")
        self.assertNotEqual(card_row.ip_hash, "203.0.113.50")
        text_row = next(row for row in rows if row.style == "natural_language")
        self.assertEqual(text_row.text_length, len(secret))
        self.assertEqual(text_row.agent_name, "Shelf Agent")
        self.assertEqual(text_row.classification, "ai_agent")
        self.assertEqual(text_row.rpc_method, "message/send")
        action_row = next(row for row in rows if row.style == "structured")
        self.assertEqual(action_row.action_name, "list_books")
        same = [row.ip_hash for row in rows if row.kind in {"card_fetch", "a2a_message"} and row.agent_name == "Shelf Agent" or row.kind == "card_fetch"]
        hashes = {row.ip_hash for row in rows if row.kind in {"card_fetch"} or row.agent_name == "Shelf Agent"}
        self.assertEqual(len(hashes), 1)
        self.assertTrue(same)

    def test_confirmation_email_includes_reply_to(self):
        registered = self.store.handle(
            {
                "action": "register_customer",
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "billing": {"line1": "1 Engine", "city": "London", "postal_code": "W1", "country": "GB"},
            }
        )
        customer_id = registered.data["customer"]["customer_id"]
        cart = self.store.handle({"action": "create_cart", "customer_id": customer_id})
        self.store.handle({"action": "add_to_cart", "cart_id": cart.data["cart"]["cart_id"], "rank": 1})
        order = self.store.handle({"action": "checkout", "cart_id": cart.data["cart"]["cart_id"]})
        paid = self.store.handle(
            {"action": "pay", "order_id": order.data["order"]["order_id"], "card_number": "4242424242424242"}
        )
        self.assertEqual(paid.state, "completed")
        self.assertEqual(self.mail.sent[0]["reply_to"], "mbueno74@hotmail.com")
        self.assertEqual(self.mail.sent[0]["to"], "ada@example.com")

    def test_beacon_cors_and_rate_limit(self):
        os.environ["PAGEVIEW_PER_HOUR"] = "2"
        headers = {"Origin": PAGE_ORIGIN, "User-Agent": BROWSER, "Content-Type": "text/plain"}
        preflight = self.client.options("/analytics/pageview", headers={"Origin": PAGE_ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight.headers["access-control-allow-origin"], PAGE_ORIGIN)
        first = self.client.post("/analytics/pageview", headers=headers, content=b'{"path":"/","referrer":"https://example.com"}')
        second = self.client.post("/analytics/pageview", headers=headers, content=b'{"path":"/"}')
        third = self.client.post("/analytics/pageview", headers=headers, content=b'{"path":"/"}')
        self.assertEqual(first.status_code, 204)
        self.assertEqual(first.headers["access-control-allow-origin"], PAGE_ORIGIN)
        self.assertEqual(second.status_code, 204)
        self.assertEqual(third.status_code, 429)
        evil = self.client.post(
            "/analytics/pageview",
            headers={"Origin": "https://evil.example", "User-Agent": BROWSER},
            content=b"{}",
        )
        self.assertEqual(evil.status_code, 403)
        self.assertNotIn("access-control-allow-origin", {key.lower() for key in evil.headers})
        views = [row for row in self._rows() if row.kind == "page_view"]
        self.assertEqual(len(views), 2)
        self.assertEqual(views[0].classification, "human")
        self.assertEqual(views[0].referer, "https://example.com")

    def test_report_auth_and_dry_run(self):
        self.store.handle(
            {
                "action": "register_customer",
                "name": "Grace Hopper",
                "email": "grace@example.com",
                "billing": {"line1": "1 Hopper", "city": "Arlington", "postal_code": "22201", "country": "US"},
            }
        )
        self.client.post(
            "/",
            headers={"Content-Type": "application/json", "User-Agent": "curl/8.7.1", "X-Forwarded-For": "198.51.100.9"},
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "tasks/get",
                "params": {"id": "missing"},
            },
        )
        missing = self.client.post("/admin/daily-report", headers={"Authorization": "Bearer secret"})
        self.assertEqual(missing.status_code, 404)
        os.environ["ADMIN_TOKEN"] = "secret-token"
        os.environ["REPORT_EMAIL_TO"] = "mbueno74@hotmail.com"
        wrong = self.client.post("/admin/daily-report", headers={"Authorization": "Bearer no"})
        self.assertEqual(wrong.status_code, 401)
        bare = self.client.get("/admin/stats")
        self.assertEqual(bare.status_code, 401)
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
        dry = self.client.post(
            f"/admin/daily-report?date={today}&dry_run=1",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(dry.status_code, 200)
        body = dry.json()
        self.assertEqual(body["email"]["status"], "dry_run")
        self.assertIn("<h1>", body["html"])
        self.assertIn("AGIQ Books daily report", body["text"])
        self.assertGreaterEqual(body["classifications"]["known_bot"], 1)
        self.assertGreaterEqual(body["funnel"]["customers_registered"], 1)
        self.assertIn("Purchase funnel", body["html"])
        self.assertEqual(self.mail.sent, [])
        live = self.client.post(
            f"/admin/daily-report?date={today}",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(live.json()["email"]["status"], "sent")
        self.assertEqual(self.mail.sent[-1]["to"], "mbueno74@hotmail.com")
        self.assertIn("<h1>", self.mail.sent[-1]["html"])
        self.assertEqual(self.mail.sent[-1]["reply_to"], "mbueno74@hotmail.com")
        stats = self.client.get(
            f"/admin/stats?date={today}",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(stats.status_code, 200)
        self.assertNotIn("html", stats.json())
        self.assertIn("funnel", stats.json())
        bad = self.client.get("/admin/stats?date=yesterday", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(bad.status_code, 400)

    def test_berlin_day_boundary(self):
        start, end = berlin_window(__import__("datetime").date(2026, 10, 26))
        with self.store.Session() as session:
            session.add(
                Visit(
                    created_at=start + timedelta(hours=12),
                    kind="card_fetch",
                    method="GET",
                    path="/.well-known/agent-card.json",
                    status_code=200,
                    latency_ms=3,
                    user_agent="curl/8.7.1",
                    ip_hash="a" * 64,
                    referer="",
                    classification="known_bot",
                    classifier_reason="known crawler: curl",
                    quota_limited=False,
                )
            )
            session.add(
                Visit(
                    created_at=end + timedelta(minutes=5),
                    kind="card_fetch",
                    method="GET",
                    path="/.well-known/agent-card.json",
                    status_code=200,
                    latency_ms=3,
                    user_agent="curl/8.7.1",
                    ip_hash="b" * 64,
                    referer="",
                    classification="known_bot",
                    classifier_reason="known crawler: curl",
                    quota_limited=False,
                )
            )
            session.commit()
        os.environ["ADMIN_TOKEN"] = "secret-token"
        response = self.client.post(
            "/admin/daily-report?date=2026-10-26&dry_run=1",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totals"]["requests"], 1)
        self.assertEqual(response.json()["totals"]["unique_visitors"], 1)

    def test_ip_hash_rotates_each_utc_day_and_hides_the_address(self):
        from datetime import datetime, timezone

        from analytics import Recorder

        recorder = Recorder(self.store)
        first = recorder.hash_ip("203.0.113.8", datetime(2026, 1, 1, tzinfo=timezone.utc))
        same_day = recorder.hash_ip("203.0.113.8", datetime(2026, 1, 1, 18, tzinfo=timezone.utc))
        next_day = recorder.hash_ip("203.0.113.8", datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(first, same_day)
        self.assertNotEqual(first, next_day)
        self.assertNotIn("203.0.113.8", first)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    unittest.main()
