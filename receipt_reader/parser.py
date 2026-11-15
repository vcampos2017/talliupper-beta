"""Rule-based conversion from OCR lines to a structured receipt.

OCR recognizes characters.  This parser assigns meaning to those characters.
The rules are intentionally readable so the project can be used to learn and
experiment with document-processing logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from statistics import mean

from .local_ocr import OcrLine
from .models import ReceiptExtraction, ReceiptItem
from .review import is_gratuity_item_name
from .vendors import vendor_profile_for


MONEY_AT_END = re.compile(
    r"(?P<amount>\(?-?\$?\s*\d{1,7}(?:,\d{3})*\.\d{2}\)?)\s*[A-Z]{0,2}\s*$",
    re.IGNORECASE,
)
QUANTITY_AND_PRICE = re.compile(
    r"(?P<quantity>\d+(?:\.\d+)?)\s*(?:@|[xX])\s*\$?"
    r"(?P<unit>\d{1,7}\.\d{2})"
)
EXPLICIT_QUANTITY_UNIT_TOTAL = re.compile(
    r"^\s*(?P<quantity>\d+(?:\.\d+)?)\s*(?:@|[xX])\s*\$?"
    r"(?P<unit>\d{1,7}\.\d{2})\s+\$?"
    r"(?P<total>\d{1,7}\.\d{2})\s*$",
    re.IGNORECASE,
)
# ``1.00 @ 86.49  86.49`` with the ``@`` dropped by OCR: a whole-number
# quantity printed with ``.00``, then the unit price and the extended price.
QUANTITY_UNIT_TOTAL_WITHOUT_AT = re.compile(
    r"^\s*(?P<quantity>\d{1,3})\.00\s+\$?"
    r"(?P<unit>\d{1,7}\.\d{2})\s+\$?"
    r"(?P<total>\d{1,7}\.\d{2})\s*$"
)
OCR_MERGED_QUANTITY_UNIT_TOTAL = re.compile(
    r"^\s*(?P<merged>\d{2,8})\.(?P<cents>\d{2})\s+\$?"
    r"(?P<total>\d{1,7}\.\d{2})\s*$"
)
UNIT_PRICE_QUANTITY_TOTAL = re.compile(
    r"^\s*(?:(?P<code>\d{5,14})\s+)?\$?(?P<unit>\d{1,7}\.\d{2})\s+"
    r"(?P<quantity>\d+(?:\.\d+)?|[Il])\s+\$?(?P<total>\d{1,7}\.\d{2})\s*$"
)
UNIT_PRICE_QUANTITY_SUFFIX = re.compile(
    r"(?:^|\s)(?:(?P<code>\d{5,14})\s+)?\$?(?P<unit>\d{1,7}\.\d{2})\s+"
    r"(?P<quantity>\d+(?:\.\d+)?|[Il])\s*$"
)
ISO_DATE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-/]?(?P<month>0?[1-9]|1[0-2])[-/]?(?P<day>0?[1-9]|[12]\d|3[01])(?!\d)")
# Require matching date separators so hardware sizes such as 1/4-20 are not dates.
US_DATE = re.compile(r"(?<!\d)(?P<month>0?[1-9]|1[0-2])(?P<separator>[-/])(?P<day>0?[1-9]|[12]\d|3[01])(?P=separator)(?P<year>\d{2}|20\d{2})(?!\d)")
MONTH_DATE = re.compile(
    r"(?<![A-Z])(?P<month>JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|"
    r"MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:TEMBER)?|OCT(?:OBER)?|"
    r"NOV(?:EMBER)?|DEC(?:EMBER)?)\s+(?P<day>\d{1,2}),?\s+(?P<year>20\d{2})(?!\d)",
    re.IGNORECASE,
)

MONTH_NUMBERS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

SUBTOTAL_WORDS = re.compile(r"\bSUB\s*-?\s*TOTAL\b", re.IGNORECASE)
AUXILIARY_SUBTOTAL_WORDS = re.compile(
    r"\b(?:FSA|HRA|SNAP|EBT|WIC|TAXABLE|NON[-\s]?TAXABLE)\b.*\bSUB\s*-?\s*TOTAL\b",
    re.IGNORECASE,
)
# The second form accepts a misread rate label such as "TAXI 8.2500 %" (TAX1)
# only when a printed rate follows, so words like "TAXI" alone stay item text.
TAX_WORDS = re.compile(
    r"\b(?:SALES\s+)?TAX(?:\s*\d+)?\b|\bTAX[Il|!]\s*\d{1,2}(?:\.\d{1,4})?\s*%",
    re.IGNORECASE,
)
# A row that is only a misread "TAX" label and an amount (``Tex $0.00``).
TAX_LABEL_MISREAD = re.compile(
    r"^\s*(?:T[E4@]X|TA[KXY]|T[E4@][KY])\s*:?\s*\$?\s*-?\d+[.,]\d{2}\s*$",
    re.IGNORECASE,
)
TOTAL_WORDS = re.compile(
    r"\b(?:GRAND\s+TOTAL|AMOUNT\s+DUE|BALANCE\s+DUE|TOTAL)\b",
    re.IGNORECASE,
)
ITEM_TABLE_HEADER = re.compile(
    r"\bITEM\s+NAME\b.*\bUNIT\s+PRICE\b.*\bQTY\b.*\bTOTAL\b",
    re.IGNORECASE,
)
# A printed column-header row made only of column titles, such as
# ``Item Qty Price Total`` or ``Description`` on the line below it.
COLUMN_HEADER_WORD = (
    r"(?:ITEMS?|DESCRIPTION|DESC\.?|NAME|QTY|QUANTITY|PRICE|UNIT|EACH|EA\.?|"
    r"TOTAL|AMOUNT|AMT|EXT(?:ENDED)?|SKU|CODE|NO\.?|#)"
)
COLUMN_HEADER_LINE = re.compile(
    rf"^\s*{COLUMN_HEADER_WORD}(?:\s*[/|]?\s*{COLUMN_HEADER_WORD})*\s*$",
    re.IGNORECASE,
)
# A purchase row that prints only ``code qty price total`` (``601-10171 1
# 59.90 59.90``); its description is printed on a line of its own.
CODE_QUANTITY_PRICE_TOTAL = re.compile(
    r"^\s*(?P<code>(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,19})\s+"
    r"(?P<quantity>\d{1,3}(?:\.\d{1,3})?|[Il])\s+\$?"
    r"(?P<unit>\d{1,7}\.\d{2})\s+\$?"
    r"(?P<total>\d{1,7}\.\d{2})\s*(?P<marker>[A-Z]{1,2})?\s*$",
    re.IGNORECASE,
)
# The same row with the space between quantity and price lost by OCR:
# ``601-10171 159.90 59.90`` is 1 at 59.90.
CODE_MERGED_QUANTITY_PRICE_TOTAL = re.compile(
    r"^\s*(?P<code>(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,19})\s+"
    r"(?P<merged>\d{2,8})\.(?P<cents>\d{2})\s+\$?"
    r"(?P<total>\d{1,7}\.\d{2})\s*(?P<marker>[A-Z]{1,2})?\s*$",
    re.IGNORECASE,
)
DISCOUNT_WORDS = re.compile(r"\b(?:COUPON|DISCOUNT|SAVINGS?)\b", re.IGNORECASE)

NON_ITEM_WORDS = re.compile(
    r"\b(?:RECEIPT|TRANSACTION|CASHIER|REGISTER|TERMINAL|AUTH|APPROVAL|"
    r"CREDIT|DEBIT|VISA|MASTERCARD|MASTRCRD|MASTERCRD|EFT|EFS|AMEX|DISCOVER|CASH|CHANGE|TENDERED|"
    r"CARD|PAYMENT|CONTACTLESS|CHIP|APPR(?:OVAL)?\s+NO|REF\s+NO|ISSUER|"
    r"AID|TVR|IAD|TSI|THANK|CUSTOMER|PHONE|TEL|GMAIL|IPHONE|BATTERY|WIFI|"
    r"REVIEW\s+ITEM|STORE\s*#?|ORDER|INVOICE|ITEMS\s+PURCHASED|SAVINGS?|SAVED|PRICE\s+CUT)\b",
    re.IGNORECASE,
)
ADDRESS_WORDS = re.compile(
    r"\b(?:STREET|ST\.?|ROAD|RD\.?|AVENUE|AVE\.?|BOULEVARD|BLVD\.?|"
    r"DRIVE|DR\.?|HIGHWAY|HWY\.?|LANE|LN\.?)\b",
    re.IGNORECASE,
)
# A city/state/ZIP line such as ``SAN ANTONIO. TX 99901-0000`` (address, not brand).
# A street-address line such as ``1234 Main Ave.`` (OCR may read a 0 as o).
STREET_ADDRESS = re.compile(
    r"^\s*\d[\dOoIl]{0,5}\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'-]+\s+){0,4}"
    r"(?:STREET|ST|ROAD|RD|AVENUE|AVE|BOULEVARD|BLVD|DRIVE|DR|HIGHWAY|HWY|"
    r"LANE|LN|PARKWAY|PKWY|FREEWAY|FWY|EXPRESSWAY|EXPY|LOOP|WAY|COURT|CT|"
    r"PLACE|PL|PLAZA|PLZ|CIRCLE|CIR|TRAIL|TRL|TERRACE|TER)\.?"
    r"(?:\s+[NSEW]\.?)?(?:\s*,?\s*(?:#|STE\.?|SUITE|UNIT|BLDG\.?)\s*\w{1,5})?\s*$",
    re.IGNORECASE,
)
CITY_STATE_ZIP = re.compile(
    r"\b[A-Z]{2}\.?,?\s*\d{5}(?:\s*-\s*\d{4})?\s*$",
    re.IGNORECASE,
)
# A suite line (``STE 2``) or text masked by diagnostic redaction is never a merchant.
SUITE_OR_REDACTED = re.compile(r"\b(?:STE|SUITE)\s*#?\s*\w{1,5}\s*$|\[REDACTED\b", re.IGNORECASE)
# A trailing store number after the brand, as in ``The UPS Store #0123``.
TRAILING_STORE_NUMBER = re.compile(r"\bSTORE\s*(?:#|NO\.?)?\s*\d*\s*$", re.IGNORECASE)
PHONE_OR_WEB = re.compile(
    r"(?:\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}|WWW\.|HTTPS?://|\.COM\b)",
    re.IGNORECASE,
)
IMPLIED_CENTS = re.compile(r"(?<!\d)(?P<digits>\d{3,7})(?!\d)")
# OCR dropped the decimal point of a dollar-signed price: ``$2997`` or ``$29 97``.
DOLLAR_MISSING_DECIMAL = re.compile(
    r"(?<![\d.])\$\s*(?P<dollars>\d{1,5})\s?(?P<cents>\d{2})\s*$"
)
# A small leading integer before the name, as in ``3 Egg Fried Rice $29.97``.
LEADING_QUANTITY = re.compile(
    r"^(?P<quantity>[1-9]\d?)\s+(?!(?:OZ|FL|LBS?|KG|G|ML|L|CT|PK|PC|PCS|IN|FT|QT|GAL|PACK|COUNT)\b)(?=[A-Za-z])",
    re.IGNORECASE,
)
WEIGHT_DETAILS = re.compile(
    r"(?P<quantity>\d+(?:\.\d+)?)\s*LBS?\b.*?(?P<unit>\d+\.\d{2})",
    re.IGNORECASE,
)
PRODUCT_CODE_AT_END = re.compile(r"\b(?P<code>\d{8,14})\s*$")
PRODUCT_CODE_AT_START = re.compile(
    r"^\s*(?P<code>\d{8,14})(?P<description>.*)$"
)
PRINTED_ITEM_COUNT = re.compile(
    # OCR can drop the space in "ITEMS SOLD" ("# ITEMSOLD 11").
    r"\b(?:ITEMS\s*(?:PURCHASED|[S5$?]?OLD)|SOLD)\s*:?\s*(?P<count>\d+)\b",
    re.IGNORECASE,
)
# Petco prints the count on its own line: ``3 Items``. Anchor the whole line so
# product text such as ``2 ITEMS FREE`` is not read as a count.
STANDALONE_ITEM_COUNT = re.compile(
    r"^\s*(?P<count>\d{1,3})\s+ITEMS?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# OCR can pick up faded print from the back of the receipt on the same row,
# e.g. ``on e1 anoada 3 Items es bruter llute``. Accept Petco's title-case
# ``N Items`` inside such a row, but never on a priced or promotional line.
EMBEDDED_ITEM_COUNT = re.compile(r"(?<![\w.$])(?P<count>\d{1,3})\s+Items\b")
ITEM_COUNT_EXCLUDED_WORDS = re.compile(
    r"\b(?:BUY|GET|FREE|OFF|SAVE|SAVINGS?|COUPON|FOR)\b|%|\$|\d\.\d{2}\b",
    re.IGNORECASE,
)
# A detail row under a priced item that only restates its quantity and unit
# price, as in Petco's ``Reg Price: 3@ 14.49`` under ``... 43.47 T``.
QUANTITY_DETAIL_ROW = re.compile(
    r"^\s*(?:(?:REG(?:ULAR)?\.?\s+)?PRICE\s*:?\s*)?"
    r"(?P<quantity>\d{1,3})\s*@\s*$",
    re.IGNORECASE,
)
ORDERED_PRICE_LINE = re.compile(
    r"^\s*ORDERED\s+PRICE(?:\s+\$?\d{1,7}(?:,\d{3})*\.\d{2})?\s*$",
    re.IGNORECASE,
)
WALMART_DETACHED_PRICE = re.compile(
    r"^\s*\$?(?P<amount>\d{1,7}\.\d{2})\s*(?P<marker>N|X)?\s*$",
    re.IGNORECASE,
)
WALMART_MULTI_BUY = re.compile(
    r"^\s*(?P<quantity>\d+(?:\.\d+)?)\s+AT\s+"
    r"(?P<bundle>\d+(?:\.\d+)?)\s+FOR\s+\$?"
    r"(?P<unit>\d{1,7}\.\d{2})\s+\$?"
    r"(?P<total>\d{1,7}\.\d{2})\s*(?P<marker>N|X)?\s*$",
    re.IGNORECASE,
)
MERCHANT_ALIASES = (
    (re.compile(r"^\s*FIVE\s*BELOW\s*$", re.IGNORECASE), "Five Below"),
    (re.compile(r"\bPETCO\b", re.IGNORECASE), "Petco"),
    (re.compile(r"\bWAL\s*[-*]?\s*MART\b|\bWALMART\b", re.IGNORECASE), "Walmart"),
    (
        re.compile(r"\bROSS\b(?:\s+DRESS\s+FOR\s+LESS)?", re.IGNORECASE),
        "Ross Dress for Less",
    ),
    (
        re.compile(r"^\s*ROCKLER\b", re.IGNORECASE),
        "Rockler",
    ),
    (re.compile(r"^\s*(?:THE\s+)?HOME\s+DEPOT\s*$", re.IGNORECASE), "The Home Depot"),
    (re.compile(r"\bH\s*[-.]?\s*E\s*[-.]?\s*B\b", re.IGNORECASE), "H-E-B"),
    (
        re.compile(r"\bMC\s*DONALD(?:'|’)?S\b", re.IGNORECASE),
        "McDonald's",
    ),
    (
        re.compile(r"\bGREAT\b.{0,40}\bCLIPS\b", re.IGNORECASE),
        "Great Clips",
    ),
)
PAYMENT_WORDS = re.compile(
    r"\b(?:VISA|AMEX|DISCOVER|DEBIT|CREDIT|MAST(?:ER|R)?C(?:A)?RD|EFS)\b",
    re.IGNORECASE,
)
NON_TRANSACTION_DATE_WORDS = re.compile(
    r"\b(?:EXPIRES?|EXPIRATION|VALID\s+(?:THRU|THROUGH|UNTIL))\b",
    re.IGNORECASE,
)
TIME_OF_DAY = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?:\s*[AP](?:\.?M\.?)?)?(?!\d)",
    re.IGNORECASE,
)
HEB_COMPACT_DATE = re.compile(
    r"(?<!\d)(?P<month>0[1-9]|1[0-2])(?P<day>0[1-9]|[12]\d|3[01])\s+"
    r"(?P<year>\d{2})(?P<hour>[01]\d|2[0-3])(?=\s|$)"
)


@dataclass(frozen=True, slots=True)
class LineDecision:
    index: int
    text: str
    confidence: float
    role: str
    reason: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    receipt: ReceiptExtraction
    decisions: tuple[LineDecision, ...]
    missing_item_candidates: tuple["MissingItemCandidate", ...] = ()


@dataclass(frozen=True, slots=True)
class MissingItemCandidate:
    """Product-like OCR text that was not safely parsed as a priced item."""

    name: str
    confidence: float
    source_line: int
    supporting_text: str = ""
    quantity: float = 1.0
    unit_price: float | None = None
    line_total: float | None = None
    tax_marker: str | None = None


def printed_item_count(text: str) -> int | None:
    """Return a receipt's explicit purchased-item count when one is printed."""

    match = PRINTED_ITEM_COUNT.search(text) or STANDALONE_ITEM_COUNT.search(text)
    if match is None:
        for line in text.splitlines():
            if ITEM_COUNT_EXCLUDED_WORDS.search(line):
                continue
            if (match := EMBEDDED_ITEM_COUNT.search(line)) is not None:
                break
    return int(match.group("count")) if match else None


