"""Run every receipt in tests/receipt_library against the parser.

Reviewed fixtures must pass every check written in their expected.json.
Drafts (``"reviewed": false``) and fixtures with a ``known_issue`` are
reported by scripts/score_receipts.py but do not fail CI.
"""

import json
import re

import pytest

from receipt_reader.receipt_library import (
    LIBRARY_ROOT,
    PAYMENT_BRAND,
    discover_fixtures,
    mask_payment_brands,
    ocr_lines_from_json,
    score_fixture,
)

FIXTURES = discover_fixtures()


def test_library_has_at_least_one_reviewed_receipt():
    assert any(fixture.reviewed for fixture in FIXTURES)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
def test_reviewed_receipt_reads_correctly(fixture):
    if not fixture.reviewed:
        pytest.skip("draft fixture: expected.json not reviewed yet")
    if fixture.known_issue:
        pytest.xfail(fixture.known_issue)
    score = score_fixture(fixture)
    assert score.passed, {check: score.details.get(check, "") for check in score.failed}


# Card or account numbers: 13-19 digits in one run, or printed in card-style
# groups (4-4-4-4 or Amex 4-6-5). A 12-digit UPC followed by a price
# ("889698929570 14.88", Walmart) is a product code, not a card number.
CARD_NUMBER = re.compile(
    r"(?<!\d)(?:\d{13,19}|\d{4}(?:[ -]\d{4}){3}|\d{4}[ -]\d{6}[ -]\d{5})(?!\d)"
)


def _personal_data_found(text: str) -> bool:
    return bool(
        CARD_NUMBER.search(text)
        or re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text)
        or re.search(r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}", text)
        or PAYMENT_BRAND.search(text)
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
def test_fixture_contains_no_obvious_personal_data(fixture):
    text = (fixture.path / "ocr.json").read_text(encoding="utf-8")
    text += (fixture.path / "expected.json").read_text(encoding="utf-8")
    assert not _personal_data_found(text)


@pytest.mark.parametrize(
    "text",
    [
        "VISA 4111 1111 1111 1111",
        "CARD 4111-1111-1111-1111",
        "ACCT 4111111111111111",
        "AMEX 3782 822463 10005",
        "TC# 0706121750107",
        "call 210-555-0100",
        "person@example.com",
        "MASTER CARD (CAPITAL ONE) -56.17",
        "AMEX 64.84",
        "Apple Pay 12.00",
    ],
)
def test_privacy_check_flags_card_numbers_phones_and_emails(text):
    assert _personal_data_found(text)


@pytest.mark.parametrize(
    "text",
    [
        "SITE MERCH 889698929570 14.88 X",  # Walmart: 12-digit UPC, then price
        "FRENCH BREAD 200989000000 1 47 N",
        "XXXX XXXX 1025 2616 XXXX XXX",  # H-E-B barcode, date digits kept
        "10/11/26 13:37:41",
    ],
)
def test_privacy_check_allows_product_codes_next_to_prices(text):
    assert not _personal_data_found(text)


def test_diagnostic_export_format_is_readable():
    payload = {
        "format": "talliupper-ocr-diagnostic-v1",
        "ocr_passes": {
            "standard": {"lines": [{"text": "OLD", "confidence": 0.5, "box": None}]},
            "merged": {"lines": [
                {"text": "H-E-B", "confidence": 0.9, "box": [1, 2, 30, 12]},
                {"text": "  ", "confidence": 0.9, "box": None},
            ]},
        },
    }
    lines = ocr_lines_from_json(payload)
    assert [line.text for line in lines] == ["H-E-B"]
    assert lines[0].box == (1, 2, 30, 12)


def test_score_reports_what_is_wrong(tmp_path):
    folder = tmp_path / "heb" / "broken"
    folder.mkdir(parents=True)
    source = LIBRARY_ROOT / "heb" / "2026-10-25-four-items"
    (folder / "ocr.json").write_text((source / "ocr.json").read_text(encoding="utf-8"))
    expected = json.loads((source / "expected.json").read_text(encoding="utf-8"))
    expected["items"][0]["quantity"] = 4
    expected["total"] = 99.99
    (folder / "expected.json").write_text(json.dumps(expected))

    from receipt_reader.receipt_library import load_fixture

    score = score_fixture(load_fixture(folder))
    assert set(score.failed) == {"quantities", "total"}
    assert "QUANTUM" in score.details["quantities"]


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("MASTER CARD (CAPITAL ONE) -56.17", "CREDIT CARD -56.17"),
        ("Mastercard Credit Card 47.06", "CREDIT CARD 47.06"),
        ("AMEX 64.84", "CREDIT CARD 64.84"),
        ("WMP MASTERCARD CREDIT TEND 133.39", "WMP CREDIT CARD TEND 133.39"),
        ("Visa Debit 9.99", "CREDIT CARD Debit 9.99"),
        ("Bank Card $11.89", "Bank Card $11.89"),
        ("SD CARD READER $5.00", "SD CARD READER $5.00"),
    ],
)
def test_payment_brands_are_masked_for_library_fixtures(text, masked):
    assert mask_payment_brands(text) == masked
    assert not PAYMENT_BRAND.search(mask_payment_brands(text))
