"""Tests for the portal (app.py + jobs.py) with a FAKE reader and a FAKE sheet: no AI, no Google.

The fake reader returns the answer key; for inv02 it pretends one line needs a person.
The fake sheet remembers what was saved and answers "duplicate" the second time.
"""

import io
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from synq_paperwork.app import create_app
from synq_paperwork.jobs import JobStore
from synq_paperwork.make_testdata import ANSWER_DIR, PDF_DIR
from synq_paperwork.pipeline import Report
from synq_paperwork.schema import Invoice, LineItem
from synq_paperwork.sheets import SaveResult

PASSWORD = "portal-test-password"
KEY = {"X-API-Key": PASSWORD}


def fake_process(pdf_bytes, on_progress=None):
    case = next(p.stem for p in PDF_DIR.glob("*.pdf") if p.read_bytes() == pdf_bytes)
    invoice = Invoice.model_validate_json((ANSWER_DIR / f"{case}.json").read_text(encoding="utf-8"))
    n = len(invoice.lines)
    flags = [i == 0 and case.startswith("inv02") for i in range(n)]
    if on_progress:
        on_progress("Reading page 1 of 1")
    return {"invoice": invoice, "report": Report(
        status="needs review" if any(flags) else "verified", problems=[], line_flags=flags,
        line_notes=["readers disagree" if f else "" for f in flags], line_pages=[1] * n, reads=1, pages_reread=[])}


class FakeSheet:
    def __init__(self):
        self.saved: list[tuple] = []

    def __call__(self, invoice, status, source_file, note=""):
        if any(s[0].invoice_number == invoice.invoice_number for s in self.saved):
            return SaveResult("duplicate", "already there")
        self.saved.append((invoice, status, note))
        return SaveResult("saved", "ok", len(invoice.lines))


@pytest.fixture
def portal(tmp_path):
    sheet = FakeSheet()
    store = JobStore(fake_process, sheet, root=tmp_path)
    with TestClient(create_app(store, password=PASSWORD)) as client:
        yield client, sheet, store


def upload(client, case_id):
    pdf = (PDF_DIR / f"{case_id}.pdf").read_bytes()
    r = client.post("/api/jobs", headers=KEY, files={"file": (f"{case_id}.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def wait(client, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}", headers=KEY).json()
        if job["status"] not in ("queued", "reading", "verified"):  # "verified" is auto-saving right now
            return job
        time.sleep(0.05)
    raise TimeoutError(job_id)


# ---------- password ----------

def test_password_is_required(portal):
    client, _, _ = portal
    assert client.get("/health").json() == {"status": "ok"}  # health check needs no password
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"X-API-Key": "guess"}).status_code == 401
    assert client.get("/api/jobs", headers=KEY).status_code == 200


