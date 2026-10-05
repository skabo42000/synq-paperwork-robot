"""Step 7: the upload queue. Every uploaded PDF becomes a JOB that a background worker reads.

Why a queue + background worker instead of reading the PDF inside the upload request?
  - A 30-page scan takes minutes; a browser (and most servers) won't wait that long for one answer.
    So the upload returns at once with a job id, and the page asks "how far is it?" every 2 seconds.
  - ONE worker reads ONE invoice at a time: the free AI plan allows only so many requests per minute.

A job's life:
  queued -> reading -> verified    -> (saved automatically) -> saved | duplicate | save failed
                    -> needs review -> (a person approves on the review screen) -> saved | duplicate
                    -> failed        (the file couldn't be read at all)

Each job lives in data/jobs/<id>/ (git-ignored): the PDF + job.json. After a restart, unfinished
jobs are put back in the queue, so nothing uploaded is silently lost.

Uploads accept a PDF OR a phone photo (JPG, PNG, or an iPhone's HEIC). A photo is turned into a
one-page PDF right away (photo_to_pdf), so every step after that - counting pages, rendering the
review screen, the checks - works on a PDF exactly like it does for a scan. A photo has no stored
text, so the checks treat it as "scanned" automatically: the numbers are proven by math, and the
product names by the second reader + referee (Step 5) - there's no separate "photo" code path
anywhere else in the project.
"""

import io
import json
import queue
import re
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pillow_heif
import pymupdf
from PIL import Image, ImageOps

from synq_paperwork.make_testdata import PROJECT_ROOT
from synq_paperwork.schema import Invoice

pillow_heif.register_heif_opener()  # lets Image.open() read HEIC/HEIF too - iPhones default to it

DATA_DIR = PROJECT_ROOT / "data" / "jobs"
MAX_UPLOAD_MB = 20
MAX_PAGES = 60
UNFINISHED = ("queued", "reading")
HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1"}


class UploadRejected(ValueError):
    """The file can't be accepted; the message is shown to the person as-is."""


def looks_like_photo(data: bytes) -> bool:
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":  # JPEG / PNG signatures
        return True
    # HEIC/HEIF: bytes 4-8 are the literal text "ftyp", followed by a 4-letter brand.
    return len(data) > 12 and data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS


