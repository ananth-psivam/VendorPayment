# Vendor Payment Auto‑Responder (Agentic AI)

A Streamlit app that ingests vendor emails (PDF/HTML), extracts invoice IDs / vendor details, checks payment status in Supabase, and drafts/sends replies via SMTP. Includes a CSV audit log.

## Quickstart

```bash
pip install -r requirements.txt
streamlit run app/main.py
```

## Configure Secrets

Create `.streamlit/secrets.toml` or set environment variables:

```toml
SUPABASE_URL = "https://<your-project>.supabase.co"
SUPABASE_ANON_KEY = "<anon-or-service-role-key>"

SMTP_HOST = "smtp.office365.com"
SMTP_PORT = "587"
SMTP_USER = "ap@yourcompany.com"
SMTP_PASS = "<smtp-app-password>"
SMTP_FROM = "Accounts Payable <ap@yourcompany.com>"
```

> Keep **Dry‑run** enabled while testing to avoid sending emails. Use the **Download .eml** option for approvals.

## Supabase Schema

See [`supabase/schema.sql`](supabase/schema.sql).

## Deploy

- **Streamlit Community Cloud**: push this repo to GitHub, set the app path to `app/main.py`, and add the secrets above.
- **Render / Fly.io / Docker**: install from `requirements.txt` and run `streamlit run app/main.py`.

## Roadmap (optional)

- Gmail/Outlook ingestion via API (auto process new messages)
- Attach remittance PDF from Supabase Storage when status == Paid
- Human-in-the-loop approval gate for On Hold/Rejected
- Policy/RAG snippets per vendor
