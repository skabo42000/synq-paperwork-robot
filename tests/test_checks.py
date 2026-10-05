"""Tests for the checkers (checks.py). Run:  uv run pytest

A checker is only useful if it
  1. never complains about CORRECT data (or people stop trusting its alarms), and
  2. always catches the mistakes we have actually seen the AI make.
Each mistake below is one the evals really found in Steps 2-3.
No AI is called here: these tests are instant and free.
"""

import json

import pymupdf
import pytest

from synq_paperwork.checks import check_invoice, check_page, compare_line, line_math, page_math, printed_lines
from synq_paperwork.make_testdata import ANSWER_DIR, PDF_DIR, TESTDATA
from synq_paperwork.schema import Invoice, LineItem, PageData

CASES = json.loads((TESTDATA / "cases.json").read_text(encoding="utf-8"))
DIGITAL = [c["id"] for c in CASES if not c["scanned"]]


def answer(case_id: str) -> Invoice:
    return Invoice.model_validate_json((ANSWER_DIR / f"{case_id}.json").read_text(encoding="utf-8"))


def correct_pages(case_id: str) -> tuple[pymupdf.Document, list[list[LineItem]]]:
    """The answer key's lines, split into pages the way they are printed."""
    doc = pymupdf.open(PDF_DIR / f"{case_id}.pdf")
    lines, pages = answer(case_id).lines, []
    for page in doc:
        n = len(printed_lines(page))
        pages.append(lines[:n])
        lines = lines[n:]
    return doc, pages


# ---------- 1. No false alarms on correct data ----------

@pytest.mark.parametrize("case_id", DIGITAL)
def test_position_check_accepts_every_correct_line(case_id):
    doc, pages = correct_pages(case_id)
    assert sum(len(p) for p in pages) == len(answer(case_id).lines)
    for page, lines in zip(doc, pages):
        check = check_page(page.number + 1, PageData(lines=lines), page)
        assert not check.line_problems and not check.problems, (page.number + 1, check.feedback())


def test_correct_lines_pass_line_math():
    for case in CASES:
        assert not [l for l in answer(case["id"]).lines if line_math(l)]


# ---------- 2. Catches the mistakes the AI really made ----------

def first_page(case_id: str):
    doc, pages = correct_pages(case_id)
    return doc[0], [l.model_copy() for l in pages[0]]


def test_catches_product_shifted_onto_neighbours_numbers():
    # Step 3: names slid one row down while the numbers stayed put. Math still adds up!
    page, lines = first_page("inv03-classic-5p")
    # Start where 5 neighbouring lines are all different products (a shift of identical ones changes nothing).
    start = next(s for s in range(len(lines) - 5)
                 if len({l.description for l in lines[s:s + 5]}) == 5)
    shifted = [l.model_copy() for l in lines]
    for i in range(start + 1, start + 5):  # 4 lines get the code + description of the line above them
        shifted[i].sku, shifted[i].description = lines[i - 1].sku, lines[i - 1].description
    assert not [l for l in shifted if line_math(l)], "the shift is invisible to math"
    check = check_page(1, PageData(lines=shifted), page)
    assert set(check.line_problems) >= set(range(start + 1, start + 5))


def test_catches_wrapped_description_cut_short():
    # Step 2: "... 4 x 5 lb" instead of "... 4 x 5 lb Bags per Case".
    page, lines = first_page("inv02-modern-1p")
    i = next(i for i, l in enumerate(lines) if "Bags per Case" in l.description)
    lines[i].description = lines[i].description.replace(" Bags per Case", "")
    assert i in check_page(1, PageData(lines=lines), page).line_problems


def test_catches_wrapped_text_glued_to_next_line():
    # Step 3: "Bags per Case Loyalty discount".
    page, lines = first_page("inv02-modern-1p")
    i = next(i for i, l in enumerate(lines) if "Bags per Case" in l.description)
    lines[i].description = lines[i].description.replace(" Bags per Case", "")
    lines[i + 1].description = "Bags per Case " + lines[i + 1].description
    problems = check_page(1, PageData(lines=lines), page).line_problems
    assert i in problems and i + 1 in problems


