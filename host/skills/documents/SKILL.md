---
name: documents
description: Find, read or file scanned letters, bills and other documents in the Paperless-ngx document server. Use when the owner asks about a bill, a letter, a contract or "that document from ...".
---

# Documents (Paperless-ngx)

Paperless-ngx runs in Docker in `~/paperless/` on the VM disk (it never depends on `/data`). Web UI on
the tailnet over HTTPS (URL in `~/assistant/MAP.md`); the container port is bound to localhost:8000.

## Access
- Use the REST API on `http://127.0.0.1:8000/api/` with the assistant's own token:
  `Authorization: Token $(cat ~/.config/paperless/claude.token)`. Never print the token.
- The assistant user has no permission on documents tagged `private`. Do not try to work around it
  (no admin login, no database queries, no reading `~/paperless/media` directly).

## Common calls
- Search: `GET /api/documents/?query=<text>` (full text), filter with `correspondent__name__icontains=`,
  `document_type__name__iexact=`, `created__date__gt=`.
- Metadata: `GET /api/documents/<id>/`; text: the `content` field; file: `/api/documents/<id>/download/`.
- Bills: find, then report amount, due date, payee and reference to the owner. The owner pays.

## Inputs and backups
- Scans arrive in `~/paperless/consume/` (also the `scan` network share) and are consumed automatically.
- A daily `document_exporter` run writes `~/paperless/export/` (timer `paperless-export`), which the
  nightly VM backup picks up. To rebuild: fresh stack, then `document_importer ../export`.
