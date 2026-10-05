"""Steps 3-5: read invoices page by page in parallel, CHECK every page in code, re-read the pages
that fail, cross-check scanned pages with a second reader + referee, then stitch - with every line
marked "verified" or "needs a person".

Try it:  uv run python -m synq_paperwork.pipeline testdata/invoices/inv05-classic-30p.pdf

Graph:
                    +-> read_page (page 1) -+
    START -> split -+-> read_page (page 2) -+-> check --(all proven, or out of tries)--> finalize -> END
                    +-> read_page (page N) -+     |
                              ^                   |
                              +---(re-read ONLY the failing pages, with the check's findings)
    check --(scanned pages)--> second_read (per page) -> compare --(disputes)--> referee -> finalize
                                                                 --(none)------------------> finalize
  Step 3 ideas:
  - FAN-OUT with Send: one read_page copy per page, run in parallel (max 4 at once for the free plan).
  - FAN-IN with a reducer: every copy ADDS its reading to a list; "check" runs when all are done.
  Step 4 ideas:
  - The CHECKER is plain code (checks.py): math + the PDF's own text positions. Unlike an AI critic,
    it can't be talked into agreeing, and it says exactly which line is wrong and why.
  - LOOP: failing pages go back to the AI with the findings ("line 7: price read 12.19, printed 12.91"),
    like the Writer <-> Critic loop in synq-ai-squad. Max 2 re-reads per page; never loops forever.
  - Every reading is kept; for each page we use the one with the fewest problems.
  - Nothing is ever marked verified without evidence.
  Step 5 ideas (crosscheck.py):
  - Scanned pages have no stored text, so a SECOND, independent reader (other model, other view of
    the page) reads them too; code compares the two line by line.
  - Only the rows they disagree on go to a blind REFEREE; 2 of 3 matching readings confirm a line.
"""

import operator
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypedDict

import pymupdf
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from synq_paperwork.checks import ColumnRule, PageCheck, check_invoice, check_page, code_consistency, same
from synq_paperwork.columns import ColumnFinding, extra_columns, investigate, prove, unexplained
from synq_paperwork.crosscheck import (READER_B_FALLBACK, READER_B_MODEL, REFEREE_DPI, REFEREE_FALLBACK, REFEREE_MODEL,
                                       Dispute, RefereeReport, compare, referee_prompt, resolve, sharpened_image)
from synq_paperwork.extract import INSTRUCTIONS, read_pdf
from synq_paperwork.schema import Invoice, PageData

MAX_PARALLEL = 4  # the free Gemini plan allows only so many requests per minute (6 hit the limit)
MAX_READS = 3     # per page: the first reading + at most 2 re-reads

PAGE_INSTRUCTIONS = INSTRUCTIONS + """

This is page {page_no} of {page_count} of ONE invoice. Read ONLY this page.
- Header fields (supplier, invoice number, date, currency): fill them only if printed on this page.
- Copy these control rows into their own fields, NEVER as lines:
  "Balance brought forward" -> brought_forward, "Subtotal this page" -> page_subtotal,
  "Carried forward" -> carried_forward.
- subtotal, tax, total, totals_rows: only if the final totals block is on this page, otherwise leave empty.
- A table can continue on this page without its header row being repeated: use the columns it has."""

REREAD = """

IMPORTANT: an automatic check of an earlier reading of this page found these problems:
{feedback}
Read the WHOLE page again carefully, row by row, and copy exactly what is printed."""


class Reading(TypedDict):
    page_no: int
    attempt: int
    data: PageData


class Report(BaseModel):
    """What the checks concluded. line_flags[i] belongs to invoice.lines[i]."""
    status: str                  # "verified" (safe to send on) or "needs review" (a person must look)
    problems: list[str]          # everything still wrong or unproven, in plain English
    line_flags: list[bool]       # True = a person must check this line
    line_notes: list[str]        # why ('' when verified)
    line_pages: list[int] = []   # the page each line was printed on (the review screen shows that page)
    reads: int                   # AI calls used (pages + re-reads)
    pages_reread: list[int]
    columns: list[ColumnFinding] = []    # extra columns found, what the AI thinks they are, what the math proved
    column_rules: list[ColumnRule] = []  # the proven ones: the review screen re-checks edits with them


