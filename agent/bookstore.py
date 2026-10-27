"""Bookstore service shared by A2A, LangChain tools, and MCP."""

import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from catalog import SIMULATED_STOCK, book_key, load_catalog, normalize_isbn
from db import (
    Cart,
    CartItem,
    Counter,
    Customer,
    Invoice,
    Notification,
    Order,
    OrderItem,
    Payment,
    Stock,
    make_engine,
    make_session_factory,
)
from emailer import NOT_CONFIGURED, email_provider_from_env
from invoicing import build_invoice_pdf, build_invoice_xml, money
from payments import PaymentProvider, SandboxPaymentProvider, digits_only, payment_provider_from_env

log = logging.getLogger("agiq.bookstore")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TAX_RATE = Decimal("0.08")
SHIPPING_CENTS = 500
PAN_LENGTH = range(13, 20)


class StoreError(Exception):
    def __init__(self, message: str, state: str = "failed", data: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.state = state
        self.data = data or {}


@dataclass
class Outcome:
    state: str
    message: str
    data: dict
    files: list[tuple[str, bytes, str]] = field(default_factory=list)


class Bookstore:
    def __init__(
        self,
        database_url: str | None = None,
        catalog: dict | None = None,
        payments: PaymentProvider | None = None,
        email="unset",
        public_base: str = "https://agent.agiq.top",
    ) -> None:
        self.catalog = catalog or load_catalog()
        self.books = list(self.catalog["books"])
        self.by_key = {book_key(book): book for book in self.books}
        self.by_rank = {book["rank"]: book for book in self.books}
        self.payments = payments if payments is not None else payment_provider_from_env()
        if email == "unset":
            self.email = email_provider_from_env()
        else:
            self.email = email
        self.public_base = public_base.rstrip("/")
        self._lock = threading.Lock()
        self.engine, self.database_url = make_engine(database_url)
        self.Session = make_session_factory(self.engine)
        from db import Base

        Base.metadata.create_all(self.engine)
        self._seed_stock()

    def _seed_stock(self) -> None:
        with self.Session() as session:
            for book in self.books:
                key = book_key(book)
                if session.get(Stock, key) is None:
                    session.add(Stock(book_key=key, quantity=SIMULATED_STOCK))
            session.commit()

    def handle(self, payload: dict, context_id: str | None = None) -> Outcome:
        if not isinstance(payload, dict):
            return Outcome("input-required", "Send a JSON object with an action field.", {})
        action = payload.get("action")
        if not isinstance(action, str) or not action.strip():
            return Outcome(
                "input-required",
                "Send a DataPart whose data includes action.",
                {},
            )
        body = dict(payload)
        if context_id and not body.get("context_id"):
            body["context_id"] = context_id
        try:
            with self._lock:
                return self._dispatch(action, body)
        except StoreError as exc:
            return Outcome(exc.state, exc.message, exc.data)
        except Exception:
            log.exception("bookstore action failed")
            return Outcome("failed", "The bookstore could not complete that action.", {})

    def _dispatch(self, action: str, payload: dict) -> Outcome:
        method = {
            "list_books": self._list_books,
            "get_book": self._get_book,
            "search_books": self._search_books,
            "register_customer": self._register_customer,
            "get_customer": self._get_customer,
            "create_cart": self._create_cart,
            "add_to_cart": self._add_to_cart,
            "view_cart": self._view_cart,
            "checkout": self._checkout,
            "pay": self._pay,
            "generate_invoice": self._generate_invoice,
            "get_order_status": self._get_order_status,
            "notify_order": self._notify_order,
        }.get(action)
        if method is None:
            return Outcome("failed", f"Unknown action: {action}.", {})
        return method(payload)

    def _list_books(self, payload: dict) -> Outcome:
        del payload
        books = [self._public_book(book) for book in self.books]
        return Outcome("completed", f"Listed {len(books)} books.", {"books": books})

    def _get_book(self, payload: dict) -> Outcome:
        book = self._resolve_book(payload, required=True)
        return Outcome("completed", f"Book {book['rank']}: {book['title']}.", {"book": self._public_book(book)})

    def _search_books(self, payload: dict) -> Outcome:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise StoreError("Send query text to search titles and authors.", "input-required")
        needle = query.casefold()
        found = [
            self._public_book(book)
            for book in self.books
            if needle in book["title"].casefold() or any(needle in author.casefold() for author in book["authors"])
        ]
        return Outcome("completed", f"Found {len(found)} books.", {"books": found, "query": query})

    def _register_customer(self, payload: dict) -> Outcome:
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip()
        billing = self._billing(payload)
        missing = []
        if not name:
            missing.append("name")
        if not email:
            missing.append("email")
        missing.extend(field for field in ("line1", "city", "postal_code", "country") if not billing.get(field))
        if missing:
            raise StoreError(
                "Send the missing customer fields: " + ", ".join(missing) + ".",
                "input-required",
                {"missing": missing},
            )
        if not EMAIL_RE.match(email):
            raise StoreError("The email address is not valid. Use a mailbox like name@example.com.", "input-required")
        tax_id = payload.get("tax_id")
        tax_id = str(tax_id).strip() if tax_id else None
        with self.Session() as session:
            existing = session.scalar(select(Customer).where(Customer.email == email))
            if existing is not None:
                raise StoreError("That email is already registered.", "failed", {"customer": self._customer_dict(existing)})
            customer = Customer(
                id=secrets.token_hex(8),
                name=name,
                email=email,
                line1=billing["line1"],
                line2=billing.get("line2"),
                city=billing["city"],
                region=billing.get("region"),
                postal_code=billing["postal_code"],
                country=billing["country"],
                tax_id=tax_id,
            )
            session.add(customer)
            session.commit()
            data = self._customer_dict(customer)
        return Outcome("completed", f"Registered customer {data['customer_id']}.", {"customer": data})

    def _get_customer(self, payload: dict) -> Outcome:
        customer_id = str(payload.get("customer_id") or "").strip()
        email = str(payload.get("email") or "").strip()
        if not customer_id and not email:
            raise StoreError("Send customer_id or email.", "input-required")
        with self.Session() as session:
            if customer_id:
                customer = session.get(Customer, customer_id)
            else:
                customer = session.scalar(select(Customer).where(Customer.email == email))
            if customer is None:
                raise StoreError("No customer matches that id or email.")
            data = self._customer_dict(customer)
        return Outcome("completed", f"Customer {data['customer_id']}.", {"customer": data})

    def _create_cart(self, payload: dict) -> Outcome:
        customer_id = str(payload.get("customer_id") or "").strip()
        if not customer_id:
            raise StoreError("Send customer_id to open a cart.", "input-required")
        with self.Session() as session:
            if session.get(Customer, customer_id) is None:
                raise StoreError("No customer has that id.")
            cart = Cart(
                id=secrets.token_hex(8),
                customer_id=customer_id,
                context_id=payload.get("context_id"),
                status="open",
            )
            session.add(cart)
            session.commit()
            data = self._cart_dict(session, cart)
        return Outcome("completed", f"Created cart {data['cart_id']}.", {"cart": data})

    def _add_to_cart(self, payload: dict) -> Outcome:
        quantity = payload.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or isinstance(quantity, str):
            if isinstance(quantity, str) and quantity.isdigit():
                quantity = int(quantity)
            else:
                raise StoreError("quantity must be a whole number from 1 to 20.", "input-required")
        if not 1 <= quantity <= 20:
            raise StoreError("quantity must be a whole number from 1 to 20.", "input-required")
        book = self._resolve_book(payload, required=True)
        if book.get("list_price_usd") is None:
            raise StoreError("This title has no verified list price, so it is not for sale.")
        key = book_key(book)
        with self.Session() as session:
            cart = self._open_cart(session, payload)
            stock = session.get(Stock, key)
            item = session.scalar(
                select(CartItem).where(CartItem.cart_id == cart.id, CartItem.book_key == key)
            )
            already = item.quantity if item is not None else 0
            if stock is None or stock.quantity < already + quantity:
                raise StoreError(f"Not enough simulated stock for {book['title']}.")
            if item is None:
                session.add(CartItem(cart_id=cart.id, book_key=key, quantity=quantity))
            else:
                item.quantity = already + quantity
            session.commit()
            data = self._cart_dict(session, cart)
        return Outcome("completed", f"Added to cart {data['cart_id']}.", {"cart": data})

    def _view_cart(self, payload: dict) -> Outcome:
        with self.Session() as session:
            cart = self._open_cart(session, payload)
            data = self._cart_dict(session, cart)
        return Outcome("completed", f"Cart {data['cart_id']}.", {"cart": data})

    def _checkout(self, payload: dict) -> Outcome:
        with self.Session() as session:
            cart = self._open_cart(session, payload)
            items = session.scalars(select(CartItem).where(CartItem.cart_id == cart.id)).all()
            if not items:
                raise StoreError("The cart is empty. Add a book before checkout.", "input-required")
            priced = []
            subtotal = 0
            for item in items:
                book = self.by_key[item.book_key]
                price = self._cents(book["list_price_usd"])
                stock = session.get(Stock, item.book_key)
                if stock is None or stock.quantity < item.quantity:
                    raise StoreError(f"Not enough simulated stock for {book['title']}.")
                priced.append((item, book, price))
                subtotal += price * item.quantity
            tax = int((Decimal(subtotal) * TAX_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            shipping = SHIPPING_CENTS
            total = subtotal + tax + shipping
            order = Order(
                id=secrets.token_hex(8),
                customer_id=cart.customer_id,
                cart_id=cart.id,
                context_id=cart.context_id,
                status="awaiting_payment",
                subtotal_cents=subtotal,
                tax_cents=tax,
                shipping_cents=shipping,
                total_cents=total,
            )
            session.add(order)
            for item, book, price in priced:
                session.add(
                    OrderItem(
                        order_id=order.id,
                        book_key=item.book_key,
                        title=book["title"],
                        unit_price_cents=price,
                        quantity=item.quantity,
                    )
                )
                stock = session.get(Stock, item.book_key)
                stock.quantity -= item.quantity
            cart.status = "checked_out"
            session.commit()
            data = self._order_dict(session, order)
        return Outcome(
            "completed",
            f"Created order {data['order_id']}. Total {data['total_usd']} USD. Sandbox payment is still required.",
            {"order": data},
        )

    def _pay(self, payload: dict) -> Outcome:
        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            raise StoreError("Send order_id to pay.", "input-required")
        card_number = payload.get("card_number")
        if card_number is None or not str(card_number).strip():
            raise StoreError(
                "Send card_number. Sandbox test cards: 4242424242424242 succeeds and 4000000000000002 declines.",
                "input-required",
            )
        digits = digits_only(str(card_number))
        if len(digits) not in PAN_LENGTH:
            raise StoreError("The card number must be 13 to 19 digits.", "input-required")
        exp_month = payload.get("exp_month")
        exp_year = payload.get("exp_year")
        cvc = payload.get("cvc")
        if not isinstance(self.payments, SandboxPaymentProvider):
            missing = [name for name, value in (("exp_month", exp_month), ("exp_year", exp_year), ("cvc", cvc)) if not value]
            if missing:
                raise StoreError("Stripe test mode needs " + ", ".join(missing) + ".", "input-required")
        with self.Session() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise StoreError("No order has that id.")
            if order.status != "awaiting_payment":
                raise StoreError(f"Order {order_id} is {order.status} and cannot be paid again.")
            result = self.payments.charge(
                amount_cents=order.total_cents,
                currency=order.currency,
                card_number=digits,
                exp_month=_optional_int(exp_month),
                exp_year=_optional_int(exp_year),
                cvc=None if cvc is None else str(cvc),
            )
            del digits
            session.add(
                Payment(
                    order_id=order.id,
                    provider=result.provider,
                    sandbox=result.sandbox,
                    status=result.public()["status"],
                    brand=result.brand,
                    last4=result.last4,
                    detail=result.detail,
                )
            )
            if not result.ok:
                order.status = "payment_declined"
                self._restore_stock(session, order.id)
                session.commit()
                data = {
                    "order": self._order_dict(session, order),
                    "payment": result.public(),
                }
                return Outcome(
                    "failed",
                    f"Sandbox payment declined. {result.brand} ending {result.last4} was not stored.",
                    data,
                )
            order.status = "paid"
            session.commit()
            invoice = self._ensure_invoice(session, order)
            notification = self._send_notification(session, order, invoice)
            session.commit()
            data = {
                "order": self._order_dict(session, order),
                "payment": result.public(),
                "invoice": self._invoice_public(invoice),
                "notification": {"status": notification.status},
            }
            files = self._invoice_files(invoice)
            number = invoice.number
            email_status = notification.status
        label = "Sandbox payment succeeded" if result.sandbox else "Payment succeeded"
        return Outcome(
            "completed",
            f"{label}. {result.brand} ending {result.last4} was not stored. Invoice {number}. Email: {email_status}",
            data,
            files,
        )

    def _generate_invoice(self, payload: dict) -> Outcome:
        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            raise StoreError("Send order_id to generate an invoice.", "input-required")
        with self.Session() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise StoreError("No order has that id.")
            if order.status != "paid":
                raise StoreError("Pay for the order before generating an invoice.")
            invoice = self._ensure_invoice(session, order)
            session.commit()
            number = invoice.number
            data = {"invoice": self._invoice_public(invoice), "order_id": order.id}
            files = self._invoice_files(invoice)
        return Outcome("completed", f"Invoice {number}.", data, files)

    def _get_order_status(self, payload: dict) -> Outcome:
        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            raise StoreError("Send order_id.", "input-required")
        with self.Session() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise StoreError("No order has that id.")
            data = {"order": self._order_dict(session, order)}
            invoice = session.scalar(select(Invoice).where(Invoice.order_id == order.id))
            files = self._invoice_files(invoice) if invoice is not None else []
        return Outcome("completed", f"Order {order_id} is {data['order']['status']}.", data, files)

    def _notify_order(self, payload: dict) -> Outcome:
        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            raise StoreError("Send order_id to email the customer.", "input-required")
        with self.Session() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise StoreError("No order has that id.")
            if order.status != "paid":
                raise StoreError("Pay for the order before sending the invoice email.")
            invoice = self._ensure_invoice(session, order)
            notification = self._send_notification(session, order, invoice)
            session.commit()
            status = notification.status
        return Outcome("completed", f"Notification status: {status}", {"notification": {"status": status}, "order_id": order_id})

    def invoice_download(self, invoice_id: str, kind: str, token: str) -> tuple[bytes, str, str] | None:
        if kind not in {"xml", "pdf"} or not token:
            return None
        with self.Session() as session:
            invoice = session.get(Invoice, invoice_id)
            if invoice is None or not secrets.compare_digest(invoice.token, token):
                return None
            if kind == "xml":
                return invoice.xml.encode("utf-8"), "application/xml", f"{invoice.number}.xml"
            return invoice.pdf, "application/pdf", f"{invoice.number}.pdf"

    def _public_book(self, book: dict) -> dict:
        key = book_key(book)
        with self.Session() as session:
            stock = session.get(Stock, key)
            quantity = stock.quantity if stock is not None else 0
        price = book.get("list_price_usd")
        return {
            "rank": book["rank"],
            "title": book["title"],
            "authors": list(book["authors"]),
            "isbn13": book.get("isbn13"),
            "publisher": book.get("publisher"),
            "publication_date": book.get("publication_date"),
            "pages": book.get("pages"),
            "format": book.get("format"),
            "language": book.get("language"),
            "genre": book.get("genre"),
            "synopsis": book.get("synopsis"),
            "list_price_usd": price,
            "selling_price_usd": price,
            "for_sale": price is not None,
            "stock": quantity,
            "stock_note": "simulated",
            "source": book.get("source"),
        }

    def _resolve_book(self, payload: dict, required: bool) -> dict:
        isbn = payload.get("isbn13") or payload.get("isbn")
        rank = payload.get("rank")
        if isbn:
            key = normalize_isbn(str(isbn))
            for book in self.books:
                if book.get("isbn13") and normalize_isbn(book["isbn13"]) == key:
                    return book
            raise StoreError("No catalog book has that ISBN-13.")
        if rank is not None and rank != "":
            try:
                number = int(rank)
            except (TypeError, ValueError):
                raise StoreError("rank must be an integer from 1 to 10.", "input-required") from None
            book = self.by_rank.get(number)
            if book is None:
                raise StoreError("No catalog book has that rank.")
            return book
        if required:
            raise StoreError("Send isbn13 or rank.", "input-required")
        raise StoreError("Send isbn13 or rank.", "input-required")

    def _billing(self, payload: dict) -> dict:
        billing = payload.get("billing") if isinstance(payload.get("billing"), dict) else {}
        def pick(name):
            value = billing.get(name, payload.get(name))
            if value is None:
                return None
            text = str(value).strip()
            return text or None
        return {
            "line1": pick("line1"),
            "line2": pick("line2"),
            "city": pick("city"),
            "region": pick("region"),
            "postal_code": pick("postal_code"),
            "country": pick("country"),
        }

    def _open_cart(self, session, payload: dict) -> Cart:
        cart_id = str(payload.get("cart_id") or "").strip()
        if cart_id:
            cart = session.get(Cart, cart_id)
            if cart is None:
                raise StoreError("No cart has that id.")
            if cart.status != "open" and payload.get("action") != "view_cart":
                raise StoreError(f"Cart {cart_id} is {cart.status}.")
            return cart
        context_id = payload.get("context_id")
        if context_id:
            cart = session.scalar(
                select(Cart)
                .where(Cart.context_id == context_id, Cart.status == "open")
                .order_by(Cart.created_at.desc())
            )
            if cart is not None:
                return cart
        raise StoreError("Send cart_id, or continue in a context that already has an open cart.", "input-required")

    def _cart_dict(self, session, cart: Cart) -> dict:
        items = []
        subtotal = 0
        for item in session.scalars(select(CartItem).where(CartItem.cart_id == cart.id)).all():
            book = self.by_key[item.book_key]
            price = book.get("list_price_usd")
            cents = self._cents(price) if price else 0
            line_total = cents * item.quantity
            subtotal += line_total
            items.append(
                {
                    "isbn13": book.get("isbn13"),
                    "rank": book["rank"],
                    "title": book["title"],
                    "quantity": item.quantity,
                    "unit_price_usd": price,
                    "line_total_usd": money(line_total),
                }
            )
        return {
            "cart_id": cart.id,
            "customer_id": cart.customer_id,
            "context_id": cart.context_id,
            "status": cart.status,
            "items": items,
            "subtotal_usd": money(subtotal),
        }

    def _order_dict(self, session, order: Order) -> dict:
        items = [
            {
                "title": item.title,
                "book_key": item.book_key,
                "quantity": item.quantity,
                "unit_price_usd": money(item.unit_price_cents),
                "line_total_usd": money(item.unit_price_cents * item.quantity),
            }
            for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
        ]
        payment = session.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc()))
        invoice = session.scalar(select(Invoice).where(Invoice.order_id == order.id))
        notification = session.scalar(
            select(Notification).where(Notification.order_id == order.id).order_by(Notification.id.desc())
        )
        return {
            "order_id": order.id,
            "customer_id": order.customer_id,
            "cart_id": order.cart_id,
            "context_id": order.context_id,
            "status": order.status,
            "currency": order.currency,
            "items": items,
            "subtotal_usd": money(order.subtotal_cents),
            "tax_usd": money(order.tax_cents),
            "shipping_usd": money(order.shipping_cents),
            "total_usd": money(order.total_cents),
            "tax_note": "Simulated 8 percent of the merchandise subtotal.",
            "shipping_note": "Flat 5.00 USD. Simulated.",
            "payment": None
            if payment is None
            else {
                "provider": payment.provider,
                "sandbox": payment.sandbox,
                "status": payment.status,
                "brand": payment.brand,
                "last4": payment.last4,
                "detail": payment.detail,
            },
            "invoice": None if invoice is None else self._invoice_public(invoice),
            "notification": None if notification is None else {"status": notification.status},
        }

    def _restore_stock(self, session, order_id: str) -> None:
        for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order_id)).all():
            stock = session.get(Stock, item.book_key)
            if stock is not None:
                stock.quantity += item.quantity

    def _ensure_invoice(self, session, order: Order) -> Invoice:
        existing = session.scalar(select(Invoice).where(Invoice.order_id == order.id))
        if existing is not None:
            return existing
        counter = session.get(Counter, "invoice")
        if counter is None:
            counter = Counter(name="invoice", value=0)
            session.add(counter)
        counter.value += 1
        number = f"INV-{counter.value:06d}"
        customer = session.get(Customer, order.customer_id)
        lines = [
            {
                "title": item.title,
                "quantity": item.quantity,
                "unit_price_cents": item.unit_price_cents,
                "book_key": item.book_key,
            }
            for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
        ]
        issued = date.today()
        xml = build_invoice_xml(
            number=number,
            issue_date=issued,
            customer={
                "name": customer.name,
                "line1": customer.line1,
                "city": customer.city,
                "postal_code": customer.postal_code,
                "country": customer.country,
                "tax_id": customer.tax_id,
            },
            lines=lines,
            subtotal_cents=order.subtotal_cents,
            tax_cents=order.tax_cents,
            shipping_cents=order.shipping_cents,
            total_cents=order.total_cents,
            tax_percent="8",
        )
        pdf_lines = [
            "AGIQ Books — sandbox invoice",
            number,
            issued.isoformat(),
            f"Bill to: {customer.name}",
            f"Subtotal {money(order.subtotal_cents)} USD",
            f"Tax {money(order.tax_cents)} USD",
            f"Shipping {money(order.shipping_cents)} USD",
            f"Total {money(order.total_cents)} USD",
            "Electronic invoice is the UBL XML file.",
        ]
        invoice = Invoice(
            id=secrets.token_hex(8),
            order_id=order.id,
            number=number,
            token=secrets.token_urlsafe(32),
            xml=xml.decode("utf-8"),
            pdf=build_invoice_pdf(pdf_lines),
        )
        session.add(invoice)
        session.flush()
        return invoice

    def _send_notification(self, session, order: Order, invoice: Invoice) -> Notification:
        customer = session.get(Customer, order.customer_id)
        if self.email is None:
            log.warning(NOT_CONFIGURED)
            status = NOT_CONFIGURED
        else:
            try:
                self.email.send(
                    to=customer.email,
                    subject=f"AGIQ Books order {order.id}",
                    text=(
                        f"Order {order.id} is paid. Total {money(order.total_cents)} USD. "
                        f"Invoice {invoice.number} is attached. This is a sandbox bookstore."
                    ),
                    attachments=[
                        (f"{invoice.number}.xml", invoice.xml.encode("utf-8"), "application/xml"),
                        (f"{invoice.number}.pdf", invoice.pdf, "application/pdf"),
                    ],
                    reply_to=getattr(self.email, "reply_to", None) or None,
                )
                status = "sent"
            except Exception:
                log.warning("email provider returned an error", exc_info=True)
                status = "not_sent: email provider returned an error"
        note = Notification(order_id=order.id, status=status)
        session.add(note)
        session.flush()
        return note

    def _invoice_public(self, invoice: Invoice) -> dict:
        query = f"token={invoice.token}"
        base = f"{self.public_base}/invoices/{invoice.id}"
        return {
            "invoice_id": invoice.id,
            "invoice_number": invoice.number,
            "xml_url": f"{base}.xml?{query}",
            "pdf_url": f"{base}.pdf?{query}",
        }

    def _invoice_files(self, invoice: Invoice) -> list[tuple[str, bytes, str]]:
        return [
            (f"{invoice.number}.xml", invoice.xml.encode("utf-8"), "application/xml"),
            (f"{invoice.number}.pdf", bytes(invoice.pdf), "application/pdf"),
        ]

    def _customer_dict(self, customer: Customer) -> dict:
        return {
            "customer_id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "tax_id": customer.tax_id,
            "billing": {
                "line1": customer.line1,
                "line2": customer.line2,
                "city": customer.city,
                "region": customer.region,
                "postal_code": customer.postal_code,
                "country": customer.country,
            },
        }

    def _cents(self, price: str) -> int:
        whole, frac = str(price).split(".")
        return int(whole) * 100 + int(frac)


def _optional_int(value):
    if value is None or value == "":
        return None
    return int(value)
