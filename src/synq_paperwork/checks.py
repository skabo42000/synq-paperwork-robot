"""Step 4: checks written in plain code. They PROVE a reading is right, or say exactly where it isn't.

Two kinds of evidence, both independent of the AI:

1. MATH - invoices contain their own answer key:
     line:     quantity x unit price (less any printed discount) = amount
     page:     brought forward + this page's lines = carried forward   (or = "Subtotal this page")
     invoice:  all lines = subtotal; subtotal + taxes, discounts, charges = total;
               page N starts where page N-1 ended
   Catches every misread NUMBER, and tells us which page it's on.

2. POSITION (digital PDFs only) - a digital PDF stores every word with its position on the page.
   We rebuild each printed line item (its row + the rows wrapped under it) and require that EVERY
   printed word is accounted for by what the AI read, and everything the AI read is printed there.
   That works for any number of columns in any order - a column the AI skipped leaves words over,
   so an unexpected column can never slip through unnoticed. Catches "right numbers, wrong product"
   too, which math can't see. It's a CHECKER, not a reader: a layout it doesn't understand means
   "can't verify" (a person looks), never "approved".

Columns we have no field for (freight, deposits, pack sizes...) are copied by the AI into
other_columns. If one of them changes the amount, columns.py lets an AI propose what it means
as a ColumnRule - and it counts only if the rule then passes the math here, line after line.

Scanned pages have no stored text, so only math applies to them; Step 5 adds a second reader.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pymupdf
from pydantic import BaseModel

from synq_paperwork.schema import Invoice, LineItem, PageData, TotalsRow

CENT = 0.005  # two amounts are equal if they differ by less than half a cent


def same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < CENT


def fmt(x: float) -> str:
    return f"{x:,.2f}"


# ---------- Reading printed numbers ----------

NUMBER = re.compile(r"^\(?[-+]?\$?[-+]?(\d[\d.,]*|[.,]\d+)%?\)?$")
MONEY = re.compile(r"^\(?[-+]?\$?[-+]?(\d{1,3}(?:,\d{3})*|\d{1,3}(?:\.\d{3})*|\d+)[.,]\d{2}\)?$")  # 2 decimals


def number_values(token: str) -> list[float]:
    """What a printed number can mean, most likely first: '1,000.50' -> [1000.5], '1000,50' -> [1000.5]
    (decimal comma), '50,000' -> [50000, 50.0], '5%' -> [5], '($25.00)' -> [-25]. Not a number -> []."""
    t = token.strip()
    m = NUMBER.match(t)
    if not m:
        return []
    digits = m.group(1)
    sign = -1 if t.startswith("(") or "-" in t.split(digits)[0] else 1
    dots, commas = digits.count("."), digits.count(",")
    if dots and commas:                       # the separator that comes last is the decimal one
        dec = "." if digits.rfind(".") > digits.rfind(",") else ","
        whole, frac = digits.rsplit(dec, 1)
        values = [float(whole.replace(",", "").replace(".", "") + "." + frac)]
    elif commas == 1 or dots == 1:            # one separator: decimal - or thousands if 3 digits follow
        sep = "," if commas else "."
        whole, frac = digits.split(sep)
        decimal = float((whole or "0") + "." + frac)
        thousands = float(whole + frac) if len(frac) == 3 and whole else None
        values = [v for v in ([thousands, decimal] if sep == "," else [decimal, thousands]) if v is not None]
    elif commas or dots:                      # 1,000,000 or 1.000.000
        values = [float(digits.replace(",", "").replace(".", ""))]
    else:
        values = [float(digits)]
    return [sign * v for v in values]


def value_of(text: str) -> float | None:
    """The first number in a printed cell like '$4.20', '5 %' or '12 kg'."""
    for word in text.split():
        if v := number_values(word):
            return v[0]
    return None


# ---------- 1. Math ----------

Effect = Literal["no_effect", "percent_off", "percent_added", "amount_off", "amount_added",
                 "per_unit_added", "price_per", "pack_size"]


class ColumnRule(BaseModel):
    """How an extra column changes a line's amount. `effect` says how its value v is used:
      percent_off     base x (1 - v/100)     percent_added  base x (1 + v/100)
      amount_off      base - v               amount_added   base + v
      per_unit_added  base + quantity x v    (a deposit or fee per unit)
      price_per       base / v               (the price is for v units, e.g. 'per 100')
      pack_size       base x v               (the quantity counts packs of v)
    where base = quantity x unit price."""
    column: str
    effect: Effect


def _column(line: LineItem, name: str) -> float | None:
    key = name.strip().casefold()
    return next((value_of(c.value) for c in line.other_columns if c.column.strip().casefold() == key), None)


def expected_amount(line: LineItem, rules: list[ColumnRule] = ()) -> float:
    """What the line should add up to, from its own printed numbers."""
    base = line.quantity * line.unit_price
    values = [(r.effect, v) for r in rules if (v := _column(line, r.column)) is not None]
    for effect, v in values:                                     # 1. how many units / per how many
        if effect == "price_per" and v:
            base /= v
        elif effect == "pack_size":
            base *= v
    if line.discount_percent:                                    # 2. percentages
        base *= 1 - line.discount_percent / 100
    for effect, v in values:
        if effect == "percent_off":
            base *= 1 - v / 100
        elif effect == "percent_added":
            base *= 1 + v / 100
    if line.discount_amount:                                     # 3. fixed amounts
        base -= abs(line.discount_amount)
    for effect, v in values:
        base += {"amount_off": -abs(v), "amount_added": v, "per_unit_added": line.quantity * v}.get(effect, 0)
    return base


def line_math(line: LineItem, rules: list[ColumnRule] = ()) -> str | None:
    if abs(round(expected_amount(line, rules), 2) - line.amount) <= 0.011:
        return None
    text = f"quantity {line.quantity:g} x price {fmt(line.unit_price)}"
    if line.discount_percent:
        text += f" less {line.discount_percent:g}%"
    if line.discount_amount:
        text += f" less {fmt(abs(line.discount_amount))}"
    text += f" is not the amount {fmt(line.amount)}"
    used = {r.column.strip().casefold() for r in rules}
    unexplained = [f"{c.column} = {c.value}" for c in line.other_columns
                   if c.column.strip().casefold() not in used and value_of(c.value) not in (None, 0)]
    if unexplained:  # show the person what else was printed on the line, instead of a bare "doesn't add up"
        text += f" (this line also shows: {', '.join(unexplained)})"
    return text


def with_math_corrected(lines: list[LineItem], rules: list[ColumnRule] = ()) -> list[LineItem]:
    """The lines with every math-failing amount replaced by what the math says. Used ONLY to ask
    'is the page/invoice total off by exactly those already-flagged lines?' - never to fix data."""
    return [l.model_copy(update={"amount": round(expected_amount(l, rules), 2)}) if line_math(l, rules) else l
            for l in lines]


def page_math(page: PageData) -> tuple[str | None, bool]:
    """Check the page's lines against the control totals printed on the page.
    Returns (problem or None, whether the page HAD a control total to check against)."""
    lines_sum = round(sum(l.amount for l in page.lines), 2)
    if page.brought_forward is not None or page.carried_forward is not None:
        start = page.brought_forward or 0.0
        end = page.carried_forward if page.carried_forward is not None else page.subtotal
        if end is None:
            return "page has 'brought forward' but no closing total to check against", False
        if not same(start + lines_sum, end):
            return (f"lines on this page add up to {fmt(lines_sum)}, but the page's printed totals say "
                    f"{fmt(end - start)} ({fmt(start)} brought forward -> {fmt(end)})"), True
        return None, True
    if page.page_subtotal is not None:
        if not same(lines_sum, page.page_subtotal):
            return (f"lines on this page add up to {fmt(lines_sum)}, but 'Subtotal this page' "
                    f"says {fmt(page.page_subtotal)}"), True
        return None, True
    return None, False  # nothing printed on this page to check against


def totals_math(subtotal: float, tax: float, total: float, rows: list[TotalsRow]) -> list[str]:
    """The totals block: subtotal, then every tax/discount/charge row, must reach the amount due.
    A running 'subtotal' row (e.g. 'Adjusted subtotal') must equal everything above it."""
    plain_ok = same(subtotal + tax, total)
    if not rows:
        return [] if plain_ok else [f"subtotal {fmt(subtotal)} + tax {fmt(tax)} is not the total {fmt(total)}"]
    problems, running = [], subtotal
    for r in rows:
        if r.kind == "subtotal":
            if not same(running, r.amount):
                problems.append(f"'{r.label}' says {fmt(r.amount)}, but the rows above it add up to {fmt(running)}")
        else:
            running = round(running + r.amount, 2)
    if not same(running, total) and plain_ok and same(sum(r.amount for r in rows if r.kind == "tax"), tax):
        return []  # the listed rows can't be the real block (e.g. a line item listed again), and the
        #            printed subtotal + tax = total: nothing wrong with what we keep (the pipeline drops the rows)
    if not same(running, total):
        problems.append(f"the subtotal plus the {len(rows)} rows under it make {fmt(running)}, "
                        f"but the amount due is {fmt(total)}")
    return problems


def code_consistency(lines: list[LineItem]) -> dict[int, str]:
    """One item code = one product. If the same code is described differently on different lines,
    the odd ones out are suspects (a wrapped row cut off, a code or name from the wrong row).
    Works on scans too, where there is no printed text to compare with. Returns index -> problem."""
    groups: dict[str, list[int]] = {}
    for i, l in enumerate(lines):
        if l.sku.strip():
            groups.setdefault(l.sku.strip().casefold(), []).append(i)
    problems: dict[int, str] = {}
    for idx in groups.values():
        names = Counter(" ".join(lines[i].description.split()).casefold() for i in idx)
        if len(names) < 2:
            continue
        (top, n), (_, second) = names.most_common(2)
        for i in idx:
            if " ".join(lines[i].description.split()).casefold() != top or n == second:
                majority = next(lines[j].description for j in idx
                                if " ".join(lines[j].description.split()).casefold() == top)
                problems[i] = (f"item code {lines[i].sku} is '{majority}' on {n} other lines of this invoice"
                               if n > second else f"item code {lines[i].sku} has different descriptions on this invoice")
    return problems


# ---------- 2. Position (digital PDFs) ----------

@dataclass
class PrintedLine:
    """One line item as it is really printed: every word of its row + the rows wrapped under it."""
    words: list[str]


def printed_rows(page: pymupdf.Page) -> list[tuple[float, list[tuple[float, str]]]]:
    """Group the page's words into visual rows (same height on the page), top to bottom.
    Each word comes with its left edge, so we know which column it starts in."""
    words = sorted(page.get_text("words"), key=lambda w: (round(w[3], 0), w[0]))  # w = x0,y0,x1,y1,text,...
    rows: list[tuple[float, list]] = []
    for w in words:
        if rows and abs(rows[-1][0] - w[3]) < 2.5:
            rows[-1][1].append(w)
        else:
            rows.append((w[3], [w]))
    return [(y, [(w[0], w[4]) for w in sorted(ws, key=lambda w: w[0])]) for y, ws in rows]


def printed_lines(page: pymupdf.Page) -> list[PrintedLine]:
    """A line item row has a price AND an amount (2+ money values). Rows right below it with no money
    values belong to it (wrapped description, wrapped item code, a note) - but only if they start in a
    column where item rows start; text elsewhere (a centred section heading, totals) ends the item."""
    rows = printed_rows(page)

    def item_row(ws: list[tuple[float, str]]) -> bool:
        money = [k for k, (_, t) in enumerate(ws) if MONEY.match(t)]
        # The amounts sit at the END of an item row (at most a short flag like a tax code after them);
        # a sentence that merely contains two amounts, e.g. in a note, is not an item.
        return len(money) >= 2 and money[-1] >= len(ws) - 3

    is_item = [item_row(ws) for _, ws in rows]
    # Column starts: left edges where words begin on many item rows (code, description...).
    # A one-off position - like a centred section heading - is not one.
    edges = Counter(round(x) for (_, ws), item in zip(rows, is_item) if item for x, _ in ws)
    starts = {x for x, n in edges.items() if n >= max(1, 0.3 * sum(is_item))}
    items: list[PrintedLine] = []
    last_y = None
    for (y, ws), item in zip(rows, is_item):
        texts = [t for _, t in ws]
        if item:
            items.append(PrintedLine(texts))
            last_y = y
        elif (items and last_y is not None and y - last_y < 20 and not any(MONEY.match(t) for t in texts)
              and any(abs(ws[0][0] - s) <= 3 for s in starts)):
            items[-1].words += texts
            last_y = y
        else:
            last_y = None  # a heading/totals/footer row ends the item above it
    return items


def norm(word: str) -> str:
    return word.casefold()


# Nothing is lost by skipping these: punctuation, a zero, or a label word like "Note:" / "Ref:".
IGNORABLE = re.compile(r"[^\w]+|0*[.,]?0+%?|[.,]0+|[^\d\s:]+:")


def code_parts(words: list[str], code: str, start: int = 0, depth: int = 4) -> list[int] | None:
    """Positions of the printed words that, in printed order, spell `code` (1 to 4 parts), or None."""
    if not code:
        return []
    if depth == 0:
        return None
    for i in range(start, len(words)):
        if words[i] and code.startswith(words[i]):
            rest = code_parts(words, code[len(words[i]):], i + 1, depth - 1)
            if rest is not None:
                return [i] + rest
    return None


def compare_line(read: LineItem, printed: PrintedLine) -> str | None:
    """Everything the AI read must be printed on this row, and every printed word must be accounted
    for. Returns the first difference in plain English, or None."""
    bag = [norm(w) for w in printed.words]

    def take_number(v: float) -> bool:
        for i, t in enumerate(bag):
            if any(abs(x - v) < CENT for x in number_values(t)):
                del bag[i]
                return True
        return False

    def take_word(w: str) -> bool:
        if w in bag:
            bag.remove(w)
            return True
        return bool(number_values(w)) and take_number(number_values(w)[0])

    if not take_number(read.amount):
        return f"amount {fmt(read.amount)} is not printed on this row"
    if not take_number(read.unit_price):
        return f"price {fmt(read.unit_price)} is not printed on this row"
    for name, v in [("discount", read.discount_amount), ("discount %", read.discount_percent)]:
        if v and not take_number(v) and not take_number(-v):
            return f"{name} {v:g} is not printed on this row"
    if not take_number(read.quantity):
        return f"quantity {read.quantity:g} is not printed on this row"
    for c in read.other_columns:
        if not all(take_word(norm(w)) for w in c.value.split()):
            return f"{c.column} '{c.value}' is not printed on this row"
    # Text before the code, so a code part can't borrow a description word ("Caster" vs "CASTER-").
    if not all(take_word(norm(w)) for w in read.description.split()):
        return f"description: read '{read.description}', but the row shows '{' '.join(bag)}'"
    if read.sku:
        code = norm(read.sku).replace(" ", "")
        parts = code_parts(bag, code)  # a code can wrap over several rows: "HW-" / "CASTER-" / "4SW"
        if parts is None:
            # Say exactly what IS printed, so a re-read can fix it (e.g. a hyphen lost at the wrap).
            left = Counter(bag)
            rest = [w for w in printed.words if left[norm(w)] > 0]
            near = code_parts([w.casefold().replace("-", "") for w in rest], code.replace("-", ""))
            if near:
                return (f"item code {read.sku} is printed as '{''.join(rest[k] for k in near)}'"
                        + (" (wrapped over several rows)" if len(near) > 1 else ""))
            return f"item code {read.sku} is not printed on this row"
        for k in sorted(parts, reverse=True):
            del bag[k]
    leftover = [t for t in bag if not IGNORABLE.fullmatch(t)]
    if leftover:
        return f"printed on this row but not read: '{' '.join(leftover)}'"
    return None


# ---------- Page and invoice checks ----------

@dataclass
class PageCheck:
    page_no: int
    scanned: bool                     # no stored text -> position check impossible
    has_control_total: bool           # the page printed a total we could check its lines against
    problems: list[str] = field(default_factory=list)            # whole-page problems
    line_problems: dict[int, str] = field(default_factory=dict)  # line index on the page -> problem

    @property
    def position_verified(self) -> bool:
        return not self.scanned and not self.problems and not self.line_problems

    @property
    def count(self) -> int:  # how bad is this reading? (used to pick the best of several readings)
        return len(self.problems) + len(self.line_problems)

    def feedback(self) -> str:
        """Plain-English list of what's wrong, handed to the AI when the page is read again."""
        return "\n".join([f"- {p}" for p in self.problems] +
                         [f"- line {i + 1}: {p}" for i, p in sorted(self.line_problems.items())])