def purchased_unit_count(quantities) -> int:
    """Count purchased units the way a printed item count usually does.

    Stores such as H-E-B count units (``3 Ea.`` counts as 3). A weighed item
    (``1.42 lb``) counts as one purchase. Missing or invalid quantities count
    as one row.
    """

    total = 0
    for value in quantities:
        try:
            quantity = float(value)
        except (TypeError, ValueError):
            quantity = 1.0
        if quantity != quantity:  # NaN from an empty table cell
            quantity = 1.0
        whole = quantity >= 1 and abs(quantity - round(quantity)) < 1e-9
        total += int(round(quantity)) if whole else 1
    return total


def printed_count_matches(expected: int | None, rows: int, units: int) -> bool:
    """A printed count may count rows or units; either agreement is a match."""

    return expected is None or expected in (rows, units)


def _walmart_tax_flag(text: str, store_name: str | None) -> str | None:
    """Return Walmart's trailing X (taxed) or N (not taxed) flag, if printed."""

    if store_name != "Walmart":
        return None
    flag = re.search(r"(?:^|\s|\d)(X|N)\s*$", text)
    return flag.group(1) if flag else None


def amount_at_end(text: str) -> tuple[float, int] | None:
    """Return the trailing monetary value and where its token begins."""

    match = MONEY_AT_END.search(text)
    if not match:
        return None
    token = match.group("amount").replace("$", "").replace(",", "").replace(" ", "")
    negative_parentheses = token.startswith("(") and token.endswith(")")
    token = token.strip("()")
    try:
        value = float(token)
    except ValueError:
        return None
    if negative_parentheses:
        value = -abs(value)
    return round(value, 2), match.start("amount")


