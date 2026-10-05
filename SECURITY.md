# Security and data handling

## Scope

This repository is a portfolio / demonstration project. Public files and Git history were scanned with Gitleaks 8.30.1, and runtime dependency versions were checked against npm / PyPI vulnerability databases during the October 4, 2026 release. These checks find known patterns and advisories; they are not a penetration test or a guarantee that the application is secure.

Keep secrets in private environment variables or a secret manager. `.env.example` is a blank template. Do not commit populated workflow exports, databases, uploads, customer records, transcripts, recordings or provider keys. A public frontend variable is visible to every visitor.

## Reporting

Report a suspected issue privately through the contact channel at [synqlogic.com](https://synqlogic.com). Include the repository, affected component and a minimal synthetic reproduction. Do not put credentials or personal data in a public GitHub issue. Rotate any exposed live secret immediately; removing it from a file does not invalidate it or erase history.

## Automated controls

The security workflow scans Git history on pushes / pull requests with fully redacted output. Workflow permissions are read-only, official checkout is pinned to a commit, and the scanner archive is verified by SHA-256 before execution. Dependency update checks are configured weekly. Review and test updates before merging them.

## Project-specific boundaries

- Use synthetic invoices first. Uploaded content is sent to the selected AI provider; the approved / verified result is sent to your configured webhook and Sheet.
- The portal has one shared password, constant-time comparison, authenticated `/api/*` routes, upload-size / page limits and UUID-based storage paths. It does not implement individual roles, tenant isolation, password-attempt throttling or shared cost quotas.
- API responses are `no-store` and public API documentation is disabled. The local job store is not encrypted and files persist until deleted. Add retention rules, backups and appropriate access controls before real documents.
- Documents and model outputs are untrusted input. Arithmetic / source checks and human review reduce errors; they cannot prove all extracted text is correct or stop every prompt injection.
- n8n credentials / Sheet IDs were removed and the export is inactive. Authentication must be configured at the receiver as well as the sender.
- Sheets duplicate detection is a read-then-write flow. It is not a transaction: concurrent writes / partial failure need durable idempotency and reconciliation before production use.
