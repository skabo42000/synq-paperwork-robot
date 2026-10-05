"""Step 1: make fake supplier invoices + their answer keys (our test set).

Run:  uv run python -m synq_paperwork.make_testdata

Why fake invoices?
  - We know the correct value of EVERY field, so grading is exact (no AI judge needed).
  - No real client data leaves anyone's computer (the free AI plan may keep what we send).
  - A fixed random "seed" per invoice means running this again makes the exact same files.

Output:
  testdata/invoices/<id>.pdf   - the document the AI will read
  testdata/answers/<id>.json   - the correct data (an Invoice, see schema.py)
  testdata/cases.json          - list of all test cases

Traps built in, because real invoices have them too:
  - a "Customer PO" number next to the invoice number, and a ship/due date next to the invoice date
  - long descriptions that wrap onto two lines
  - "Brought forward / Carried forward" running totals, or per-page subtotals (these are NOT line items)
  - discount lines with negative amounts, written as -25.00 or ($25.00)
  - "scanned" copies: slightly tilted, grainy images with no selectable text
"""

import io
import json
import random
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymupdf  # reads and renders PDFs
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from synq_paperwork.schema import Invoice, LineItem

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TESTDATA = PROJECT_ROOT / "testdata"
PDF_DIR = TESTDATA / "invoices"
ANSWER_DIR = TESTDATA / "answers"

CENT = Decimal("0.01")
BILL_TO = ["Demo Company LLC", "1200 Harbor Street, Suite 4", "Columbus, OH 43215"]

# name, address, SKU prefix, catalog of (description, lowest price, highest price, sold by weight/length?)
SUPPLIERS = {
    "building": ("Northfield Building Supply", ["418 Quarry Road", "Dayton, OH 45402"], "NB", [
        ("2x4x8 Kiln-Dried Stud, Premium Grade", 3.5, 6.5, False),
        ("1/2 in. x 4 ft. x 8 ft. Drywall Panel, Moisture Resistant, Paper-Faced, Tapered Edge", 14, 22, False),
        ("Concrete Mix 80 lb Bag", 5, 8, False),
        ("Wood Screws #8 x 2-1/2 in., Box of 100", 7, 12, False),
        ("PVC Pipe Schedule 40, 3/4 in. x 10 ft.", 4, 9, False),
        ("Copper Wire 12 AWG THHN Solid, Sold by the Foot", 0.3, 0.9, True),
        ("Exterior Latex Paint, Satin, White, 5 Gallon Bucket, Mildew-Resistant Finish", 120, 190, False),
        ("Construction Adhesive 10 oz Cartridge", 4, 7, False),
        ("Pressure-Treated Plywood 3/4 in. x 4 ft. x 8 ft.", 45, 70, False),
        ("Roofing Nails 1-1/4 in. Galvanized, 5 lb Box", 14, 24, False),
        ("Fiberglass Insulation Batt R-13, 15 in. x 93 in., 40 sq. ft. per Bag", 30, 48, False),
        ("Rebar #4 x 20 ft.", 9, 15, False),
    ]),
    "food": ("Harbor Fresh Foodservice", ["77 Market Lane", "Toledo, OH 43604"], "HF", [
        ("Chicken Breast, Boneless Skinless, Fresh (per lb)", 2.8, 4.9, True),
        ("Roma Tomatoes, Case 25 lb", 22, 38, False),
        ("Extra Virgin Olive Oil 3 L Tin", 28, 44, False),
        ("Mozzarella, Whole Milk, Low Moisture, Shredded, 4 x 5 lb Bags per Case", 55, 85, False),
        ("Paper Napkins, 2-Ply Dinner, White, Pack of 3000", 38, 60, False),
        ("Ground Beef 80/20 (per lb)", 3.5, 5.8, True),
        ("Yellow Onions, 50 lb Sack", 18, 32, False),
        ("All-Purpose Flour, 50 lb Bag", 19, 29, False),
        ("Heavy Cream 36%, 1/2 Gallon", 6, 10, False),
        ("Takeout Containers, Hinged, 9 x 9 in., Compostable, Case of 200", 45, 72, False),
        ("Atlantic Salmon Fillet, Skin-On (per lb)", 8, 13, True),
        ("Basil, Fresh, 1 lb", 9, 16, False),
    ]),
    "office": ("Summit Office & Janitorial", ["9 Commerce Park Drive", "Akron, OH 44308"], "SO", [
        ("Copy Paper, Letter, 20 lb, 10 Reams per Case", 42, 60, False),
        ("Black Toner Cartridge, High Yield, Compatible", 65, 120, False),
        ("Trash Bags 55 Gallon Heavy Duty, Box of 100", 28, 45, False),
        ("Disinfecting Wipes, Lemon, Canister of 75", 5, 9, False),
        ("Ballpoint Pens, Medium, Blue, Box of 12", 3, 7, False),
        ("Hand Soap Refill, Foaming, Unscented, 1200 mL, Fits Most Touch-Free Dispensers", 12, 20, False),
        ("Paper Towels, Multifold, White, 16 Packs per Case", 30, 48, False),
        ("File Folders, Letter, 1/3 Cut, Manila, Box of 100", 9, 16, False),
        ("Floor Cleaner Concentrate, Neutral pH, 1 Gallon", 14, 24, False),
        ("Sticky Notes 3 x 3 in., Yellow, 24 Pads", 11, 19, False),
        ("Nitrile Gloves, Powder-Free, Large, Box of 100", 8, 15, False),
        ("Desk Chair Mat, Hard Floor, 36 x 48 in.", 30, 55, False),
    ]),
}

