"""Load the fixed bestseller catalog. Unverified fields stay null."""

import json
from pathlib import Path

REQUIRED_FIELDS = (
    "rank",
    "title",
    "authors",
    "isbn13",
    "publisher",
    "publication_date",
    "pages",
    "format",
    "language",
    "genre",
    "synopsis",
    "list_price_usd",
    "source",
)

SIMULATED_STOCK = 20


def catalog_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "books.json"


def load_catalog(path: Path | None = None) -> dict:
    raw = json.loads((path or catalog_path()).read_text(encoding="utf-8"))
    books = raw.get("books")
    if not isinstance(books, list) or len(books) != 10:
        raise ValueError("The catalog must contain exactly 10 books.")
    ranks = [book.get("rank") for book in books]
    if ranks != list(range(1, 11)):
        raise ValueError("Book ranks must be 1 through 10 in order.")
    for book in books:
        missing = [field for field in REQUIRED_FIELDS if field not in book]
        if missing:
            raise ValueError(f"Book {book.get('rank')} is missing {missing}.")
        if not isinstance(book["authors"], list) or not book["authors"]:
            raise ValueError(f"Book {book['rank']} needs at least one author.")
    return raw


def book_key(book: dict) -> str:
    isbn = book.get("isbn13")
    if isbn:
        return str(isbn)
    return f"rank-{book['rank']}"


def normalize_isbn(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def isbn13_is_valid(value: str) -> bool:
    digits = normalize_isbn(value)
    if len(digits) != 13:
        return False
    total = sum(int(ch) * (1 if index % 2 == 0 else 3) for index, ch in enumerate(digits[:-1]))
    check = (10 - (total % 10)) % 10
    return check == int(digits[-1])