def check_page(page_no: int, reading: PageData, page: pymupdf.Page,
               prev_carried: float | None = None, rules: list[ColumnRule] = ()) -> PageCheck:
    """prev_carried: the previous page's 'carried forward', if known. When this page's own
    'brought forward' was misread, the previous page's (math-proven) figure is used instead.
    rules: proven meanings of extra columns (columns.py)."""
    scanned = len(page.get_text("words")) < 20
    if (prev_carried is not None and reading.brought_forward is not None
            and not same(prev_carried, reading.brought_forward) and page_math(reading)[0]
            and not page_math(reading.model_copy(update={"brought_forward": prev_carried}))[0]):
        reading = reading.model_copy(update={"brought_forward": prev_carried})
    problem, has_control = page_math(reading)
    check = PageCheck(page_no, scanned, has_control)
    if problem and page_math(reading.model_copy(update={"lines": with_math_corrected(reading.lines, rules)}))[0]:
        # Only a problem for the whole page if the already-flagged lines DON'T explain it.
        # (One misread amount makes the page total off by exactly that much: flag that line, not all 36.)
        check.problems.append(problem)
    for i, line in enumerate(reading.lines):
        if p := line_math(line, rules):
            check.line_problems[i] = p
        elif not line.description.strip():
            check.line_problems[i] = "line has no description"
        elif line.quantity == 0 and line.amount == 0:  # 0 x 0 = 0 "adds up", but proves nothing
            check.line_problems[i] = "line has no quantity and no amount"
    if not scanned:
        printed = printed_lines(page)
        if len(printed) != len(reading.lines):
            check.problems.append(f"the page prints {len(printed)} line items, but {len(reading.lines)} were read")
        else:
            for i, (line, row) in enumerate(zip(reading.lines, printed)):
                if (p := compare_line(line, row)) and i not in check.line_problems:
                    check.line_problems[i] = f"doesn't match the printed row: {p}"
    return check


