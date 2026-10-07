# Case study: supplier-invoice entry with "no silent errors"

**Status: working prototype, tested on synthetic data. Not deployed for a customer.**
Built by Bosko Tutnjilovic (Synq Logic). Numbers below come from a recorded local run on 2026-09-27 and the test suite of this repository, run on 2026-10-06. Where something was not measured, this document says so.

## 1. The problem

Many small and mid-sized businesses re-key supplier invoices by hand: open the PDF, type each line into a spreadsheet or accounting tool. It is slow, and a wrong number that nobody notices is worse than a slow process, because it ends up in payments and reports.

Letting an AI read the invoices is the obvious idea. The hard part is trust: an AI can return a perfectly formatted answer that is wrong. So the design goal was not "extract fast" but **never let a wrong line pass quietly**.

## 2. The assumed customer (a scenario, not a real client)

An office manager at a distributor receives 20-100 supplier invoices a week, from one page up to about 30 pages, as digital PDFs, scans, and sometimes phone photos. They want the lines in a Google Sheet. They are not technical and will not read logs.

This scenario was chosen to set requirements. No real customer has used this system.

## 3. What "done" means (acceptance criteria)

| # | Criterion | How it is checked | Result |
|---|---|---|---|
| A1 | Every extracted line is either proven by independent evidence or flagged for a person | Eval metric `silent errors` (wrong line that was not flagged) | **0** on 9 synthetic invoices |
| A2 | No invoice with a wrong line is marked "verified" | Eval metric `approved but wrong` | **0** |
| A3 | Most clean invoices need no human touch | Share of invoices verified automatically | **8 of 9** (the 9th was a hard 30-page scan) |
| A4 | A person can fix flagged lines quickly | Review screen: page image beside editable fields | Built, browser-tested; no user study |
| A5 | Nothing unverified reaches the spreadsheet | Server refuses to send unless `verified` or person-`approved`; server re-checks reviewed data | Covered by tests |
| A6 | Re-sending the same invoice does not create duplicates | Duplicate check on supplier + invoice number | Covered by tests and a live check against a test sheet |
| A7 | A server restart does not lose uploads | Unfinished jobs go back in the queue on start | Covered by tests |
| A8 | Time and cost per invoice are known | Latency and AI-request count recorded; **cost not recorded** | Partly (see section 7) |

## 4. How it works

```
upload (PDF or phone photo) -> checks + job queue -> page-by-page AI reading (parallel)
   -> plain-code checks -> verified? -> yes: authenticated n8n webhook -> duplicate check -> Google Sheets
                              |
                              no: re-read the failing pages (max 2) -> still unsure: human review screen -> approve -> same webhook
```

- **Python + LangGraph** orchestrates the steps. **FastAPI** serves a password-protected portal. **n8n** receives the verified invoice and writes to **Google Sheets**.
- AI reading uses Gemini models through one provider. Model fallback, retries and a 120-second timeout are built in.

## 5. Design decisions and why

1. **Read page by page, in parallel (up to 4 at a time).** The first version sent the whole PDF in one AI call. Page-by-page reading, stitched back together, replaced it and the 30-page digital invoice then came out perfect in the eval. The limit of 4 parallel reads comes from the free plan's per-minute cap (6 hit it).
2. **Prove with code, not with the AI's confidence.** Line math (quantity x price - discount = amount), page control totals, carried-forward totals, the totals block, and, for digital PDFs, a check that **every printed word in a row is accounted for** by the reading. Early evals showed math alone is not enough: rows can shift while the numbers still add up. The word-position check closed that gap.
3. **Two readers plus a referee for scans.** Scans have no text layer to check against, so a second reader (different model, sharpened image) reads each page, differences go to a blind referee, and 2-of-3 agreement decides. Anything unresolved is flagged.
4. **Unknown columns: the AI may explain, only the math may approve.** Invoices have columns the schema does not know (discount %, units, tax codes). An AI proposes a meaning and one of 7 testable formulas; code keeps it only if the numbers add up on 80%+ of the lines showing that column.
5. **Human review is part of the product.** Flagged lines must be ticked or corrected; the server re-checks the reviewed data before saving.
6. **Second validation inside n8n.** The webhook checks the data again, detects duplicates, writes raw values (so text like `=1+1` cannot become a formula), and writes the invoice row last as the "done" marker.

## 6. Integration contract (portal to n8n)

