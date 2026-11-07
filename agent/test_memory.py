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
