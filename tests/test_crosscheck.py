"""Tests for the second-reader comparison and the 2-of-3 referee rule (crosscheck.py). No AI calls."""

from synq_paperwork.crosscheck import Dispute, RowReading, compare, referee_prompt, resolve
from synq_paperwork.schema import LineItem


def line(desc, qty=1.0, price=10.0, sku="HF-1"):
    return LineItem(sku=sku, description=desc, quantity=qty, unit_price=price, amount=round(qty * price, 2))


A = [line("All-Purpose Flour"), line("Basil, Fresh", 2), line("Olive Oil", 3), line("Napkins", 4)]


def test_identical_readings_have_no_disputes():
    assert compare(A, [l.model_copy() for l in A]) == []


def test_case_and_spacing_differences_are_not_disputes():
    b = [l.model_copy(update={"description": l.description.upper() + "  "}) for l in A]
    assert compare(A, b) == []


def test_text_difference_is_a_dispute_on_that_line_only():
    b = [l.model_copy() for l in A]
    b[0] = line("All-Flour Flour")
    disputes = compare(A, b)
    assert [d.index for d in disputes] == [0] and disputes[0].b.description == "All-Flour Flour"


def test_second_reader_missing_a_line_does_not_shift_the_rest():
    b = [A[0], A[2], A[3]]  # B skipped line 2
    disputes = compare(A, b)
    assert [(d.index, d.b) for d in disputes] == [(1, None)]


def test_extra_line_in_second_reading_is_ignored():
    # A's line count is already proven by the page totals; an extra line is B's mistake.
    assert compare(A, [A[0], line("Bags per Case", 0, 0, ""), *A[1:]]) == []


def row(n, l: LineItem):
    return RowReading(row=n, sku=l.sku, description=l.description, quantity=l.quantity,
                      unit_price=l.unit_price, amount=l.amount)


def test_referee_agreeing_with_first_reader_confirms_it():
    b = line("All-Flour Flour")
    res = resolve(A[0], Dispute(0, b), row(1, A[0]))
    assert res.confirmed and res.line == A[0]


def test_referee_agreeing_with_second_reader_corrects_the_line():
    a = line("All-Flour Flour")
    b = line("All-Purpose Flour")
    res = resolve(a, Dispute(0, b), row(1, b))
    assert res.confirmed and res.line.description == "All-Purpose Flour"


def test_referee_agreeing_with_nobody_goes_to_a_person():
    res = resolve(A[0], Dispute(0, line("All-Flour Flour")), row(1, line("All-Porpose Flour")))
    assert not res.confirmed and "referee read" in res.note


def test_no_referee_answer_goes_to_a_person():
    assert not resolve(A[0], Dispute(0, line("x")), None).confirmed


def test_second_reader_cannot_change_proven_numbers():
    # A's amount is proven by math. Even if the referee agrees with B, a different amount isn't accepted.
    b = line("All-Purpose Flour", qty=2)
    res = resolve(A[0], Dispute(0, b), row(1, b))
    assert not res.confirmed


def test_referee_prompt_is_blind():
    prompt = referee_prompt(A, [Dispute(0, line("All-Flour Flour"))])
    assert "row 1" in prompt and "10.00" in prompt
    assert "Flour" not in prompt  # it must not be told what either reader read


def test_second_reader_may_fix_numbers_that_failed_math():
    # Step 5 run: A read 289.30 (8 x 36.17 = 289.36, so A fails math); B and the referee read 289.36.
    a = LineItem(sku="HF-10274", description="Olive Oil", quantity=8, unit_price=36.17, amount=289.30)
    b = a.model_copy(update={"amount": 289.36})
    res = resolve(a, Dispute(0, b), row(1, b))
    assert res.confirmed and res.line.amount == 289.36


def test_referee_is_not_given_a_misread_amount_as_a_landmark():
    a = [LineItem(sku="HF-10274", description="Olive Oil", quantity=8, unit_price=36.17, amount=289.30)]
    assert "289.30" not in referee_prompt(a, [Dispute(0, None)])
