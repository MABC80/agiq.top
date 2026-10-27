"""Action names and the JSON schemas published on the agent card."""

import json

TAX_NOTE = "Tax is a simulated 8 percent of the merchandise subtotal. Shipping is a flat 5.00 USD. Neither figure comes from the bestseller list."

ACTIONS = [
    {
        "id": "list_books",
        "name": "List books",
        "summary": "Return the 10-title catalog, simulated stock, and the selling price (the verified list price).",
        "example": '{"action":"list_books"}',
        "utterance": "List the books you sell.",
        "input": {
            "type": "object",
            "required": ["action"],
            "properties": {"action": {"const": "list_books"}},
        },
        "output": {
            "type": "object",
            "required": ["books"],
            "properties": {"books": {"type": "array", "items": {"$ref": "#/book"}}},
        },
        "files": False,
    },
    {
        "id": "get_book",
        "name": "Get book",
        "summary": "Return one catalog record by ISBN-13 or by rank.",
        "example": '{"action":"get_book","isbn13":"9781668236512"}',
        "utterance": "Get the book ranked number 1.",
        "input": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"const": "get_book"},
                "isbn13": {"type": "string"},
                "rank": {"type": "integer"},
            },
        },
        "output": {"type": "object", "required": ["book"], "properties": {"book": {"$ref": "#/book"}}},
        "files": False,
    },
    {
        "id": "search_books",
        "name": "Search books",
        "summary": "Case-insensitive match on title or author.",
        "example": '{"action":"search_books","query":"Weir"}',
        "utterance": "Search for books by Weir.",
        "input": {
            "type": "object",
            "required": ["action", "query"],
            "properties": {"action": {"const": "search_books"}, "query": {"type": "string"}},
        },
        "output": {"type": "object", "required": ["books"], "properties": {"books": {"type": "array"}}},
        "files": False,
    },
    {
        "id": "register_customer",
        "name": "Register customer",
        "summary": "Create a customer. Email must contain a name, a domain, and a dot. tax_id is optional and is not checked against a registry.",
        "example": '{"action":"register_customer","name":"Ada Lovelace","email":"ada@example.com","billing":{"line1":"1 Analytical Engine","city":"London","postal_code":"W1","country":"GB"},"tax_id":null}',
        "utterance": "Register a customer named Ada Lovelace, email ada@example.com, billing 1 Analytical Engine, London, W1, GB.",
        "input": {
            "type": "object",
            "required": ["action", "name", "email", "billing"],
            "properties": {
                "action": {"const": "register_customer"},
                "name": {"type": "string"},
                "email": {"type": "string"},
                "tax_id": {"type": ["string", "null"]},
                "billing": {
                    "type": "object",
                    "required": ["line1", "city", "postal_code", "country"],
                    "properties": {
                        "line1": {"type": "string"},
                        "line2": {"type": ["string", "null"]},
                        "city": {"type": "string"},
                        "region": {"type": ["string", "null"]},
                        "postal_code": {"type": "string"},
                        "country": {"type": "string"},
                    },
                },
            },
        },
        "output": {"type": "object", "required": ["customer"], "properties": {"customer": {"type": "object"}}},
        "files": False,
    },
    {
        "id": "get_customer",
        "name": "Get customer",
        "summary": "Look up a customer by customer_id or email.",
        "example": '{"action":"get_customer","email":"ada@example.com"}',
        "utterance": "Look up the customer ada@example.com.",
        "input": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"const": "get_customer"},
                "customer_id": {"type": "string"},
                "email": {"type": "string"},
            },
        },
        "output": {"type": "object", "required": ["customer"], "properties": {"customer": {"type": "object"}}},
        "files": False,
    },
    {
        "id": "create_cart",
        "name": "Create cart",
        "summary": "Open a cart for a customer. The A2A contextId is stored when the caller omits context_id.",
        "example": '{"action":"create_cart","customer_id":"CUSTOMER_ID"}',
        "utterance": "Create a cart for that customer.",
        "input": {
            "type": "object",
            "required": ["action", "customer_id"],
            "properties": {
                "action": {"const": "create_cart"},
                "customer_id": {"type": "string"},
                "context_id": {"type": "string"},
            },
        },
        "output": {"type": "object", "required": ["cart"], "properties": {"cart": {"type": "object"}}},
        "files": False,
    },
    {
        "id": "add_to_cart",
        "name": "Add to cart",
        "summary": "Add a priced title. A title with a null list price is not for sale. cart_id may be omitted when the A2A contextId already has an open cart.",
        "example": '{"action":"add_to_cart","cart_id":"CART_ID","isbn13":"9781668236512","quantity":1}',
        "utterance": "Add one copy of Theo of Golden to the cart.",
        "input": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"const": "add_to_cart"},
                "cart_id": {"type": "string"},
                "context_id": {"type": "string"},
                "isbn13": {"type": "string"},
                "rank": {"type": "integer"},
                "quantity": {"type": "integer"},
            },
        },
        "output": {"type": "object", "required": ["cart"], "properties": {"cart": {"type": "object"}}},
        "files": False,
    },
    {
        "id": "view_cart",
        "name": "View cart",
        "summary": "Return the open cart and line totals.",
        "example": '{"action":"view_cart","cart_id":"CART_ID"}',
        "utterance": "Show the cart.",
        "input": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"const": "view_cart"},
                "cart_id": {"type": "string"},
                "context_id": {"type": "string"},
            },
        },
        "output": {"type": "object", "required": ["cart"], "properties": {"cart": {"type": "object"}}},
        "files": False,
    },
    {
        "id": "checkout",
        "name": "Checkout",
        "summary": "Turn the cart into an order awaiting sandbox payment. " + TAX_NOTE + " Simulated stock is reserved.",
        "example": '{"action":"checkout","cart_id":"CART_ID"}',
        "utterance": "Check out the cart.",
        "input": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"const": "checkout"},
                "cart_id": {"type": "string"},
                "context_id": {"type": "string"},
            },
        },
        "output": {"type": "object", "required": ["order"], "properties": {"order": {"type": "object"}}},
        "files": False,
    },
    {
        "id": "pay",
        "name": "Pay (sandbox)",
        "summary": (
            "SANDBOX payment simulator, not a live charge. "
            "4242424242424242 succeeds (Visa). 4000000000000002 declines. "
            "The full card number is never stored or returned; only brand and last4. "
            "Set STRIPE_SECRET_KEY to send the same call to Stripe test mode instead. "
            "A successful sandbox charge creates the invoice and attempts the confirmation email."
        ),
        "example": '{"action":"pay","order_id":"ORDER_ID","card_number":"4242424242424242","exp_month":12,"exp_year":2034,"cvc":"123"}',
        "utterance": "Pay the order with the sandbox card 4242424242424242.",
        "input": {
            "type": "object",
            "required": ["action", "order_id", "card_number"],
            "properties": {
                "action": {"const": "pay"},
                "order_id": {"type": "string"},
                "card_number": {"type": "string"},
                "exp_month": {"type": "integer"},
                "exp_year": {"type": "integer"},
                "cvc": {"type": "string"},
            },
        },
        "output": {
            "type": "object",
            "required": ["order", "payment"],
            "properties": {
                "order": {"type": "object"},
                "payment": {"type": "object"},
                "invoice": {"type": "object"},
                "notification": {"type": "object"},
            },
        },
        "files": True,
    },
    {
        "id": "generate_invoice",
        "name": "Generate invoice",
        "summary": "Build a sequential UBL 2.1 / EN 16931-style XML invoice and a PDF for a paid order. The same invoice is returned if it already exists. Download URLs require an unguessable token.",
        "example": '{"action":"generate_invoice","order_id":"ORDER_ID"}',
        "utterance": "Generate the invoice for the order.",
        "input": {
            "type": "object",
            "required": ["action", "order_id"],
            "properties": {"action": {"const": "generate_invoice"}, "order_id": {"type": "string"}},
        },
        "output": {"type": "object", "required": ["invoice"], "properties": {"invoice": {"type": "object"}}},
        "files": True,
    },
    {
        "id": "get_order_status",
        "name": "Get order status",
        "summary": "Return the order, payment (last4 and brand only), invoice links, and email status.",
        "example": '{"action":"get_order_status","order_id":"ORDER_ID"}',
        "utterance": "What is the status of the order?",
        "input": {
            "type": "object",
            "required": ["action", "order_id"],
            "properties": {"action": {"const": "get_order_status"}, "order_id": {"type": "string"}},
        },
        "output": {"type": "object", "required": ["order"], "properties": {"order": {"type": "object"}}},
        "files": True,
    },
    {
        "id": "notify_order",
        "name": "Email order",
        "summary": "Send the order confirmation and the invoice to the customer. If neither Resend nor SMTP is configured, the action still succeeds and the status is exactly \"not_sent: email provider not configured\". Pay already attempts this once.",
        "example": '{"action":"notify_order","order_id":"ORDER_ID"}',
        "utterance": "Email the invoice to the customer.",
        "input": {
            "type": "object",
            "required": ["action", "order_id"],
            "properties": {"action": {"const": "notify_order"}, "order_id": {"type": "string"}},
        },
        "output": {
            "type": "object",
            "required": ["notification"],
            "properties": {"notification": {"type": "object"}},
        },
        "files": False,
    },
]


def skill_description(spec: dict) -> str:
    return (
        spec["summary"]
        + "\n\nInput JSON schema:\n"
        + json.dumps(spec["input"])
        + "\n\nOutput JSON schema:\n"
        + json.dumps(spec["output"])
        + "\n\nSend this as an A2A DataPart for a deterministic result that does not call a model. "
        + "A TextPart is handled by the language model, which calls the same tool. "
        + "Every result also includes state and message."
    )
