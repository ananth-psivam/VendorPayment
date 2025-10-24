import os
import re
import io
import smtplib
from email.message import EmailMessage
from typing import List, Dict, Tuple, Optional
from datetime import datetime

import streamlit as st
from supabase import create_client, Client

# PDF/HTML parsing
try:
    import pdfplumber
except Exception:
    pdfplumber = None
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

# ---------- CONFIG ----------
APP_TITLE = "Vendor Payment Auto‑Responder"
DEFAULT_SUBJECT_PREFIX = "Re: Payment Inquiry – "

# Expected Supabase table and columns
# Table: invoices
# Columns: invoice_id (text, pk), vendor_email (text), vendor_name (text), amount (numeric), currency (text),
#          status (text: 'Paid'|'Processing'|'Queued'|'On Hold'|'Rejected'|'Not Found'),
#          due_date (date), paid_date (date), remittance_ref (text),
#          last_updated (timestamptz)

# ---------- UTIL ----------
INVOICE_REGEXES = [
    r"invoice\\s*(?:#|no\\.?|id\\:?)\\s*([A-Z0-9\\-_/]{4,})",
    r"\\bINV[-_/]?(\\d{4,})\\b",
    r"\\b([A-Z]{2,5}\\d{4,})\\b",
]
EMAIL_REGEX = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"
CURRENCY_REGEX = r"\\b(USD|EUR|GBP|INR|CAD|AUD|JPY|SGD|AED)\\b"
AMOUNT_REGEX = r"(?i)(?:amount|total|payment)\\s*(?:due|paid|)\\s*[:$]?\\s*([\\$€£₹]?[\\s]*\\d{1,3}(?:[,\\s]\\d{3})*(?:\\.\\d{1,2})?)"


def parse_amount(text: str) -> Optional[str]:
    m = re.search(AMOUNT_REGEX, text)
    if m:
        return m.group(1).replace(" ", "")
    return None


def extract_invoice_ids(text: str) -> List[str]:
    found = []
    low = text.lower()
    for rx in INVOICE_REGEXES:
        for m in re.finditer(rx, low, flags=re.I):
            val = m.group(1).upper().strip(".,;: )(")
            if len(val) >= 4 and val not in found:
                found.append(val)
    return found


def extract_emails(text: str) -> List[str]:
    return sorted(set(re.findall(EMAIL_REGEX, text)))


def read_pdf(file_bytes: bytes) -> str:
    if not pdfplumber:
        return ""
    buf = io.BytesIO(file_bytes)
    out = []
    with pdfplumber.open(buf) as pdf:
        for page in pdf.pages:
            out.append(page.extract_text() or "")
    return "\\n".join(out)


def read_html(file_bytes: bytes) -> str:
    if not BeautifulSoup:
        return ""
    html = file_bytes.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    # get visible text; fallback to body text
    text = soup.get_text(" ", strip=True)
    return text


def load_supabase() -> Optional[Client]:
    url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


def lookup_invoices(sb: Client, invoice_ids: List[str]) -> Dict[str, Dict]:
    results: Dict[str, Dict] = {}
    if not invoice_ids:
        return results
    # Chunk queries to avoid URL length issues
    chunk_size = 50
    for i in range(0, len(invoice_ids), chunk_size):
        chunk = invoice_ids[i:i+chunk_size]
        q = sb.table("invoices").select("*").in_("invoice_id", chunk)
        resp = q.execute()
        for row in resp.data or []:
            results[row["invoice_id"].upper()] = row
    return results


# ---------- RESPONSE TEMPLATES ----------

def fmt_amount(amount: Optional[float], currency: Optional[str]) -> str:
    if amount is None:
        return ""
    cur = currency or "USD"
    return f"{cur} {amount:,.2f}"


def subject_for(vendor_name: str, invoice_ids: List[str]) -> str:
    inv = ", ".join(invoice_ids[:3]) + ("…" if len(invoice_ids) > 3 else "")
    name = vendor_name or "Vendor"
    return f"{DEFAULT_SUBJECT_PREFIX}{name} – {inv if inv else 'Payment Inquiry'}"