# id, layout, supplier, pages, scanned, seed
CASES = [
    ("inv01-classic-1p", "classic", "building", 1, False, 101),
    ("inv02-modern-1p", "modern", "food", 1, False, 102),
    ("inv03-classic-5p", "classic", "food", 5, False, 103),
    ("inv04-modern-5p", "modern", "office", 5, False, 104),
    ("inv05-classic-30p", "classic", "building", 30, False, 105),
    ("inv06-modern-30p", "modern", "food", 30, False, 106),
    ("inv07-scan-classic-1p", "classic", "office", 1, True, 107),
    ("inv08-scan-modern-5p", "modern", "building", 5, True, 108),
    ("inv09-scan-classic-30p", "classic", "food", 30, True, 109),
]

# ---------- Page geometry (points; a letter page is 612 x 792) ----------
FONT, SIZE, ROW = "Helvetica", 9, 13
BOTTOM = 80  # rows stop above this line (footer below)
LAYOUTS = {
    # description column x/width, top of the table on page 1 and on later pages, tax rate
    "classic": {"desc_x": 125, "desc_w": 245, "top_first": 585, "top_other": 690, "tax": Decimal("0.075")},
    "modern": {"desc_x": 165, "desc_w": 235, "top_first": 600, "top_other": 700, "tax": Decimal("0.0825")},
}
TOTALS_ROWS = 5  # room needed under the last line for subtotal / tax / total


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------- 1. Invent the invoice content ----------

class Line:
    """One line while we build the invoice (Decimal for exact money math)."""

    def __init__(self, sku: str, desc: str, qty: Decimal, price: Decimal, layout: str):
        self.sku, self.desc, self.qty, self.price = sku, desc, qty, price
        self.amount = money(qty * price)
        self.wrapped = simpleSplit(desc, FONT, SIZE, LAYOUTS[layout]["desc_w"])  # text lines it needs


def random_line(rng: random.Random, supplier: str, layout: str) -> Line:
    _, _, prefix, catalog = SUPPLIERS[supplier]
    desc, low, high, by_weight = rng.choice(catalog)
    sku = f"{prefix}-{10000 + catalog.index((desc, low, high, by_weight)) * 137 % 90000}"
    price = money(Decimal(str(rng.uniform(low, high))))
    if by_weight:
        qty = Decimal(rng.randint(4, 120)) / 2 if rng.random() < 0.5 else Decimal(rng.randint(2, 80))
    else:
        qty = Decimal(rng.choice([1, 1, 2, 2, 3, 4, 5, 6, 8, 10, 12, 20, 24, 40]))
    return Line(sku, desc, qty, price, layout)


