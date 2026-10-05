"""Extra columns, discounts, notes under items, wrapped codes and totals blocks - the 'never be surprised' checks.
No AI calls: the page is drawn here, and the AI's guesses in the columns tests are written by hand."""

import pymupdf
import pytest

from synq_paperwork.checks import (ColumnRule, PrintedLine, check_page, compare_line, line_math, number_values, printed_lines,
                                   totals_math)
from synq_paperwork.columns import ColumnGuess, extra_columns, prove
from synq_paperwork.schema import ColumnValue, LineItem, PageData, TotalsRow

# Column left edges, like the Omnicorp stress-test invoice: code, description, qty, UOM, price, disc %,
# tax code, freight, line total.
X = {"code": 55, "desc": 114, "qty": 300, "uom": 332, "price": 385, "disc": 430, "tax": 463, "frt": 490, "total": 525}


def draw(rows: list[tuple[float, dict[str, str]]]) -> pymupdf.Page:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for y, cells in rows:
        for col, text in cells.items():
            page.insert_text((X[col] if col in X else col, y), text, fontsize=8)
    return page


def row(code, desc, qty, uom, price, disc, tax, frt, total):
    return {k: v for k, v in dict(code=code, desc=desc, qty=qty, uom=uom, price=price, disc=disc,
                                  tax=tax, frt=frt, total=total).items() if v}


PAGE_ROWS = [
    (60, {"code": "Part #", "desc": "Description / Notes", "qty": "Qty", "uom": "UOM", "price": "Price",
          "disc": "Disc %", "tax": "Tax", "frt": "Frt", "total": "Total"}),
    (80, row("EL-99-A", "Micro-controller Unit 32-bit", "500", "EA", "12.50", "5.0", "T1", "", "5,937.50")),
    (92, {"code": "Note: Delivered via DHL Express"}),
    (107, row("CBL-CAT6-", "CAT6 Patch Cable - 1 Meter (Blue)", "1000", "PCS", "1.25", "10", "T2", "", "1,125.00")),
    (117, {"code": "1M"}),
    (133, {208: "--- MECHANICAL COMPONENTS BATCH 1 ---"}),
    (148, row("FST-WSH-", "Washer M4 Nylon - White", "100000", "EA", "0.01", "5.00", "T1", "", "950.00")),
    (157, {"code": "M4", 310: ".0"}),
    (172, row("PWR-5V-2A", "Power Supply Adapter 5V 2A DC", "2,000", "EA", "4.00", "", "EX", "4.20", "8,004.20")),
    (188, row("FST-M4-10", "Hex Bolt M4 x 10mm Stainless", "50000", "EA", "0.05", "0", "T1", "", "2500.00")),
]

CORRECT = [
    LineItem(sku="EL-99-A", description="Micro-controller Unit 32-bit Note: Delivered via DHL Express", quantity=500,
             unit_price=12.5, amount=5937.5, discount_percent=5,
             other_columns=[ColumnValue(column="UOM", value="EA"), ColumnValue(column="Tax", value="T1")]),
    LineItem(sku="CBL-CAT6-1M", description="CAT6 Patch Cable - 1 Meter (Blue)", quantity=1000, unit_price=1.25,
             amount=1125, discount_percent=10,
             other_columns=[ColumnValue(column="UOM", value="PCS"), ColumnValue(column="Tax", value="T2")]),
    LineItem(sku="FST-WSH-M4", description="Washer M4 Nylon - White", quantity=100000, unit_price=0.01, amount=950,
             discount_percent=5,
             other_columns=[ColumnValue(column="UOM", value="EA"), ColumnValue(column="Tax", value="T1")]),
    LineItem(sku="PWR-5V-2A", description="Power Supply Adapter 5V 2A DC", quantity=2000, unit_price=4, amount=8004.2,
             other_columns=[ColumnValue(column="UOM", value="EA"), ColumnValue(column="Tax", value="EX"),
                            ColumnValue(column="Frt", value="4.20")]),
    LineItem(sku="FST-M4-10", description="Hex Bolt M4 x 10mm Stainless", quantity=50000, unit_price=0.05, amount=2500,
             other_columns=[ColumnValue(column="UOM", value="EA"), ColumnValue(column="Tax", value="T1")]),
]
FREIGHT = [ColumnRule(column="Frt", effect="amount_added")]


@pytest.fixture(scope="module")
def page():
    return draw(PAGE_ROWS)


# ---------- reading printed numbers ----------

@pytest.mark.parametrize("token, value", [("1,000.50", 1000.5), ("1000,50", 1000.5), ("1.000,50", 1000.5),
                                          ("50,000", 50000), ("5%", 5), ("($25.00)", -25), ("-25.00", -25),
                                          ("$1,234.50", 1234.5), (".0", 0), ("12.50", 12.5)])
