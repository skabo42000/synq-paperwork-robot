# Sheets handoff template

`save-invoice-to-sheets.json` is an **inactive reference export**. Credential bindings and the original Sheet identifier have been removed. Import it into your own n8n instance, then:

1. Bind the Webhook node to your own Header Auth credential: `X-Paperwork-Auth` with a random secret. Set the same secret as `PAPERWORK_WEBHOOK_SECRET` in the Python environment.
2. Bind the Google Sheets nodes to an account that can access only your intended test Sheet.
3. Replace each `REPLACE_WITH_GOOGLE_SHEET_ID` with your test Sheet ID.
4. Create `Invoices` and `Line items` tabs. Use the field names emitted by the **Validate & prepare** code node as the column headers; auto-mapping uses these exact names.
5. Verify the header check and test only synthetic data before activating the workflow.
6. Set `PAPERWORK_WEBHOOK_URL` to your own production webhook URL and `PAPERWORK_SHEET_ID` for the authenticated portal link.

The sender accepts only `verified` or `approved` status. The workflow validates again, checks supplier / invoice-number duplicates, writes line rows using `RAW`, and writes the invoice marker last. HTTP 409 indicates a duplicate.

**Limit:** a read-then-append Sheets duplicate check is not an atomic transaction. Partial failures or concurrent executions can leave duplicate / incomplete line rows. A production connector should use durable idempotency and reconciliation. Exported execution logging is disabled to reduce storage of document data; select your own retention policy in n8n.
