"""Synthetic Sheet failures: no credentials, AI calls or real writes."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from synq_paperwork.jobs import Job, JobStore, UploadRejected
from synq_paperwork.schema import Invoice, LineItem
from synq_paperwork.sheets import SaveResult, send_to_sheet

INVOICE = Invoice(supplier_name="Synthetic", invoice_number="TEST-1", invoice_date="2026-01-01", currency="USD",
                  lines=[LineItem(description="x", quantity=1, unit_price=1, amount=1)], subtotal=1, tax=0, total=1)


def make_store(root, send, status="needs review"):
    store = JobStore(lambda *a, **k: pytest.fail("must not read"), send, root=root)
    job = Job(id="a" * 32, file_name="synthetic.pdf", created="2026-01-01", pages=1,
              status=status, invoice=INVOICE.model_dump())
    store._dir(job.id).mkdir(exist_ok=True)
    store.jobs[job.id] = job
    store._write(job)
    return store, job


def test_timeout_after_acceptance_is_unknown_and_cannot_be_resent(tmp_path):
    calls = []
    def send(*args):
        calls.append(args)
        raise httpx.ReadTimeout("a private provider detail")
    store, job = make_store(tmp_path, send)
    assert store.save(job.id, INVOICE, "approved").status == "save unknown"
    assert "Nothing was saved" not in job.message and "private" not in job.message
    with pytest.raises(UploadRejected):
        store.save(job.id, INVOICE, "approved")
    with pytest.raises(UploadRejected):
        store.delete(job.id)
    assert len(calls) == 1
    recovered = JobStore(lambda *a: pytest.fail("must not read"), send, root=tmp_path)
    assert recovered.get(job.id).status == "save unknown"


def test_restart_during_save_keeps_unknown_result_without_retry(tmp_path):
    _, job = make_store(tmp_path, lambda *a: pytest.fail("must not send"), "saving")
    recovered = JobStore(lambda *a: pytest.fail("must not read"), lambda *a: pytest.fail("must not send"), root=tmp_path)
    assert recovered.get(job.id).status == "save unknown"
    assert json.loads((tmp_path / job.id / "job.json").read_text())["status"] == "save unknown"


def test_concurrent_approvals_send_once_and_journal_before_request(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def send(*args):
        calls.append(args)
        assert json.loads((tmp_path / ("a" * 32) / "job.json").read_text())["status"] == "saving"
        entered.set()
        assert release.wait(5)
        return SaveResult("saved", "ok", 1)
    store, job = make_store(tmp_path, send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.save, job.id, INVOICE, "approved")
        assert entered.wait(5)
        try:
            with pytest.raises(UploadRejected):
                store.save(job.id, INVOICE, "approved")
        finally:
            release.set()
        assert first.result(timeout=5).status == "saved"
    assert len(calls) == 1


@pytest.mark.parametrize("body", [None, [], {}, {"status": "saved", "lines": 0}, {"status": "saved", "lines": True}])
def test_invalid_success_acknowledgment_never_claims_saved(body, monkeypatch):
    monkeypatch.setenv("PAPERWORK_WEBHOOK_URL", "https://example.test/sheet")
    monkeypatch.setenv("PAPERWORK_WEBHOOK_SECRET", "synthetic-only")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))
    with pytest.raises(RuntimeError):
        send_to_sheet(INVOICE, "verified")


def test_explicit_complete_acknowledgment_is_saved(monkeypatch):
    monkeypatch.setenv("PAPERWORK_WEBHOOK_URL", "https://example.test/sheet")
    monkeypatch.setenv("PAPERWORK_WEBHOOK_SECRET", "synthetic-only")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"status": "saved", "lines": 1}))
    assert send_to_sheet(INVOICE, "verified").status == "saved"
