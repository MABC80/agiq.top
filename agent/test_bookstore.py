"""Catalog, purchase flow, structured A2A, MCP, and action limits."""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

os.environ["A2A_FAKE_LLM"] = "1"
os.environ.pop("OPENAI_API_KEY", None)
for _name in (
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "STRIPE_SECRET_KEY",
    "DATABASE_URL",
):
    os.environ.pop(_name, None)

import app  # noqa: E402
from actions import ACTIONS  # noqa: E402
from bookstore import Bookstore  # noqa: E402
from catalog import isbn13_is_valid, load_catalog  # noqa: E402
from db import Payment  # noqa: E402
from emailer import NOT_CONFIGURED  # noqa: E402
from payments import SandboxPaymentProvider  # noqa: E402
from quota import ACTION_DAILY_MESSAGE, MessageQuota  # noqa: E402
from sqlalchemy import select  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TITLES = [
    "Theo of Golden",
    "Project Hail Mary",
    "The Let Them Theory",
    "The Correspondent",
    "Oh, the Places You'll Go!",
    "Dear Debbie",
    "Dungeon Crawler Carl",
    "Yesteryear",
    "Heated Rivalry",
    "The Housemaid",
]
PAN = "4242424242424242"
DECLINE = "4000000000000002"


def bookstore(directory: str) -> Bookstore:
    return Bookstore(
        database_url="sqlite:///" + str(Path(directory) / "books.sqlite"),
        payments=SandboxPaymentProvider(),
        email=None,
        public_base="https://agent.agiq.top",
    )


def billing():
    return {"line1": "1 Analytical Engine", "city": "London", "postal_code": "W1", "country": "GB"}


class CatalogTests(unittest.TestCase):
    def test_top_ten_matches_the_first_half_list(self):
        catalog = load_catalog()
        self.assertEqual(catalog["list"]["as_of"], "2026-07-04")
        self.assertIn("100790", catalog["list"]["url"])
        self.assertEqual([book["title"] for book in catalog["books"]], TITLES)
        for book in catalog["books"]:
            if book["isbn13"] is not None:
                self.assertTrue(isbn13_is_valid(book["isbn13"]), book["title"])
            self.assertEqual(book["source"]["as_of"], "2026-07-04")
        housemaid = catalog["books"][9]
        seuss = catalog["books"][4]
        for book in (housemaid, seuss):
            self.assertIsNone(book["isbn13"])
            self.assertIsNone(book["pages"])
            self.assertIsNone(book["format"])
            self.assertIsNone(book["list_price_usd"])
            self.assertIsNone(book["publication_date"])
            self.assertIsNone(book["language"])
        theo = catalog["books"][0]
        self.assertEqual(theo["isbn13"], "9781668236512")
        self.assertEqual(theo["list_price_usd"], "20.00")
        self.assertIsNone(theo["pages"])
        self.assertIsNone(theo["publication_date"])
        readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("2026-07-04", readme)
        self.assertIn(catalog["list"]["url"], readme)
        for spec in ACTIONS:
            self.assertIn(spec["id"], readme)
            self.assertIn(spec["example"], readme)


