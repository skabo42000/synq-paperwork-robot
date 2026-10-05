"""Step 1: the test script. Grade an extractor on every test invoice, in plain code.

Run:   uv run python -m synq_paperwork.evals <extractor> [filter]
       uv run python -m synq_paperwork.evals answer-key         (must score 100%: proves the grader works)
       uv run python -m synq_paperwork.evals empty              (must score 0%: the "before any AI" baseline)
       uv run python -m synq_paperwork.evals broken             (4 known mistakes per invoice: must catch all 4)
       uv run python -m synq_paperwork.evals answer-key 30p     (only cases whose id contains "30p")
       uv run python -m synq_paperwork.evals lite-3.1 1p,5p     (several filters, comma-separated)

Why plain code and not an AI judge (like synq-ai-squad used)? Every invoice comes with an exact
answer key, so "is this number right?" has one true answer. Code checks it perfectly, for free.

What we measure per invoice:
  header    - how many of the 7 header fields are exactly right (supplier, number, date, currency, 3 totals)
  found     - share of the real line items that were read exactly right (all 5 fields)
  wrong     - extracted lines that match no real line: misread OR invented. Dangerous: must be 0.
  perfect   - everything right. This is what "no one needs to touch it" means.

Step 4 - extractors that also CHECK their work (e.g. "checked") are graded on what matters most:
  silent    - wrong lines that were NOT flagged for a person. The dangerous number: must be 0.
  approved-but-wrong - invoices marked "verified" that aren't perfect. Must be 0.
  flagged   - lines a person has to look at (the workload); false alarms = flagged but actually right.
"""

import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from synq_paperwork.extract import extract
from synq_paperwork.pipeline import Report, extract_by_pages, extract_checked
from synq_paperwork.make_testdata import ANSWER_DIR, PDF_DIR, PROJECT_ROOT, TESTDATA
from synq_paperwork.schema import Invoice, LineItem

RESULTS_DIR = PROJECT_ROOT / "evals" / "results"
HEADER_FIELDS = ["supplier_name", "invoice_number", "invoice_date", "currency", "subtotal", "tax", "total"]


def load_answer(case_id: str) -> Invoice:
    return Invoice.model_validate_json((ANSWER_DIR / f"{case_id}.json").read_text(encoding="utf-8"))


# ---------- Extractors: anything that turns a PDF into an Invoice ----------
# Step 2 adds the real AI extractor here. These two exist to test the grader itself.

def extract_empty(case_id: str) -> Invoice:
    """Knows nothing: the score before any AI."""
    return Invoice(supplier_name="", invoice_number="", invoice_date="", currency="",
                   lines=[], subtotal=0, tax=0, total=0)


def extract_answer_key(case_id: str) -> Invoice:
    """Cheats by returning the answer key: must score 100%, or the grader is broken."""
    return load_answer(case_id)


def extract_broken(case_id: str) -> Invoice:
    """Makes 4 known mistakes. The grader must report exactly: header 6/7, 2 lines not found, 2 wrong."""
    inv = load_answer(case_id)
    inv.total += 10                               # 1. wrong total
    inv.lines[0].amount += 1                      # 2. one misread amount
    inv.lines.pop()                               # 3. one missing line
    inv.lines.append(LineItem(sku="XX-1", description="Invented item", quantity=1,
                              unit_price=9.99, amount=9.99))  # 4. one made-up line
    return inv


def ai_extractor(model: str | None) -> Callable[[str], Invoice]:
    return lambda case_id: extract(PDF_DIR / f"{case_id}.pdf", model)


EXTRACTORS: dict[str, Callable[[str], Invoice]] = {
    "empty": extract_empty,
    "answer-key": extract_answer_key,
    "broken": extract_broken,
    # Step 2: the whole PDF in one AI call. "ai" = what the product uses (default model + fallback).
    "ai": ai_extractor(None),
    "lite-3.1": ai_extractor("gemini-3.1-flash-lite"),
    "lite-3.5": ai_extractor("gemini-3.5-flash-lite"),
    "flash-3.5": ai_extractor("gemini-3.5-flash"),
    "flash-3.8": ai_extractor("gemini-3.8-flash"),
    # Step 3: page by page, in parallel, then stitched (default model + fallback).
    "pages": lambda case_id: extract_by_pages(PDF_DIR / f"{case_id}.pdf"),
    # Step 4: + code checks, re-reads of failing pages, and a verified / needs-review report.
    "checked": lambda case_id: extract_checked(PDF_DIR / f"{case_id}.pdf"),
}


# ---------- Grading ----------

def norm_text(s: str) -> str:
    return " ".join(s.split()).casefold()  # ignore extra spaces and upper/lower case


def same_value(expected, got) -> bool:
    if isinstance(expected, float):
        return isinstance(got, (int, float)) and abs(expected - got) < 0.005  # within half a cent
    return norm_text(str(expected)) == norm_text(str(got))


def line_key(l: LineItem) -> tuple:
    # Two lines are "the same" only if all 5 fields match. Money rounded to cents.
    return (norm_text(l.sku), norm_text(l.description), round(l.quantity, 3),
            round(l.unit_price, 2), round(l.amount, 2))