def test_private_responses_and_schema(portal):
    client, _, _ = portal
    for path in ("/api/jobs", "/api/jobs/not-a-job"):
        response = client.get(path, headers=KEY)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_portal_refuses_to_start_without_a_real_password(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(JobStore(fake_process, FakeSheet(), root=tmp_path), password="short")


# ---------- uploads ----------

def test_rejects_files_that_are_not_usable_pdfs(portal, tmp_path):
    client, _, _ = portal
    bad = client.post("/api/jobs", headers=KEY, files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert bad.status_code == 400 and "PDF" in bad.json()["detail"]

    too_long = pymupdf.open()
    for _ in range(61):
        too_long.new_page()
    r = client.post("/api/jobs", headers=KEY, files={"file": ("long.pdf", too_long.tobytes(), "application/pdf")})
    assert r.status_code == 400 and "61 pages" in r.json()["detail"]

    locked = pymupdf.open(PDF_DIR / "inv01-classic-1p.pdf").tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    r = client.post("/api/jobs", headers=KEY, files={"file": ("locked.pdf", locked, "application/pdf")})
    assert r.status_code == 400 and "password" in r.json()["detail"]


# ---------- photo uploads ----------

def make_photo(fmt, size=(600, 800), exif_rotate=False):
    img = Image.new("RGB", size, "white")
    buf = io.BytesIO()
    if exif_rotate:  # simulate a phone photo taken sideways: EXIF says "rotate 90", pixels stay as-is
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation tag: 6 = rotate 90 CW to display upright
        img.save(buf, fmt, exif=exif)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def any_invoice_process(pdf_bytes, on_progress=None):
    inv = Invoice(supplier_name="Photo Test Supplier", invoice_number="P-1", invoice_date="2026-01-01",
                 currency="USD", lines=[LineItem(description="x", quantity=1, unit_price=5, amount=5)],
                 subtotal=5, tax=0, total=5)
    return {"invoice": inv, "report": Report(status="verified", problems=[], line_flags=[False],
            line_notes=[""], line_pages=[1], reads=1, pages_reread=[])}


def test_accepts_a_jpg_photo_as_a_one_page_invoice(tmp_path):
    sheet = FakeSheet()
    store = JobStore(any_invoice_process, sheet, root=tmp_path)
    with TestClient(create_app(store, password=PASSWORD)) as client:
        r = client.post("/api/jobs", headers=KEY,
                        files={"file": ("photo.jpg", make_photo("JPEG"), "image/jpeg")})
        assert r.status_code == 200 and r.json()["pages"] == 1
        with pymupdf.open(store.pdf_path(r.json()["id"])) as doc:
            assert doc.page_count == 1  # the photo really became a one-page PDF on disk
        job = wait(client, r.json()["id"])
        assert job["status"] == "saved"  # went through the normal pipeline like any scan


def test_accepts_a_png_photo(tmp_path):
    sheet = FakeSheet()
    store = JobStore(any_invoice_process, sheet, root=tmp_path)
    with TestClient(create_app(store, password=PASSWORD)) as client:
        r = client.post("/api/jobs", headers=KEY,
                        files={"file": ("photo.png", make_photo("PNG"), "image/png")})
        assert r.status_code == 200 and r.json()["pages"] == 1


def test_accepts_an_iphone_heic_photo(tmp_path):
    store = JobStore(any_invoice_process, FakeSheet(), root=tmp_path)
    with TestClient(create_app(store, password=PASSWORD)) as client:
        r = client.post("/api/jobs", headers=KEY,
                        files={"file": ("IMG_1234.HEIC", make_photo("HEIF"), "image/heic")})
        assert r.status_code == 200 and r.json()["pages"] == 1
        with pymupdf.open(store.pdf_path(r.json()["id"])) as doc:
            assert doc.page_count == 1


def test_a_sideways_phone_photo_is_straightened(tmp_path):
    # EXIF says "rotate 90" but the pixels are still portrait 600x800 -> after straightening
    # the PDF page should be landscape (rotated), not the raw portrait pixel size.
    store = JobStore(any_invoice_process, FakeSheet(), root=tmp_path)
    with TestClient(create_app(store, password=PASSWORD)) as client:
        r = client.post("/api/jobs", headers=KEY,
                        files={"file": ("sideways.jpg", make_photo("JPEG", exif_rotate=True), "image/jpeg")})
        with pymupdf.open(store.pdf_path(r.json()["id"])) as doc:
            assert doc[0].rect.width > doc[0].rect.height  # landscape: the 90-degree turn was applied


def test_rejects_a_corrupted_photo_and_random_bytes(portal):
    client, _, _ = portal
    r = client.post("/api/jobs", headers=KEY,
                    files={"file": ("broken.jpg", b"\xff\xd8\xff" + b"not really a photo", "image/jpeg")})
    assert r.status_code == 400 and "photo" in r.json()["detail"]
    r = client.post("/api/jobs", headers=KEY, files={"file": ("mystery.bin", b"random bytes", "application/octet-stream")})
    assert r.status_code == 400 and "PDF, JPG, PNG or HEIC" in r.json()["detail"]


def test_verified_invoice_is_saved_automatically_and_only_once(portal):
    client, sheet, _ = portal
    job = wait(client, upload(client, "inv01-classic-1p"))
    assert job["status"] == "saved" and job["saved_as"] == "verified"
    assert sheet.saved[0][1] == "verified"
    again = wait(client, upload(client, "inv01-classic-1p"))
    assert again["status"] == "duplicate" and len(sheet.saved) == 1


def test_needs_review_waits_for_a_person(portal):
    client, sheet, _ = portal
    job = wait(client, upload(client, "inv02-modern-1p"))
    assert job["status"] == "needs review" and job["report"]["line_flags"][0] is True
    assert sheet.saved == []  # nothing saved without a person


# ---------- review screen ----------

def test_person_approves_and_it_is_saved_as_approved(portal):
    client, sheet, _ = portal
    job = wait(client, upload(client, "inv02-modern-1p"))
    r = client.post(f"/api/jobs/{job['id']}/approve", headers=KEY, json={"invoice": job["invoice"]})
    assert r.status_code == 200 and r.json()["status"] == "saved"
    assert sheet.saved[0][1] == "approved" and sheet.saved[0][2] == "checked by a person"
    # A saved upload can't be approved (or saved) a second time.
    assert client.post(f"/api/jobs/{job['id']}/approve", headers=KEY,
                       json={"invoice": job["invoice"]}).status_code == 409


def test_corrections_that_dont_add_up_are_refused_unless_supplier_error(portal):
    client, sheet, _ = portal
    job = wait(client, upload(client, "inv02-modern-1p"))
    job["invoice"]["lines"][0]["amount"] += 1  # a typo by the person
    check = client.post(f"/api/jobs/{job['id']}/check", headers=KEY, json=job["invoice"]).json()
    assert any("Line 1" in p for p in check["problems"])

    r = client.post(f"/api/jobs/{job['id']}/approve", headers=KEY, json={"invoice": job["invoice"]})
    assert r.status_code == 422 and sheet.saved == []

    r = client.post(f"/api/jobs/{job['id']}/approve", headers=KEY,
                    json={"invoice": job["invoice"], "accept_supplier_errors": True})
    assert r.status_code == 200 and "saved as printed" in sheet.saved[0][2]


def test_page_images_and_unknown_ids(portal):
    client, _, _ = portal
    job = wait(client, upload(client, "inv03-classic-5p"))
    img = client.get(f"/api/jobs/{job['id']}/pages/5", headers=KEY)
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG")
    assert client.get(f"/api/jobs/{job['id']}/pages/6", headers=KEY).status_code == 404
    for bad_id in ["nope", "..%2F..%2Fsecrets", "0" * 32]:
        assert client.get(f"/api/jobs/{bad_id}", headers=KEY).status_code == 404


def test_delete_removes_the_file(portal, tmp_path):
    client, _, store = portal
    job = wait(client, upload(client, "inv01-classic-1p"))
    assert store.pdf_path(job["id"]).exists()
    assert client.delete(f"/api/jobs/{job['id']}", headers=KEY).status_code == 200
    assert not (tmp_path / job["id"]).exists()
    assert client.get(f"/api/jobs/{job['id']}", headers=KEY).status_code == 404


def test_unfinished_uploads_continue_after_a_restart(tmp_path):
    first = JobStore(lambda *a, **k: time.sleep(60), FakeSheet(), root=tmp_path)  # a reader that never finishes
    job = first.add("inv01-classic-1p.pdf", (PDF_DIR / "inv01-classic-1p.pdf").read_bytes())
    time.sleep(0.2)
    sheet = FakeSheet()
    second = JobStore(fake_process, sheet, root=tmp_path)  # "the server restarted"
    end = time.time() + 10
    while second.get(job.id).status in ("queued", "reading") and time.time() < end:
        time.sleep(0.05)
    assert second.get(job.id).status == "saved" and len(sheet.saved) == 1