def test_number_formats(token, value):
    assert value in number_values(token)


def test_words_are_not_numbers():
    assert not any(number_values(t) for t in ["T1", "32-bit", "#9938274", "EA", "-", "$"])


# ---------- discounts in the math ----------

def test_discount_percent_and_amount_are_part_of_the_math():
    assert line_math(CORRECT[0]) is None                                   # 500 x 12.50 less 5% = 5,937.50
    assert line_math(LineItem(description="x", quantity=2, unit_price=10, amount=15, discount_amount=5)) is None
    wrong = CORRECT[0].model_copy(update={"discount_percent": 4})
    assert "less 4%" in line_math(wrong)


def test_unexplained_extra_column_is_shown_not_hidden():
    problem = line_math(CORRECT[3])                                        # freight not understood yet
    assert problem and "this line also shows: Frt = 4.20" in problem
    assert line_math(CORRECT[3], FREIGHT) is None                          # once proven, it adds up


# ---------- the printed rows (digital PDF) ----------

def test_rows_rebuilt_with_notes_wrapped_codes_and_no_section_heading(page):
    items = printed_lines(page)
    assert len(items) == 5                                                 # the heading is not an item
    assert "Note:" in items[0].words and "1M" in items[1].words and ".0" in items[2].words
    assert not any("MECHANICAL" in w for it in items for w in it.words)


def test_correct_reading_passes_the_position_check(page):
    check = check_page(1, PageData(lines=CORRECT), page, rules=FREIGHT)
    assert not check.problems and not check.line_problems, check.feedback()


@pytest.mark.parametrize("i, change, expect", [
    (0, {"description": "Micro-controller Unit 32-bit"}, "not read: 'delivered via dhl express'"),  # note dropped
    (1, {"sku": "CBL-CAT6-"}, "not read: '1m'"),                              # wrapped code cut short
    (2, {"discount_percent": None, "amount": 1000}, "amount"),                # discount ignored
    (3, {"other_columns": [ColumnValue(column="UOM", value="EA"),              # a column skipped (math is
                           ColumnValue(column="Frt", value="4.20")]}, "not read: 'ex'"),  # still fine without it)
    (4, {"description": "Hex Bolt M4 x 12mm Stainless"}, "description"),      # misread text
])
def test_position_check_catches_what_was_skipped_or_misread(page, i, change, expect):
    lines = list(CORRECT)
    lines[i] = lines[i].model_copy(update=change)
    check = check_page(1, PageData(lines=lines), page, rules=FREIGHT)
    assert expect in check.line_problems.get(i, "").casefold()


def test_note_turned_into_its_own_line_is_caught(page):
    fake = LineItem(description="Note: Delivered via DHL Express", quantity=1, unit_price=0, amount=0)
    lines = [CORRECT[0].model_copy(update={"description": "Micro-controller Unit 32-bit"}), fake] + CORRECT[1:]
    assert check_page(1, PageData(lines=lines), page, rules=FREIGHT).problems  # "prints 5, but 6 were read"


# ---------- totals block ----------

OMNICORP_TOTALS = [TotalsRow(label="Discount Applied", amount=-1250, kind="discount"),
                   TotalsRow(label="Adjusted Subtotal", amount=23527.50, kind="subtotal"),
                   TotalsRow(label="Tax T1 (Standard - 10%)", amount=1845.20, kind="tax"),
                   TotalsRow(label="Tax T2 (Reduced - 5%)", amount=140.00, kind="tax"),
                   TotalsRow(label="Tax T3 (Exempt/Zero - 0%)", amount=0, kind="tax"),
                   TotalsRow(label="Freight / Shipping (EX)", amount=1250, kind="charge")]


def test_totals_block_with_discount_several_taxes_and_freight():
    assert totals_math(24777.50, 1985.20, 26762.70, OMNICORP_TOTALS) == []
    assert totals_math(24777.50, 1985.20, 26762.00, OMNICORP_TOTALS)          # total misread
    wrong_running = [r.model_copy(update={"amount": 23000}) if r.kind == "subtotal" else r for r in OMNICORP_TOTALS]
    assert "Adjusted Subtotal" in totals_math(24777.50, 1985.20, 26762.70, wrong_running)[0]


# ---------- the AI's guesses must pass the math ----------

def lines_with_freight():
    return [LineItem(description=f"item {n}", quantity=n, unit_price=10, amount=n * 10 + f,
                     other_columns=[ColumnValue(column="Freight", value=f"{f:.2f}"),
                                    ColumnValue(column="UOM", value="EA")])
            for n, f in [(1, 4.20), (2, 3.10), (3, 0), (5, 7.75)]]


