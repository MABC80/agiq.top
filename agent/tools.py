"""The same bookstore functions for LangChain and for MCP."""

import functools
import json

from langchain_core.tools import tool
from mcp.server.mcpserver import MCPServer

from bookstore import Bookstore


def make_functions(store: Bookstore):
    def call(payload: dict) -> dict:
        outcome = store.handle(payload)
        return {"state": outcome.state, "message": outcome.message, **outcome.data}

    def list_books() -> dict:
        """List the catalog, simulated stock, and selling price for each of the 10 books."""
        return call({"action": "list_books"})

    def get_book(isbn13: str | None = None, rank: int | None = None) -> dict:
        """Get one book by ISBN-13 or rank."""
        return call({"action": "get_book", "isbn13": isbn13, "rank": rank})

    def search_books(query: str) -> dict:
        """Search titles and authors."""
        return call({"action": "search_books", "query": query})

    def register_customer(
        name: str,
        email: str,
        line1: str,
        city: str,
        postal_code: str,
        country: str,
        line2: str | None = None,
        region: str | None = None,
        tax_id: str | None = None,
    ) -> dict:
        """Register a customer. Validate the email. tax_id is optional."""
        return call(
            {
                "action": "register_customer",
                "name": name,
                "email": email,
                "tax_id": tax_id,
                "billing": {
                    "line1": line1,
                    "line2": line2,
                    "city": city,
                    "region": region,
                    "postal_code": postal_code,
                    "country": country,
                },
            }
        )

    def get_customer(customer_id: str | None = None, email: str | None = None) -> dict:
        """Look up a customer by id or email."""
        return call({"action": "get_customer", "customer_id": customer_id, "email": email})

    def create_cart(customer_id: str, context_id: str | None = None) -> dict:
        """Open a cart for a customer."""
        return call({"action": "create_cart", "customer_id": customer_id, "context_id": context_id})

    def add_to_cart(
        isbn13: str | None = None,
        rank: int | None = None,
        quantity: int = 1,
        cart_id: str | None = None,
        context_id: str | None = None,
    ) -> dict:
        """Add a book to a cart. Titles with no verified price are not for sale."""
        return call(
            {
                "action": "add_to_cart",
                "isbn13": isbn13,
                "rank": rank,
                "quantity": quantity,
                "cart_id": cart_id,
                "context_id": context_id,
            }
        )

    def view_cart(cart_id: str | None = None, context_id: str | None = None) -> dict:
        """Show a cart."""
        return call({"action": "view_cart", "cart_id": cart_id, "context_id": context_id})

    def checkout(cart_id: str | None = None, context_id: str | None = None) -> dict:
        """Create an order with merchandise, simulated 8 percent tax, and flat 5 USD shipping."""
        return call({"action": "checkout", "cart_id": cart_id, "context_id": context_id})

    def pay(
        order_id: str,
        card_number: str,
        exp_month: int | None = None,
        exp_year: int | None = None,
        cvc: str | None = None,
    ) -> dict:
        """Sandbox pay. 4242424242424242 succeeds and 4000000000000002 declines. Never store the full card number."""
        return call(
            {
                "action": "pay",
                "order_id": order_id,
                "card_number": card_number,
                "exp_month": exp_month,
                "exp_year": exp_year,
                "cvc": cvc,
            }
        )

    def generate_invoice(order_id: str) -> dict:
        """Create or return the UBL invoice and PDF links for a paid order."""
        return call({"action": "generate_invoice", "order_id": order_id})

    def get_order_status(order_id: str) -> dict:
        """Return order, payment last4, invoice links, and email status."""
        return call({"action": "get_order_status", "order_id": order_id})

    def notify_order(order_id: str) -> dict:
        """Email the order confirmation and invoice. Skip cleanly when no email provider is configured."""
        return call({"action": "notify_order", "order_id": order_id})

    return [
        list_books,
        get_book,
        search_books,
        register_customer,
        get_customer,
        create_cart,
        add_to_cart,
        view_cart,
        checkout,
        pay,
        generate_invoice,
        get_order_status,
        notify_order,
    ]


def langchain_tools(functions):
    wrapped = []
    for fn in functions:
        def text_tool(fn=fn):
            @functools.wraps(fn)
            def inner(*args, **kwargs):
                return json.dumps(fn(*args, **kwargs))

            inner.__annotations__ = dict(fn.__annotations__)
            inner.__annotations__["return"] = str
            return inner

        wrapped.append(tool(text_tool()))
    return wrapped


def build_mcp(functions) -> MCPServer:
    server = MCPServer(
        "AGIQ Books",
        instructions=(
            "Agent-to-agent bookstore. Payments are a sandbox unless Stripe is configured. "
            "Do not invent catalog data. Never repeat a full card number."
        ),
    )
    for fn in functions:
        server.tool()(fn)
    return server