class State(TypedDict):
    pdf_bytes: bytes
    model: str | None                                    # None = default model with fallback
    page_count: int
    readings: Annotated[list[Reading], operator.add]     # reducer: every (re-)read adds its reading
    best: dict[int, Reading]                             # page_no -> best reading so far
    checks: dict[int, PageCheck]                         # page_no -> check of that best reading
    invoice_problems: list[str]
    blame: list[int]                                     # pages suspected by the invoice-level checks
    second: Annotated[list[dict], operator.add]         # Step 5: reader B's readings of scanned pages
    disputes: dict[int, list[Dispute] | None]            # page_no -> lines A and B disagree on (None = B failed)
    verdicts: Annotated[list[dict], operator.add]       # the referee's readings of disputed rows
    rules: list[ColumnRule]                              # proven meanings of extra columns (columns.py)
    findings: list[ColumnFinding]
    investigated: list[str]                              # extra columns already investigated
    column_reads: int                                    # AI calls used to investigate columns
    invoice: Invoice
    report: Report


class PageTask(TypedDict):
    """The small input each parallel read_page copy receives (via Send)."""
    page_bytes: bytes
    page_no: int
    page_count: int
    model: str | None
    attempt: int
    feedback: str


def one_page_pdf(doc: pymupdf.Document, index: int) -> bytes:
    single = pymupdf.open()
    single.insert_pdf(doc, from_page=index, to_page=index)
    return single.tobytes()


def task(doc: pymupdf.Document, state: State, page_no: int, attempt: int, feedback: str = "") -> Send:
    return Send("read_page", {"page_bytes": one_page_pdf(doc, page_no - 1), "page_no": page_no,
                              "page_count": doc.page_count, "model": state["model"],
                              "attempt": attempt, "feedback": feedback})


# ---------- Nodes ----------

def split(state: State) -> dict:
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        return {"page_count": doc.page_count}


def launch_readers(state: State) -> list[Send]:
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        return [task(doc, state, n, attempt=1) for n in range(1, doc.page_count + 1)]


def read_page(t: PageTask) -> dict:
    prompt = PAGE_INSTRUCTIONS.format(page_no=t["page_no"], page_count=t["page_count"])
    if t["feedback"]:
        prompt += REREAD.format(feedback=t["feedback"])
    data = read_pdf(t["page_bytes"], prompt, PageData, t["model"])
    return {"readings": [{"page_no": t["page_no"], "attempt": t["attempt"], "data": data}]}


def pick_best(state: State, rules: list[ColumnRule]) -> tuple[dict[int, Reading], dict[int, PageCheck]]:
    best: dict[int, Reading] = {}
    checks: dict[int, PageCheck] = {}
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        for r in sorted(state["readings"], key=lambda r: (r["page_no"], r["attempt"])):
            # Pages go in order, so the previous page's best reading is already chosen.
            prev = best.get(r["page_no"] - 1)
            c = check_page(r["page_no"], r["data"], doc[r["page_no"] - 1],
                           prev_carried=prev["data"].carried_forward if prev else None, rules=rules)
            if r["page_no"] not in best or c.count < checks[r["page_no"]].count:
                best[r["page_no"]], checks[r["page_no"]] = r, c
    return best, checks


def check(state: State) -> dict:
    """Plain code: check every reading, keep the best one per page, then check the whole invoice.
    If the pages show columns we have no field for, an AI investigates them ONCE (columns.py) and
    only the meanings that make the numbers add up are used - before any page is re-read for them."""
    rules = state.get("rules") or []
    findings = state.get("findings") or []
    investigated = state.get("investigated") or []
    column_reads = state.get("column_reads") or 0
    best, checks = pick_best(state, rules)
    pages = [best[n]["data"] for n in sorted(best)]
    columns = extra_columns(pages)
    if columns and set(columns) - set(investigated):
        guesses = investigate(state["pdf_bytes"], pages, columns)
        column_reads += 1
        if guesses is None:
            findings = unexplained(columns)
        else:
            rules, findings = prove(guesses, columns)
            for f in findings:
                print(f"  [columns] '{f.column}': {f.status} - {f.meaning or f.detail}")
            best, checks = pick_best(state, rules)
            pages = [best[n]["data"] for n in sorted(best)]
        investigated = sorted(columns)
    problems, blame = check_invoice(pages, [checks[n] for n in sorted(checks)], rules)
    failing = sum(1 for c in checks.values() if c.count)
    print(f"  [check] {len(state['readings'])} readings, {failing} pages with problems, "
          f"{len(problems)} invoice problems")
    return {"best": best, "checks": checks, "invoice_problems": problems, "blame": sorted(blame),
            "rules": rules, "findings": findings, "investigated": investigated, "column_reads": column_reads}