class PurchaseFlowTests(unittest.TestCase):
    def test_register_cart_checkout_pay_invoice_and_skipped_email(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bookstore(directory)
            with self.assertLogs("agiq.bookstore", level="WARNING") as captured:
                registered = store.handle(
                    {
                        "action": "register_customer",
                        "name": "Ada Lovelace",
                        "email": "ada@example.com",
                        "billing": billing(),
                        "tax_id": "GB123",
                    }
                )
                self.assertEqual(registered.state, "completed")
                customer_id = registered.data["customer"]["customer_id"]
                cart = store.handle({"action": "create_cart", "customer_id": customer_id, "context_id": "ctx-1"})
                cart_id = cart.data["cart"]["cart_id"]
                added = store.handle(
                    {"action": "add_to_cart", "cart_id": cart_id, "isbn13": "978-1-6682-3651-2", "quantity": 1}
                )
                self.assertEqual(added.data["cart"]["items"][0]["unit_price_usd"], "20.00")
                unsellable = store.handle({"action": "add_to_cart", "cart_id": cart_id, "rank": 10})
                self.assertEqual(unsellable.state, "failed")
                self.assertIn("not for sale", unsellable.message)
                order = store.handle({"action": "checkout", "cart_id": cart_id})
                self.assertEqual(order.state, "completed")
                self.assertEqual(order.data["order"]["subtotal_usd"], "20.00")
                self.assertEqual(order.data["order"]["tax_usd"], "1.60")
                self.assertEqual(order.data["order"]["shipping_usd"], "5.00")
                self.assertEqual(order.data["order"]["total_usd"], "26.60")
                order_id = order.data["order"]["order_id"]
                stock_after_reserve = store.handle({"action": "get_book", "rank": 1}).data["book"]["stock"]
                self.assertEqual(stock_after_reserve, 19)

                declined = store.handle({"action": "pay", "order_id": order_id, "card_number": DECLINE})
                self.assertEqual(declined.state, "failed")
                self.assertEqual(declined.data["payment"]["status"], "declined")
                self.assertEqual(declined.data["payment"]["last4"], "0002")
                self.assertEqual(declined.data["payment"]["brand"], "visa")
                self.assertTrue(declined.data["payment"]["sandbox"])
                self.assertNotIn(DECLINE, json.dumps(declined.data))
                self.assertEqual(store.handle({"action": "get_book", "rank": 1}).data["book"]["stock"], 20)

                again = store.handle({"action": "create_cart", "customer_id": customer_id})
                store.handle({"action": "add_to_cart", "cart_id": again.data["cart"]["cart_id"], "rank": 1})
                paid_order = store.handle({"action": "checkout", "cart_id": again.data["cart"]["cart_id"]})
                paid = store.handle(
                    {"action": "pay", "order_id": paid_order.data["order"]["order_id"], "card_number": PAN}
                )
            self.assertEqual(paid.state, "completed")
            self.assertEqual(paid.data["payment"]["last4"], "4242")
            self.assertEqual(paid.data["payment"]["sandbox"], True)
            self.assertEqual(paid.data["notification"]["status"], NOT_CONFIGURED)
            self.assertTrue(any(NOT_CONFIGURED in line for line in captured.output))
            self.assertNotIn(PAN, json.dumps(paid.data))
            invoice = paid.data["invoice"]
            self.assertEqual(invoice["invoice_number"], "INV-000001")
            self.assertTrue(invoice["xml_url"].startswith("https://agent.agiq.top/invoices/"))
            xml = next(content for name, content, mime in paid.files if name.endswith(".xml"))
            pdf = next(content for name, content, mime in paid.files if name.endswith(".pdf"))
            self.assertTrue(pdf.startswith(b"%PDF-"))
            root = ET.fromstring(xml)
            self.assertTrue(root.tag.endswith("Invoice"))
            children = {node.tag.split("}")[-1]: node.text for node in list(root)}
            self.assertEqual(children["CustomizationID"], "urn:cen.eu:en16931:2017")
            self.assertEqual(children["ID"], "INV-000001")
            self.assertEqual(children["DocumentCurrencyCode"], "USD")
            self.assertGreaterEqual(sum(1 for node in root.iter() if node.tag.endswith("InvoiceLine")), 1)
            self.assertNotIn(PAN, xml.decode())
            status = store.handle({"action": "get_order_status", "order_id": paid_order.data["order"]["order_id"]})
            self.assertEqual(status.data["order"]["status"], "paid")
            with store.Session() as session:
                columns = {column.name for column in Payment.__table__.columns}
                self.assertNotIn("card_number", columns)
                self.assertNotIn("pan", columns)
                stored = session.scalars(select(Payment)).all()
                blob = " ".join(row.last4 + row.detail + row.brand for row in stored)
                self.assertNotIn(PAN, blob)
                self.assertNotIn(DECLINE, blob)

    def test_bad_email_asks_for_input(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bookstore(directory)
            outcome = store.handle(
                {"action": "register_customer", "name": "Ada", "email": "not-an-email", "billing": billing()}
            )
            self.assertEqual(outcome.state, "input-required")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.client = TestClient(
            app.create_app(database_url="sqlite:///" + str(Path(self._dir.name) / "books.sqlite"))
        )
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._dir.cleanup()

    def send(self, data=None, text=None, request_id="1", context_id=None, ip="203.0.113.10"):
        part = {"kind": "data", "data": data} if data is not None else {"kind": "text", "text": text}
        message = {"role": "user", "messageId": "m-" + str(request_id), "parts": [part]}
        if context_id:
            message["contextId"] = context_id
        return self.client.post(
            "/",
            headers={"X-Forwarded-For": ip, "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": request_id, "method": "message/send", "params": {"message": message}},
        )

    def test_structured_purchase_does_not_call_the_model(self):
        class Boom:
            async def ainvoke(self, *args, **kwargs):
                raise AssertionError("the model was called")

        self.client.__exit__(None, None, None)
        self.client = TestClient(
            app.create_app(
                database_url="sqlite:///" + str(Path(self._dir.name) / "boom.sqlite"),
                graph=Boom(),
            )
        )
        self.client.__enter__()
        registered = self.send(
            {
                "action": "register_customer",
                "name": "Grace Hopper",
                "email": "grace@example.com",
                "billing": billing(),
            },
            request_id="reg",
            context_id="ctx-buy",
        )
        body = registered.json()["result"]
        self.assertEqual(body["status"]["state"], "completed")
        customer_id = _data(body)["customer"]["customer_id"]
        created = self.send(
            {"action": "create_cart", "customer_id": customer_id},
            request_id="cart",
            context_id="ctx-buy",
        )
        self.assertEqual(created.json()["result"]["status"]["state"], "completed")
        added = self.send(
            {"action": "add_to_cart", "rank": 1, "quantity": 1},
            request_id="add",
            context_id="ctx-buy",
        )
        self.assertEqual(_data(added.json()["result"])["cart"]["items"][0]["title"], "Theo of Golden")
        checked = self.send({"action": "checkout"}, request_id="out", context_id="ctx-buy")
        order_id = _data(checked.json()["result"])["order"]["order_id"]
        self.assertEqual(_data(checked.json()["result"])["order"]["total_usd"], "26.60")
        missing = self.send({"action": "pay", "order_id": order_id}, request_id="need")
        self.assertEqual(missing.json()["result"]["status"]["state"], "input-required")
        paid = self.send(
            {"action": "pay", "order_id": order_id, "card_number": PAN, "exp_month": 12, "exp_year": 2034, "cvc": "123"},
            request_id="pay",
        )
        result = paid.json()["result"]
        self.assertEqual(result["status"]["state"], "completed")
        payload = _data(result)
        self.assertEqual(payload["notification"]["status"], NOT_CONFIGURED)
        self.assertNotIn(PAN, json.dumps(payload))
        kinds = [part["kind"] for part in result["artifacts"][0]["parts"]]
        self.assertIn("data", kinds)
        self.assertIn("file", kinds)
        xml_url = payload["invoice"]["xml_url"]
        parsed = urlparse(xml_url)
        downloaded = self.client.get(parsed.path + "?" + parsed.query)
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn(b"urn:cen.eu:en16931:2017", downloaded.content)
        blocked = self.client.get(parsed.path + "?token=wrong")
        self.assertEqual(blocked.status_code, 404)

    def test_mcp_lists_bookstore_tools(self):
        response = self.client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        names = {tool["name"] for tool in body["result"]["tools"]}
        self.assertTrue({spec["id"] for spec in ACTIONS}.issubset(names))
        called = self.client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_books", "arguments": {}},
            },
        )
        self.assertEqual(called.status_code, 200, called.text)
        listed = json.dumps(called.json())
        self.assertIn("Theo of Golden", listed)
        self.assertIn("The Housemaid", listed)

    def test_action_limit_is_separate_from_the_llm_limit(self):
        self.client.__exit__(None, None, None)
        self.client = TestClient(
            app.create_app(
                quota=MessageQuota(0, 5),
                action_quota=MessageQuota(1, 60),
                database_url="sqlite:///" + str(Path(self._dir.name) / "limits.sqlite"),
            )
        )
        self.client.__enter__()
        card = self.client.get("/.well-known/agent-card.json")
        health = self.client.get("/health")
        self.assertEqual(card.status_code, 200)
        self.assertEqual(health.status_code, 200)
        first = self.send({"action": "list_books"}, request_id="a1")
        second = self.send({"action": "list_books"}, request_id="a2")
        text = self.send(text="Hello", request_id="llm")
        self.assertEqual(first.json()["result"]["status"]["state"], "completed")
        self.assertEqual(second.json()["error"]["message"], ACTION_DAILY_MESSAGE)
        from quota import DAILY_MESSAGE

        self.assertEqual(text.json()["error"]["message"], DAILY_MESSAGE)


def _data(task: dict) -> dict:
    for part in task["artifacts"][0]["parts"]:
        if part.get("kind") == "data":
            return part["data"]
    raise AssertionError("no data part")


if __name__ == "__main__":
    unittest.main()
