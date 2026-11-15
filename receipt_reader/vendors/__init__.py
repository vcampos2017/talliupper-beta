"""Vendor-specific receipt rules.

The general parser in ``parser.py`` handles layouts most stores share. Some
merchants print a distinctive format that general rules misread (for example
H-E-B prints multi-quantity purchases on two lines). Each module in this
package describes one merchant's layout as a small, testable profile.

A profile can:

* ``detect`` whether the OCR lines belong to its merchant,
* ``preprocess`` OCR lines into the single-row shape the general parser reads,
* name ``metadata`` lines (coupon notes, savings lines) that belong to a
  neighboring item and must never be offered as purchases.

To add a store, create a module with a ``PROFILE`` and list it in
``PROFILES`` below. Keep rules scoped: a profile only runs when ``detect``
matches, so it cannot change how other stores are parsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..local_ocr import OcrLine


@dataclass(frozen=True, slots=True)
class VendorProfile:
    name: str
    detect: Callable[[list[OcrLine]], bool]
    preprocess: Callable[[list[OcrLine]], list[OcrLine]]
    metadata_reason: Callable[[str], str | None]


def _profiles() -> tuple[VendorProfile, ...]:
    from . import heb

    return (heb.PROFILE,)


def vendor_profile_for(lines: list[OcrLine]) -> VendorProfile | None:
    """Return the first profile whose merchant matches these OCR lines."""

    for profile in _profiles():
        if profile.detect(lines):
            return profile
    return None
