"""Step 5: a second, independent reader for SCANNED pages, and a blind referee for disagreements.

Why: a scan has no stored text, so Step 4 could prove its numbers (math) but not its product names.
The fix is the "four eyes" rule from accounting: two readers that work independently rarely make
the SAME mistake, so where they agree we can trust the line, and where they disagree we look closer.

How independent are the readers?
  Reader A: gemini-3.5-flash-lite reading the page as a PDF (Steps 3-4, numbers proven by math)
  Reader B: a DIFFERENT model (gemini-3.1-flash-lite) reading a DIFFERENT view: a sharpened,
            high-resolution image of the page
  Referee : a bigger model (gemini-3.5-flash, backup 3.5-flash-lite) reading its own, even sharper
            image, told only WHERE the disputed rows are - never what A or B read - so it must read
            them itself instead of picking a side.
A line is confirmed when A and B agree, or when the referee's own reading matches A or B
(2 of 3 independent readings). Anything else goes to a person.
"""

import io
from dataclasses import dataclass
from difflib import SequenceMatcher

import pymupdf
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from synq_paperwork.checks import line_math, same
from synq_paperwork.schema import LineItem

READER_B_MODEL = "gemini-3.1-flash-lite"
READER_B_FALLBACK = "gemini-3.5-flash"
REFEREE_MODEL = "gemini-3.5-flash"
# 3.8-flash's free quota ran out in Step 2 and 3.5-flash's is small; flash-lite has room.
# It stays independent: it reads blind, from its own sharper image of the page.
REFEREE_FALLBACK = "gemini-3.5-flash-lite"
REFEREE_DPI = 250  # sharper than reader B's 200 dpi view


def sharpened_image(page: pymupdf.Page, dpi: int = 200) -> bytes:
    """Reader B's view of the page: higher resolution than the scan, grey, contrast stretched."""
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    img = ImageOps.autocontrast(Image.frombytes("L", (pix.width, pix.height), pix.samples), cutoff=1)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- Comparing two readings ----------

def norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def same_line(a: LineItem, b: LineItem) -> bool:
    return (norm(a.sku) == norm(b.sku) and norm(a.description) == norm(b.description)
            and same(a.quantity, b.quantity) and same(a.unit_price, b.unit_price) and same(a.amount, b.amount))


def differences(a: LineItem, b: LineItem | None) -> str:
    if b is None:
        return "the second reader didn't find this line"
    fields = [f for f in ["sku", "description", "quantity", "unit_price", "amount"]
              if (norm(str(getattr(a, f))) != norm(str(getattr(b, f))) if f in ("sku", "description")
                  else not same(getattr(a, f), getattr(b, f)))]
    return "; ".join(f"{f}: '{getattr(a, f)}' vs '{getattr(b, f)}'" for f in fields)


@dataclass
class Dispute:
    index: int              # line index on the page (reader A's order)
    b: LineItem | None      # what reader B read for this row (None = B has no matching line)


def compare(a_lines: list[LineItem], b_lines: list[LineItem]) -> list[Dispute]:
    """Line up the two readings and return every line of A that B doesn't confirm exactly.
    Lines are matched in order, like comparing two versions of a document (difflib), so one
    missing or extra line doesn't make everything after it look different."""
    key = lambda l: (norm(l.sku), norm(l.description), round(l.quantity, 3), round(l.unit_price, 2), round(l.amount, 2))
    disputes = []
    for op, a1, a2, b1, b2 in SequenceMatcher(None, [key(l) for l in a_lines], [key(l) for l in b_lines],
                                               autojunk=False).get_opcodes():
        if op == "equal":
            continue
        for k, i in enumerate(range(a1, a2)):  # pair up rows inside a changed block, in order
            disputes.append(Dispute(i, b_lines[b1 + k] if b1 + k < b2 else None))
    return disputes


# ---------- The referee ----------

class RowReading(BaseModel):
    row: int = Field(description="The row number you were asked about")
    sku: str = Field(default="", description="Item code printed on that row, or ''")
    description: str = Field(description="Full description, including any wrapped continuation text below it")
    quantity: float
    unit_price: float
    amount: float


class RefereeReport(BaseModel):
    rows: list[RowReading]


REFEREE_INSTRUCTIONS = """You are checking a scanned supplier invoice page. Number the line-item rows from the top:
row 1 is the first line item under the column headers. A description that wraps onto the next printed
line belongs to its item: it is NOT a new row. Brought/carried forward, subtotals and totals are not rows.

Copy EXACTLY what is printed on these rows (the amount in the right-hand column helps you find them):
{rows}

For each row give: item code (or '' if none), the full description (join wrapped text with one space),
quantity, unit price and amount. Numbers without $ or commas; ($25.00) means -25.00.
Read the page yourself, character by character. Do not guess or correct anything."""


def referee_prompt(a_lines: list[LineItem], disputes: list[Dispute]) -> str:
    # Blind: only the row's position (+ its amount as a landmark when math proved it), never A's or B's text.
    rows = "\n".join(f"- row {d.index + 1}" + ("" if line_math(a_lines[d.index])
                                                else f" (amount {a_lines[d.index].amount:,.2f})")
                     for d in disputes)
    return REFEREE_INSTRUCTIONS.format(rows=rows)


@dataclass
class Resolution:
    line: LineItem          # the line to keep (A's, or B's text if the referee sided with B)
    confirmed: bool         # True = 2 of 3 independent readings agree
    note: str               # why, in plain English


def resolve(a: LineItem, dispute: Dispute, referee: RowReading | None) -> Resolution:
    """2 of 3: the referee's own reading must match A or B exactly."""
    diff = differences(a, dispute.b)
    if referee is None:
        return Resolution(a, False, f"readers disagree ({diff}); the referee couldn't read the row")
    r = LineItem(sku=referee.sku, description=referee.description, quantity=referee.quantity,
                 unit_price=referee.unit_price, amount=referee.amount)
    if same_line(r, a):
        return Resolution(a, True, "")
    a_numbers_proven = not line_math(a)
    if dispute.b is not None and same_line(r, dispute.b) and not line_math(dispute.b) \
            and (same(dispute.b.amount, a.amount) or not a_numbers_proven):
        # B was right (2 of 3). B may change NUMBERS only if A's numbers failed math; the pipeline
        # then re-runs every check on the corrected page before trusting it.
        return Resolution(dispute.b, True, "")
    return Resolution(a, False, f"readers disagree ({diff}); the referee read "
                                f"'{r.sku} {r.description} {r.quantity:g} x {r.unit_price:,.2f} = {r.amount:,.2f}'")