def reread_or_finish(state: State) -> list[Send] | str:
    """The decision after each check: re-read failing pages that still have tries left, or finish."""
    attempts = {}
    for r in state["readings"]:
        attempts[r["page_no"]] = max(attempts.get(r["page_no"], 0), r["attempt"])
    suspects = {n for n, c in state["checks"].items() if c.count} | set(state["blame"])
    todo = sorted(n for n in suspects if attempts[n] < MAX_READS)
    if not todo:
        return second_opinion(state)
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        sends = []
        for n in todo:
            feedback = state["checks"][n].feedback()
            if n in state["blame"]:
                feedback += "\n" + "\n".join(f"- {p} (this page is a suspect)" for p in state["invoice_problems"])
            sends.append(task(doc, state, n, attempt=attempts[n] + 1, feedback=feedback.strip()))
    print(f"  [re-read] pages {todo}")
    return sends


# ---------- Step 5: second reader + referee for scanned pages ----------

def second_opinion(state: State) -> list[Send] | str:
    scanned = sorted(n for n, c in state["checks"].items() if c.scanned)
    if not scanned:
        return "finalize"
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        sends = [Send("second_read", {"image": sharpened_image(doc[n - 1]), "page_no": n,
                                      "page_count": doc.page_count}) for n in scanned]
    print(f"  [second reader] scanned pages {scanned}")
    return sends


def second_read(t: dict) -> dict:
    prompt = PAGE_INSTRUCTIONS.format(page_no=t["page_no"], page_count=t["page_count"])
    try:
        data = read_pdf(t["image"], prompt, PageData, primary=READER_B_MODEL,
                        fallback=READER_B_FALLBACK, mime_type="image/png")
    except Exception as e:  # a failed second reading just means the page can't be confirmed
        print(f"  [second reader] page {t['page_no']} failed: {type(e).__name__}")
        data = None
    return {"second": [{"page_no": t["page_no"], "data": data}]}


def compare_readings(state: State) -> dict:
    disputes = {}
    for s in state["second"]:
        n = s["page_no"]
        disputes[n] = None if s["data"] is None else compare(state["best"][n]["data"].lines, s["data"].lines)
    agreed = sum(len(state["best"][n]["data"].lines) - len(d) for n, d in disputes.items() if d is not None)
    print(f"  [compare] readers agree on {agreed} lines, disagree on "
          f"{sum(len(d) for d in disputes.values() if d)}")
    return {"disputes": disputes}


def referee_or_finish(state: State) -> list[Send] | str:
    pages = sorted(n for n, d in state["disputes"].items() if d)
    if not pages:
        return "finalize"
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        return [Send("referee", {"page_no": n, "image": sharpened_image(doc[n - 1], dpi=REFEREE_DPI),
                                 "prompt": referee_prompt(state["best"][n]["data"].lines, state["disputes"][n])})
                for n in pages]


def referee(t: dict) -> dict:
    try:
        report = read_pdf(t["image"], t["prompt"], RefereeReport, primary=REFEREE_MODEL,
                          fallback=REFEREE_FALLBACK, mime_type="image/png")
    except Exception as e:
        print(f"  [referee] page {t['page_no']} failed: {type(e).__name__}")
        report = None
    return {"verdicts": [{"page_no": t["page_no"], "report": report}]}


