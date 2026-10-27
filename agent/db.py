"""SQLite by default. DATABASE_URL can point at Postgres."""

import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    line1: Mapped[str] = mapped_column(String(200))
    line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(80))
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Cart(Base):
    __tablename__ = "carts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32))
    context_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CartItem(Base):
    __tablename__ = "cart_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cart_id: Mapped[str] = mapped_column(String(32))
    book_key: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32))
    cart_id: Mapped[str] = mapped_column(String(32))
    context_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    subtotal_cents: Mapped[int] = mapped_column(Integer)
    tax_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32))
    book_key: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)


class Payment(Base):
    """Sandbox or Stripe result. The full card number is never a column."""

    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    sandbox: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    brand: Mapped[str] = mapped_column(String(32))
    last4: Mapped[str] = mapped_column(String(4))
    detail: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True)
    number: Mapped[str] = mapped_column(String(16), unique=True)
    token: Mapped[str] = mapped_column(String(80))
    xml: Mapped[str] = mapped_column(Text)
    pdf: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Stock(Base):
    __tablename__ = "stock"
    book_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)


class Counter(Base):
    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Visit(Base):
    """One request or landing-page view. The raw IP address is never stored."""

    __tablename__ = "visits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(200))
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    ip_hash: Mapped[str] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    referer: Mapped[str] = mapped_column(String(500), default="")
    rpc_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    style: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_card_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    classification: Mapped[str] = mapped_column(String(16))
    classifier_reason: Mapped[str] = mapped_column(String(200))
    quota_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    screen_w: Mapped[int | None] = mapped_column(Integer, nullable=True)
    screen_h: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DailySalt(Base):
    """Salt for the IP hash. A new value is created for each UTC day."""

    __tablename__ = "analytics_salts"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    salt: Mapped[str] = mapped_column(String(64))


def env_text(name: str) -> str:
    """Blank and hyphen placeholders count as unset (Render blueprint prompts)."""
    value = os.environ.get(name, "").strip()
    if value.lower() in {"", "-", "none", "null", "unused", "changeme"}:
        return ""
    return value


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def database_url() -> str:
    configured = env_text("DATABASE_URL")
    if configured:
        return normalize_database_url(configured)
    path = os.environ.get("BOOKSTORE_SQLITE_PATH", "").strip()
    if not path:
        path = str(Path_data())
    return "sqlite:///" + path


def Path_data():
    from pathlib import Path

    return Path(__file__).resolve().parent / "data" / "bookstore.sqlite"


def make_engine(url: str | None = None):
    target = normalize_database_url(url) if url else database_url()
    kwargs = {}
    if target.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return create_engine(target, **kwargs), target


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