def test_catches_wrapped_text_as_its_own_line():
    # Step 3: an extra line "Bags per Case" with 0 / 0 / 0.
    page, lines = first_page("inv02-modern-1p")
    i = next(i for i, l in enumerate(lines) if "Bags per Case" in l.description)
    lines[i].description = lines[i].description.replace(" Bags per Case", "")
    lines.insert(i + 1, LineItem(description="Bags per Case", quantity=0, unit_price=0, amount=0))
    assert check_page(1, PageData(lines=lines), page).problems  # "prints 27 line items, but 28 were read"


def test_catches_text_misread():
    # Step 3: "all-flour flour" instead of "All-Purpose Flour".
    page, lines = first_page("inv03-classic-5p")
    i = next(i for i, l in enumerate(lines) if "All-Purpose" in l.description)
    lines[i].description = lines[i].description.replace("All-Purpose", "All-Flour")
    assert i in check_page(1, PageData(lines=lines), page).line_problems


def test_catches_quantity_from_neighbouring_row():
    # Step 2: quantity 24 instead of 1 - caught by math on ANY page, even scanned.
    line = LineItem(sku="NB-11507", description="Rebar #4 x 20 ft.", quantity=24, unit_price=13.48, amount=13.48)
    assert line_math(line)


def test_catches_missing_line():
    page, lines = first_page("inv01-classic-1p")
    del lines[3]
    assert check_page(1, PageData(lines=lines), page).problems


# ---------- 3. Page and invoice totals ----------

def lines_worth(*amounts):
    return [LineItem(description="x", quantity=1, unit_price=a, amount=a) for a in amounts]


def test_page_math_carried_forward():
    ok = PageData(brought_forward=100, lines=lines_worth(10, 5.5), carried_forward=115.5)
    assert page_math(ok) == (None, True)
    bad = PageData(brought_forward=100, lines=lines_worth(10, 5.5), carried_forward=125.5)
    assert page_math(bad)[0]


def test_page_math_page_subtotal():
    assert page_math(PageData(lines=lines_worth(10, 20), page_subtotal=30)) == (None, True)
    assert page_math(PageData(lines=lines_worth(10, 20), page_subtotal=31))[0]
    assert page_math(PageData(lines=lines_worth(10))) == (None, False)  # nothing to check against


def test_invoice_checks_blame_the_right_page():
    head = dict(supplier_name="S", invoice_number="1", invoice_date="2026-01-31", currency="USD")
    p1 = PageData(**head, lines=lines_worth(10, 20), page_subtotal=30)          # proven by its subtotal
    p2 = PageData(lines=lines_worth(5, 5), subtotal=41, tax=4.1, total=45.1)     # no page total: unproven
    problems, blame = check_invoice([p1, p2], [check_page_stub(1, True), check_page_stub(2, False)])
    assert problems and blame == {2}  # lines = 40, subtotal says 41 -> page 2 is the suspect


def test_invoice_checks_pass_when_everything_adds_up():
    head = dict(supplier_name="S", invoice_number="1", invoice_date="2026-01-31", currency="USD")
    p1 = PageData(**head, lines=lines_worth(10, 20), carried_forward=30)
    p2 = PageData(brought_forward=30, lines=lines_worth(5, 5), subtotal=40, tax=4, total=44)
    assert check_invoice([p1, p2], [check_page_stub(1, True), check_page_stub(2, True)]) == ([], set())


def check_page_stub(page_no, has_control):
    from synq_paperwork.checks import PageCheck
    return PageCheck(page_no, scanned=True, has_control_total=has_control)