def cross_check_results(state: State, page_no: int, lines: list) -> tuple[list, list[str]] | None:
    """For a scanned page: the lines to keep and a note per line ('' = confirmed). None = not cross-checked."""
    disputes = state.get("disputes", {}).get(page_no, "missing")
    if disputes == "missing" or disputes is None:
        return None
    verdict = next((v["report"] for v in state.get("verdicts", []) if v["page_no"] == page_no), None)
    by_row = {r.row: r for r in verdict.rows} if verdict else {}
    kept, notes = list(lines), [""] * len(lines)
    for d in disputes:
        res = resolve(lines[d.index], d, by_row.get(d.index + 1))
        kept[d.index], notes[d.index] = res.line, res.note
    return kept, notes


def finalize(state: State) -> dict:
    """Stitch the best readings into one Invoice and mark every line verified or not."""
    pages = [state["best"][n]["data"] for n in sorted(state["best"])]
    checks = [state["checks"][n] for n in sorted(state["checks"])]
    # (the invoice-level problems from the loop are recomputed below, after any corrections)

    def first(field: str) -> str:
        return next((getattr(p, field) for p in pages if getattr(p, field)), "")

    def from_totals_page(field: str) -> float:
        return next((getattr(p, field) for p in reversed(pages) if getattr(p, field) is not None), 0.0)

    rules = state.get("rules") or []
    totals_rows = next((p.totals_rows for p in reversed(pages) if p.total is not None), [])
    tax_rows = [r.amount for r in totals_rows if r.kind == "tax"]
    invoice = Invoice(
        supplier_name=first("supplier_name"), invoice_number=first("invoice_number"),
        invoice_date=first("invoice_date"), currency=first("currency"),
        lines=[line for p in pages for line in p.lines],
        subtotal=from_totals_page("subtotal"), total=from_totals_page("total"), totals_rows=totals_rows,
        # several tax rows: the tax is their sum, added up here in code from the printed rows
        tax=round(sum(tax_rows), 2) if tax_rows else from_totals_page("tax"),
    )

    # 1. Scanned pages: apply the cross-check (a line may be corrected by 2-of-3 agreement).
    problems, cross_notes = [], {}
    with pymupdf.open(stream=state["pdf_bytes"], filetype="pdf") as doc:
        for k, (page, c) in enumerate(zip(pages, checks)):
            cross_notes[c.page_no] = [""] * len(page.lines)
            if not c.scanned:
                continue
            cross = cross_check_results(state, c.page_no, list(page.lines))
            if cross is None:
                problems.append(f"page {c.page_no}: scanned page could not be cross-checked by a second reader")
                cross_notes[c.page_no] = ["scanned page: product names not verified (second reader unavailable)"] \
                    * len(page.lines)
                continue
            corrected, cross_notes[c.page_no] = cross
            if corrected != page.lines:
                # 2. A corrected page must pass EVERY check again before we trust it.
                pages[k] = page.model_copy(update={"lines": corrected})
                checks[k] = check_page(c.page_no, pages[k], doc[c.page_no - 1],
                                       prev_carried=pages[k - 1].carried_forward if k else None, rules=rules)
    invoice.lines = [line for p in pages for line in p.lines]
    if invoice.totals_rows and same(invoice.subtotal + invoice.tax, invoice.total) and not same(
            invoice.subtotal + sum(r.amount for r in invoice.totals_rows if r.kind != "subtotal"), invoice.total):
        invoice.totals_rows = []  # the rows the AI listed don't add up, but the printed totals do: not real
    invoice_problems, blame = check_invoice(pages, checks, rules)
    problems = invoice_problems + problems
    same_code = code_consistency(invoice.lines)  # works on scans too: one code = one description

    # 3. Mark every line: verified, or needs a person (with the reason).
    flags, notes, line_pages = [], [], []
    for page, c in zip(pages, checks):
        page_note = ("; ".join(c.problems) if c.problems
                     else "invoice totals don't add up and this page is a suspect" if c.page_no in blame
                     else "")
        if c.problems:
            problems += [f"page {c.page_no}: {p}" for p in c.problems]
        for i in range(len(page.lines)):
            own = c.line_problems.get(i) or cross_notes[c.page_no][i] or same_code.get(len(flags), "")
            if own:
                problems.append(f"page {c.page_no}, line {i + 1}: {own}")
            flags.append(bool(own or page_note))
            notes.append(own or page_note)
            line_pages.append(c.page_no)

    reread = sorted({r["page_no"] for r in state["readings"] if r["attempt"] > 1})
    reads = (len(state["readings"]) + len(state.get("second", [])) + len(state.get("verdicts", []))
             + (state.get("column_reads") or 0))
    findings = state.get("findings") or []
    report = Report(status="needs review" if problems or any(flags) else "verified",
                    problems=problems, line_flags=flags, line_notes=notes, line_pages=line_pages,
                    reads=reads, pages_reread=reread, columns=findings, column_rules=rules)
    return {"invoice": invoice, "report": report}


