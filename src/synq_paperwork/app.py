"""Step 7: the upload portal + review screen, as one small web app.

Start it:   uv run uvicorn synq_paperwork.app:app --port 8000
Open:       http://localhost:8000   (password = PORTAL_PASSWORD in .env)

Pages and endpoints (all /api/* need the header X-API-Key: <PORTAL_PASSWORD>):
  GET  /                          the portal page (asks for the password once)
  GET  /health                    {"status": "ok"}, no password (used to check the server is up)
  POST /api/jobs                  upload a PDF -> it joins the queue
  GET  /api/jobs                  all uploads, newest first
  GET  /api/jobs/{id}             one upload: status, progress, the invoice, per-line flags
  GET  /api/jobs/{id}/pages/{n}   page n as an image (the review screen shows it next to the table)
  POST /api/jobs/{id}/check       re-check a person's corrections (math) without saving
  POST /api/jobs/{id}/approve     a person approves -> checked again -> saved to the Google Sheet
  DELETE /api/jobs/{id}           remove an upload and its file from the server
"""

import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pymupdf
from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from synq_paperwork.checks import ColumnRule, check_reviewed
from synq_paperwork.config import load_settings
from synq_paperwork.jobs import MAX_UPLOAD_MB, Job, JobStore, UploadRejected
from synq_paperwork.schema import Invoice

load_settings()

PAGE = Path(__file__).parent / "static" / "index.html"
SHEET_URL = "https://docs.google.com/spreadsheets/d/{id}/edit"


class ApproveBody(BaseModel):
    invoice: Invoice
    accept_supplier_errors: bool = False  # "the supplier's own invoice has this math error - save as printed"


def rules_of(job: Job) -> list[ColumnRule]:
    """The extra-column meanings the math proved while reading (columns.py)."""
    return [ColumnRule(**r) for r in (job.report or {}).get("column_rules", [])]


def summary(job: Job) -> dict:
    inv, rep = job.invoice or {}, job.report or {}
    return {"id": job.id, "file_name": job.file_name, "created": job.created, "pages": job.pages,
            "status": job.status, "progress": job.progress, "message": job.message,
            "supplier": inv.get("supplier_name", ""), "invoice_number": inv.get("invoice_number", ""),
            "total": inv.get("total"), "currency": inv.get("currency", ""),
            "lines": len(inv.get("lines", [])), "flagged": sum(rep.get("line_flags", []))}


def create_app(store: JobStore | None = None, password: str | None = None) -> FastAPI:
    password = password if password is not None else os.getenv("PORTAL_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError("Set PORTAL_PASSWORD in .env (at least 12 characters) before starting the portal.")
    holder: dict[str, JobStore | None] = {"store": store}

    def get_store() -> JobStore:
        if holder["store"] is None:  # the real thing: AI pipeline + Google Sheet
            from synq_paperwork.pipeline import run_bytes
            from synq_paperwork.sheets import send_to_sheet
            holder["store"] = JobStore(run_bytes, send_to_sheet)
        return holder["store"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        get_store()  # start the worker at once, so uploads from before a restart continue
        yield

    app = FastAPI(title="Paperwork Robot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def check_key(key: str | None = Security(APIKeyHeader(name="X-API-Key", auto_error=False))) -> None:
        # compare_digest takes the same time whether a guess is close or not, so it leaks nothing.
        if not key or not secrets.compare_digest(key, password):
            raise HTTPException(401, "Wrong password.")

    def job_or_404(job_id: str) -> Job:
        try:
            return get_store().get(job_id)
        except KeyError:
            raise HTTPException(404, "Upload not found.")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    auth = [Depends(check_key)]

    @app.get("/api/jobs", dependencies=auth)
    def list_jobs() -> dict:
        return {"jobs": [summary(j) for j in get_store().all()],
                "sheet_url": SHEET_URL.format(id=os.getenv("PAPERWORK_SHEET_ID", ""))}

    @app.post("/api/jobs", dependencies=auth)
    async def upload(file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
        try:
            return summary(get_store().add(file.filename or "invoice.pdf", data))
        except UploadRejected as e:
            raise HTTPException(400, str(e))

    @app.get("/api/jobs/{job_id}", dependencies=auth)
    def get_job(job_id: str) -> dict:
        return asdict(job_or_404(job_id))

    @app.get("/api/jobs/{job_id}/pages/{page_no}", dependencies=auth)
    def page_image(job_id: str, page_no: int) -> Response:
        job = job_or_404(job_id)
        if not 1 <= page_no <= job.pages:
            raise HTTPException(404, "No such page.")
        with pymupdf.open(get_store().pdf_path(job_id)) as doc:
            png = doc[page_no - 1].get_pixmap(dpi=110).tobytes("png")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/jobs/{job_id}/check", dependencies=auth)
    def check(job_id: str, invoice: Invoice) -> dict:
        return {"problems": check_reviewed(invoice, rules_of(job_or_404(job_id)))}

    @app.post("/api/jobs/{job_id}/approve", dependencies=auth)
    def approve(job_id: str, body: ApproveBody) -> dict:
        job = job_or_404(job_id)
        if job.status not in ("needs review", "save failed"):
            raise HTTPException(409, f"This upload is '{job.status}' and can't be approved now.")
        if len(body.invoice.lines) > 5000:
            raise HTTPException(400, "Too many line items.")
        problems = check_reviewed(body.invoice, rules_of(job))  # never trust the page: check again here
        if problems and not body.accept_supplier_errors:
            raise HTTPException(422, {"problems": problems})
        edited = body.invoice.model_dump() != job.invoice
        # Unchanged + already proven by the checks (e.g. a retry after "save failed") stays "verified".
        status = "verified" if (job.report or {}).get("status") == "verified" and not edited and not problems \
            else "approved"
        note = ("saved as printed - the supplier's invoice itself has: " + "; ".join(problems))[:300] if problems \
            else "corrected by a person" if edited else "checked by a person"
        return asdict(get_store().save(job_id, body.invoice, status, note if status == "approved" else ""))

    @app.delete("/api/jobs/{job_id}", dependencies=auth)
    def delete(job_id: str) -> dict:
        job_or_404(job_id)
        try:
            get_store().delete(job_id)
        except UploadRejected as e:
            raise HTTPException(409, str(e))
        return {"deleted": job_id}

    return app


# `uvicorn synq_paperwork.app:app` uses this. Tests call create_app() with a fake store instead.
app = create_app()
