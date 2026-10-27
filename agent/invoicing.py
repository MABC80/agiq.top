"""UBL 2.1 / EN 16931-style XML and a one-page PDF. Seller details are a sandbox placeholder."""

import xml.etree.ElementTree as ET
from datetime import date

INV = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

ET.register_namespace("", INV)
ET.register_namespace("cac", CAC)
ET.register_namespace("cbc", CBC)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _cbc(parent, name: str, text: str, **attributes) -> ET.Element:
    node = ET.SubElement(parent, f"{{{CBC}}}{name}")
    node.text = text
    for key, value in attributes.items():
        node.set(key, value)
    return node


def _cac(parent, name: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{CAC}}}{name}")


def _party(parent, name: str, street: str, city: str, postal: str, country: str, tax_id: str | None) -> None:
    party = _cac(parent, "Party")
    party_name = _cac(party, "PartyName")
    _cbc(party_name, "Name", name)
    address = _cac(party, "PostalAddress")
    _cbc(address, "StreetName", street)
    _cbc(address, "CityName", city)
    _cbc(address, "PostalZone", postal)
    country_node = _cac(address, "Country")
    _cbc(country_node, "IdentificationCode", country)
    if tax_id:
        tax = _cac(party, "PartyTaxScheme")
        _cbc(tax, "CompanyID", tax_id)
        scheme = _cac(tax, "TaxScheme")
        _cbc(scheme, "ID", "VAT")


def build_invoice_xml(
    *,
    number: str,
    issue_date: date,
    customer: dict,
    lines: list[dict],
    subtotal_cents: int,
    tax_cents: int,
    shipping_cents: int,
    total_cents: int,
    tax_percent: str,
) -> bytes:
    root = ET.Element(f"{{{INV}}}Invoice")
    _cbc(root, "CustomizationID", "urn:cen.eu:en16931:2017")
    _cbc(root, "ProfileID", "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0")
    _cbc(root, "ID", number)
    _cbc(root, "IssueDate", issue_date.isoformat())
    _cbc(root, "InvoiceTypeCode", "380")
    _cbc(root, "DocumentCurrencyCode", "USD")
    _cbc(root, "Note", "Sandbox invoice from AGIQ Books. Not a filed tax document.")

    supplier = _cac(root, "AccountingSupplierParty")
    _party(
        supplier,
        "AGIQ Books",
        "Sandbox",
        "Online",
        "00000",
        "US",
        None,
    )
    customer_party = _cac(root, "AccountingCustomerParty")
    _party(
        customer_party,
        customer["name"],
        customer["line1"],
        customer["city"],
        customer["postal_code"],
        _country_code(customer["country"]),
        customer.get("tax_id"),
    )

    tax_total = _cac(root, "TaxTotal")
    _cbc(tax_total, "TaxAmount", money(tax_cents), currencyID="USD")
    subtotal = _cac(tax_total, "TaxSubtotal")
    _cbc(subtotal, "TaxableAmount", money(subtotal_cents), currencyID="USD")
    _cbc(subtotal, "TaxAmount", money(tax_cents), currencyID="USD")
    category = _cac(subtotal, "TaxCategory")
    _cbc(category, "ID", "S")
    _cbc(category, "Percent", tax_percent)
    scheme = _cac(category, "TaxScheme")
    _cbc(scheme, "ID", "VAT")

    legal = _cac(root, "LegalMonetaryTotal")
    tax_exclusive = subtotal_cents + shipping_cents
    _cbc(legal, "LineExtensionAmount", money(tax_exclusive), currencyID="USD")
    _cbc(legal, "TaxExclusiveAmount", money(tax_exclusive), currencyID="USD")
    _cbc(legal, "TaxInclusiveAmount", money(total_cents), currencyID="USD")
    _cbc(legal, "PayableAmount", money(total_cents), currencyID="USD")

    for index, line in enumerate(lines, start=1):
        _line(root, index, line, taxed=True, tax_percent=tax_percent)
    if shipping_cents:
        _line(
            root,
            len(lines) + 1,
            {
                "title": "Shipping",
                "quantity": 1,
                "unit_price_cents": shipping_cents,
                "book_key": "shipping",
            },
            taxed=False,
            tax_percent="0",
        )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _line(root, index: int, line: dict, taxed: bool, tax_percent: str) -> None:
    node = _cac(root, "InvoiceLine")
    _cbc(node, "ID", str(index))
    _cbc(node, "InvoicedQuantity", str(line["quantity"]), unitCode="C62")
    extension = line["unit_price_cents"] * line["quantity"]
    _cbc(node, "LineExtensionAmount", money(extension), currencyID="USD")
    item = _cac(node, "Item")
    _cbc(item, "Name", line["title"])
    tax = _cac(item, "ClassifiedTaxCategory")
    _cbc(tax, "ID", "S" if taxed else "Z")
    _cbc(tax, "Percent", tax_percent if taxed else "0")
    scheme = _cac(tax, "TaxScheme")
    _cbc(scheme, "ID", "VAT")
    price = _cac(node, "Price")
    _cbc(price, "PriceAmount", money(line["unit_price_cents"]), currencyID="USD")


def _country_code(country: str) -> str:
    text = (country or "").strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    lowered = text.lower()
    if lowered in {"united states", "united states of america", "usa"}:
        return "US"
    if lowered in {"spain", "españa"}:
        return "ES"
    return "US"


def build_invoice_pdf(lines_of_text: list[str]) -> bytes:
    """A small single-page PDF. The XML is the electronic invoice."""
    commands = ["BT", "/F1 11 Tf", "50 760 Td"]
    for index, line in enumerate(lines_of_text):
        if index:
            commands.append("0 -16 Td")
        safe = line.encode("latin-1", errors="replace").decode("latin-1")
        safe = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"({safe}) Tj")
    commands.append("ET")
    stream = ("\n".join(commands) + "\n").encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)