def photo_to_pdf(data: bytes) -> bytes:
    """Turn one photo into a one-page PDF, so the rest of the project only ever deals with PDFs.
    Phones store the 'this way up' as a separate EXIF tag, not by rotating the pixels - exif_transpose
    applies it for real, or a sideways photo would silently reach the AI reader sideways."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    jpeg = io.BytesIO()
    img.save(jpeg, "JPEG", quality=92)

    # A real photo's pixel size says nothing about physical page size (that's in DPI metadata phones
    # often omit), so we place it on a normal-sized page instead - this keeps later dpi-based
    # rendering (checks.py, crosscheck.py) sane, the same as it is for a scanned PDF page.
    long_side_pt = 11 * 72
    w_px, h_px = img.size
    w, h = (long_side_pt, long_side_pt * h_px / w_px) if w_px >= h_px else (long_side_pt * w_px / h_px, long_side_pt)
    with pymupdf.open() as doc:
        doc.new_page(width=w, height=h).insert_image(pymupdf.Rect(0, 0, w, h), stream=jpeg.getvalue())
        return doc.tobytes()


@dataclass
class Job:
    id: str
    file_name: str
    created: str
    pages: int
    status: str = "queued"
    progress: str = "Waiting in line"
    message: str = ""                 # plain-English result for the person
    invoice: dict | None = None       # the Invoice (after reading; after approval: the approved version)
    report: dict | None = None        # the pipeline's Report: verified / needs review + per-line flags
    saved_as: str = ""                # "verified" (by the checks) or "approved" (by a person)


class JobStore:
    """process(pdf_bytes, on_progress) -> {"invoice", "report"}; send(invoice, status, file, note) -> SaveResult.
    Both are passed in, so tests can use fakes instead of real AI and a real Google Sheet."""

    def __init__(self, process: Callable, send: Callable, root: Path = DATA_DIR):
        self.process, self.send, self.root = process, send, root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self.jobs: dict[str, Job] = {}
        for f in sorted(self.root.glob("*/job.json")):
            job = Job(**json.loads(f.read_text(encoding="utf-8")))
            self.jobs[job.id] = job
            if job.status in UNFINISHED:  # the server stopped mid-way: read it again
                job.status, job.progress = "queued", "Waiting in line (restarted)"
                self.queue.put(job.id)
        threading.Thread(target=self._worker, daemon=True, name="invoice-worker").start()

    # ---------- storage ----------

    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):  # never let an id like "../.." become a path
            raise KeyError(job_id)
        return self.root / job_id

    def _write(self, job: Job) -> None:
        (self._dir(job.id) / "job.json").write_text(json.dumps(asdict(job), indent=1), encoding="utf-8")

    def get(self, job_id: str) -> Job:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    def all(self) -> list[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def update(self, job_id: str, **changes) -> Job:
        with self.lock:
            job = self.jobs[job_id]
            for k, v in changes.items():
                setattr(job, k, v)
            self._write(job)
            return job

    def pdf_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "invoice.pdf"

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.status in UNFINISHED:
            raise UploadRejected("This file is still being read. Delete it when it's finished.")
        with self.lock:
            del self.jobs[job_id]
            (self._dir(job_id) / "job.json").unlink(missing_ok=True)  # first: without it the job is gone for good
            for f in self._dir(job_id).iterdir():
                f.unlink(missing_ok=True)
            try:
                self._dir(job_id).rmdir()
            except OSError:
                pass  # OneDrive can hold an empty folder for a moment; an empty folder is ignored on restart

    # ---------- upload ----------

    def add(self, file_name: str, data: bytes) -> Job:
        """Check the upload, store it, and put it in the queue."""
        if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            raise UploadRejected(f"The file is larger than {MAX_UPLOAD_MB} MB.")
        if not data.startswith(b"%PDF"):
            if not looks_like_photo(data):
                raise UploadRejected("That doesn't look like a PDF, JPG, PNG or HEIC file.")
            try:
                data = photo_to_pdf(data)
            except Exception:
                raise UploadRejected("Couldn't read that photo. Please try a clearer picture, or a PDF.")
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass:
                    raise UploadRejected("The PDF is password-protected. Please upload an unlocked copy.")
                pages = doc.page_count
        except UploadRejected:
            raise
        except Exception:
            raise UploadRejected("The PDF couldn't be opened. It may be damaged.")
        if pages == 0 or pages > MAX_PAGES:
            raise UploadRejected(f"The PDF has {pages} pages; up to {MAX_PAGES} are supported.")

        name = re.sub(r"[^\w .()-]", "_", Path(file_name or "invoice.pdf").name)[:120]
        job = Job(id=uuid.uuid4().hex, file_name=name, pages=pages,
                  created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._dir(job.id).mkdir(parents=True)
        self.pdf_path(job.id).write_bytes(data)
        with self.lock:
            self.jobs[job.id] = job
            self._write(job)
        self.queue.put(job.id)
        return job

    # ---------- the background worker ----------

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            try:
                self._read(job_id)
            except Exception:
                traceback.print_exc()  # details for the server log; the person gets a plain message
                self.update(job_id, status="failed", progress="",
                            message="Something went wrong while reading this file. Please try uploading it again.")

    def _read(self, job_id: str) -> None:
        self.update(job_id, status="reading", progress="Starting")
        out = self.process(self.pdf_path(job_id).read_bytes(),
                           on_progress=lambda text: self.update(job_id, progress=text))
        invoice, report = out["invoice"], out["report"]
        flagged = sum(report.line_flags)
        self.update(job_id, invoice=invoice.model_dump(), report=report.model_dump(), status=report.status,
                    progress="",
                    message="Every line checked." if report.status == "verified"
                    else f"{flagged} of {len(report.line_flags)} lines need your check before saving." if flagged
                    else "Every line matches the invoice, but its totals need your look before saving.")
        if report.status == "verified":
            self.save(job_id, invoice, "verified")  # the promise: proven invoices need no typing at all

    def save(self, job_id: str, invoice: Invoice, status: str, note: str = "") -> Job:
        """Send to the Google Sheet and record what happened."""
        job = self.get(job_id)
        try:
            result = self.send(invoice, status, job.file_name, note)
        except Exception:
            traceback.print_exc()
            return self.update(job_id, status="save failed",
                               message="Couldn't reach the Google Sheet. Nothing was saved; please try again.")
        if result.status == "saved":
            return self.update(job_id, status="saved", saved_as=status, invoice=invoice.model_dump(),
                               message=f"Saved to the Google Sheet ({result.lines} lines).")
        if result.status == "duplicate":
            return self.update(job_id, status="duplicate",
                               message="This invoice is already in the Google Sheet. Nothing new was written.")
        return self.update(job_id, status="save failed", message=f"The Google Sheet refused it: {result.message}")
