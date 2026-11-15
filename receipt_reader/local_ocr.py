"""Local OCR adapter for PaddleOCR.

This module deliberately stops at text recognition.  It does not decide which
line is a store, item, or total; that separate job belongs to ``parser.py``.
Keeping those stages independent makes both the code and its tests easier to
understand.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from statistics import mean, median
from typing import Any, Iterable

from PIL import Image, ImageFilter, ImageOps


@dataclass(frozen=True, slots=True)
class OcrLine:
    """One OCR text region and its location in the image."""

    text: str
    confidence: float
    box: tuple[int, int, int, int] | None = None

    @property
    def x(self) -> int:
        return self.box[0] if self.box else 0

    @property
    def y(self) -> int:
        return self.box[1] if self.box else 0


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Text returned by the local OCR engine."""

    lines: tuple[OcrLine, ...]
    engine: str

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def average_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return mean(line.confidence for line in self.lines)


class LocalOcrError(RuntimeError):
    """Raised when local OCR cannot be initialized or completed."""


def _terminal_status(stage: str, message: str) -> None:
    """Expose slow local OCR stages in the development terminal."""

    timestamp = time.strftime("%H:%M:%S")
    print(f"[TalliUpper OCR {timestamp}] {stage:<8} {message}", flush=True)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return list(value.tolist())
    return list(value)


def _result_payload(result: Any) -> dict[str, Any]:
    """Normalize PaddleOCR's documented result object to one dictionary."""

    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise LocalOcrError("The OCR engine returned an unfamiliar result format.")
    nested = payload.get("res", payload)
    if not isinstance(nested, dict):
        raise LocalOcrError("The OCR result did not contain readable text data.")
    return nested