def guess(column, effect):
    return ColumnGuess(column=column, meaning="m", effect=effect, reasoning="r")


def test_a_right_guess_is_proven_and_used():
    columns = extra_columns([PageData(lines=lines_with_freight())])
    rules, findings = prove([guess("Freight", "amount_added"), guess("UOM", "no_effect")], columns)
    assert rules == [ColumnRule(column="Freight", effect="amount_added")]
    by = {f.column: f for f in findings}
    assert by["Freight"].status == "proven" and by["UOM"].status == "information only"


def test_a_wrong_guess_is_shown_but_never_used():
    columns = extra_columns([PageData(lines=lines_with_freight())])
    rules, findings = prove([guess("Freight", "percent_added"), guess("UOM", "no_effect")], columns)
    assert rules == []
    assert {f.column: f.status for f in findings}["Freight"] == "not proven"


def test_saying_a_column_changes_nothing_is_checked_too():
    columns = extra_columns([PageData(lines=lines_with_freight())])
    _, findings = prove([guess("Freight", "no_effect"), guess("UOM", "no_effect")], columns)
    assert {f.column: f.status for f in findings}["Freight"] == "not proven"   # the amounts don't add up without it


def test_lost_hyphen_in_wrapped_code_gets_a_precise_hint(page):
    lines = list(CORRECT)
    lines[1] = lines[1].model_copy(update={"sku": "CBL-CAT61M"})   # hyphen lost where the code wrapped
    problem = check_page(1, PageData(lines=lines), page, rules=FREIGHT).line_problems[1]
    assert "printed as 'CBL-CAT6-1M'" in problem


def test_note_label_left_out_is_fine(page):
    lines = list(CORRECT)
    lines[0] = lines[0].model_copy(update={"description": "Micro-controller Unit 32-bit Delivered via DHL Express"})
    assert not check_page(1, PageData(lines=lines), page, rules=FREIGHT).line_problems


def test_supplier_invoice_that_does_not_add_up_blames_no_proven_line(page):
    from synq_paperwork.checks import check_invoice
    reading = PageData(supplier_name="S", invoice_number="1", invoice_date="2026-09-27", currency="USD",
                       lines=CORRECT, subtotal=100.0, tax=0, total=100.0)          # its own subtotal is wrong
    check = check_page(1, reading, page, rules=FREIGHT)
    assert check.position_verified
    problems, blame = check_invoice([reading], [check], FREIGHT)
    assert blame == set() and "supplier's invoice doesn't add up" in problems[0]


def test_code_wrapped_over_three_rows():
    # Omnicorp page 9: "HW-" / "CASTER-" / "4SW" printed under each other.
    row = PrintedLine(["HW-", '4"', "Swivel", "Caster", "40", "EA", "12.00", "10", "T1", "432.00", "CASTER-", "4SW"])
    line = LineItem(sku="HW-CASTER-4SW", description='4" Swivel Caster', quantity=40, unit_price=12,
                    amount=432, discount_percent=10, other_columns=[ColumnValue(column="UOM", value="EA"),
                                                                  ColumnValue(column="Tax", value="T1")])
    assert compare_line(line, row) is None
    assert "printed as 'HW-CASTER-4SW'" in compare_line(line.model_copy(update={"sku": "HW-CASTER4SW"}), row)


# ---------- one item code = one description (works on scans too) ----------

def test_truncated_description_stands_out_against_the_same_code_elsewhere():
    from synq_paperwork.checks import code_consistency
    full = "Mozzarella, Whole Milk, Low Moisture, Shredded, 4 x 5 lb Bags per Case"
    cut = "Mozzarella, Whole Milk, Low Moisture, Shredded, 4 x 5 lb"          # eval run: wrapped row lost
    lines = [LineItem(sku="HF-10411", description=d, quantity=1, unit_price=1, amount=1)
             for d in [full, full, cut, full, cut, full]] + [LineItem(sku="HF-1", description="Basil", quantity=1,
                                                                     unit_price=1, amount=1)]
    problems = code_consistency(lines)
    assert set(problems) == {2, 4} and "Bags per Case" in problems[2]


def test_consistent_codes_raise_nothing():
    from synq_paperwork.checks import code_consistency
    from synq_paperwork.evals import load_answer
    assert code_consistency(load_answer("inv09-scan-classic-30p").lines) == {}


def test_extra_totals_row_is_harmless_when_the_printed_totals_add_up():
    # Eval run: "Delivery charge 45.00" (a line item) listed again as a totals row.
    rows = [TotalsRow(label="Delivery charge", amount=45, kind="charge"),
            TotalsRow(label="Sales tax", amount=688.48, kind="tax")]
    assert totals_math(8345.18, 688.48, 9033.66, rows) == []