def test_compare_line_accepts_quantity_inside_description():
    from synq_paperwork.checks import PrintedLine
    row = PrintedLine(["HF-11507", "Basil,", "Fresh,", "1", "lb", "20", "12.19", "243.80"])
    assert compare_line(LineItem(sku="HF-11507", description="Basil, Fresh, 1 lb", quantity=20,
                                 unit_price=12.19, amount=243.80), row) is None
    row = PrintedLine(["HF-11507", "Basil,", "Fresh,", "1", "lb", "1", "12.19", "12.19"])
    assert compare_line(LineItem(sku="HF-11507", description="Basil, Fresh, 1 lb", quantity=1,
                                 unit_price=12.19, amount=12.19), row) is None


def test_page_total_off_by_exactly_one_flagged_line_flags_only_that_line():
    # Step 5 run: one amount read as 289.30 instead of 289.36 -> the page total is off by 0.06.
    # That line is flagged; the other lines on the page must not be.
    page, lines = first_page("inv09-scan-classic-30p")
    good = lines_worth(10, 20) + [LineItem(description="Olive oil", quantity=8, unit_price=36.17, amount=289.30)]
    reading = PageData(lines=good, page_subtotal=10 + 20 + 289.36)
    check = check_page(1, reading, page)
    assert check.line_problems.keys() == {2} and not check.problems


def test_page_total_not_explained_by_flagged_lines_flags_the_page():
    page, _ = first_page("inv09-scan-classic-30p")
    bad = lines_worth(10, 20) + [LineItem(description="Olive oil", quantity=8, unit_price=36.17, amount=289.30)]
    check = check_page(1, PageData(lines=bad, page_subtotal=10 + 20 + 289.36 + 5), page)  # 5 more missing
    assert check.problems


def test_misread_brought_forward_is_explained_by_previous_page():
    # Step 5 run: page 13 carried 141,226.06 forward; page 14 was read as bringing forward 141,226.00.
    page, _ = first_page("inv09-scan-classic-30p")
    p14 = PageData(brought_forward=141226.00, lines=lines_worth(10, 20), carried_forward=141256.06)
    assert check_page(14, p14, page).problems                                  # alone: looks wrong
    assert not check_page(14, p14, page, prev_carried=141226.06).problems      # with page 13: explained
    head = dict(supplier_name="S", invoice_number="1", invoice_date="2026-01-31", currency="USD")
    p13 = PageData(**head, lines=lines_worth(141226.06), carried_forward=141226.06)
    last = PageData(brought_forward=141256.06, lines=[], subtotal=141256.06, tax=0, total=141256.06)
    problems, _ = check_invoice([p13, p14, last], [check_page_stub(n, True) for n in (1, 2, 3)])
    assert not problems


def test_brought_forward_rule_does_not_hide_a_wrong_line():
    # If the page still doesn't add up with the previous page's figure, it stays a problem.
    page, _ = first_page("inv09-scan-classic-30p")
    p = PageData(brought_forward=100.00, lines=lines_worth(10, 20), carried_forward=135.00)
    assert check_page(2, p, page, prev_carried=100.00).problems
    assert check_page(2, p, page, prev_carried=101.00).problems


# ---------- Step 7: re-check after a person's edits ----------

def test_reviewed_invoice_that_adds_up_passes():
    from synq_paperwork.checks import check_reviewed
    for case in CASES:
        assert check_reviewed(answer(case["id"])) == [], case["id"]


def test_reviewed_invoice_catches_what_a_person_might_get_wrong():
    from synq_paperwork.checks import check_reviewed
    inv = answer("inv01-classic-1p")
    inv.lines[4].amount += 1                 # typo in an amount
    inv.invoice_date = "13/06/2026"          # wrong date format
    inv.supplier_name = "  "                 # cleared by accident
    problems = " | ".join(check_reviewed(inv))
    assert "Line 5" in problems and "not a date" in problems and "Supplier is empty" in problems
    assert "lines add up to" in problems     # the typo also breaks the subtotal