def grade(expected: Invoice, got: Invoice) -> dict:
    header_ok = [f for f in HEADER_FIELDS if same_value(getattr(expected, f), getattr(got, f))]
    # Counter = a bag of lines, so a line that appears twice must be found twice.
    want, have = Counter(map(line_key, expected.lines)), Counter(map(line_key, got.lines))
    matched = sum((want & have).values())
    return {
        "header_ok": len(header_ok),
        "header_wrong": [f"{f}: expected {getattr(expected, f)!r}, got {getattr(got, f)!r}"
                         for f in HEADER_FIELDS if f not in header_ok],
        "lines_expected": len(expected.lines),
        "lines_extracted": len(got.lines),
        "lines_found": matched,
        "lines_wrong": len(got.lines) - matched,
        "perfect": len(header_ok) == len(HEADER_FIELDS) and matched == len(expected.lines) == len(got.lines),
        # The actual mistakes (first 20 of each), so we can see WHAT goes wrong, not just how often.
        "lines_missing": [list(k) for k in (want - have).elements()][:20],
        "lines_wrong_detail": [list(k) for k in (have - want).elements()][:20],
    }


def grade_report(expected: Invoice, got: Invoice, report: Report, graded: dict) -> dict:
    """Did the checks flag every wrong line? Walk the extracted lines: each one either matches a
    real line (correct) or not (wrong). A wrong line without a flag is a SILENT error."""
    remaining = Counter(map(line_key, expected.lines))
    silent, false_alarms = [], 0
    for line, flagged in zip(got.lines, report.line_flags):
        key = line_key(line)
        correct = remaining[key] > 0
        if correct:
            remaining[key] -= 1
        if not correct and not flagged:
            silent.append(list(key))
        false_alarms += correct and flagged
    return {
        "status": report.status,
        "approved_but_wrong": report.status == "verified" and not graded["perfect"],
        "silent_errors": len(silent),
        "silent_detail": silent[:20],
        "flagged": sum(report.line_flags),
        "false_alarms": false_alarms,
        "reads": report.reads,
        "pages_reread": report.pages_reread,
        "problems": report.problems[:20],
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows terminals can't print some AI characters otherwise
    if len(sys.argv) < 2 or sys.argv[1] not in EXTRACTORS:
        sys.exit(f"Usage: python -m synq_paperwork.evals <{'|'.join(EXTRACTORS)}> [filter]")
    name, extractor = sys.argv[1], EXTRACTORS[sys.argv[1]]
    cases = json.loads((TESTDATA / "cases.json").read_text(encoding="utf-8"))
    filters = sys.argv[2].split(",") if len(sys.argv) > 2 else [""]  # e.g. "1p,5p" = 1- and 5-page cases
    cases = [c for c in cases if any(f in c["id"] for f in filters)]

    results = []
    for case in cases:
        start = time.time()
        try:
            expected, got = load_answer(case["id"]), extractor(case["id"])
            if isinstance(got, tuple):  # (invoice, report): an extractor that checks its own work
                got, report = got
                r = grade(expected, got)
                r |= grade_report(expected, got, report, r)
            else:
                r = grade(expected, got)
        except Exception as e:  # one broken invoice shouldn't stop the whole run
            first_line = str(e).strip().splitlines()[0][:150]  # the full error can be pages long
            r = {"header_ok": 0, "header_wrong": [f"Crashed: {type(e).__name__}: {first_line}"],
                 "lines_expected": case["lines"], "lines_extracted": 0, "lines_found": 0, "lines_wrong": 0,
                 "perfect": False}
        r |= {"id": case["id"], "seconds": round(time.time() - start, 1)}
        results.append(r)
        print(f"  {'PERFECT' if r['perfect'] else 'errors ':8} {r['id']:24} header {r['header_ok']}/7  "
              f"lines found {r['lines_found']:>4}/{r['lines_expected']:<4}  wrong {r['lines_wrong']:>3}  "
              f"{r['seconds']:>6}s")
        if "status" in r:
            print(f"           {r['status'].upper():13} flagged {r['flagged']:>4} lines "
                  f"(false alarms {r['false_alarms']}), SILENT ERRORS {r['silent_errors']}, "
                  f"{r['reads']} reads, re-read pages {r['pages_reread'] or 'none'}"
                  + ("   <<< APPROVED BUT WRONG" if r["approved_but_wrong"] else ""))
        for problem in r["header_wrong"]:
            print(f"           - {problem}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = RESULTS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}_{name}.json"
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")

    total_lines = sum(r["lines_expected"] for r in results)
    print("\n" + "=" * 70)
    print(f"Extractor: {name}")
    print(f"PERFECT DOCUMENTS : {sum(r['perfect'] for r in results)}/{len(results)}")
    print(f"Header fields     : {sum(r['header_ok'] for r in results)}/{7 * len(results)}")
    print(f"Lines found       : {sum(r['lines_found'] for r in results)}/{total_lines} "
          f"({100 * sum(r['lines_found'] for r in results) / total_lines:.1f}%)")
    print(f"Lines wrong/made up: {sum(r['lines_wrong'] for r in results)}")
    checked = [r for r in results if "status" in r]
    if checked:
        print(f"--- checks ---")
        print(f"Verified (safe to send on): {sum(r['status'] == 'verified' for r in checked)}/{len(checked)} invoices")
        print(f"SILENT ERRORS      : {sum(r['silent_errors'] for r in checked)}   (wrong lines not flagged: must be 0)")
        print(f"APPROVED BUT WRONG : {sum(r['approved_but_wrong'] for r in checked)}   (must be 0)")
        print(f"Lines for a person : {sum(r['flagged'] for r in checked)}/{total_lines} "
              f"(false alarms {sum(r['false_alarms'] for r in checked)})")
        print(f"AI reads           : {sum(r['reads'] for r in checked)}")
    print(f"Results saved: {out_file.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