def date_from_text(text: str, *, heb_compact: bool = False) -> str | None:
    """Find a common receipt date and normalize it to ISO form."""

    for pattern in (ISO_DATE, US_DATE):
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            return date(year, int(match.group("month")), int(match.group("day"))).isoformat()
        except ValueError:
            continue

    month_match = MONTH_DATE.search(text)
    if month_match:
        try:
            return date(
                int(month_match.group("year")),
                MONTH_NUMBERS[month_match.group("month")[:3].upper()],
                int(month_match.group("day")),
            ).isoformat()
        except (KeyError, ValueError):
            return None

    if heb_compact:
        compact_match = HEB_COMPACT_DATE.search(text)
        if compact_match:
            try:
                return date(
                    2000 + int(compact_match.group("year")),
                    int(compact_match.group("month")),
                    int(compact_match.group("day")),
                ).isoformat()
            except ValueError:
                return None
    return None


def _receipt_date_candidate(
    lines: list[OcrLine], *, heb: bool = False
) -> tuple[str, int] | None:
    """Choose a transaction date without mistaking expiry dates for purchases."""

    candidates: list[tuple[float, int, str]] = []
    for index, line in enumerate(lines):
        parsed = date_from_text(line.text, heb_compact=heb)
        if parsed is None or NON_TRANSACTION_DATE_WORDS.search(line.text):
            continue
        score = line.confidence
        if TIME_OF_DAY.search(line.text):
            score += 4
        if re.search(r"\b(?:DATE|TRANSACTION|PURCHASE)\b", line.text, re.IGNORECASE):
            score += 2
        candidates.append((score, index, parsed))
    if not candidates:
        return None
    _, index, parsed = max(candidates, key=lambda candidate: (candidate[0], -candidate[1]))
    return parsed, index


def _quantity_value(token: str) -> float:
    """Normalize a numeric quantity, including common OCR 1/I/l confusion."""

    return 1.0 if token in {"I", "l"} else float(token)


def _valid_unit_quantity_total(text: str) -> re.Match[str] | None:
    """Return an auditable unit-price/quantity/total row when its math reconciles."""

    match = UNIT_PRICE_QUANTITY_TOTAL.fullmatch(text)
    if match is None:
        return None
    unit_price = float(match.group("unit"))
    quantity = _quantity_value(match.group("quantity"))
    line_total = float(match.group("total"))
    if abs((unit_price * quantity) - line_total) > 0.02:
        return None
    # ``1.00 86.49 86.49`` is "1 @ 86.49" with the ``@`` lost, not 86.49
    # units at $1.00: a "quantity" equal to the line amount is a price.
    if (
        unit_price == 1.0
        and not match.group("code")
        and "." in match.group("quantity")
        and abs(quantity - line_total) <= 0.02
    ):
        return None
    return match


def _quantity_unit_total(text: str) -> tuple[float, float, float] | None:
    """Recover quantity, unit price, and extended charge from a pricing row.

    Home Depot prints rows such as ``5@6.98 34.90``. OCR can drop the ``@``
    and collapse the first two values to ``506.98 34.90``. Only accept a
    recovered split when its arithmetic reconciles to the printed charge.
    """

    explicit = EXPLICIT_QUANTITY_UNIT_TOTAL.fullmatch(text)
    if explicit is not None:
        quantity = float(explicit.group("quantity"))
        unit_price = float(explicit.group("unit"))
        line_total = float(explicit.group("total"))
        if abs((quantity * unit_price) - line_total) <= 0.02:
            return quantity, unit_price, line_total
        return None

    without_at = QUANTITY_UNIT_TOTAL_WITHOUT_AT.fullmatch(text)
    if without_at is not None:
        quantity = float(without_at.group("quantity"))
        unit_price = float(without_at.group("unit"))
        line_total = float(without_at.group("total"))
        if quantity > 0 and abs((quantity * unit_price) - line_total) <= 0.02:
            return quantity, unit_price, line_total

    merged = OCR_MERGED_QUANTITY_UNIT_TOTAL.fullmatch(text)
    if merged is None:
        return None

    digits = merged.group("merged")
    cents = merged.group("cents")
    line_total = float(merged.group("total"))
    candidates: list[tuple[float, float, float]] = []
    for split in range(1, len(digits)):
        quantity = float(digits[:split])
        unit_price = float(f"{digits[split:]}.{cents}")
        if quantity <= 0 or unit_price <= 0:
            continue
        if abs((quantity * unit_price) - line_total) <= 0.02:
            candidates.append((quantity, unit_price, line_total))

    return candidates[0] if len(candidates) == 1 else None


def _merchant_check_text(text: str) -> str:
    """Return the text to screen as a merchant name.

    ``STORE #0123`` alone is store metadata, but a brand printed before it
    (``The UPS Store #0123``) is the merchant header, so only the trailing
    store number is set aside before the non-merchant word checks.
    """

    match = TRAILING_STORE_NUMBER.search(text)
    if match is None:
        return text
    brand = re.sub(r"^\s*THE\b", "", text[: match.start()], flags=re.IGNORECASE)
    if sum(character.isalpha() for character in brand) < 2:
        return text
    return text[: match.start()]


def _is_item_price_follow_up(text: str) -> bool:
    """Return whether a row prices the line above it (``3 Ea.@ 1/ 2.77 8.31``)."""

    text = text.strip()
    return _bare_amount(text) is not None or (
        amount_at_end(text) is not None
        and (_is_price_detail_row(text) or "@" in text)
    )


# ``The UPS Store 10123``: OCR often reads a printed ``#`` as ``1``.
STORE_NUMBER_HASH_AS_ONE = re.compile(r"(?P<label>\bSTORE\s+)1(?P<number>\d{3,6})\s*$", re.IGNORECASE)


def _restore_store_number_hash(name: str, lines: list[OcrLine]) -> str:
    """Undo an OCR ``#`` → ``1`` misread in a merchant's store number.

    ``The UPS Store #0123`` is often read as ``The UPS Store 10123``. Only
    restore the ``#`` when the receipt itself confirms the number: the digits
    without the leading ``1`` appear on another line (a terminal or register
    ID such as ``P0S0123A``) and the full misread number does not.
    """

    match = STORE_NUMBER_HASH_AS_ONE.search(name)
    if match is None:
        return name
    number = match.group("number")
    misread = "1" + number
    other_text = [line.text for line in lines if line.text.strip() != name.strip()]
    confirmed = any(re.search(rf"(?<!\d){number}(?!\d)", text) for text in other_text)
    contradicted = any(re.search(rf"(?<!\d){misread}(?!\d)", text) for text in other_text)
    if not confirmed or contradicted:
        return name
    return name[: match.start()] + match.group("label") + "#" + number


def _store_candidate(lines: list[OcrLine]) -> int | None:
    """Pick the best merchant header line, or None rather than guess.

    The merchant is printed above the purchases, so scanning stops at the
    first priced row, and a line priced by the row below it is an item name.
    Address lines (street, city/state/ZIP) are never the merchant.
    """

    best: tuple[float, int] | None = None
    for index, line in enumerate(lines[:10]):
        text = line.text.strip()
        if amount_at_end(text) is not None and not date_from_text(text):
            break
        if index + 1 < len(lines) and _is_item_price_follow_up(lines[index + 1].text):
            continue
        if re.fullmatch(r"\+?\s*ADD", text, re.IGNORECASE):
            continue
        if re.search(
            r"\b(?:HOW\s+ARE\s+WE\s+DOING|LET\s+US\s+KNOW)\b",
            text,
            re.IGNORECASE,
        ):
            continue
        if re.search(r"\b(?:ENTR(?:Y|IES|ANTS)|SURVEY|PASSWORD|USER\s+ID|PURCHASE\s+NECESSARY|RULES|COMPLETED\s+WITHIN|OLDER\s+TO\s+ENTER|MORE\s+(?:SAVING|DOING))\b", text, re.IGNORECASE):
            continue
        letters = sum(character.isalpha() for character in text)
        if letters < 3 or date_from_text(text):
            continue
        check_text = _merchant_check_text(text)
        if (
            PHONE_OR_WEB.search(text)
            or ADDRESS_WORDS.search(check_text)
            or SUITE_OR_REDACTED.search(text)
            or CITY_STATE_ZIP.search(text)
            or NON_ITEM_WORDS.search(check_text)
        ):
            continue
        score = (20 - index) + (line.confidence * 5) + min(letters, 20) / 5
        if text.isupper():
            score += 1
        if best is None or score > best[0]:
            best = (score, index)
    return best[1] if best else None


