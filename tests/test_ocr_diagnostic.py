import json

import pytest

from receipt_reader.local_ocr import OcrLine, OcrResult
from receipt_reader.ocr_diagnostic import OcrDiagnosticTrace, build_ocr_diagnostic, redact_text
from receipt_reader.parser import parse_receipt


def test_standard_diagnostic_preserves_boxes_decisions_and_redacts() -> None:
    lines = (
        OcrLine("SHOP 555-222-1234", .91, (1, 2, 30, 15)),
        OcrLine("MILK 2.00", .88, (1, 18, 60, 31)),
        OcrLine("VISA 4111 1111 1111 1111", .95, (1, 34, 80, 47)),
    )
    parsed = parse_receipt(lines)
    trace = OcrDiagnosticTrace(
        image_dimensions=[(800, 1200)], standard=OcrResult(lines, "test"),
        merged=OcrResult(lines, "test"), merged_origins=[{"origin": "standard"}] * 3,
        stage="complete",
    )
    document = build_ocr_diagnostic(trace, parsed=parsed, error="contact a@b.com")
    standard = document["ocr_passes"]["standard"]["lines"]
    assert standard[0]["box"] == (1, 2, 30, 15)
    assert "555-222-1234" not in str(document)
    assert "4111 1111" not in str(document)
    assert "a@b.com" not in str(document)
    assert document["parser"]["classifications"]
    assert document["parser"]["ignored_lines"]


def test_enhanced_diagnostic_keeps_passes_and_merge_provenance() -> None:
    standard = OcrResult((OcrLine("EGGS 2.00", .6, (1, 1, 40, 12)),), "standard")
    enhanced = OcrResult((OcrLine("EGGS LARGE 2.00", .9, (1, 1, 60, 12)),), "enhanced")
    trace = OcrDiagnosticTrace(
        standard=standard, enhanced=enhanced, enhanced_triggered=True,
        enhanced_reason="identified items below printed count", merged=enhanced,
        merged_origins=[{"origin": "enhanced_substitution", "standard_line": 1, "enhanced_line": 1, "discarded_text": "VISA 4111 1111 1111 1111"}], stage="complete",
    )
    document = build_ocr_diagnostic(trace, parsed=parse_receipt(enhanced.lines))
    assert document["metadata"]["enhanced_ocr"]["triggered"] is True
    assert document["ocr_passes"]["enhanced"]["lines"][0]["box"] == (1, 1, 60, 12)
    assert document["ocr_passes"]["merged"]["origins"][0]["origin"] == "enhanced_substitution"
    assert "4111 1111" not in str(document["ocr_passes"]["merged"]["origins"])


def test_failure_before_parsing_is_exportable_and_redacted() -> None:
    trace = OcrDiagnosticTrace(stage="failed:standard_ocr")
    document = build_ocr_diagnostic(trace, error="payment 4111111111111111 email person@example.com")
    assert document["parser"] is None
    assert document["ocr_passes"]["standard"] is None
    assert "4111111111111111" not in document["error"]
    assert "person@example.com" not in document["error"]


# RL-72: payment details are masked in full, while item lines, prices, tax and
# totals stay readable for parser fixes.
@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("AMEX 64.84", "CREDIT CARD 64.84"),
        ("MASTER CARD (CAPITAL ONE) -56.17", "CREDIT CARD -56.17"),
        ("Visa Debit 9.99", "CREDIT CARD Debit 9.99"),
        ("APPLE PAY 12.00", "CREDIT CARD 12.00"),
        ("XXXXXXXXXXX2732", "[REDACTED PAYMENT NUMBER]"),
        ("************1234", "[REDACTED PAYMENT NUMBER]"),
        ("xxxx-xxxx-xxxx-1234", "[REDACTED PAYMENT NUMBER]"),
        ("ACCT # 1234", "ACCT [REDACTED PAYMENT NUMBER]"),
        ("Card No: ...5678", "Card [REDACTED PAYMENT NUMBER]"),
        ("Ending in 9012", "Ending in [REDACTED PAYMENT NUMBER]"),
        ("Auth: 929022", "Auth [REDACTED PAYMENT CODE]"),
        ("AUTH CODE 01234Z", "AUTH CODE [REDACTED PAYMENT CODE]"),
        ("APPROVAL # 0A12BC", "APPROVAL [REDACTED PAYMENT CODE]"),
        ("AID: A0000000031010", "AID [REDACTED PAYMENT CODE]"),
        ("TC: 9F26A1B2C3D4E5F6", "TC [REDACTED PAYMENT CODE]"),
        ("REF # 004512", "REF # [REDACTED PAYMENT CODE]"),
        ("TERMINAL ID 77123401", "TERMINAL ID [REDACTED PAYMENT CODE]"),
        ("MERCHANT ID: 4455667788", "MERCHANT ID [REDACTED PAYMENT CODE]"),
        # From a real H-E-B diagnostic (RL-72 Dell check).
        ("MASTERCD", "CREDIT CARD"),
        ("IAD :0110A04001220000000000000000000000FF", "IAD [REDACTED PAYMENT CODE]"),
        ("TSI : ARC :00", "TSI : ARC [REDACTED PAYMENT CODE]"),
        ("0195 0021 0689 11-03-26 12:56A", "0195 0021 0689 11-03-26 12:56A"),
        ("4111 1111 1111 1111 10/22/2026", "[REDACTED PAYMENT NUMBER] 10/22/2026"),
    ],
)
def test_payment_details_are_fully_masked(text, masked) -> None:
    assert redact_text(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "SmallRig Sling Handle for DJI",
        "601-10171 1 59.90 59.90",
        "Subtotal 59.90",
        "Tax 4.94",
        "Total 64.84",
        "APPROVED",
        "AUTHORIZED SIGNATURE",
        "SD CARD READER $5.00",
        "Bank Card $11.89",
        "TC 2X4 8FT 6.98",
        "10/22/2026 5:15:07 PM",
        "Item Qty Price Total",
        "RECEIPT EXPIRES ON 01-24-27",
        "11/03/26 12:56 PM",
        "Mode: Issuer",
    ],
)
def test_item_price_and_total_lines_are_not_masked(text) -> None:
    assert redact_text(text) == text


def test_card_paid_receipt_export_has_only_placeholders_for_payment() -> None:
    lines = tuple(
        OcrLine(text, .9, (1, 10 * index, 120, 10 * index + 8))
        for index, text in enumerate(
            [
                "CAMERA SHOP",
                "601-10171 1 59.90 59.90",
                "SmallRig Sling Handle",
                "Subtotal 59.90",
                "Tax 4.94",
                "Total 64.84",
                "Tender:",
                "AMEX 64.84",
                "XXXXXXXXXXX2732",
                "Auth: 929022",
                "Chase Sapphire",
            ]
        )
    )
    parsed = parse_receipt(lines)
    trace = OcrDiagnosticTrace(
        standard=OcrResult(lines, "test"), merged=OcrResult(lines, "test"),
        merged_origins=[{"origin": "standard", "discarded_text": "VISA ****4321"}], stage="complete",
    )
    exported = json.dumps(build_ocr_diagnostic(trace, parsed=parsed, error="declined AMEX XXXX2732"))
    for secret in ("AMEX", "2732", "929022", "Chase", "VISA", "4321"):
        assert secret.lower() not in exported.lower()
    for kept in ("59.90", "4.94", "64.84", "SmallRig Sling Handle"):
        assert kept in exported
    assert "CREDIT CARD 64.84" in exported
