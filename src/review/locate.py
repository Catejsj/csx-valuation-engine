"""Find where a stored value sits on its PDF page, so the review screen can box it.

The value is searched as it would be printed ("1,234", "(1,234)", "-"), on lines whose
words overlap the row label. Boxes are returned as fractions of the page size (0..1).
"""
from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path


def printed_forms(value: Decimal) -> set[str]:
    if value == 0:
        return {"-", "0"}
    text = f"{abs(value):,}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return {f"({text})", f"-{text}"} if value < 0 else {text}


@lru_cache(maxsize=64)
def _page_words(pdf_path: str, page_no: int) -> tuple[float, float, tuple[dict, ...]]:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no - 1]
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
        keep = tuple({"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"],
                      "bottom": w["bottom"]} for w in words)
        return float(page.width), float(page.height), keep


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", text.lower()) if len(t) > 2}


NUMERIC = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$|^-$")


def locate(pdf_path: Path, page_no: int, label: str, value: Decimal, prefer_left: bool = True,
           period_rank: int = 0, n_periods: int = 1) -> dict:
    """Return {"value": box|None, "line": box|None}; box = {x0,y0,x1,y1} as page fractions.

    period_rank: 0 for the latest period in the filing, 1 for the one before, ...
    With n_periods known, the number is taken from its column position on the line
    (e.g. 4 numbers and 2 periods -> the 1st or 3rd), which matters for repeated values.
    """
    width, height, words = _page_words(str(pdf_path), page_no)
    forms = printed_forms(value)
    label_tokens = _tokens(label)
    best = None
    for cand in (w for w in words if w["text"] in forms):
        line = [w for w in words if abs(w["top"] - cand["top"]) < 3]
        line_tokens = _tokens(" ".join(w["text"] for w in line))
        # wrapped labels: also look one line up
        above = [w for w in words if 3 <= cand["top"] - w["top"] < 16]
        above_tokens = _tokens(" ".join(w["text"] for w in above))
        score = len(label_tokens & line_tokens) * 2 + len(label_tokens & above_tokens)
        if not label_tokens:
            score = 1
        key = (score, -cand["x0"] if prefer_left else cand["x0"])
        if score and (best is None or key > best[0]):
            best = (key, cand, line)

    def box(x0, top, x1, bottom):
        return {"x0": x0 / width, "y0": top / height, "x1": x1 / width, "y1": bottom / height}

    if best is None:
        return {"value": None, "line": None}
    _, cand, line = best
    nums = sorted((w for w in line if NUMERIC.match(w["text"])), key=lambda w: w["x0"])
    nums = [w for w in nums if not (len(w["text"]) <= 2 and w["text"].isdigit())]  # drop note refs
    if n_periods > 0 and nums and len(nums) % n_periods == 0:
        target = nums[period_rank * (len(nums) // n_periods)]
        if target["text"] in forms:
            cand = target
    return {
        "value": box(cand["x0"] - 2, cand["top"] - 2, cand["x1"] + 2, cand["bottom"] + 2),
        "line": box(min(w["x0"] for w in line) - 4, min(w["top"] for w in line) - 3,
                    max(w["x1"] for w in line) + 4, max(w["bottom"] for w in line) + 3),
    }