def _merchant_alias(lines: list[OcrLine]) -> tuple[str, int] | None:
    """Recognize distinctive merchant text even when the logo OCR is missed."""

    for index, line in enumerate(lines[:15]):
        for pattern, name in MERCHANT_ALIASES:
            if pattern.search(line.text):
                if name == "Great Clips":
                    nearby_text = " ".join(
                        candidate.text
                        for candidate in lines[max(0, index - 1) : index + 3]
                    )
                    if re.search(r"\bNORTH\s*TOWNE?\s+SC\b", nearby_text, re.IGNORECASE):
                        name = "Great Clips — Northtowne SC"
                return name, index
    # The logo may be absent from OCR, but the receipt's own website remains.
    # Require a domain, not a gift-card product or promotional brand mention.
    for index, line in enumerate(lines):
        if re.search(r"(?<![\w.-])(?:www\.)?homedepot\.com(?=[/\s]|$)", line.text, re.IGNORECASE):
            return "The Home Depot", index
    return None


def _is_subtotal_label(text: str) -> bool:
    if SUBTOTAL_WORDS.search(text):
        return True
    compact = re.sub(r"[^A-Z]", "", text.upper())
    # Common OCR damage for decorative "Sale Subtotal" headings.
    return compact.startswith("S") and "BTAL" in compact


