"""Step 6: send a VERIFIED invoice to Google Sheets (through an n8n workflow).

Run the whole thing on one PDF:
    uv run python -m synq_paperwork.sheets testdata/invoices/inv01-classic-1p.pdf
Test the sheet side without using any AI (sends a test invoice's answer key as if verified):
    uv run python -m synq_paperwork.sheets --answer-key inv01-classic-1p

How it fits together:
  Python (this file)  --POST + secret header-->  n8n webhook "paperwork-invoice"
    n8n: check the data again -> is this supplier + invoice number already saved?
         yes -> answer 409 "duplicate", write nothing
         no  -> write the line items (tab "Line items"), then the invoice row (tab "Invoices")
Why n8n for this part: adding QuickBooks, a CRM or email later is one more branch in n8n,
with no change to the AI code.

Safety rules:
  - Only invoices whose Report says "verified" are sent. Anything else waits for a person
    (the review screen, Step 7). n8n checks this again, in case someone calls it directly.
  - The webhook needs the secret header X-Paperwork-Auth (PAPERWORK_WEBHOOK_SECRET in .env).
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from synq_paperwork.config import load_settings
from synq_paperwork.schema import Invoice

load_settings()

SHEET_URL = "https://docs.google.com/spreadsheets/d/{id}/edit"


class NotVerified(Exception):
    """Raised when someone tries to send an invoice that the checks did not verify."""


@dataclass
class SaveResult:
    status: str        # "saved", "duplicate" or "rejected"
    message: str
    lines: int = 0


def send_to_sheet(invoice: Invoice, status: str, source_file: str = "", note: str = "") -> SaveResult:
    """Save one invoice. `status` must be 'verified' (proven by the checks) or 'approved' (a person
    checked every flagged line on the review screen - Step 7). `note` is recorded with an approval."""
    if status not in ("verified", "approved"):
        raise NotVerified(f"Not sent: the invoice is '{status}'. A person must review it first.")
    url, secret = os.getenv("PAPERWORK_WEBHOOK_URL"), os.getenv("PAPERWORK_WEBHOOK_SECRET")
    if not url or not secret:
        raise RuntimeError("PAPERWORK_WEBHOOK_URL / PAPERWORK_WEBHOOK_SECRET missing in .env")
    r = httpx.post(url, headers={"X-Paperwork-Auth": secret}, timeout=120,
                   json={"status": status, "source_file": source_file, "note": note,
                         "invoice": invoice.model_dump()})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200:
        return SaveResult("saved", f"saved {body.get('lines')} lines", body.get("lines", 0))
    if r.status_code == 409:
        return SaveResult("duplicate", body.get("message", "already in the sheet"))
    if r.status_code == 400:
        return SaveResult("rejected", body.get("error", "rejected"))
    r.raise_for_status()  # 403 (wrong secret), 500 (n8n/Google problem): a real error
    return SaveResult("rejected", f"unexpected answer {r.status_code}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows terminals can't print some AI characters otherwise
    if len(sys.argv) == 3 and sys.argv[1] == "--answer-key":
        from synq_paperwork.make_testdata import ANSWER_DIR
        invoice = Invoice.model_validate_json((ANSWER_DIR / f"{sys.argv[2]}.json").read_text(encoding="utf-8"))
        status, source = "verified", f"{sys.argv[2]}.pdf (answer key, test)"
    else:
        from synq_paperwork.pipeline import extract_checked
        pdf = Path(sys.argv[1])
        invoice, report = extract_checked(pdf)
        status, source = report.status, pdf.name
        print(f"Checks: {report.status.upper()} ({sum(report.line_flags)} of {len(report.line_flags)} lines need a person)")
    try:
        result = send_to_sheet(invoice, status, source)
    except NotVerified as e:
        print(e)
        return
    print(f"{result.status.upper()}: {result.message}")
    print("Sheet:", SHEET_URL.format(id=os.getenv("PAPERWORK_SHEET_ID", "?")))


if __name__ == "__main__":
    main()