def paginate(lines: list[Line], layout: str) -> list[list[Line]]:
    """Split lines into pages the same way the drawing code will. Wrapped lines use 2 rows."""
    g = LAYOUTS[layout]
    pages: list[list[Line]] = [[]]
    used = 0

    def capacity(page_no: int) -> int:
        top = g["top_first"] if page_no == 0 else g["top_other"]
        rows = int((top - BOTTOM) / ROW) - 1  # minus the column header row
        rows -= 1                               # room for the page subtotal / carried forward row
        if layout == "classic" and page_no > 0:
            rows -= 1                           # "Brought forward" row at the top
        return rows

    for line in lines:
        if used + len(line.wrapped) > capacity(len(pages) - 1):
            pages.append([])
            used = 0
        pages[-1].append(line)
        used += len(line.wrapped)
    if used + TOTALS_ROWS > capacity(len(pages) - 1):
        pages.append([])  # totals don't fit: they go on an extra page (happens on real invoices too)
    return pages


def build_lines(rng: random.Random, supplier: str, layout: str, target_pages: int) -> list[Line]:
    lines: list[Line] = []
    while True:
        lines.append(random_line(rng, supplier, layout))
        if len(paginate(lines + extra_lines(layout), layout)) > target_pages:
            lines.pop()
            return lines + extra_lines(layout)


def extra_lines(layout: str) -> list[Line]:
    # Real invoices end with lines that have no item code: a discount and a delivery charge.
    return [
        Line("", "Loyalty discount", Decimal(1), Decimal("-25.00"), layout),
        Line("", "Delivery charge", Decimal(1), Decimal("45.00"), layout),
    ]


# ---------- 2. Draw the PDF ----------

def fmt_qty(q: Decimal) -> str:
    return f"{q:f}".rstrip("0").rstrip(".") if "." in f"{q:f}" else f"{q:f}"


def fmt_money(x: Decimal, layout: str) -> str:
    if layout == "classic":
        return f"{x:,.2f}"                                   # 1,234.50 and -25.00
    return f"(${-x:,.2f})" if x < 0 else f"${x:,.2f}"       # $1,234.50 and ($25.00)


