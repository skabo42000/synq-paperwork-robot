"""The sheet sender must refuse anything that isn't verified - before any network call."""

import pytest

from synq_paperwork.schema import Invoice, LineItem
from synq_paperwork.sheets import NotVerified, send_to_sheet

INVOICE = Invoice(supplier_name="S", invoice_number="1", invoice_date="2026-01-31", currency="USD",
                  lines=[LineItem(description="x", quantity=1, unit_price=1, amount=1)], subtotal=1, tax=0, total=1)


@pytest.mark.parametrize("status", ["needs review", "", "VERIFIED ", "error"])
def test_only_verified_invoices_are_sent(status, monkeypatch):
    # If the code tried to send, this fake would fail the test.
    monkeypatch.setattr("httpx.post", lambda *a, **k: pytest.fail("must not send"))
    with pytest.raises(NotVerified):
        send_to_sheet(INVOICE, status)