def _implied_cents_at_end(text: str) -> tuple[float, int] | None:
    """Read a digit-only amount such as 1188 as 11.88, with low trust."""

    matches = list(IMPLIED_CENTS.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    if text[match.end() :].strip(" *:-"):
        return None
    return int(match.group("digits")) / 100, match.start("digits")


def _dollar_amount_missing_decimal(text: str) -> tuple[float, int] | None:
    """Read an item price such as ``$2997`` or ``$29 97`` as 29.97.

    Dollar-signed item prices always print cents, so the last two digits are
    cents. Used only for item rows that have no normal decimal amount.
    """

    match = DOLLAR_MISSING_DECIMAL.search(text)
    if match is None:
        return None
    value = float(f"{match.group('dollars')}.{match.group('cents')}")
    return value, match.start()


def _is_price_detail_row(text: str) -> bool:
    """Return whether a priced row holds only quantity/weight/unit details."""

    if amount_at_end(text) is None:
        return False
    words = re.findall(r"[A-Za-z]{3,}", MONEY_AT_END.sub("", text))
    return all(word.upper() in {"LBS", "EACH", "FOR", "KGS"} for word in words)


def _continues_item_name(
    item_line: OcrLine, previous_line: OcrLine, line: OcrLine
) -> bool:
    """Return whether ``line`` is the next printed line of an item's name.

    Restaurant tickets wrap long names under the first line, for example
    ``Stuffed Carb $6.99`` then ``Rangoon(5)``. Require OCR boxes: the line
    must sit directly below the previous name line, not start left of the
    priced row's text, and stop well short of its price column.
    """

    if item_line.box is None or previous_line.box is None or line.box is None:
        return False
    ix1, iy1, ix2, iy2 = item_line.box
    py1 = previous_line.box[1]
    x1, y1, x2, y2 = line.box
    height = iy2 - iy1
    if height <= 0 or y2 <= y1:
        return False
    text = line.text.strip()
    return (
        0 < y1 - py1 <= 1.6 * height
        and x1 >= ix1 - 0.25 * height
        and x2 <= ix1 + 0.75 * (ix2 - ix1)
        and amount_at_end(text) is None
        and any(character.isalpha() for character in text)
        and not NON_ITEM_WORDS.search(text)
        and not ADDRESS_WORDS.search(text)
        and not PHONE_OR_WEB.search(text)
        and not date_from_text(text)
        and not _is_summary(text)
    )


def _bare_amount(text: str) -> float | None:
    """Return the amount when a row holds nothing else, such as ``1.60``."""

    amount = amount_at_end(text)
    if amount is None or text[: amount[1]].strip():
        return None
    return amount[0]


def _split_row_tax(
    lines: list[OcrLine],
    roles: dict[int, tuple[str, str]],
    subtotal: float | None,
) -> tuple[int, int, float] | None:
    """Pair a tax label that has no amount with an adjacent bare amount row.

    Tax is always smaller than the subtotal, and an amount that sits between the
    tax label and another label missing its amount is left alone, so a split
    subtotal or total row is never taken as tax.
    """

    for index, line in enumerate(lines):
        if not TAX_WORDS.search(line.text) or amount_at_end(line.text) is not None:
            continue
        for neighbor in (index + 1, index - 1):
            if not 0 <= neighbor < len(lines) or neighbor in roles:
                continue
            value = _bare_amount(lines[neighbor].text)
            if value is None or value < 0:
                continue
            if subtotal is not None and value >= subtotal:
                continue
            # "SUBTOTAL", "27.23", "TAX1 8.2500 %": the amount belongs to the
            # other label, which also lost its amount.
            beyond = neighbor + (neighbor - index)
            if (
                0 <= beyond < len(lines)
                and _is_summary(lines[beyond].text)
                and amount_at_end(lines[beyond].text) is None
            ):
                continue
            return index, neighbor, value
    return None


def _is_tax_label(text: str) -> bool:
    """Return whether a row is a tax line, including an OCR-misread ``TAX``."""

    return bool(TAX_WORDS.search(text) or TAX_LABEL_MISREAD.fullmatch(text))


def _is_column_header(text: str) -> bool:
    """Return whether a row only names the item table's columns.

    ``Item Qty Price Total`` must not end the purchase section the way a real
    ``Total`` row does, and ``Description`` is a title, not a product. A lone
    ``Total`` or ``Total Price`` row is still a summary label.
    """

    if ITEM_TABLE_HEADER.search(text):
        return True
    if COLUMN_HEADER_LINE.fullmatch(text) is None:
        return False
    words = {word.upper() for word in re.findall(r"[A-Za-z]+", text)}
    return bool(
        words & {"QTY", "QUANTITY", "DESCRIPTION", "DESC"}
        or ({"ITEM", "ITEMS"} & words and {"PRICE", "AMOUNT", "AMT"} & words)
    )


def _is_summary(text: str) -> bool:
    if _is_column_header(text):
        return False
    return bool(_is_subtotal_label(text) or _is_tax_label(text) or TOTAL_WORDS.search(text))


def _looks_like_item(text: str) -> bool:
    description = MONEY_AT_END.sub("", text).strip(" :-")
    # CARD is normally tender metadata, but an SD card reader is merchandise.
    # Exempt only this product phrase; CREDIT, AUTH, etc. still reject a row.
    metadata_description = re.sub(
        r"\bSD\s+CARD\s+READER\b", "SD READER", description, flags=re.IGNORECASE
    )
    return (
        bool(description)
        and any(character.isalpha() for character in description)
        and not NON_ITEM_WORDS.search(metadata_description)
        and not PHONE_OR_WEB.search(description)
        and not date_from_text(description)
    )


def _item_description(text: str, *, heb: bool = False) -> tuple[str, str | None]:
    """Separate a terminal tax field without stripping product sizes or numbers."""
    text = re.sub(r"\s+\d(?:\s+[A-Za-z0-9]){0,3}\s+_+\s*$", "", text).strip()
    # Run 5's H-E-B OCR appended WHL after the printed tax field.
    suffix = r"\b(FW|F|T)\s*$"
    if heb:
        suffix = r"\b(FW|F|T)(?:\s+(?:WHL|\d{1,2}))?\s*$"
    marker = re.search(suffix, text, re.IGNORECASE)
    if not marker:
        return text, None
    name = text[:marker.start()].strip()
    # Limit the observed spurious '1' repair to H-E-B's loose produce.
    if heb and marker.group(1).upper() == "FW":
        name = re.sub(r"\b(AVOCADOS|ONIONS)\s+1$", r"\1", name, flags=re.IGNORECASE)
    return name, marker.group(1).upper()


def join_price_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """Assign each left-hand fragment to its nearest unambiguous price row.

    Allow a full text-height offset for tilted/creased receipts. Never attach
    a fragment when two neighboring prices are equally plausible.
    """
    anchors = [(i, line) for i, line in enumerate(lines)
               if line.box and amount_at_end(line.text) is not None]
    # A weight/unit-price fragment may itself end in money. Prefer the
    # separate rightmost charge on that row, not the unit price.
    unit_fragments = set()
    for i, line in anchors:
        if not re.search(r"\b(?:LBS?|KG)\b|@", line.text, re.IGNORECASE):
            continue
        for j, charge in anchors:
            if j != i and charge.x >= line.box[2]:
                height = min(line.box[3]-line.y, charge.box[3]-charge.y)
                if height > 0 and abs((line.y+line.box[3]-charge.y-charge.box[3])/2) <= .75*height:
                    unit_fragments.add(i)
                    break
    anchors = [(i, line) for i, line in anchors if i not in unit_fragments]
    groups = {i: [(i, line)] for i, line in anchors}
    consumed = set()
    for i, line in enumerate(lines):
        if not line.box or (amount_at_end(line.text) is not None and i not in unit_fragments):
            continue
        x1, y1, x2, y2 = line.box
        height = y2 - y1
        if height <= 0:
            continue
        candidates = []
        for j, anchor in anchors:
            ax1, ay1, ax2, ay2 = anchor.box
            if x2 > ax1 or ay2 <= ay1:
                continue
            scale = min(height, ay2 - ay1)
            distance = abs((y1 + y2 - ay1 - ay2) / 2) / scale
            if distance <= 0.75:
                candidates.append((distance, j))
        candidates.sort()
        if not candidates or (len(candidates) > 1 and candidates[1][0] - candidates[0][0] < .25):
            continue
        target = candidates[0][1]
        # Two fragments occupying the same horizontal column are different rows,
        # not a wrapped description to concatenate blindly.
        existing = groups[target]
        if any(min(x2, other.box[2]) > max(x1, other.box[0])
               for _, other in existing):
            continue
        groups[target].append((i, line))
        consumed.add(i)
    result = []
    for i, line in enumerate(lines):
        if i in consumed:
            continue
        members = sorted(groups.get(i, [(i, line)]), key=lambda pair: pair[1].x)
        if len(members) > 1:
            boxes = [member.box for _, member in members]
            line = OcrLine(" ".join(member.text for _, member in members),
                           min(member.confidence for _, member in members),
                           (min(box[0] for box in boxes), min(box[1] for box in boxes),
                            max(box[2] for box in boxes), max(box[3] for box in boxes)))
        result.append(line)
    return sorted(result, key=lambda line: (line.y, line.x)) if all(line.box for line in result) else result


def _split_embedded_ocr_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """Keep OCR engines from turning several printed rows into one parser row."""

    result: list[OcrLine] = []
    for line in lines:
        parts = [part.strip() for part in line.text.splitlines() if part.strip()]
        if len(parts) <= 1:
            result.append(line)
            continue
        # A shared OCR box cannot describe the geometry of each embedded row.
        # Preserve reading order and confidence; later rules do not need a box.
        result.extend(OcrLine(part, line.confidence) for part in parts)
    return result


def _combined_ocr_line(first: OcrLine, second: OcrLine, text: str) -> OcrLine:
    box = None
    if first.box is not None and second.box is not None:
        box = (
            min(first.box[0], second.box[0]),
            min(first.box[1], second.box[1]),
            max(first.box[2], second.box[2]),
            max(first.box[3], second.box[3]),
        )
    return OcrLine(text, min(first.confidence, second.confidence), box)


def _join_walmart_detached_price_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """Attach Walmart price columns and multi-buy rows to their product rows.

    PaddleOCR can return the description/UPC and the right-aligned price as
    separate regions. Both fields are already recognized, so recovery should
    not make the user type the price again. This rule is deliberately scoped
    to Walmart and requires a UPC-bearing product row immediately followed by
    a supported price layout.
    """

    if not any(
        re.search(r"\bWAL\s*[-*]?\s*MART\b|\bWALMART\b", line.text, re.IGNORECASE)
        for line in lines[:20]
    ):
        return lines

    result: list[OcrLine] = []
    index = 0
    while index < len(lines):
        current = lines[index]
        if index + 1 >= len(lines):
            result.append(current)
            break

        following = lines[index + 1]
        stem = current.text.strip()
        has_product_shape = (
            amount_at_end(stem) is None
            and _looks_like_item(stem)
            and PRODUCT_CODE_AT_END.search(_item_description(stem)[0]) is not None
        )
        if not has_product_shape:
            result.append(current)
            index += 1
            continue

        detached = WALMART_DETACHED_PRICE.fullmatch(following.text)
        if detached is not None:
            marker = detached.group("marker") or ""
            combined = f"{stem} {detached.group('amount')} {marker}".strip()
            result.append(_combined_ocr_line(current, following, combined))
            index += 2
            continue

        multi_buy = WALMART_MULTI_BUY.fullmatch(following.text)
        if multi_buy is not None:
            quantity = float(multi_buy.group("quantity"))
            bundle = float(multi_buy.group("bundle"))
            printed_unit = float(multi_buy.group("unit"))
            line_total = float(multi_buy.group("total"))
            effective_unit = printed_unit / bundle if bundle else 0.0
            if (
                quantity > 0
                and bundle > 0
                and abs((quantity * effective_unit) - line_total) <= 0.02
            ):
                name_and_code, printed_marker = _item_description(stem)
                code_match = PRODUCT_CODE_AT_END.search(name_and_code)
                if code_match is not None:
                    name = name_and_code[: code_match.start()].strip(" :-")
                    product_code = code_match.group("code")
                    marker_text = f" {printed_marker}" if printed_marker else ""
                    trailing_marker = multi_buy.group("marker") or ""
                    combined = (
                        f"{name}{marker_text} {product_code} "
                        f"{effective_unit:.2f} {quantity:g} "
                        f"{line_total:.2f} {trailing_marker}"
                    ).strip()
                    result.append(_combined_ocr_line(current, following, combined))
                    index += 2
                    continue

        result.append(current)
        index += 1

    return result


def _code_row_values(text: str) -> tuple[str, float, float, float, str] | None:
    """Read a ``code qty price total`` row whose arithmetic reconciles."""

    match = CODE_QUANTITY_PRICE_TOTAL.fullmatch(text.strip())
    if match is None:
        merged = CODE_MERGED_QUANTITY_PRICE_TOTAL.fullmatch(text.strip())
        if merged is None:
            return None
        # Accept the split only when exactly one quantity reconciles.
        digits, cents = merged.group("merged"), merged.group("cents")
        line_total = float(merged.group("total"))
        splits = [
            (float(digits[:split]), float(f"{digits[split:]}.{cents}"))
            for split in range(1, min(4, len(digits)))
            if float(digits[:split]) > 0
            and abs(float(digits[:split]) * float(f"{digits[split:]}.{cents}") - line_total) <= 0.02
        ]
        if len(splits) != 1:
            return None
        quantity, unit_price = splits[0]
        return (
            merged.group("code"),
            quantity,
            unit_price,
            line_total,
            (merged.group("marker") or "").upper(),
        )
    quantity = _quantity_value(match.group("quantity"))
    unit_price = float(match.group("unit"))
    line_total = float(match.group("total"))
    if quantity <= 0 or abs(quantity * unit_price - line_total) > 0.02:
        return None
    return (
        match.group("code"),
        quantity,
        unit_price,
        line_total,
        (match.group("marker") or "").upper(),
    )


def _is_separate_name_line(code_line: OcrLine, line: OcrLine) -> bool:
    """Return whether ``line`` can be the printed name of an adjacent code row."""

    text = line.text.strip()
    if (
        amount_at_end(text) is not None
        or not _looks_like_item(text)
        or _is_summary(text)
        or _is_column_header(text)
        or ADDRESS_WORDS.search(text)
        or CITY_STATE_ZIP.search(text)
        or SUITE_OR_REDACTED.search(text)
        or printed_item_count(text) is not None
        or _code_row_values(text) is not None
        or sum(character.isalpha() for character in text) < 3
    ):
        return False
    if code_line.box is None or line.box is None:
        return True
    cx1, cy1, cx2, cy2 = code_line.box
    x1, y1, _, _ = line.box
    height = cy2 - cy1
    # Directly above or below the code row, starting in its left half.
    return (
        height > 0
        and abs(y1 - cy1) <= 1.6 * height
        and x1 < cx1 + 0.5 * (cx2 - cx1)
    )


def _join_code_rows_with_names(
    lines: list[OcrLine],
) -> tuple[list[OcrLine], dict[OcrLine, str]]:
    """Join ``code qty price total`` rows to the name printed on its own line.

    Small-shop point-of-sale receipts often print each purchase as two lines::

        601-10171 1 59.90 59.90
        SmallRig Sling Handle for DJI

    Neither line is an item alone: one has no name, the other no price. The
    name sits on the same side of every code row on a receipt, so the side is
    chosen once: below when every code row has a name line under it and not
    all have one above (for example a ``Description`` header sits above the
    first row), above in the opposite case. When both fit, a column header
    that ends with ``Description`` (name printed under the code row) decides;
    otherwise the rows are left alone rather than guessed.

    Returns the joined lines and the product code for each joined row.
    """

    code_indexes = [
        index for index, line in enumerate(lines) if _code_row_values(line.text)
    ]
    if not code_indexes:
        return lines, {}

    def name_at(code_index: int, offset: int) -> bool:
        other = code_index + offset
        return 0 <= other < len(lines) and _is_separate_name_line(
            lines[code_index], lines[other]
        )

    below = all(name_at(index, 1) for index in code_indexes)
    above = all(name_at(index, -1) for index in code_indexes)
    # Consecutive code rows can't share a name line.
    if any(b - a < 2 for a, b in zip(code_indexes, code_indexes[1:])):
        return lines, {}
    if below and above:
        header_ends_with_description = any(
            _is_column_header(lines[index].text)
            and re.search(r"\bDESC(?:RIPTION)?\.?\s*$", lines[index].text, re.IGNORECASE)
            for index in range(code_indexes[0])
        )
        above = not header_ends_with_description
        below = header_ends_with_description
    if below == above:
        return lines, {}
    offset = 1 if below else -1

    codes: dict[OcrLine, str] = {}
    name_indexes = {index + offset: index for index in code_indexes}
    result: list[OcrLine] = []
    for index, line in enumerate(lines):
        if index in name_indexes:
            continue
        values = _code_row_values(line.text)
        if values is None or index not in code_indexes:
            result.append(line)
            continue
        code, quantity, unit_price, line_total, marker = values
        name_line = lines[index + offset]
        text = (
            f"{name_line.text.strip()} {quantity:g}@{unit_price:.2f} "
            f"{line_total:.2f} {marker}"
        ).strip()
        combined = _combined_ocr_line(line, name_line, text)
        codes[combined] = code
        result.append(combined)
    return result, codes


def parse_receipt(lines: list[OcrLine] | tuple[OcrLine, ...]) -> ParseResult:
    """Interpret OCR lines using transparent receipt-oriented rules."""

    expanded_lines = _split_embedded_ocr_rows(
        [line for line in lines if line.text.strip()]
    )
    clean_lines = join_price_rows(expanded_lines)
    clean_lines = _join_walmart_detached_price_rows(clean_lines)
    vendor = vendor_profile_for(clean_lines)
    if vendor is not None:
        clean_lines = vendor.preprocess(clean_lines)
    clean_lines, code_row_codes = _join_code_rows_with_names(clean_lines)
    # Join complete quantity/price rows to an unpriced description, with an
    # optional UPC-bearing product row before its descriptive continuation.
    joined = []
    for line in clean_lines:
        weighted_columns = _valid_unit_quantity_total(line.text)
        if weighted_columns is not None and joined:
            previous = joined[-1]
            previous_amount = amount_at_end(previous.text)
            if previous_amount is not None and _looks_like_item(previous.text):
                unit_price = float(weighted_columns.group("unit"))
                if abs(previous_amount[0] - unit_price) <= 0.02:
                    description = previous.text[: previous_amount[1]].strip(" :-")
                    joined[-1] = OcrLine(
                        f"{description} {line.text}",
                        min(previous.confidence, line.confidence),
                    )
                    continue
            if (
                amount_at_end(previous.text) is None
                and _looks_like_item(previous.text)
                and not _is_summary(previous.text)
            ):
                joined[-1] = OcrLine(
                    f"{previous.text} {line.text}",
                    min(previous.confidence, line.confidence),
                )
                continue
        quantity_row = _quantity_unit_total(line.text)
        if quantity_row and joined:
            previous = joined[-1]
            if (_looks_like_item(previous.text) and not _is_summary(previous.text)
                    and amount_at_end(previous.text) is None):
                name_line = joined.pop()
                if (joined and re.match(r"^\d{8,14}\s*[A-Za-z]", joined[-1].text)
                        and amount_at_end(joined[-1].text) is None
                        and not _is_summary(joined[-1].text)):
                    name_line = joined.pop()
                quantity, unit_price, line_total = quantity_row
                normalized_price_row = (
                    f"{quantity:g}@{unit_price:.2f} {line_total:.2f}"
                )
                line = OcrLine(
                    name_line.text + " " + normalized_price_row,
                    min(name_line.confidence, line.confidence),
                )
        joined.append(line)
    clean_lines = joined
    roles: dict[int, tuple[str, str]] = {}
    if vendor is not None:
        for index, line in enumerate(clean_lines):
            reason = vendor.metadata_reason(line.text)
            if reason is not None:
                roles[index] = ("item_metadata", reason)
    notes = [
        "Extracted locally with OCR and rule-based parsing; review before saving.",
        "OCR regions sharing a visual row were combined before receipt rules were applied.",
    ]

    merchant_alias = _merchant_alias(clean_lines)
    store_index = (
        _store_candidate(clean_lines)
        if merchant_alias is None and vendor is None
        else None
    )
    if merchant_alias is not None:
        store_name = merchant_alias[0]
        alias_index = merchant_alias[1]
        if amount_at_end(clean_lines[alias_index].text) is None:
            roles[alias_index] = ("store", "Matched distinctive merchant text")
        notes.append("Store name was inferred from distinctive merchant text.")
    elif vendor is not None:
        # The vendor profile recognized the store from its layout or a store
        # code although no line names it (for example a misread H-E-B logo).
        # Trust that over a guess from the garbled header text (RL-60).
        store_name = vendor.name
        notes.append(
            f"Store name {vendor.name} was inferred from the receipt layout; "
            "check it before saving."
        )
    elif store_index is not None:
        store_name = _restore_store_number_hash(
            clean_lines[store_index].text, clean_lines
        )
        roles[store_index] = ("store", "Best merchant-name candidate near the top")
    else:
        store_name = ""
        notes.append("Store name was not identified.")

    transaction_date: str | None = None
    date_candidate = _receipt_date_candidate(
        clean_lines, heb=store_name == "H-E-B"
    )
    if date_candidate is not None:
        transaction_date, date_index = date_candidate
        roles.setdefault(
            date_index,
            ("date", "Selected a transaction-date pattern and excluded expiry dates"),
        )
    if transaction_date is None:
        notes.append("Transaction date was not identified.")

    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    # The printed label ends the item section even when its amount is unreadable.
    summary_indexes: set[int] = {i for i, line in enumerate(clean_lines) if _is_summary(line.text)}

    for index, line in enumerate(clean_lines):
        amount = amount_at_end(line.text)
        amount_was_implied = False
        if amount is None and (_is_subtotal_label(line.text) or TAX_WORDS.search(line.text)):
            amount = _implied_cents_at_end(line.text)
            amount_was_implied = amount is not None
        if amount is None:
            continue
        value = amount[0]
        if _is_subtotal_label(line.text):
            summary_indexes.add(index)
            if AUXILIARY_SUBTOTAL_WORDS.search(line.text):
                roles[index] = (
                    "supplemental_subtotal",
                    "Ignored an FSA/HRA or other supplemental subtotal",
                )
                continue
            subtotal = value
            roles[index] = (
                "subtotal",
                "Matched the subtotal label"
                + (" and restored a missing decimal" if amount_was_implied else ""),
            )
        elif _is_tax_label(line.text):
            tax = round((tax or 0.0) + value, 2)
            summary_indexes.add(index)
            tax_decimal_note = (
                " and tentatively restored a missing decimal"
                if amount_was_implied
                else ""
            )
            roles[index] = (
                "tax",
                "Matched a tax line and added it to the receipt tax total"
                + tax_decimal_note,
            )
        elif TOTAL_WORDS.search(line.text) and not DISCOUNT_WORDS.search(line.text):
            total = value
            summary_indexes.add(index)
            roles[index] = ("total", "Matched a final-total label")

    # Walmart prints a wide gap between "TAX1 8.2500 %" and its amount, and OCR
    # can return that amount as its own row just above or below the label.
    if tax is None:
        split_tax = _split_row_tax(clean_lines, roles, subtotal)
        if split_tax is not None:
            label_index, amount_index, tax = split_tax
            summary_indexes.update((label_index, amount_index))
            roles[label_index] = ("tax", "Matched a tax label whose amount OCR read as a separate row")
            roles[amount_index] = ("tax", "Standalone amount next to a tax label; verify against the photo")

    # Some receipts lose the decorative total label but retain a standalone
    # decimal amount immediately after the summary block. Use it only when a
    # subtotal was found and the candidate is large enough to be the final total.
    if total is None and subtotal is not None and summary_indexes:
        search_start = max(summary_indexes) + 1
        for index in range(search_start, min(len(clean_lines), search_start + 4)):
            text = clean_lines[index].text
            amount = amount_at_end(text)
            is_payment_line = bool(PAYMENT_WORDS.search(text))
            if NON_ITEM_WORDS.search(text) and not is_payment_line:
                break
            if amount is None:
                continue
            if amount[0] + 0.02 < subtotal:
                continue
            total = amount[0]
            summary_indexes.add(index)
            if is_payment_line:
                roles[index] = (
                    "total",
                    "Tender amount after the receipt summary; verify against the photo",
                )
                notes.append("Final total was inferred from the payment tender amount.")
            else:
                roles[index] = (
                    "total",
                    "Standalone amount after the receipt summary; verify against the photo",
                )
                notes.append("Final total was inferred from an unlabeled summary amount.")
            break

    cutoff = min(summary_indexes) if summary_indexes else len(clean_lines)
    items: list[ReceiptItem] = []
    item_confidences: list[float] = []
    ordered_price_rows = 0
    expected_count = next(
        (
            count
            for line in clean_lines
            if (count := printed_item_count(line.text)) is not None
        ),
        None,
    )

    for index, line in enumerate(clean_lines[:cutoff]):
        if index == store_index or index in roles or _is_summary(line.text):
            continue
        if ORDERED_PRICE_LINE.fullmatch(line.text):
            ordered_price_rows += 1
            roles[index] = (
                "item_metadata",
                "Ordered-price estimate belongs to the preceding product, not a purchased item",
            )
            continue
        parsed_amount = amount_at_end(line.text)
        restored_decimal = False
        if parsed_amount is None:
            parsed_amount = _dollar_amount_missing_decimal(line.text)
            restored_decimal = parsed_amount is not None
        if parsed_amount is None or not _looks_like_item(line.text):
            continue

        line_total, amount_start = parsed_amount
        description = line.text[:amount_start].strip(" :-")
        description, tax_marker = _item_description(description, heb=store_name == "H-E-B")
        # Columnar receipts sometimes leave the currency symbol from the final
        # amount attached to the preceding unit-price/quantity columns.  Remove
        # that orphan before interpreting a trailing ``SKU unit qty`` suffix.
        description = re.sub(r"\s+\$\s*$", "", description).strip()
        product_code: str | None = None
        leading_code = PRODUCT_CODE_AT_START.fullmatch(description)
        if leading_code is not None:
            product_code = leading_code.group("code")
            description = leading_code.group("description").strip(" :-")
            # Ross OCR occasionally inserts a single barcode-adjacent letter
            # before the real description (for example ``N NT ...`` or
            # ``L UR ...``). It is not part of the printed product name.
            if store_name == "Ross Dress for Less":
                description = re.sub(
                    r"^[A-Z]\s+(?=[A-Z]{2,}\b)", "", description
                ).strip()
        weighted_suffix = UNIT_PRICE_QUANTITY_SUFFIX.search(description)
        weighted_columns = None
        if weighted_suffix is not None:
            candidate_unit = float(weighted_suffix.group("unit"))
            candidate_quantity = _quantity_value(weighted_suffix.group("quantity"))
            if abs((candidate_unit * candidate_quantity) - line_total) <= 0.02:
                weighted_columns = weighted_suffix
                product_code = weighted_suffix.group("code") or product_code
                description = description[: weighted_suffix.start()].strip(" :-")
                # Walmart multi-buy normalization places its F/T marker before
                # the recovered UPC/unit/quantity suffix.
                if tax_marker is None:
                    description, tax_marker = _item_description(
                        description, heb=store_name == "H-E-B"
                    )
        if line in code_row_codes:
            product_code = code_row_codes[line]
        if weighted_columns is None:
            product_code_match = PRODUCT_CODE_AT_END.search(description)
            if product_code_match:
                product_code = product_code_match.group("code")
                description = description[: product_code_match.start()].strip(" :-")
        after_price = re.search(r"\b(FW|F|T)\s*$", line.text[amount_start:], re.IGNORECASE)
        if tax_marker is None and after_price:
            tax_marker = after_price.group(1).upper()
        # Walmart prints its tax status after the price: X = taxed, N = not
        # taxed.  The F before the price marks food-benefit eligibility, not
        # tax, so the trailing X/N wins when it is present.
        walmart_tax = _walmart_tax_flag(line.text[amount_start:], store_name)
        if walmart_tax is not None:
            tax_marker = walmart_tax


        if re.search(r"\b(?:SAVED|SAVINGS?|PRICE\s+CUT)\b", description, re.IGNORECASE):
            continue

        quantity_detail = QUANTITY_DETAIL_ROW.fullmatch(description)
        if quantity_detail is not None and items:
            detail_quantity = float(quantity_detail.group("quantity"))
            previous = items[-1]
            if (
                detail_quantity >= 2
                and previous.quantity == 1.0
                and previous.discount is None
                and abs(detail_quantity * line_total - previous.line_total) <= 0.02
            ):
                items[-1] = previous.model_copy(
                    update={"quantity": detail_quantity, "unit_price": line_total}
                )
                roles[index] = (
                    "item_metadata",
                    "Quantity and unit price for the previous item, not a purchased item",
                )
                continue

        if DISCOUNT_WORDS.search(description) and items:
            discount = abs(line_total)
            previous = items[-1]
            items[-1] = previous.model_copy(
                update={
                    "discount": round((previous.discount or 0) + discount, 2),
                    "line_total": round(previous.line_total - discount, 2),
                }
            )
            roles[index] = ("discount", "Applied this discount to the previous item")
            item_confidences.append(line.confidence)
            continue

        quantity = 1.0
        unit_price: float | None = line_total
        if weighted_columns is not None:
            quantity = _quantity_value(weighted_columns.group("quantity"))
            unit_price = float(weighted_columns.group("unit"))
        weight_match = re.match(r"(?P<qty>\d+(?:\.\d+)?)\s*(?:LBS?|KG)\b", description, re.IGNORECASE)
        if weighted_columns is None and weight_match and index > 0:
            previous_line = clean_lines[index - 1]
            previous_text = previous_line.text.strip()
            if (amount_at_end(previous_text) is None and not _is_summary(previous_text)
                    and not NON_ITEM_WORDS.search(previous_text) and index - 1 != store_index
                    and any(c.isalpha() for c in previous_text)):
                quantity = float(weight_match.group("qty"))
                weight_price = re.search(r"(\d+\.\d{2})\s*$", description)
                unit_price = float(weight_price.group(1)) if weight_price else None
                description = previous_text
                roles[index - 1] = ("item_description", "Description continued on the weighted price row")
        quantity_match = QUANTITY_AND_PRICE.search(description)
        if weighted_columns is not None:
            pass
        elif quantity_match:
            quantity = float(quantity_match.group("quantity"))
            unit_price = float(quantity_match.group("unit"))
            description = description[: quantity_match.start()].strip(" :-")
        else:
            weight_match = WEIGHT_DETAILS.search(description)
            if weight_match:
                quantity = float(weight_match.group("quantity"))
                unit_price = float(weight_match.group("unit"))
                previous_index = index - 1
                if (
                    previous_index >= 0
                    and previous_index not in roles
                    and amount_at_end(clean_lines[previous_index].text) is None
                    and _looks_like_item(clean_lines[previous_index].text)
                ):
                    description = clean_lines[previous_index].text.strip(" :-")
                    roles[previous_index] = (
                        "item name",
                        "Name continued by weighted-price details on the next row",
                    )

        row_number = re.match(r"^(\d{1,3})[.)]?\s+", description)
        numbered_heb_row = bool(
            row_number
            and store_name == "H-E-B"
            and expected_count is not None
            and 10 <= int(row_number.group(1)) <= expected_count
        )
        if row_number and (
            int(row_number.group(1)) == len(items) + 1 or numbered_heb_row
        ):
            description = description[row_number.end():]
        elif (
            quantity == 1.0
            and weighted_columns is None
            and "$" in line.text[amount_start:]
            and (leading_quantity := LEADING_QUANTITY.match(description))
        ):
            # Restaurant tickets print the ordered quantity before the name
            # and the charged line total after it.
            quantity = float(leading_quantity.group("quantity"))
            unit_price = round(line_total / quantity, 2)
            description = description[leading_quantity.end():]
        description = re.sub(r"^\d{8,14}\s*(?=[A-Za-z])", "", description).strip()
        description = re.sub(r"^\d{5,}\s+", "", description).strip()
        description = re.sub(r"\s*<A>\s*$", "", description).strip()
        if store_name == "Walmart" and product_code is not None:
            # A lone letter between a Walmart name and its UPC is a stray OCR
            # mark ("FRENCH BREAD L 200989000000"), not part of the name.
            description = re.sub(
                r"^(\S+(?:\s+\S+)+?)\s+[A-Za-z]$", r"\1", description
            ).strip()
        if not description:
            continue

        items.append(
            ReceiptItem(
                tax_marker=tax_marker,
                name=description,
                product_code=product_code,
                quantity=quantity,
                unit_price=unit_price,
                discount=None,
                line_total=line_total,
            )
        )
        item_confidences.append(line.confidence)
        roles[index] = (
            "item",
            "Reconciled unit price multiplied by quantity to the charged line total"
            if weighted_columns is not None
            else "Text followed by a monetary amount before the totals"
            + (" and restored a missing decimal" if restored_decimal else ""),
        )
        name_line = line
        next_index = index + 1
        while (
            next_index < cutoff
            and next_index not in roles
            and _continues_item_name(line, name_line, clean_lines[next_index])
            # A name line followed by its own quantity/weight row is the next
            # product, not the rest of this one.
            and not (
                next_index + 1 < len(clean_lines)
                and _is_price_detail_row(clean_lines[next_index + 1].text)
            )
        ):
            name_line = clean_lines[next_index]
            items[-1] = items[-1].model_copy(
                update={"name": f"{items[-1].name} {name_line.text.strip()}"}
            )
            roles[next_index] = (
                "item_description",
                "Name continued on the line below the priced row",
            )
            next_index += 1

    if ordered_price_rows:
        notes.append(
            f"Ignored {ordered_price_rows} ordered-price estimate "
            f"{'line' if ordered_price_rows == 1 else 'lines'}; actual charged prices "
            "remain attached to their products."
        )

    # Service receipts often print gratuity after the payment block. Preserve
    # an explicitly charged tip as part of the expense, but keep it distinct
    # from the merchant's service/merchandise subtotal.
    gratuity_rows = 0
    if not any(is_gratuity_item_name(item.name) for item in items):
        for index, line in enumerate(clean_lines):
            if index in roles or not is_gratuity_item_name(line.text):
                continue
            parsed_amount = amount_at_end(line.text)
            if parsed_amount is None:
                continue
            line_total, amount_start = parsed_amount
            description = line.text[:amount_start].strip(" :-")
            if not description:
                description = "Tip / gratuity"
            items.append(
                ReceiptItem(
                    name=description,
                    quantity=1.0,
                    unit_price=line_total,
                    discount=None,
                    line_total=line_total,
                )
            )
            item_confidences.append(line.confidence)
            roles[index] = (
                "gratuity",
                "Explicitly charged tip preserved separately from the printed subtotal",
            )
            gratuity_rows += 1
    if gratuity_rows:
        notes.append(
            "A charged tip or gratuity was preserved as a separate expense row and "
            "excluded from the printed merchandise/service subtotal comparison."
        )

    purchased_items = [
        item for item in items if not is_gratuity_item_name(item.name)
    ]
    missing_item_candidates: list[MissingItemCandidate] = []
    subtotal_shortfall = (
        subtotal is not None
        and subtotal - sum(item.line_total for item in purchased_items) > 0.02
    )
    purchased_units = purchased_unit_count(item.quantity for item in purchased_items)
    count_short = expected_count is not None and not printed_count_matches(
        expected_count, len(purchased_items), purchased_units
    ) and expected_count > len(purchased_items)
    if count_short or (expected_count is None and subtotal_shortfall):
        item_indexes = [index for index, role in roles.items() if role[0] == "item"]
        # A printed count makes it safe to inspect unused rows throughout the
        # purchase section. This recovers an unpriced product between valid
        # items instead of looking only after the final parsed item.
        search_start = (
            0
            if expected_count is not None
            else max(item_indexes) + 1 if item_indexes else 0
        )
        shortfall = (
            expected_count - max(len(purchased_items), purchased_units)
            if expected_count is not None
            else len(clean_lines)
        )
        for index in range(search_start, cutoff):
            if index in roles:
                continue
            line = clean_lines[index]
            text = line.text.strip(" :-")
            text = re.sub(r"^\d{1,3}[.)]?\s+(?=[A-Za-z])", "", text)
            text = re.sub(r"^\d{5,}\s+", "", text).strip()
            text, candidate_marker = _item_description(text, heb=store_name == "H-E-B")
            words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
            if (
                amount_at_end(text) is not None
                or len(words) < 2
                or sum(len(word) for word in words) < 7
                or NON_ITEM_WORDS.search(text)
                or PHONE_OR_WEB.search(text)
                or STREET_ADDRESS.fullmatch(line.text.strip())
                or CITY_STATE_ZIP.search(text)
                or _is_column_header(text)
                or _is_summary(text)
                or date_from_text(text)
                or WEIGHT_DETAILS.search(text)
            ):
                continue
            supporting_text = ""
            quantity = 1.0
            unit_price: float | None = None
            line_total: float | None = None
            tax_marker: str | None = (
                _walmart_tax_flag(text, store_name) or candidate_marker
            )
            support_index = index + 1
            if support_index < cutoff and support_index not in roles:
                support = clean_lines[support_index].text.strip()
                # A complete detached tax/price field can be offered for review.
                # Bare amounts remain unassigned: they may belong to another row.
                price_field = re.fullmatch(r"(FW|F|T)\s+\$?(\d+\.\d{2})", support, re.IGNORECASE)
                if price_field:
                    supporting_text = support
                    tax_marker = price_field.group(1).upper()
                    line_total = unit_price = float(price_field.group(2))
                    roles[support_index] = ("missing_item_support", "Tax and price field offered for review with the preceding candidate")
                elif re.search(r"\b(?:LBS?|KG)\b|@", support, re.IGNORECASE):
                    supporting_text = support
                    weight = WEIGHT_DETAILS.search(support)
                    if weight:
                        quantity = float(weight.group("quantity"))
                        unit_price = float(weight.group("unit"))
                    amounts = re.findall(
                        r"(?<!\d)(\d+\.\d{2})(?!\d)", support
                    )
                    if amounts:
                        line_total = float(amounts[-1])
                    marker = re.search(
                        r"\b(FW|F|T)\b", support, re.IGNORECASE
                    )
                    tax_marker = marker.group(1).upper() if marker else None
                    roles[support_index] = (
                        "missing_item_support",
                        "Weight or price details linked to the preceding candidate",
                    )

            missing_item_candidates.append(
                MissingItemCandidate(
                    name=text,
                    confidence=line.confidence,
                    source_line=index + 1,
                    supporting_text=supporting_text,
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                    tax_marker=tax_marker,
                )
            )
            roles[index] = (
                "missing_item_candidate",
                "Product-like text revisited because the printed count or subtotal indicates missing items",
            )
            if len(missing_item_candidates) >= shortfall:
                break

        if missing_item_candidates:
            notes.append(
                "A second pass found product-like text for a missing row; its price "
                "was not guessed and must be verified."
            )

    item_sum = round(sum(item.line_total for item in purchased_items), 2)
    gratuity_total = round(
        sum(
            item.line_total
            for item in items
            if is_gratuity_item_name(item.name)
        ),
        2,
    )
    mismatch = False
    if subtotal is None and items:
        subtotal = item_sum
        notes.append("Subtotal was inferred from the item lines.")
    elif subtotal is not None and items and abs(subtotal - item_sum) > 0.02:
        mismatch = True
        notes.append(
            f"Item lines add to {item_sum:.2f}, which does not match the printed "
            f"subtotal of {subtotal:.2f}."
        )

    if total is None and subtotal is not None and tax is not None:
        total = round(subtotal + tax + gratuity_total, 2)
        notes.append("Total was inferred from subtotal, tax, and gratuity.")
    elif total is None:
        notes.append("Final total was not identified.")

    if total is not None and subtotal is not None and tax is None:
        if abs(total - (subtotal + gratuity_total)) <= 0.02:
            tax = 0.0
            if gratuity_total:
                notes.append(
                    "No sales-tax line was printed; zero tax was inferred after "
                    "separating the charged gratuity."
                )
            else:
                notes.append(
                    "No sales-tax line was printed; zero tax was inferred because "
                    "the total matches the subtotal."
                )

    if total is not None and subtotal is not None and tax is not None:
        expected_total = round(subtotal + tax + gratuity_total, 2)
        if abs(total - expected_total) > 0.02:
            inferred_tax = round(total - subtotal - gratuity_total, 2)
            if inferred_tax >= 0 and inferred_tax <= subtotal:
                tax = inferred_tax
                notes.append(
                    "Sales-tax OCR was inconsistent; tax was inferred as total minus subtotal."
                )
            else:
                mismatch = True
                notes.append(
                    f"Subtotal plus tax is {expected_total:.2f}, which does not match "
                    f"the printed total of {total:.2f}."
                )

    if not items:
        notes.append("No item lines were identified.")
    if not printed_count_matches(expected_count, len(purchased_items), purchased_units):
        notes.append(
            f"Receipt lists {expected_count} items; {len(purchased_items)} purchase rows "
            f"({purchased_units} units) were identified. Check for missing or combined rows."
        )

    ocr_confidence = mean(line.confidence for line in clean_lines) if clean_lines else 0.0
    completeness = mean(
        [bool(store_name), bool(transaction_date), bool(items), total is not None]
    )
    confidence = (ocr_confidence * 0.65) + (completeness * 0.35)
    if mismatch:
        confidence -= 0.12
    confidence = round(max(0.0, min(0.99, confidence)), 2)

    decisions = tuple(
        LineDecision(
            index=index + 1,
            text=line.text,
            confidence=line.confidence,
            role=roles.get(index, ("ignored", "No receipt rule matched this line"))[0],
            reason=roles.get(index, ("ignored", "No receipt rule matched this line"))[1],
        )
        for index, line in enumerate(clean_lines)
    )

    receipt = ReceiptExtraction(
        store_name=store_name,
        transaction_date=transaction_date,
        currency="USD",
        items=items,
        subtotal=subtotal,
        tax=tax,
        total=total,
        confidence=confidence,
        notes=notes,
    )
    return ParseResult(
        receipt=receipt,
        decisions=decisions,
        missing_item_candidates=tuple(missing_item_candidates),
    )