def draw_invoice(path: Path, layout: str, supplier: str, pages: list[list[Line]], head: dict) -> None:
    name, address, _, _ = SUPPLIERS[supplier]
    g = LAYOUTS[layout]
    c = canvas.Canvas(str(path), pagesize=letter, invariant=1)  # invariant: no timestamp, same bytes every run
    c.setTitle(f"Invoice {head['number']}")
    running = Decimal(0)
    total_pages = len(pages)

    for page_no, page_lines in enumerate(pages):
        # --- header block ---
        if layout == "classic":
            c.setFont("Helvetica-Bold", 15); c.drawString(50, 745, name)
            c.setFont(FONT, SIZE)
            for i, a in enumerate(address):
                c.drawString(50, 730 - i * 12, a)
            c.setFont("Helvetica-Bold", 20); c.drawRightString(562, 745, "INVOICE")
            c.setFont(FONT, SIZE)
            c.drawRightString(562, 728, f"Invoice No: {head['number']}")
            c.drawRightString(562, 716, f"Date: {head['date']:%m/%d/%Y}")
            c.drawRightString(562, 704, f"Customer PO: {head['po']}")
            if page_no == 0:
                c.drawRightString(562, 692, f"Ship Date: {head['ship']:%m/%d/%Y}")
                c.setFont("Helvetica-Bold", SIZE); c.drawString(50, 670, "Bill To:")
                c.setFont(FONT, SIZE)
                for i, a in enumerate(BILL_TO):
                    c.drawString(50, 658 - i * 12, a)
        else:
            c.setFont("Helvetica-Bold", 22); c.drawString(50, 740, "TAX INVOICE")
            c.setFont("Helvetica-Bold", 11); c.drawRightString(562, 748, name)
            c.setFont(FONT, 8)
            for i, a in enumerate(address):
                c.drawRightString(562, 736 - i * 10, a)
            c.setFont(FONT, SIZE)
            c.drawString(50, 722, f"Invoice # {head['number']}")
            if page_no == 0:
                c.drawString(50, 708, f"Issued {head['date']:%B %d, %Y}")
                c.drawString(50, 696, f"Due {head['due']:%B %d, %Y}")
                c.drawString(330, 708, f"Ordered by: {BILL_TO[0]}")
                c.drawString(330, 696, f"Your order ref: {head['po']}")

        # --- column headers ---
        y = g["top_first"] if page_no == 0 else g["top_other"]
        c.setFont("Helvetica-Bold", SIZE)
        if layout == "classic":
            c.drawString(50, y, "Item Code"); c.drawString(125, y, "Description")
            c.drawRightString(430, y, "Qty"); c.drawRightString(495, y, "Unit Price"); c.drawRightString(562, y, "Amount")
        else:
            c.drawRightString(80, y, "Qty"); c.drawString(90, y, "Code"); c.drawString(165, y, "Description")
            c.drawRightString(470, y, "Price"); c.drawRightString(562, y, "Line Total")
        c.line(50, y - 4, 562, y - 4)
        y -= ROW
        c.setFont(FONT, SIZE)

        if layout == "classic" and page_no > 0:
            c.setFont("Helvetica-Oblique", SIZE)
            c.drawString(125, y, "Balance brought forward"); c.drawRightString(562, y, fmt_money(running, layout))
            c.setFont(FONT, SIZE)
            y -= ROW

        # --- line items ---
        page_sum = Decimal(0)
        for line in page_lines:
            if layout == "classic":
                c.drawString(50, y, line.sku)
                c.drawRightString(430, y, fmt_qty(line.qty))
                c.drawRightString(495, y, fmt_money(line.price, layout))
                c.drawRightString(562, y, fmt_money(line.amount, layout))
            else:
                c.drawRightString(80, y, fmt_qty(line.qty))
                c.drawString(90, y, line.sku)
                c.drawRightString(470, y, fmt_money(line.price, layout))
                c.drawRightString(562, y, fmt_money(line.amount, layout))
            for text in line.wrapped:
                c.drawString(g["desc_x"], y, text)
                y -= ROW
            page_sum += line.amount
        running += page_sum

        # --- bottom of the page ---
        last = page_no == total_pages - 1
        if not last:
            c.line(50, y + 8, 562, y + 8)
            c.setFont("Helvetica-Oblique", SIZE)
            if layout == "classic":
                c.drawString(125, y - 4, "Carried forward"); c.drawRightString(562, y - 4, fmt_money(running, layout))
            else:
                c.drawString(165, y - 4, "Subtotal this page"); c.drawRightString(562, y - 4, fmt_money(page_sum, layout))
            c.setFont(FONT, SIZE)
        else:
            c.line(350, y + 8, 562, y + 8)
            labels = (["Subtotal", f"Sales Tax ({float(head['tax_rate']) * 100:g}%)", "TOTAL DUE"] if layout == "classic"
                      else ["Net amount", "Sales tax", "Amount due (USD)"])
            for i, (label, value) in enumerate(zip(labels, [head["subtotal"], head["tax"], head["total"]])):
                c.setFont("Helvetica-Bold" if i == 2 else FONT, SIZE)
                c.drawString(370, y - 4 - i * ROW, label)
                c.drawRightString(562, y - 4 - i * ROW, fmt_money(value, layout))

        # --- footer ---
        c.setFont(FONT, 7)
        footer = ("All amounts in US dollars. Payment terms: Net 30." if layout == "classic"
                  else f"Thank you for your business. Questions? billing@example.com")
        c.drawString(50, 50, footer)
        c.drawRightString(562, 50, f"Page {page_no + 1} of {total_pages}")
        c.showPage()
    c.save()


def make_scanned(path: Path, rng: random.Random) -> None:
    """Turn a clean PDF into a 'scanned' one: grey, slightly tilted, grainy JPEG images, no text layer."""
    images = []
    with pymupdf.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            img = img.rotate(rng.uniform(-1.0, 1.0), resample=Image.BICUBIC, expand=False, fillcolor=255)
            noise = Image.frombytes("L", img.size, rng.randbytes(img.width * img.height))  # seeded grain
            img = Image.blend(img, noise, 0.12)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=50)  # low quality, like a cheap office scanner
            images.append(Image.open(buf).convert("L"))
    fixed = datetime(2026, 1, 1).timetuple()  # a fixed date inside the file, so every run makes identical bytes
    images[0].save(path, save_all=True, append_images=images[1:], resolution=110,
                   creationDate=fixed, modDate=fixed)


# ---------- 3. Put it together ----------

