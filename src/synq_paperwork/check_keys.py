"""Check which AI models your keys can use, and whether Gemini can read our PDFs.

Run:  uv run python -m synq_paperwork.check_keys
It never prints the keys themselves, only whether they work.
Only FAKE test invoices are sent (the free Gemini plan may keep what we send).
"""

import base64
import json
import os
import sys

from synq_paperwork.config import load_settings
from synq_paperwork.make_testdata import ANSWER_DIR, PDF_DIR

load_settings()  # uses the project's own dev key (see config.py)

MODEL = "gemini-3.1-flash-lite"


def check_gemini() -> None:
    if not os.getenv("GOOGLE_API_KEY"):
        print("Gemini: no GOOGLE_API_KEY in .env - skipped")
        return
    from google import genai

    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    try:
        names = [m.name.removeprefix("models/") for m in client.models.list()]
    except Exception as e:
        print(f"Gemini: key did not work ({type(e).__name__}: {e})")
        return
    print(f"Gemini: key works, {len(names)} models listed. Gemini models:")
    for n in sorted(n for n in names if n.startswith("gemini")):
        print("   -", n)


def check_groq() -> None:
    if not os.getenv("GROQ_API_KEY"):
        print("Groq: no GROQ_API_KEY in .env - skipped")
        return
    from groq import Groq

    try:
        models = Groq().models.list().data
    except Exception as e:
        print(f"Groq: key did not work ({type(e).__name__}: {e})")
        return
    print(f"Groq: key works, {len(models)} models:")
    for m in sorted(models, key=lambda m: m.id):
        print("   -", m.id)


def check_pdf_reading(case_id: str) -> None:
    """Send one test PDF to Gemini and compare 3 fields with the answer key."""
    from langchain_core.messages import HumanMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    pdf = base64.b64encode((PDF_DIR / f"{case_id}.pdf").read_bytes()).decode()
    answer = json.loads((ANSWER_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    message = HumanMessage(content=[
        {"type": "text", "text": "From this invoice, reply with only JSON: "
                                 '{"invoice_number": "...", "invoice_date": "YYYY-MM-DD", "total": 0.00}'},
        {"type": "file", "base64": pdf, "mime_type": "application/pdf"},
    ])
    try:
        reply = ChatGoogleGenerativeAI(model=MODEL, temperature=0).invoke([message]).text
        got = json.loads(reply.strip().removeprefix("```json").removesuffix("```"))
    except Exception as e:
        print(f"  {case_id}: FAILED ({type(e).__name__}: {e})")
        return
    for field in ["invoice_number", "invoice_date", "total"]:
        ok = str(got.get(field)) == str(answer[field])
        print(f"  {case_id}: {field:15} expected {answer[field]!s:12} got {got.get(field)!s:12} {'OK' if ok else 'WRONG'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows terminals can't print some AI characters otherwise
    check_gemini()
    print()
    check_groq()
    print(f"\nCan {MODEL} read PDFs? (a clean one and a scanned one)")
    check_pdf_reading("inv01-classic-1p")
    check_pdf_reading("inv07-scan-classic-1p")
