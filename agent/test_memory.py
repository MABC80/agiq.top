"""Memory bounds: SQL report, request cap, task eviction, and a light health reading."""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["A2A_FAKE_LLM"] = "1"
os.environ.pop("OPENAI_API_KEY", None)

import app  # noqa: E402
from analytics import build_report  # noqa: E402
from app import MAX_REQUEST_BODY, BoundedTaskStore  # noqa: E402
from bookstore import Bookstore  # noqa: E402
from db import Invoice, Visit  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


class StartupTests(unittest.TestCase):
    def test_import_does_not_load_langgraph(self):
        env = os.environ.copy()
        env["A2A_FAKE_LLM"] = "1"
        env.pop("OPENAI_API_KEY", None)
        code = (
            "import app, sys; "
            "raise SystemExit(0 if 'langgraph' not in sys.modules and 'langchain_openai' not in sys.modules else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parent,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class BoundTests(unittest.TestCase):
    def test_task_store_forgets_old_threads(self):
        store = BoundedTaskStore(limit=2)

        async def fill():
            for number in range(3):
                await store.save(type("Task", (), {"id": f"t{number}"})())

        import asyncio

        asyncio.run(fill())
        self.assertEqual(set(store.tasks), {"t1", "t2"})

    def test_oversized_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(
                app.create_app(database_url="sqlite:///" + str(Path(directory) / "books.sqlite"))
            )
            with client:
                response = client.post("/", content=b"x" * (MAX_REQUEST_BODY + 1))
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"], "Request body is too large.")

    def test_health_reports_status_and_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(
                app.create_app(database_url="sqlite:///" + str(Path(directory) / "books.sqlite"))
            )
            with client:
                body = client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertGreater(body["rss_kb"], 0)
        self.assertEqual(set(body), {"status", "rss_kb"})


class ReportFlightTests(unittest.TestCase):
    def tearDown(self):
        app.reset_report_gate()
        os.environ.pop("ADMIN_TOKEN", None)

    def test_a_second_report_is_rejected_and_health_still_answers(self):
        import asyncio
        import threading

        import httpx

        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        original = app.deliver_report

        def slow(store, day, *, dry_run):
            calls["n"] += 1
            started.set()
            release.wait(5)
            return {"date": day.isoformat(), "email": {"status": "dry_run"}}

        app.deliver_report = slow
        application = app.create_app(database_url="sqlite:///" + str(Path(tempfile.mkdtemp()) / "books.sqlite"))
        os.environ["ADMIN_TOKEN"] = "secret-token"

        async def run():
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://report") as client:
                headers = {"Authorization": "Bearer secret-token"}

                async def first():
                    return await client.post("/admin/daily-report?date=2026-10-26&dry_run=1", headers=headers)

                task = asyncio.create_task(first())
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                import time

                clock = time.perf_counter()
                health = await client.get("/health")
                self.assertLess(time.perf_counter() - clock, 1.0)
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")
                second = await client.post("/admin/daily-report?date=2026-10-26&dry_run=1", headers=headers)
                self.assertEqual(second.status_code, 409)
                self.assertEqual(second.json()["error"], app.REPORT_BUSY)
                release.set()
                done = await task
                self.assertEqual(done.status_code, 200)
                again = await client.post("/admin/daily-report?date=2026-10-26&dry_run=1", headers=headers)
                self.assertEqual(again.status_code, 200)
                self.assertEqual(again.json()["email"]["status"], "dry_run")
                self.assertEqual(calls["n"], 1)

        try:
            asyncio.run(run())
        finally:
            release.set()
            app.deliver_report = original
            app.reset_report_gate()

    def test_conversation_load_keeps_the_latest_rows_and_cuts_text(self):
        from conversations import MESSAGE_LIMIT, load_threads
        from datetime import timezone

        from analytics import berlin_window
        from db import ConversationMessage

        with tempfile.TemporaryDirectory() as directory:
            store = Bookstore(database_url="sqlite:///" + str(Path(directory) / "books.sqlite"))
            start, _end = berlin_window(__import__("datetime").date(2026, 10, 26))
            with store.Session() as session:
                for number in range(MESSAGE_LIMIT + 5):
                    session.add(
                        ConversationMessage(
                            created_at=start + timedelta(minutes=number),
                            visitor_hash="ab" * 32,
                            classification="ai_agent",
                            classifier_reason="declared agent identity",
                            style="natural_language",
                            context_id="ctx",
                            inbound="m" + str(number) + ("x" * 800),
                            reply="reply " + str(number),
                        )
                    )
                session.commit()
            threads = load_threads(store, start, start + timedelta(days=1), text_chars=500)
        self.assertEqual(len(threads), 1)
        messages = threads[0]["messages"]
        self.assertEqual(len(messages), MESSAGE_LIMIT)
        self.assertTrue(messages[0]["inbound"].startswith("m5"))
        self.assertTrue(messages[-1]["inbound"].startswith("m204"))
        self.assertLessEqual(len(messages[0]["inbound"]), 500)


class ReportQueryTests(unittest.TestCase):
    def test_report_counts_the_day_without_loading_other_days(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Bookstore(database_url="sqlite:///" + str(Path(directory) / "books.sqlite"))
            inside = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
            outside = inside - timedelta(days=3)
            with store.Session() as session:
                session.add(
                    Visit(
                        created_at=inside,
                        kind="card_fetch",
                        method="GET",
                        path="/.well-known/agent-card.json",
                        status_code=200,
                        latency_ms=2,
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
                        created_at=inside,
                        kind="monitor",
                        method="GET",
                        path="/health",
                        status_code=200,
                        latency_ms=1,
                        user_agent="UptimeRobot/2.0",
                        ip_hash="b" * 64,
                        referer="",
                        classification="known_bot",
                        classifier_reason="known crawler: UptimeRobot",
                        quota_limited=False,
                    )
                )
                session.add(
                    Visit(
                        created_at=outside,
                        kind="card_fetch",
                        method="GET",
                        path="/.well-known/agent-card.json",
                        status_code=500,
                        latency_ms=2,
                        user_agent="curl/8.7.1",
                        ip_hash="c" * 64,
                        referer="",
                        classification="known_bot",
                        classifier_reason="known crawler: curl",
                        quota_limited=False,
                    )
                )
                session.add(
                    Invoice(
                        id="inv-in",
                        order_id="order-in",
                        number="INV-000099",
                        token="tok",
                        xml="<Invoice/>",
                        pdf=b"x" * 100_000,
                        created_at=inside,
                    )
                )
                session.add(
                    Invoice(
                        id="inv-out",
                        order_id="order-out",
                        number="INV-000098",
                        token="tok2",
                        xml="<Invoice/>",
                        pdf=b"y" * 100_000,
                        created_at=outside,
                    )
                )
                session.commit()
            report = build_report(store, inside.date())
        self.assertEqual(report["totals"]["requests"], 1)
        self.assertEqual(report["monitors"], 1)
        self.assertEqual(report["errors"]["5xx"], 0)
        self.assertEqual(report["funnel"]["invoices"], 1)
        self.assertEqual(report["classifications"]["known_bot"], 1)


if __name__ == "__main__":
    unittest.main()