def _box_from_polygon(polygon: Any) -> tuple[int, int, int, int] | None:
    points = _as_list(polygon)
    if not points:
        return None
    try:
        xs = [int(point[0]) for point in points]
        ys = [int(point[1]) for point in points]
    except (IndexError, TypeError, ValueError):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _same_text_row(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> bool:
    """Return whether two OCR boxes sit on the same printed line."""

    first_height = max(1, first[3] - first[1])
    second_height = max(1, second[3] - second[1])
    overlap = min(first[3], second[3]) - max(first[1], second[1])
    if overlap >= min(first_height, second_height) * 0.45:
        return True

    first_center = (first[1] + first[3]) / 2
    second_center = (second[1] + second[3]) / 2
    return abs(first_center - second_center) <= max(first_height, second_height) * 0.55


# A row may be at most this many typical text-line heights tall. Without a cap,
# each joined fragment widens the row, which widens the tolerance, which lets
# it swallow the next printed row: one blurred region could merge six items.
ROW_HEIGHT_CAP = 1.3


def _merge_fragments_into_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """Join separately detected words that occupy one visual receipt row.

    PaddleOCR can return an item name, its tax marker, and its price as three
    regions. Sorting every region by its exact top coordinate can scramble that
    row when the photo is slightly tilted. Clustering by vertical overlap first
    preserves the relationship the receipt communicates spatially.

    Row bands are capped near the page's typical line height, and unusually
    tall fragments (a crease or smudge spanning two printed rows) may join a
    row but never stretch it, so neighboring purchases stay separate.
    """

    positioned = [line for line in lines if line.box is not None]
    unpositioned = [line for line in lines if line.box is None]
    if not positioned:
        return unpositioned

    def height(line: OcrLine) -> int:
        return max(1, line.box[3] - line.box[1])  # type: ignore[index]

    def center(line: OcrLine) -> float:
        return (line.box[1] + line.box[3]) / 2  # type: ignore[index]

    typical = median(height(line) for line in positioned)
    cap = typical * ROW_HEIGHT_CAP

    def is_tall(line: OcrLine) -> bool:
        return height(line) > cap

    def row_band(row: list[OcrLine]) -> tuple[float, float]:
        regular = [part for part in row if not is_tall(part)] or row
        middle = median(center(part) for part in regular)
        top = min(part.box[1] for part in regular)  # type: ignore[index]
        bottom = max(part.box[3] for part in regular)  # type: ignore[index]
        if bottom - top > cap:
            top, bottom = max(top, middle - cap / 2), min(bottom, middle + cap / 2)
        return top, bottom

    def fragment_band(line: OcrLine) -> tuple[float, float]:
        if not is_tall(line):
            return line.box[1], line.box[3]  # type: ignore[index]
        middle = center(line)
        return middle - cap / 2, middle + cap / 2

    def reference_band(row: list[OcrLine]) -> tuple[float, float]:
        # Follow the printed line from left to right: compare with the row's
        # most recent regular piece, so a gently curled row stays together
        # while its neighbors, one line-height away, stay apart.
        for part in reversed(row):
            if not is_tall(part):
                return part.box[1], part.box[3]  # type: ignore[index]
        return row_band(row)

    def best_row_for(line: OcrLine, rows: list[list[OcrLine]], band_of) -> list[OcrLine] | None:
        band = fragment_band(line)
        best_row: list[OcrLine] | None = None
        best_distance: float | None = None
        for row in rows:
            row_top, row_bottom = band_of(row)
            if not _same_text_row(
                (0, round(row_top), 1, round(row_bottom)),
                (0, round(band[0]), 1, round(band[1])),
            ):
                continue
            distance = abs((row_top + row_bottom) / 2 - (band[0] + band[1]) / 2)
            if best_distance is None or distance < best_distance:
                best_row = row
                best_distance = distance
        return best_row

    rows: list[list[OcrLine]] = []
    regular = sorted(
        (line for line in positioned if not is_tall(line)),
        key=lambda candidate: (candidate.x, center(candidate)),
    )
    for line in regular:
        best_row = best_row_for(line, rows, reference_band)
        if best_row is None:
            rows.append([line])
        else:
            best_row.append(line)
    # Tall pieces (a crease or smudge across rows) join the nearest row last,
    # without changing which regular pieces share a row.
    for line in sorted((line for line in positioned if is_tall(line)), key=center):
        best_row = best_row_for(line, rows, row_band)
        if best_row is None:
            rows.append([line])
        else:
            best_row.append(line)

    merged: list[OcrLine] = []
    for row in rows:
        ordered = sorted(row, key=lambda part: part.x)
        boxes = [part.box for part in ordered if part.box is not None]
        vertical = [part.box for part in ordered if part.box is not None and not is_tall(part)] or boxes
        merged.append(
            OcrLine(
                text=" ".join(part.text for part in ordered),
                confidence=mean(part.confidence for part in ordered),
                box=(
                    min(box[0] for box in boxes),
                    min(box[1] for box in vertical),
                    max(box[2] for box in boxes),
                    max(box[3] for box in vertical),
                ),
            )
        )

    merged.sort(key=lambda line: (line.y, line.x))
    return merged + unpositioned


def paddle_results_to_lines(results: Iterable[Any]) -> list[OcrLine]:
    """Convert PaddleOCR result objects into reading-order OCR lines."""

    lines: list[OcrLine] = []
    for result in results:
        payload = _result_payload(result)
        texts = _as_list(payload.get("rec_texts"))
        scores = _as_list(payload.get("rec_scores"))
        boxes = _as_list(payload.get("rec_boxes"))
        polygons = _as_list(payload.get("rec_polys"))
        # Rectify line coordinates using the text baselines, so slanted receipts
        # do not align prices with a neighboring description.
        slopes = []
        for polygon in polygons:
            points = _as_list(polygon)
            if len(points) >= 4:
                dx = float(points[1][0]) - float(points[0][0])
                dy = float(points[1][1]) - float(points[0][1])
                if dx > 40 and abs(dy / dx) < .3:
                    slopes.append(dy / dx)
        slope = median(slopes) if slopes else 0.0

        for index, raw_text in enumerate(texts):
            text = " ".join(str(raw_text).split()).strip()
            if not text:
                continue

            try:
                confidence = float(scores[index])
            except (IndexError, TypeError, ValueError):
                confidence = 0.0

            box: tuple[int, int, int, int] | None = None
            if index < len(boxes):
                raw_box = _as_list(boxes[index])
                if len(raw_box) >= 4:
                    try:
                        box = tuple(int(value) for value in raw_box[:4])  # type: ignore[assignment]
                    except (TypeError, ValueError):
                        box = None
            if box is None and index < len(polygons):
                box = _box_from_polygon(polygons[index])

            if index < len(polygons):
                points = _as_list(polygons[index])
                if len(points) >= 4:
                    rectified = [(float(point[0]), float(point[1]) - slope * float(point[0])) for point in points]
                    box = _box_from_polygon(rectified)

            lines.append(
                OcrLine(
                    text=text,
                    confidence=max(0.0, min(1.0, confidence)),
                    box=box,
                )
            )

    return _merge_fragments_into_rows(lines)


@lru_cache(maxsize=4)
def _load_engine(language: str) -> Any:
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise LocalOcrError(
            "Local OCR is not installed. Run the Windows setup again or install "
            "the packages from requirements-local.txt."
        ) from exc

    try:
        started = time.perf_counter()
        _terminal_status("MODEL", f"Loading local OCR engine for language={language}.")
        engine = PaddleOCR(
            lang=language,
            ocr_version="PP-OCRv5",
            device="cpu",
            # Avoid the oneDNN PIR attribute crash on Windows CPU inference.
            enable_mkldnn=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            # Phone EXIF rotation is normalized before OCR. Disabling these two
            # auxiliary classifiers avoids loading extra models on Windows CPU.
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        _terminal_status(
            "MODEL",
            f"Local OCR engine ready in {time.perf_counter() - started:.1f}s.",
        )
        return engine
    except Exception as exc:
        raise LocalOcrError(
            "The local OCR models could not be initialized. The first run needs "
            "an internet connection so PaddleOCR can download its models."
        ) from exc


def extract_text_locally(
    image_bytes: bytes,
    *,
    language: str = "en",
    enhanced: bool = False,
) -> OcrResult:
    """Run PaddleOCR locally, optionally enhancing faint thermal-receipt text."""

    try:
        import numpy as np

        with Image.open(BytesIO(image_bytes)) as image:
            image = image.convert("RGB")
            image_size = image.size
            if enhanced:
                # Thermal receipts often have uneven paper tone, wrinkles, and
                # faded rows. A grayscale contrast/sharpen pass gives the detector
                # a second view without changing coordinates used to merge results.
                grayscale = ImageOps.grayscale(image)
                grayscale = ImageOps.autocontrast(grayscale, cutoff=1)
                grayscale = grayscale.filter(
                    ImageFilter.UnsharpMask(radius=1.2, percent=180, threshold=2)
                )
                image = grayscale.convert("RGB")
            image_array = np.asarray(image)
    except Exception as exc:
        raise LocalOcrError("The prepared receipt image could not be opened.") from exc

    _terminal_status(
        "INPUT",
        f"{'Enhanced recovery image' if enhanced else 'Prepared receipt'} is "
        f"{image_size[0]}x{image_size[1]} pixels.",
    )
    engine = _load_engine(language)
    try:
        prediction_started = time.perf_counter()
        _terminal_status(
            "READ",
            "Detecting and recognizing receipt text"
            + (" with enhanced contrast." if enhanced else "."),
        )
        raw_results = engine.predict(image_array)
        lines = paddle_results_to_lines(raw_results)
        _terminal_status(
            "READ",
            f"Recognized {len(lines)} text lines in "
            f"{time.perf_counter() - prediction_started:.1f}s.",
        )
    except LocalOcrError:
        raise
    except Exception as exc:
        _terminal_status("ERROR", f"OCR prediction failed: {type(exc).__name__}: {exc}")
        logging.getLogger(__name__).exception("Receipt OCR prediction failed")
        raise LocalOcrError(
            "The local OCR engine encountered a software error while processing "
            "the receipt. Your photo may be fine. Use manual entry for now; "
            "the error details are in the app terminal."
        ) from exc

    if not lines:
        raise LocalOcrError(
            "No readable text was found. Try filling the frame with the receipt "
            "and avoiding shadows or glare."
        )

    engine_name = "PaddleOCR PP-OCRv5 mobile (CPU)"
    if enhanced:
        engine_name += "; enhanced recovery pass"
    return OcrResult(lines=tuple(lines), engine=engine_name)

