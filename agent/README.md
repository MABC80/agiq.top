# AGIQ Books

Agent-to-agent bookstore. Other agents buy from it over A2A JSON-RPC. There is no human checkout page. The landing page at https://agiq.top only links to the agent card.

The service speaks A2A 0.3 (`message/send`) at the card URL. Natural-language text goes through LangGraph (`create_react_agent`) and `langchain-openai` `gpt-4o-mini`. A `DataPart` whose `data.action` is one of the actions below is handled in process and does not call the model. The same functions are MCP tools at `https://agent.agiq.top/mcp` (streamable HTTP, official Python MCP SDK).

https://agiq.top publishes the same agent card at `/.well-known/agent-card.json` and `/.well-known/agent.json`, plus `/agentfacts.json`. `.nojekyll` keeps GitHub Pages from hiding `.well-known`. `OPENAI_API_KEY` stays in the server environment and is not committed.

## Catalog

Rank source: Publishers Weekly / Circana BookScan, “Print Book Sales Flat in First Half of 2026” (Jim Milliot, July 9, 2026). The table is print units for the period ended **2026-07-04**. It is a first-half list, not a full-year ranking through October.

https://www.publishersweekly.com/pw/by-topic/industry-news/financial-reporting/article/100790-print-book-sales-flat-in-first-half-of-2026.html

The ten titles, in that order: Theo of Golden; Project Hail Mary; The Let Them Theory; The Correspondent; Oh, the Places You'll Go!; Dear Debbie; Dungeon Crawler Carl; Yesteryear; Heated Rivalry; The Housemaid.

Edition fields (ISBN-13, pages, format, list price, publication date, language) are filled only when another page names that edition. Anything that could not be checked is `null` in `agent/data/books.json`. In particular, Oh, the Places You'll Go! and The Housemaid have no verified ISBN or list price, so they are listed and are not for sale. Synopses are short original sentences, not copied reviews. Selling price equals the verified list price. Stock is simulated (20 copies) and is not a BookScan figure.

Each record's `source` object repeats the rank URL, `as_of` date `2026-07-04`, unit count, and any edition URLs.

## Money

Tax is a simulated 8 percent of the merchandise subtotal. Shipping is a flat 5.00 USD. Both are store rules, not facts from the bestseller list. A declined sandbox payment restores the reserved stock. A declined order cannot be paid again; check out a new cart.

## Payments are a sandbox

The built-in provider accepts `4242424242424242` (Visa, success) and declines `4000000000000002`. Any other number is declined. The service stores and returns brand and last4 only. It does not store the full card number.

`PaymentProvider` is the plug-in point. Set `STRIPE_SECRET_KEY` to send `pay` to Stripe PaymentIntents instead of the simulator. Stripe must allow raw card numbers on that account, and `exp_month`, `exp_year`, and `cvc` are required on that path. Responses then say `provider: stripe` and `sandbox: false`. Leave the key unset to keep the sandbox.

## Actions

Send `message/send` with a part `{"kind":"data","data":{...}}`. The task ends `completed`, `input-required` (a field is missing or invalid), or `failed` (declined, unknown id, not for sale). `checkout`, `pay`, `generate_invoice`, and `notify_order` also emit `working` before the final state. Pass `contextId` on later messages. `create_cart` stores it, and `add_to_cart`, `view_cart`, and `checkout` reuse the open cart for that context when `cart_id` is omitted.

Every result is a DataPart plus a short English text message. `pay`, `generate_invoice`, and `get_order_status` also attach the invoice XML and PDF as FileParts when an invoice exists. The same bytes are at `https://agent.agiq.top/invoices/{id}.xml` and `.pdf` with `?token=` an unguessable token. A wrong token is a 404.

Invoice numbers are `INV-000001` upward. The XML is UBL 2.1 shaped for EN 16931 (`CustomizationID` `urn:cen.eu:en16931:2017`). The seller address in the file is a sandbox placeholder, not a registered tax address. The PDF is a one-page rendering of the totals; the XML is the electronic invoice.

A TextPart is the language-model path. The model has these same functions as tools. With `A2A_FAKE_LLM=1` the model echoes `echo: ...` and does not call tools.

### list_books

Input: `{"action":"list_books"}`

Output: `{"state","message","books":[book]}`. A book includes rank, title, authors, isbn13, publisher, publication_date, pages, format, language, genre, synopsis, list_price_usd, selling_price_usd, for_sale, stock, stock_note (`simulated`), and source. Nulls stay null.

### get_book

Example: `{"action":"get_book","isbn13":"9781668236512"}`

Input: `action` plus `isbn13` or `rank`. Output: `{"book": book}`.

### search_books

Example: `{"action":"search_books","query":"Weir"}`

Input: `action`, `query`. Output: `{"books":[book],"query"}`.

### register_customer

Example: `{"action":"register_customer","name":"Ada Lovelace","email":"ada@example.com","billing":{"line1":"1 Analytical Engine","city":"London","postal_code":"W1","country":"GB"},"tax_id":null}`

Input: `name`, `email`, `billing` (`line1`, `city`, `postal_code`, `country`; optional `line2`, `region`), optional `tax_id`. Email must look like `name@example.com`. Output: `{"customer":{...}}`.

### get_customer

Example: `{"action":"get_customer","email":"ada@example.com"}`

Input: `customer_id` or `email`. Output: `{"customer":{...}}`.

### create_cart

Example: `{"action":"create_cart","customer_id":"CUSTOMER_ID"}`

Input: `customer_id`, optional `context_id` (filled from the A2A `contextId` when omitted). Output: `{"cart":{...}}`.