def build_body(name: str, status_row: Optional[Dict], invoice_id: str) -> str:
    greeting = f"Hi {name or 'Team'},"
    if not status_row:
        return (
            f"{greeting}\\n\\n"
            f"Thanks for reaching out. We couldn't find invoice **{invoice_id}** in our records. "
            f"Could you please confirm the invoice number, amount, and date, or attach the invoice copy?\\n\\n"
            f"Regards,\\nAccounts Payable"
        )

    status = (status_row.get("status") or "").title()
    amount = fmt_amount(status_row.get("amount"), status_row.get("currency"))
    due_date = status_row.get("due_date")
    paid_date = status_row.get("paid_date")
    rem_ref = status_row.get("remittance_ref")

    details = []
    if amount:
        details.append(f"Amount: {amount}")
    if due_date:
        details.append(f"Due Date: {due_date}")
    if paid_date:
        details.append(f"Paid Date: {paid_date}")
    if rem_ref:
        details.append(f"Remittance Ref: {rem_ref}")
    details_str = "\\n".join(f"• {d}" for d in details) if details else ""

    if status == "Paid":
        core = (
            f"Invoice **{invoice_id}** shows as **Paid** in our system.\\n\\n{details_str}\\n\\n"
            f"If you haven't received the remittance advice, let us know and we'll resend."
        )
    elif status in {"Processing", "Queued"}:
        core = (
            f"Invoice **{invoice_id}** is currently **{status}**.\\n\\n{details_str}\\n\\n"
            f"We expect completion soon; we'll notify you once it posts."
        )
    elif status == "On Hold":
        core = (
            f"Invoice **{invoice_id}** is **On Hold** pending additional review.\\n\\n{details_str}\\n\\n"
            f"Our team will reach out if further information is required."
        )
    elif status == "Rejected":
        core = (
            f"Invoice **{invoice_id}** was **Rejected**.\\n\\n{details_str}\\n\\n"
            f"Please review and resend a corrected invoice or reply with clarifications."
        )
    else:
        core = (
            f"We couldn't find invoice **{invoice_id}** in our system. "
            f"Please confirm the invoice number, amount, and date, or attach the invoice."
        )

    return (
        f"{greeting}\\n\\n{core}\\n\\nRegards,\\nAccounts Payable"
    )


# ---------- EMAIL SENDING ----------

def send_email(
    to_addr: str,
    subject: str,
    body_md: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """Send via SMTP using env/secrets. If SMTP creds missing, return False with reason."""
    host = st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST")
    port = int(st.secrets.get("SMTP_PORT") or os.getenv("SMTP_PORT") or 587)
    user = st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER")
    pwd = st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS")
    from_addr = st.secrets.get("SMTP_FROM") or os.getenv("SMTP_FROM") or user

    if not (host and user and pwd and from_addr):
        return False, "SMTP configuration missing"

    # Basic Markdown to plain text (keep it simple for now)
    body_txt = re.sub(r"\\*\\*(.*?)\\*\\*", r"\\1", body_md)

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        # BCC not set in headers for recipients, but we'll include in send list
        pass
    msg["Subject"] = subject
    msg.set_content(body_txt)

    recipients = [to_addr] + (cc or []) + (bcc or [])

    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg, to_addrs=recipients)
        return True, "Sent"
    except Exception as e:
        return False, f"SMTP error: {e}"