def check_reviewed(invoice: Invoice, rules: list[ColumnRule] = ()) -> list[str]:
    """Step 7: re-check an invoice after a PERSON edited it on the review screen, before saving.
    Same math as always, on the whole edited invoice. Empty list = everything adds up."""
    problems = []
    labels = {"supplier_name": "Supplier", "invoice_number": "Invoice number",
              "invoice_date": "Invoice date", "currency": "Currency"}
    for name, label in labels.items():
        if not str(getattr(invoice, name)).strip():
            problems.append(f"{label} is empty")
    if invoice.invoice_date.strip():
        try:
            date.fromisoformat(invoice.invoice_date.strip())
        except ValueError:
            problems.append(f"Invoice date '{invoice.invoice_date}' is not a date like 2026-03-12")
    if invoice.currency.strip() and not re.fullmatch(r"[A-Za-z]{3}", invoice.currency.strip()):
        problems.append(f"Currency '{invoice.currency}' should be a 3-letter code like USD")
    if not invoice.lines:
        problems.append("The invoice has no line items")
    for i, line in enumerate(invoice.lines, start=1):
        if not line.description.strip():
            problems.append(f"Line {i}: description is empty")
        if p := line_math(line, rules):
            problems.append(f"Line {i}: {p}")
    lines_sum = round(sum(l.amount for l in invoice.lines), 2)
    if invoice.lines and not same(lines_sum, invoice.subtotal):
        problems.append(f"The lines add up to {fmt(lines_sum)}, but the subtotal is {fmt(invoice.subtotal)}")
    problems += [p[0].upper() + p[1:] for p in
                 totals_math(invoice.subtotal, invoice.tax, invoice.total, invoice.totals_rows)]
    return problems


