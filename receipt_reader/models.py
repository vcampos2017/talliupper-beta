"""Data models shared by extraction, storage, and the user interface."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Printed tax-column codes.  H-E-B: T taxed, F/FW not taxed.  Walmart: X taxed,
# N not taxed (its F before the price marks food-benefit eligibility).
TAX_MARKERS = ("F", "FW", "T", "X", "N")

class ReceiptItem(BaseModel):
    """One purchased line item as it appears on a receipt."""

    model_config = ConfigDict(extra="forbid")

    tax_marker: Literal["F", "FW", "T", "X", "N"] | None = Field(default=None, description="Printed receipt marker F, FW, T, or Walmart's X (taxed) / N (not taxed); null if absent or unclear. User must verify its meaning.")
    category: str = Field(
        default="Uncategorized",
        description="User-reviewed expense category for this purchased item",
    )
    name: str = Field(description="Readable item or service name from the receipt")
    product_code: str | None = Field(
        default=None,
        description="Printed UPC, SKU, or other product code; null when absent or unclear",
    )
    quantity: float = Field(description="Quantity purchased; use 1 when no quantity is shown")
    unit_price: float | None = Field(
        description="Price for one unit, or null when it is not printed or cannot be inferred"
    )
    discount: float | None = Field(
        description="Positive item-level discount amount, or null when none is shown"
    )
    line_total: float = Field(
        description="Final amount charged for this line after item-level discounts"
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split()).strip()

    @field_validator("product_code")
    @classmethod
    def clean_product_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"\s+", "", value).strip()
        return cleaned or None

    @field_validator("category")
    @classmethod
    def clean_category(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        return cleaned or "Uncategorized"

    @model_validator(mode="after")
    def separate_embedded_product_columns(self) -> "ReceiptItem":
        """Recover structured fields accidentally retained in an item name.

        Columnar OCR rows can reach the review form with product metadata still
        embedded in ``name`` even though the price was parsed separately. Two
        layouts are handled conservatively:

        * description, product code, unit price, quantity; and
        * product code, description, line price, tax marker, receipt row.

        Numeric values must reconcile with the reviewed item before anything
        is separated. This avoids stripping legitimate numbers from product
        names and keeps uncertain tax marker meanings unchanged.
        """

        if self.product_code is not None:
            return self

        prefix_match = re.fullmatch(
            r"(?P<code>\d{5,14})\s+(?P<name>.+?)\s+"
            r"\$?(?P<line_total>\d{1,7}\.\d{2})"
            r"(?:\s+(?P<marker>FW|F|T|N|X))?"
            r"(?:\s+(?P<receipt_order>\d+))?",
            self.name,
            re.IGNORECASE,
        )
        if prefix_match is not None:
            recovered_name = prefix_match.group("name").strip(" :-")
            recovered_total = float(prefix_match.group("line_total"))
            if (
                re.search(r"[A-Za-z]", recovered_name)
                and abs(recovered_total - self.line_total) <= 0.02
            ):
                self.name = recovered_name
                self.product_code = prefix_match.group("code")
                if self.unit_price is None and abs(self.quantity - 1.0) <= 0.001:
                    self.unit_price = recovered_total
                recovered_marker = str(prefix_match.group("marker") or "").upper()
                if self.tax_marker is None and recovered_marker in {"F", "FW", "T", "X", "N"}:
                    self.tax_marker = recovered_marker
                return self

        suffix_match = re.fullmatch(
            r"(?P<name>.+?[A-Za-z])\s+(?P<code>\d{5,14})\s+"
            r"\$?(?P<unit>\d{1,7}\.\d{2})\s+"
            r"(?P<quantity>\d+(?:\.\d+)?)\s*\$?",
            self.name,
        )
        if suffix_match is None or self.unit_price is None:
            return self

        unit_price = float(suffix_match.group("unit"))
        quantity = float(suffix_match.group("quantity"))
        if (
            abs(unit_price - self.unit_price) > 0.02
            or abs(quantity - self.quantity) > 0.001
            or abs((unit_price * quantity) - self.line_total) > 0.02
        ):
            return self

        self.name = suffix_match.group("name").strip(" :-")
        self.product_code = suffix_match.group("code")
        return self


class ReceiptExtraction(BaseModel):
    """Structured information extracted from a receipt image."""

    model_config = ConfigDict(extra="forbid")

    store_name: str = Field(description="Merchant or store name")
    transaction_date: str | None = Field(
        description="Transaction date in YYYY-MM-DD form, or null when unreadable"
    )
    currency: str = Field(description="Three-letter currency code, normally USD")
    items: list[ReceiptItem] = Field(description="Purchased goods or services")
    subtotal: float | None = Field(description="Printed subtotal, or null when absent")
    tax: float | None = Field(description="Printed total tax, or null when absent")
    total: float | None = Field(description="Final transaction total, or null when unreadable")
    confidence: float = Field(
        ge=0,
        le=1,
        description="Overall extraction confidence from 0 to 1",
    )
    notes: list[str] = Field(
        description="Short warnings about unreadable or inconsistent information"
    )

    @field_validator("store_name", "currency")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(value.split()).strip()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        return normalized if normalized else "USD"


def blank_receipt() -> ReceiptExtraction:
    """Return an empty receipt for manual entry."""

    return ReceiptExtraction(
        store_name="",
        transaction_date=None,
        currency="USD",
        items=[],
        subtotal=None,
        tax=None,
        total=None,
        confidence=0.0,
        notes=["Created manually."],
    )