# ---------- STREAMLIT APP ----------

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="📧", layout="wide")
    st.title("📧 Vendor Payment Auto‑Responder (Agentic)")
    st.caption("Upload vendor emails (PDF/HTML). I will extract invoice details, check Supabase, and draft/send responses.")

    with st.expander("⚙️ Configuration", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            default_cc = st.text_input("Default CC (comma‑separated emails)", value="")
            dry_run = st.checkbox("Dry‑run (do not send emails)", value=True, help="Generate drafts only.")
            allow_multi = st.checkbox("One reply per invoice (split emails by multiple invoices)", value=True)
        with col2:
            supa_ok = load_supabase() is not None
            st.write("Supabase connected ✅" if supa_ok else "Supabase not configured ❌ – set SUPABASE_URL and SUPABASE_ANON_KEY in Secrets/Env")
            smtp_ok = all([
                st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST"),
                st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER"),
                st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS"),
            ])
            st.write("SMTP configured ✅" if smtp_ok else "SMTP not configured ❌ – set SMTP_* secrets to send emails")

    uploads = st.file_uploader("Upload vendor inquiries (PDF or HTML)", type=["pdf", "html", "htm"], accept_multiple_files=True)

    if not uploads:
        st.info("Upload one or more PDF/HTML emails to begin.")
        return

    sb = load_supabase()
    if not sb:
        st.warning("Supabase not configured; lookups will be skipped.")

    cc_list = [e.strip() for e in (default_cc or "").split(",") if e.strip()]

    results_log: List[Dict] = []

    for up in uploads:
        st.subheader(f"📎 {up.name}")
        ext = (up.name.split(".")[-1] or "").lower()
        raw = up.read()

        if ext == "pdf":
            text = read_pdf(raw)
        else:
            text = read_html(raw)

        if not text:
            st.error("Could not parse the file. Ensure pdfplumber / BeautifulSoup are available.")
            continue

        with st.expander("View parsed text"):
            st.text(text[:5000])

        invoice_ids = extract_invoice_ids(text)
        emails_in_text = extract_emails(text)
        vendor_email = emails_in_text[0] if emails_in_text else None
        amount_str = parse_amount(text)
        currency_match = re.search(CURRENCY_REGEX, text)
        currency = currency_match.group(1) if currency_match else None

        colA, colB, colC = st.columns(3)
        with colA:
            st.write("**Detected invoices:**", ", ".join(invoice_ids) if invoice_ids else "—")
        with colB:
            st.write("**Detected vendor email:**", vendor_email or "—")
        with colC:
            st.write("**Detected amount/currency:**", (amount_str or "—"), (currency or ""))

        lookup = lookup_invoices(sb, invoice_ids) if sb and invoice_ids else {}

        # Build drafts
        if allow_multi and invoice_ids:
            to_process = [(iid, lookup.get(iid)) for iid in invoice_ids]
        else:
            # single email covering first invoice (or none)
            iid = invoice_ids[0] if invoice_ids else "(not provided)"
            to_process = [(iid, lookup.get(iid))]

        for invoice_id, row in to_process:
            vendor_name = (row or {}).get("vendor_name") or (vendor_email.split("@")[0].title() if vendor_email else "Vendor")
            subject = subject_for(vendor_name, [invoice_id] if invoice_ids else [])
            body = build_body(vendor_name, row, invoice_id)

            st.markdown(f"**Subject:** {subject}")
            st.code(body)

            action_col1, action_col2 = st.columns([1,2])
            with action_col1:
                send_now = st.button(f"Send to {vendor_email or '—'}", key=f"send_{up.name}_{invoice_id}", disabled=(not vendor_email))
            with action_col2:
                download = st.download_button(
                    label="Download .eml draft",
                    data=f"Subject: {subject}\\nTo: {vendor_email or ''}\\nCc: {', '.join(cc_list)}\\n\\n{body}",
                    file_name=f"reply_{invoice_id or 'inquiry'}.eml",
                    mime="message/rfc822",
                    key=f"dl_{up.name}_{invoice_id}"
                )

            status_msg = "Draft"
            sent_ok = False
            if send_now and not dry_run and vendor_email:
                ok, info = send_email(vendor_email, subject, body, cc=cc_list)
                sent_ok = ok
                status_msg = "Sent" if ok else f"Failed – {info}"
                st.success("Email sent." if ok else f"Failed to send: {info}")
            elif send_now and dry_run:
                st.info("Dry‑run enabled. Email not sent.")

            results_log.append({
                "file": up.name,
                "vendor_email": vendor_email,
                "invoice_id": invoice_id,
                "status": (row or {}).get("status") if row else "Not Found",
                "amount": (row or {}).get("amount"),
                "currency": (row or {}).get("currency"),
                "action": status_msg,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            })

    st.divider()
    st.subheader("Run Log")
    if results_log:
        import pandas as pd
        df = pd.DataFrame(results_log)
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "Download CSV log",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name="run_log.csv",
            mime="text/csv",
        )

    st.caption("Tip: configure Streamlit secrets with SUPABASE_URL, SUPABASE_ANON_KEY, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM")


if __name__ == "__main__":
    main()