### add_to_cart

Example: `{"action":"add_to_cart","cart_id":"CART_ID","isbn13":"9781668236512","quantity":1}`

Input: `isbn13` or `rank`, optional `quantity` (default 1), and `cart_id` or a context that already has an open cart. Output: `{"cart":{...}}`.

### view_cart

Example: `{"action":"view_cart","cart_id":"CART_ID"}`

Input: `cart_id` or context. Output: `{"cart":{...}}`.

### checkout

Example: `{"action":"checkout","cart_id":"CART_ID"}`

Input: `cart_id` or context. Output: `{"order":{...}}` with `subtotal_usd`, `tax_usd`, `shipping_usd`, `total_usd`, and status `awaiting_payment`.

### pay

Example: `{"action":"pay","order_id":"ORDER_ID","card_number":"4242424242424242","exp_month":12,"exp_year":2034,"cvc":"123"}`

Input: `order_id`, `card_number`. Expiry and `cvc` are optional for the sandbox and required when `STRIPE_SECRET_KEY` is set. Output on success: `order`, `payment` (`provider`, `sandbox`, `status`, `brand`, `last4`, `detail`), `invoice`, `notification`. A decline is task state `failed` and does not include an invoice. The full card number is not in the output.

### generate_invoice

Example: `{"action":"generate_invoice","order_id":"ORDER_ID"}`

Input: `order_id` for a paid order. Output: `{"invoice":{"invoice_id","invoice_number","xml_url","pdf_url"}}`. Calling it again returns the same number.

### get_order_status

Example: `{"action":"get_order_status","order_id":"ORDER_ID"}`

Input: `order_id`. Output: `{"order":{...}}` including payment, invoice, and notification when those exist.

### notify_order

Example: `{"action":"notify_order","order_id":"ORDER_ID"}`

Input: `order_id` for a paid order. Output: `{"notification":{"status"}}`. `pay` already attempts this. If no provider is configured the status is exactly `not_sent: email provider not configured` and that string is written to the log. The order still completes.

The agent card repeats these input and output JSON schemas on each skill.

## Email

`EmailProvider` has two implementations:

- Resend, when both `RESEND_API_KEY` and `EMAIL_FROM` are set.
- SMTP, when `SMTP_HOST` and `EMAIL_FROM` are set. `SMTP_PORT` defaults to 587. `SMTP_USER` and `SMTP_PASSWORD` are used when set.

If neither pair is set, notification status is `not_sent: email provider not configured`. A hyphen, `none`, `null`, `unused`, or `changeme` is treated as unset so a Render prompt can be filled without turning email on. A provider that errors is recorded as `not_sent: email provider returned an error` and does not undo the payment.

## Persistence

SQLAlchemy uses SQLite at `agent/data/bookstore.sqlite` unless `BOOKSTORE_SQLITE_PATH` or `DATABASE_URL` is set. On the Render free plan that disk is ephemeral: customers, carts, orders, invoices, and simulated stock reset on redeploy or restart. The catalog JSON is part of the image and remains.

To keep data, set `DATABASE_URL` to Postgres, for example `postgresql://user:pass@host:5432/agiq` (a `postgres://` URL is accepted too). The app loads it with the `psycopg` driver. Create the database yourself; tables are created on startup. Render Postgres is a separate service from the free web instance.

## Rate limits

Counters are in memory and reset when the process restarts, including a free instance waking up.

- Natural language (`message/send` and `message/stream` with a TextPart): `MAX_DAILY_MESSAGES` (default 30, UTC day) and `MAX_MESSAGES_PER_IP_PER_HOUR` (default 5). The IP is the first `X-Forwarded-For` hop.
- Structured DataPart actions and `POST /mcp`: `MAX_DAILY_ACTIONS` (default 1000) and `MAX_ACTIONS_PER_IP_PER_HOUR` (default 60). These calls do not use the OpenAI key.
- The agent card, `agent.json`, and `/health` are not counted.

Over the cap, JSON-RPC returns HTTP 200 with error `-32029`. A POST that is not JSON is HTTP 429.

## MCP

`https://agent.agiq.top/mcp` is streamable HTTP, stateless, JSON responses. Tool names match the actions. The card lists the endpoint as an extension (`capabilities.extensions`). The action rate limit applies to `POST /mcp`.

## Run locally

Python 3.12 (the blueprint pins `3.12.3`).

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini
export PUBLIC_URL=http://127.0.0.1:8000
uvicorn app:app --host 0.0.0.0 --port 8000
```

Without a key, the protocol tests use a fake model:

```bash
A2A_FAKE_LLM=1 uvicorn app:app --host 127.0.0.1 --port 8000
```

Do not set `A2A_FAKE_LLM` on Render.

```bash
A2A_FAKE_LLM=1 python -m unittest test_limits test_bookstore
```

## curl

Card (both well-known paths return the same JSON):

```bash
curl -s https://agent.agiq.top/.well-known/agent-card.json
```

List the catalog without calling the model:

```bash
curl -s https://agent.agiq.top/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","messageId":"m1","parts":[{"kind":"data","data":{"action":"list_books"}}]}}}'
```

Health: `curl -s https://agent.agiq.top/health`

## Render

`render.yaml` builds `agent/` and runs `uvicorn app:app --host 0.0.0.0 --port $PORT`. Set `OPENAI_API_KEY` for natural language. Email and Stripe variables are `sync: false`; leave them as a hyphen unless you want that provider. `MAX_DAILY_MESSAGES` is `30` and `MAX_DAILY_ACTIONS` is `1000`.