builder = StateGraph(State)
builder.add_node("split", split)
builder.add_node("read_page", read_page)
builder.add_node("check", check)
builder.add_node("second_read", second_read)
builder.add_node("compare", compare_readings)
builder.add_node("referee", referee)
builder.add_node("finalize", finalize)
builder.add_edge(START, "split")
builder.add_conditional_edges("split", launch_readers, ["read_page"])            # fan-out
builder.add_edge("read_page", "check")                                            # fan-in
builder.add_conditional_edges("check", reread_or_finish, ["read_page", "second_read", "finalize"])  # the loop
builder.add_edge("second_read", "compare")                                        # fan-in
builder.add_conditional_edges("compare", referee_or_finish, ["referee", "finalize"])
builder.add_edge("referee", "finalize")
builder.add_edge("finalize", END)
graph = builder.compile()


def progress_text(done: Counter, state: dict) -> str:
    """Plain-English progress for the upload page, from how many times each step has finished."""
    pages = state.get("page_count") or 0
    if done["finalize"]:
        return "Done"
    if done["referee"] or done["compare"]:
        return "Settling the lines the two readers disagree on"
    if done["second_read"]:
        scanned = sum(1 for c in state.get("checks", {}).values() if c.scanned)
        return f"Second reader checking scanned page {done['second_read']} of {scanned}"
    if done["check"]:
        return "Checking every line and re-reading pages that don't add up"
    if done["read_page"]:
        return f"Reading page {min(done['read_page'], pages)} of {pages}"
    return "Starting"


def run_bytes(pdf_bytes: bytes, model: str | None = None,
              on_progress: Callable[[str], None] | None = None) -> dict:
    """Run the whole pipeline. Returns the final state: 'invoice' + 'report' (+ per-page details).
    on_progress gets a plain-English line after every step (used by the upload page)."""
    # STREAMING: instead of waiting for the end (invoke), stream() hands us every finished step,
    # so a person watching a 30-page scan sees it move. "values" = the full state after each step.
    done: Counter = Counter()
    state: dict = {}
    for mode, chunk in graph.stream({"pdf_bytes": pdf_bytes, "model": model, "readings": []},
                                    config={"max_concurrency": MAX_PARALLEL, "recursion_limit": 50},
                                    stream_mode=["updates", "values"]):
        if mode == "values":
            state = chunk
        else:
            done.update(chunk.keys())
            if on_progress:
                on_progress(progress_text(done, state))
    return state


def run(pdf_path: Path, model: str | None = None) -> dict:
    return run_bytes(pdf_path.read_bytes(), model)


def extract_by_pages(pdf_path: Path, model: str | None = None) -> Invoice:
    return run(pdf_path, model)["invoice"]


def extract_checked(pdf_path: Path, model: str | None = None) -> tuple[Invoice, Report]:
    out = run(pdf_path, model)
    return out["invoice"], out["report"]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows terminals can't print some AI characters otherwise
    out = run(Path(sys.argv[1]), *sys.argv[2:3])
    inv, rep = out["invoice"], out["report"]
    print(f"\n{inv.supplier_name} | invoice {inv.invoice_number} | {inv.invoice_date} | "
          f"{len(inv.lines)} lines from {out['page_count']} pages | total {inv.total} {inv.currency}")
    print(f"STATUS: {rep.status.upper()}  ({rep.reads} AI reads, re-read pages {rep.pages_reread or 'none'}, "
          f"{sum(rep.line_flags)} of {len(rep.line_flags)} lines need a person)")
    for p in rep.problems[:15]:
        print("  -", p)