def make_case(case_id: str, layout: str, supplier: str, target_pages: int, scanned: bool, seed: int) -> dict:
    rng = random.Random(seed)
    lines = build_lines(rng, supplier, layout, target_pages)
    pages = paginate(lines, layout)

    invoice_date = date(2026, 1, 1) + timedelta(days=rng.randint(0, 240))
    subtotal = sum((l.amount for l in lines), Decimal(0))
    tax = money(subtotal * LAYOUTS[layout]["tax"])
    head = {
        "number": str(rng.randint(200000, 299999)) if layout == "classic" else f"INV-2026-{rng.randint(1, 9999):04d}",
        "po": str(rng.randint(40000, 69999)),
        "date": invoice_date,
        "ship": invoice_date - timedelta(days=rng.randint(1, 4)),
        "due": invoice_date + timedelta(days=30),
        "tax_rate": LAYOUTS[layout]["tax"],
        "subtotal": subtotal, "tax": tax, "total": subtotal + tax,
    }

    pdf_path = PDF_DIR / f"{case_id}.pdf"
    draw_invoice(pdf_path, layout, supplier, pages, head)
    if scanned:
        make_scanned(pdf_path, rng)

    answer = Invoice(
        supplier_name=SUPPLIERS[supplier][0],
        invoice_number=head["number"],
        invoice_date=invoice_date.isoformat(),
        currency="USD",
        lines=[LineItem(sku=l.sku, description=l.desc, quantity=float(l.qty),
                        unit_price=float(l.price), amount=float(l.amount)) for l in lines],
        subtotal=float(subtotal), tax=float(tax), total=float(subtotal + tax),
    )
    (ANSWER_DIR / f"{case_id}.json").write_text(answer.model_dump_json(indent=2), encoding="utf-8")
    return {"id": case_id, "layout": layout, "supplier": supplier, "pages": len(pages),
            "scanned": scanned, "lines": len(lines), "pdf_kb": round(pdf_path.stat().st_size / 1024)}


def verify_case(case: dict) -> list[str]:
    """Check the answer key against the text actually printed in the PDF (digital PDFs only)."""
    answer = Invoice.model_validate_json((ANSWER_DIR / f"{case['id']}.json").read_text(encoding="utf-8"))
    problems = []
    # 1. The key's own math must add up.
    for l in answer.lines:
        if money(Decimal(str(l.quantity)) * Decimal(str(l.unit_price))) != Decimal(str(l.amount)):
            problems.append(f"line math wrong: {l.description}")
    if abs(sum(l.amount for l in answer.lines) - answer.subtotal) > 0.005:
        problems.append("lines don't add up to the subtotal")
    if abs(answer.subtotal + answer.tax - answer.total) > 0.005:
        problems.append("subtotal + tax != total")
    # 2. What the key says must really be printed in the PDF.
    if not case["scanned"]:
        with pymupdf.open(PDF_DIR / f"{case['id']}.pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
        for label, value in [("invoice number", answer.invoice_number),
                             ("total", fmt_money(Decimal(str(answer.total)), case["layout"]))]:
            if value not in text:
                problems.append(f"{label} {value} not printed in the PDF")
        prefix = SUPPLIERS[case["supplier"]][2]
        codes_in_key = [l.sku for l in answer.lines if l.sku]
        codes_in_pdf = re.findall(rf"^{prefix}-\d+$", text, re.MULTILINE)
        only_key = Counter(codes_in_key) - Counter(codes_in_pdf)
        only_pdf = Counter(codes_in_pdf) - Counter(codes_in_key)
        if only_key or only_pdf:
            problems.append(f"item codes only in the key: {dict(only_key)}, only in the PDF: {dict(only_pdf)}")
    return problems


def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_DIR.mkdir(parents=True, exist_ok=True)
    cases = [make_case(*c) for c in CASES]
    (TESTDATA / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
    for c in cases:
        problems = verify_case(c)
        print(f"  {c['id']:24} {c['pages']:>2} pages  {c['lines']:>4} lines  "
              f"{'scanned' if c['scanned'] else 'digital':8} {c['pdf_kb']:>5} KB  "
              f"{'self-check OK' if not problems else 'PROBLEMS: ' + '; '.join(problems)}")


if __name__ == "__main__":
    main()
