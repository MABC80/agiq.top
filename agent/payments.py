"""Payment providers. The sandbox never stores a full card number."""

from dataclasses import dataclass

SUCCESS_CARD = "4242424242424242"
DECLINE_CARD = "4000000000000002"


@dataclass
class PaymentResult:
    ok: bool
    provider: str
    sandbox: bool
    brand: str
    last4: str
    detail: str

    def public(self) -> dict:
        return {
            "provider": self.provider,
            "sandbox": self.sandbox,
            "status": "succeeded" if self.ok else "declined",
            "brand": self.brand,
            "last4": self.last4,
            "detail": self.detail,
        }


def digits_only(number: str) -> str:
    return "".join(ch for ch in str(number) if ch.isdigit())


def brand_of(digits: str) -> str:
    if digits.startswith("4"):
        return "visa"
    if digits.startswith("5"):
        return "mastercard"
    if digits.startswith("3"):
        return "amex"
    return "unknown"


class PaymentProvider:
    """Interface a Stripe test-mode provider can implement."""

    def charge(
        self,
        *,
        amount_cents: int,
        currency: str,
        card_number: str,
        exp_month: int | None,
        exp_year: int | None,
        cvc: str | None,
    ) -> PaymentResult:
        raise NotImplementedError


class SandboxPaymentProvider(PaymentProvider):
    """Built-in simulator. 4242424242424242 succeeds. 4000000000000002 declines."""

    def charge(
        self,
        *,
        amount_cents: int,
        currency: str,
        card_number: str,
        exp_month: int | None,
        exp_year: int | None,
        cvc: str | None,
    ) -> PaymentResult:
        del amount_cents, currency, exp_month, exp_year, cvc
        digits = digits_only(card_number)
        brand = brand_of(digits)
        last4 = digits[-4:] if len(digits) >= 4 else ""
        if digits == SUCCESS_CARD:
            return PaymentResult(True, "sandbox", True, brand, last4, "Sandbox payment succeeded.")
        if digits == DECLINE_CARD:
            return PaymentResult(False, "sandbox", True, brand, last4, "Sandbox payment declined.")
        return PaymentResult(
            False,
            "sandbox",
            True,
            brand,
            last4,
            "Sandbox declined the card. Use 4242424242424242 or 4000000000000002.",
        )


class StripePaymentProvider(PaymentProvider):
    """Stripe PaymentIntents. Selected only when STRIPE_SECRET_KEY is set.

    Stripe must allow raw card data on the account. The full number is sent to
    Stripe and is not kept on the order.
    """

    def __init__(self, secret_key: str) -> None:
        self._secret_key = secret_key

    def charge(
        self,
        *,
        amount_cents: int,
        currency: str,
        card_number: str,
        exp_month: int | None,
        exp_year: int | None,
        cvc: str | None,
    ) -> PaymentResult:
        import httpx

        digits = digits_only(card_number)
        brand = brand_of(digits)
        last4 = digits[-4:] if len(digits) >= 4 else ""
        form = {
            "amount": str(amount_cents),
            "currency": currency.lower(),
            "confirm": "true",
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": digits,
            "payment_method_data[card][exp_month]": "" if exp_month is None else str(exp_month),
            "payment_method_data[card][exp_year]": "" if exp_year is None else str(exp_year),
            "payment_method_data[card][cvc]": cvc or "",
        }
        response = httpx.post(
            "https://api.stripe.com/v1/payment_intents",
            data=form,
            auth=(self._secret_key, ""),
            timeout=30,
        )
        if response.status_code >= 400:
            return PaymentResult(False, "stripe", False, brand, last4, "Stripe declined the charge.")
        status = response.json().get("status")
        if status == "succeeded":
            return PaymentResult(True, "stripe", False, brand, last4, "Stripe reported the charge as succeeded.")
        return PaymentResult(False, "stripe", False, brand, last4, f"Stripe status: {status}.")


def payment_provider_from_env() -> PaymentProvider:
    from db import env_text

    key = env_text("STRIPE_SECRET_KEY")
    if key:
        return StripePaymentProvider(key)
    return SandboxPaymentProvider()