def check_invoice(readings: list[PageData], checks: list[PageCheck],
                  rules: list[ColumnRule] = ()) -> tuple[list[str], set[int]]:
    """Checks across the whole invoice. Returns (problems, page numbers to blame)."""
    problems: list[str] = []
    blame: set[int] = set()
    first = readings[0]
    for name in ["supplier_name", "invoice_number", "invoice_date", "currency"]:
        if not any(getattr(r, name) for r in readings):
            problems.append(f"{name.replace('_', ' ')} was not found")
            blame.add(1)
    if first.invoice_date:
        try:
            date.fromisoformat(first.invoice_date)
        except ValueError:
            problems.append(f"invoice date '{first.invoice_date}' is not a real date")
            blame.add(1)

    totals = next((r for r in reversed(readings) if r.total is not None), None)
    if totals is None or totals.subtotal is None or (totals.tax is None and not totals.totals_rows):
        problems.append("the final totals (subtotal, tax, total) were not found")
        blame.add(len(readings))
        return problems, blame
    if block := totals_math(totals.subtotal, totals.tax or 0.0, totals.total, totals.totals_rows):
        problems += block
        blame.add(readings.index(totals) + 1)

    # Page N must start where page N-1 ended.
    for n in range(1, len(readings)):
        before, after = readings[n - 1].carried_forward, readings[n].brought_forward
        # Explained if page N adds up perfectly using page N-1's figure: only the printed
        # 'brought forward' number was misread, not any line.
        misread_only = before is not None and not page_math(readings[n].model_copy(update={"brought_forward": before}))[0]
        if before is not None and after is not None and not same(before, after) and not misread_only:
            problems.append(f"page {n} carries {fmt(before)} forward, but page {n + 1} brings forward {fmt(after)}")
            blame |= {n, n + 1}

    lines_sum = round(sum(l.amount for r in readings for l in r.lines), 2)
    explained_sum = round(sum(l.amount for r in readings for l in with_math_corrected(r.lines, rules)), 2)
    if not same(lines_sum, totals.subtotal) and not same(explained_sum, totals.subtotal):
        # (if the difference is exactly the already-flagged math errors, those lines carry the flag)
        # Pages whose lines are proven - by their own control total, or line by line against the
        # printed rows - can't be the cause; blame the others. If every page is proven, the lines
        # were read right and the supplier's own invoice doesn't add up: that goes to a person as
        # an invoice problem, without pretending the (correct) lines are wrong.
        unproven = {c.page_no for c in checks
                    if (not c.has_control_total or c.problems) and not c.position_verified}
        if unproven:
            problems.append(f"all lines add up to {fmt(lines_sum)}, but the invoice subtotal is {fmt(totals.subtotal)}")
            blame |= unproven
        else:
            problems.append(f"every line matches the printed invoice, but they add up to {fmt(lines_sum)} while "
                            f"the invoice's own subtotal says {fmt(totals.subtotal)}: the supplier's invoice "
                            f"doesn't add up")
    return problems, blame
