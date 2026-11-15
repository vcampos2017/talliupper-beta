"""Human-review helpers that do not make destructive receipt decisions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
import math
import re
from typing import Any


GRATUITY_WORDS = re.compile(r"\bGRATUITY\b", re.IGNORECASE)
TIP_CHARGE_WORDS = re.compile(
    r"^(?:CHARGED\s+)?TIP\b|\bTIP\s*(?:/|:|-)\s*(?:GRATUITY)?\b",
    re.IGNORECASE,
)
NON_CHARGED_GRATUITY_WORDS = re.compile(
    r"\b(?:SUGGESTED|RECOMMENDED|OPTION|GUIDE|PERCENT|%)\b",
    re.IGNORECASE,
)


def is_gratuity_item_name(value: object) -> bool:
    """Return whether a reviewed charge row is an actual tip or gratuity.

    Suggested-tip guides are deliberately excluded. The helper is shared by
    parsing, review arithmetic, and printable reports so a tip remains part of
    the final expense without being mistaken for merchandise/service subtotal.
    """

    text = str(value or "").strip()
    return bool(
        text
        and (GRATUITY_WORDS.search(text) or TIP_CHARGE_WORDS.search(text))
        and not NON_CHARGED_GRATUITY_WORDS.search(text)
    )


AMOUNT_TOLERANCE = 0.02


def amount_check_status(*differences: float) -> str:
    """Classify review differences as ``match``, ``near`` or ``differ``.

    A one- or two-cent difference is within rounding tolerance, so it does not
    need acknowledging, but it is also how a misread tax digit shows up (RL-43:
    Rockler's three tax lines read as $0.89 instead of $0.90). Report it as
    ``near`` so the page can ask for a quick check instead of a plain match.
    """

    rounded = [abs(round(float(value), 2)) for value in differences]
    if any(value > AMOUNT_TOLERANCE for value in rounded):
        return "differ"
    if any(value > 0 for value in rounded):
        return "near"
    return "match"


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _name_identity(value: object) -> str:
    """Normalize punctuation and word order for conservative duplicate matching."""
    return " ".join(sorted(re.findall(r"[a-z0-9]+", _clean_text(value))))


def _number(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def potential_duplicate_labels(
    items: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Label matching purchased rows for review without deleting any of them.

    A printed product code is preferred when available. Otherwise a
    punctuation- and word-order-insensitive item name is used. Quantity and
    line price must also match, keeping the comparison conservative.
    """

    signatures: list[tuple[str, str, float, float]] = []
    positions: dict[tuple[str, str, float, float], list[int]] = defaultdict(list)

    for index, item in enumerate(items):
        product_code = re.sub(
            r"\s+", "", str(item.get("Product code") or "")
        ).casefold()
        item_name = _name_identity(item.get("Item"))
        identity_kind = "code" if product_code else "name"
        identity = product_code or item_name
        signature = (
            identity_kind,
            identity,
            round(_number(item.get("Qty"), 1.0), 3),
            round(_number(item.get("Line total"), 0.0), 2),
        )
        signatures.append(signature)
        if identity:
            positions[signature].append(index)

    labels = [""] * len(items)
    group_number = 0
    for signature in signatures:
        matching_positions = positions[signature]
        if len(matching_positions) < 2 or labels[matching_positions[0]]:
            continue
        group_number += 1
        label = f"Possible duplicate {group_number} ({len(matching_positions)} matching)"
        for position in matching_positions:
            labels[position] = label

    return labels


def _split_reason(previous: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    previous_name = _clean_text(previous.get("Item"))
    current_name = _clean_text(current.get("Item"))
    if not previous_name or not current_name:
        return ""

    if re.match(r"^(?:fl\s+)?oz\b|^lbs?\b|^package\b", current_name):
        return "description appears to continue the prior receipt row"

    if (
        re.search(r"\b(?:boneless|skinless)\s*,?\s*$", previous_name)
        and re.search(r"\b(?:chicken|breast)\b", current_name)
    ):
        return "product name appears split across two rows"

    previous_total = _number(previous.get("Line total"), 0.0)
    current_total = _number(current.get("Line total"), 0.0)
    if previous_total > 0 and current_total > 0:
        ratio = previous_total / current_total
        multiple = round(ratio)
        current_words = re.findall(r"[a-z]+", current_name)
        shared_words = set(re.findall(r"[a-z]+", previous_name)) & set(current_words)
        short_label = len(current_words) <= 3
        if 2 <= multiple <= 10 and abs(ratio - multiple) <= 0.01 and (
            short_label or shared_words
        ):
            return f"{_money(current_total)} × {multiple} equals prior line price {_money(previous_total)}"

    return ""


def _money(value: float) -> str:
    return "$" + f"{value:.2f}"


def possible_split_labels(
    items: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Flag likely continuation rows while leaving every row untouched."""
    labels = [""] * len(items)
    ordered_indexes = sorted(
        range(len(items)),
        key=lambda index: (
            _number(items[index].get("Receipt order"), float(index + 1)),
            index,
        ),
    )
    for position in range(1, len(ordered_indexes)):
        previous_index = ordered_indexes[position - 1]
        current_index = ordered_indexes[position]
        reason = _split_reason(items[previous_index], items[current_index])
        if not reason:
            current_name = _clean_text(items[current_index].get("Item"))
            if re.search(r"\b(?:chicken|breast)\b", current_name):
                for earlier_position in range(
                    position - 2, max(-1, position - 9), -1
                ):
                    candidate_index = ordered_indexes[earlier_position]
                    candidate_name = _clean_text(
                        items[candidate_index].get("Item")
                    )
                    if re.search(
                        r"\b(?:boneless|skinless)\s*,?\s*$", candidate_name
                    ):
                        previous_index = candidate_index
                        reason = "product name appears split across non-adjacent rows"
                        break
        if reason:
            previous_order = int(
                _number(items[previous_index].get("Receipt order"), previous_index + 1)
            )
            labels[current_index] = (
                f"Possible split from receipt row {previous_order}: {reason}"
            )
    return labels


def compact_review_labels(
    items: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Combine duplicate and split suggestions into short table labels."""
    duplicate_labels = potential_duplicate_labels(items)
    split_labels = possible_split_labels(items)
    labels: list[str] = []
    for duplicate_label, split_label in zip(duplicate_labels, split_labels):
        parts: list[str] = []
        duplicate_match = re.fullmatch(
            r"Possible duplicate (\d+) \((\d+) matching\)", duplicate_label
        )
        if duplicate_match:
            parts.append(
                f"Duplicate {duplicate_match.group(1)} "
                f"({duplicate_match.group(2)} rows)"
            )
        elif duplicate_label:
            parts.append(duplicate_label)

        split_match = re.match(
            r"Possible split from receipt row (\d+):", split_label
        )
        if split_match:
            parts.append(f"Merge? row {split_match.group(1)}")
        elif split_label:
            parts.append(split_label)
        labels.append("; ".join(parts))
    return labels


def _is_checked(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def assign_checked_review_merges(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Turn simple Merge checkboxes into explicit, reviewable merge targets.

    A detected split uses its suggested receipt row. For a duplicate group, the
    checked row is absorbed by an unchecked matching row. If every matching row
    is checked, the earliest row is retained as the target. Nothing is inferred
    for a row that has no duplicate or split relationship.
    """
    rows = [dict(item) for item in items]
    duplicate_labels = potential_duplicate_labels(rows)
    split_labels = possible_split_labels(rows)
    selected = {
        index for index, row in enumerate(rows) if _is_checked(row.get("Merge"))
    }
    for index in selected:
        if _is_checked(rows[index].get("Delete")):
            receipt_order = int(
                _number(rows[index].get("Receipt order"), float(index + 1))
            )
            raise ValueError(
                f"Receipt row {receipt_order} cannot be marked for both Delete and Merge."
            )

    for row in rows:
        row["Merge into row"] = None
        row["Merge mode"] = None

    handled: set[int] = set()
    for index in sorted(selected):
        split_match = re.match(
            r"Possible split from receipt row (\d+):", split_labels[index]
        )
        if not split_match:
            continue
        rows[index]["Merge into row"] = int(split_match.group(1))
        rows[index]["Merge mode"] = "split"
        handled.add(index)

    groups: dict[str, list[int]] = defaultdict(list)
    for index, label in enumerate(duplicate_labels):
        if label:
            groups[label].append(index)
    for positions in groups.values():
        sources = [
            index for index in positions if index in selected and index not in handled
        ]
        if not sources:
            continue
        target_candidates = [index for index in positions if index not in sources]
        if target_candidates:
            target_index = min(
                target_candidates,
                key=lambda index: _number(
                    rows[index].get("Receipt order"), float(index + 1)
                ),
            )
        else:
            target_index = min(
                sources,
                key=lambda index: _number(
                    rows[index].get("Receipt order"), float(index + 1)
                ),
            )
            sources.remove(target_index)
            rows[target_index]["Merge"] = False
            handled.add(target_index)
        target_order = int(
            _number(
                rows[target_index].get("Receipt order"), float(target_index + 1)
            )
        )
        for source_index in sources:
            rows[source_index]["Merge into row"] = target_order
            rows[source_index]["Merge mode"] = "duplicate"
            handled.add(source_index)

    unresolved = selected - handled
    if unresolved:
        receipt_rows = sorted(
            int(_number(rows[index].get("Receipt order"), float(index + 1)))
            for index in unresolved
        )
        joined = ", ".join(str(value) for value in receipt_rows)
        raise ValueError(
            f"Receipt row{'s' if len(receipt_rows) != 1 else ''} {joined} "
            "has no suggested duplicate or split-item match."
        )
    return rows


def _merged_name(previous_name: object, current_name: object) -> str:
    previous = re.sub(r"\s+", " ", str(previous_name or "")).strip(" ,")
    current = re.sub(r"\s+", " ", str(current_name or "")).strip(" ,")
    if not current or _clean_text(current) in _clean_text(previous):
        return previous
    alphabetic_words = re.findall(r"[A-Za-z]+", current)
    if current == current.upper() and len(alphabetic_words) <= 2:
        return f"{current} {previous}".strip()
    return f"{previous} {current}".strip()


def _merge_target(value: object, source_order: int) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Merge target for receipt row {source_order} must be a receipt row number."
        ) from exc
    # A blank numeric pandas cell is represented as NaN after one row is edited.
    if math.isnan(number):
        return None
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(
            f"Merge target for receipt row {source_order} must be a whole receipt row number."
        )
    return int(number)


def merge_selected_review_rows(
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Merge user-confirmed duplicate or split rows into their target rows.

    Duplicate mode collapses a redundant OCR copy without increasing quantity,
    line value, or discount. A split fragment keeps the target row's final line
    price; when that total is an exact multiple of the fragment price, the
    fragment price becomes the unit price and the multiple becomes quantity.
    """
    ordered = sorted(
        (dict(item) for item in items),
        key=lambda item: _number(item.get("Receipt order"), float("inf")),
    )
    by_order: dict[int, dict[str, Any]] = {}
    for fallback_order, item in enumerate(ordered, start=1):
        receipt_order = int(
            _number(item.get("Receipt order"), float(fallback_order))
        )
        if receipt_order in by_order:
            raise ValueError(f"Receipt row {receipt_order} appears more than once.")
        by_order[receipt_order] = item

    merge_count = 0
    for source_order in sorted(tuple(by_order)):
        current = by_order.get(source_order)
        if current is None:
            continue
        target_order = _merge_target(current.get("Merge into row"), source_order)
        if target_order is None:
            continue
        if target_order == source_order:
            raise ValueError(f"Receipt row {source_order} cannot merge into itself.")
        if target_order not in by_order:
            raise ValueError(
                f"Receipt row {source_order} cannot merge into missing receipt row {target_order}."
            )

        target = by_order[target_order]
        if _merge_target(target.get("Merge into row"), target_order) is not None:
            raise ValueError(
                f"Receipt row {target_order} is also marked to merge. "
                "Choose a final target row instead."
            )
        merge_mode = str(current.get("Merge mode") or "split").casefold()
        target["Item"] = _merged_name(target.get("Item"), current.get("Item"))
        if _clean_text(target.get("Category")) in ("", "uncategorized"):
            current_category = str(current.get("Category") or "").strip()
            if current_category and current_category.casefold() != "uncategorized":
                target["Category"] = current_category
        for field in ("Product code", "Tax marker"):
            if not target.get(field) and current.get(field):
                target[field] = current[field]

        previous_total = _number(target.get("Line total"), 0.0)
        current_total = _number(current.get("Line total"), 0.0)
        if merge_mode == "duplicate":
            # A duplicate group represents repeated OCR capture of the same
            # purchased row. Keep the retained row's financial values rather
            # than turning two OCR copies into two purchased units. Fill only
            # metadata that is missing from the retained copy.
            for field in ("Unit price", "Discount"):
                target_value = target.get(field)
                if (
                    target_value is None
                    or (isinstance(target_value, float) and math.isnan(target_value))
                ) and current.get(field) is not None:
                    target[field] = current[field]
        elif previous_total > 0 and current_total > 0:
            ratio = previous_total / current_total
            multiple = round(ratio)
            if 2 <= multiple <= 10 and abs(ratio - multiple) <= 0.01:
                target["Qty"] = float(multiple)
                target["Unit price"] = current_total

        target["Merge"] = False
        target["Merge into row"] = None
        target["Merge mode"] = None
        del by_order[source_order]
        merge_count += 1

    return [by_order[order] for order in sorted(by_order)], merge_count


def duplicate_review_order(
    items: Sequence[Mapping[str, Any]],
) -> list[int]:
    """Order duplicate groups first, then alphabetically, without changing data."""

    labels = potential_duplicate_labels(items)
    return sorted(
        range(len(items)),
        key=lambda index: (
            0 if labels[index] else 1,
            _clean_text(items[index].get("Item")),
            _clean_text(items[index].get("Product code")),
            round(_number(items[index].get("Line total"), 0.0), 2),
            index,
        ),
    )


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def selected_candidate_error(
    candidate_rows: Sequence[Mapping[str, Any]],
    expected_count: int | None,
    purchased_row_count: int,
) -> str | None:
    """Return the first problem with the selected unused OCR lines, if any.

    The review page shows this message beside the unused-line table, where the
    missing name, price, or quantity has to be entered.
    """
    selected = [
        row for row in candidate_rows
        if not _is_blank(row.get("Add")) and bool(row.get("Add"))
    ]
    if not selected:
        return "Select at least one OCR line to add."
    if expected_count is not None and purchased_row_count + len(selected) > expected_count:
        return "Too many OCR lines are selected for the printed item count."
    for row in selected:
        name = str(row.get("Item name", "") or "").strip()
        if not name or name == "nan":
            return "Every selected OCR line needs an item name."
        if _is_blank(row.get("Line price")):
            return f"Enter the line price for selected item {name}."
        quantity = row.get("Qty", 1.0)
        if _is_blank(quantity) or float(quantity) <= 0:
            return f"Enter a positive quantity for selected item {name}."
    return None