`POST` to the webhook with header `X-Paperwork-Auth` (a secret held as an n8n credential) and the invoice as JSON plus `status` = `verified` or `approved`.

| Answer | Meaning | Portal behavior |
|---|---|---|
| 200 | saved | job -> saved |
| 409 | duplicate (supplier + invoice number already saved) | job -> duplicate, nothing written |
| 400 | rejected by the second validation | job -> save failed, with the reason shown; a person can fix and retry |
| 403 / 5xx | wrong secret or n8n/Google problem | job -> save failed; can be retried |

## 7. Results

Recorded run, 2026-09-27, synthetic invoices with exact answer keys (two layouts; 1, 5 and 30 pages; digital and scanned):

| Invoice | Lines | Matched | Result | Seconds | AI reads |
|---|---:|---:|---|---:|---:|
| Classic digital, 1 page | 30 | 30 | verified | 7.9 | 1 |
| Modern digital, 1 page | 27 | 27 | verified | 5.4 | 1 |
| Classic digital, 5 pages | 177 | 177 | verified | 12.5 | 5 |
| Modern digital, 5 pages | 196 | 196 | verified | 15.7 | 5 |
| Classic digital, 30 pages | 1,012 | 1,012 | verified | 62.3 | 30 |
| Modern digital, 30 pages | 1,157 | 1,157 | verified | 75.3 | 30 |
| Classic scanned, 1 page | 30 | 30 | verified | 20.1 | 2 |
| Modern scanned, 5 pages | 165 | 165 | verified | 45.4 | 11 |
| Classic scanned, 30 pages | 1,081 | 1,050 | **needs review** | 234.2 | 66 |

- **3,844 of 3,875 lines (99.2%)** matched exactly. All 31 wrong lines were in the hardest invoice and **all were flagged** (33 lines flagged in total, 2 of them were actually correct).
- **Silent errors: 0. Approved-but-wrong: 0.** On a set this small and generated by the same author, that does not prove a zero-error rate on real documents.
- **Tests:** 90 automated tests, all offline (fake readers, fake sheet), pass in about 20 seconds (run on this repository, 2026-10-06).

**Latency:** about 8 seconds for a clean 1-page invoice, about 1 to 1.3 minutes for a clean 30-page digital invoice, about 4 minutes for a hard 30-page scan (single run each; not a statistical result).

**Cost: not measured.** The runs used a free-tier key and the system does not yet record token usage. What is known: the number of AI requests per invoice (column above). Turning that into money needs token counts and the provider's current price list.

## 8. What is tested vs. deployed

| Piece | Tested | Deployed |
|---|---|---|
| Extraction + checks + review + queue | yes (offline tests; recorded eval run) | no, runs locally |
| n8n workflow + Google Sheets | yes, against a test sheet with test data | test workflow only |
| Portal login, upload limits (20 MB, 60 pages), approve/reject, private-response headers (no-store, no API docs page) | yes (offline tests, browser-tested on one machine) | no |
| Real photographs, other languages, other layouts | **no** | no |

## 9. What blocks real use

- **Storage:** uploads and jobs sit on local disk. A free cloud host forgets files on restart, so durable storage is needed first.
- **Privacy and plan:** the free AI tier may keep data, so only fake invoices were used. Real documents need a paid plan and a data-handling agreement.
- **Single user:** one shared password, no per-customer separation. A multi-customer version needs accounts, isolation and audit logs.
- **No monitoring or cost tracking** yet.
- **Concurrent writes:** the duplicate check in Sheets is not a transaction; two simultaneous saves could both pass.

## 10. What I would do in the first two weeks with a real customer

1. **Discovery (days 1-3):** collect 30-50 of their real invoices (with permission), list suppliers and layouts, and ask where the data goes after the sheet (accounting software? approvals?).
2. **Agree acceptance criteria in numbers:** for example "no wrong line saved without a flag" and "at least X% of invoices need no human fix". Add their invoices to the eval set.
3. **Measure before promising:** run the eval, record token usage and cost per invoice, and report the share of invoices that needed a person.
4. **Pilot with a shadow period:** the robot and the office manager both enter the data for two weeks; compare differences.
5. **Add what the pilot shows is missing:** durable storage, monitoring, per-supplier price-history checks (for the one error class code cannot catch: the right numbers on the wrong product).

## 11. Honest limits

This is a prototype built to show an engineering approach: define "wrong", prove or flag every line, put a person in the loop, and measure. It is not production software, not a financial audit, and it has not saved anyone time yet.
