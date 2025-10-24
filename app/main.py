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
APP_TITLE = "Vendor Payment Auto-Responder (Agentic)"
DEFAULT_SUBJECT_PREFIX = "Re: Payment Inquiry – "

# ---------- UTIL ----------
INVOICE_REGEXES = [
    r"invoice\s*(?:#|no\.?|id\:?)\s*([A-Z0-9\-_\/]{4,})",
    r"\bINV[-_\/]?(\d{4,})\b",
    r"\b([A-Z]{2,5}\d{4,})\b",
]
EMAIL_REGEX = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
CURRENCY_REGEX = r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY|SGD|AED)\b"
AMOUNT_REGEX = r"(?i)(?:amount|total|payment)\s*(?:due|paid|)?\s*[:$]?\s*([\$€£₹]?\s*\d{1,3}(?:[, \s]\d{3})*(?:\.\d{1,2})?)"

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
    return "\n".join(out)

def read_html(file_bytes: bytes) -> str:
    if not BeautifulSoup:
        return ""
    html = file_bytes.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True)

def load_supabase() -> Optional[Client]:
    url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    return create_client(url, key)

def lookup_invoices_by_supplier_invoice_no(sb: Client, ids: List[str]) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    if not sb or not ids:
        return out
    chunk = 50
    for i in range(0, len(ids), chunk):
        part = ids[i:i+chunk]
        resp = sb.table("invoices").select("*").in_("Supplier_Invoice_No", part).execute()
        for row in resp.data or []:
            key = str(row.get("Supplier_Invoice_No") or "").upper()
            out[key] = row
    return out

# ---------- RESPONSE TEMPLATES ----------
def fmt_amount_from_text(amount_text: Optional[str], currency: Optional[str]) -> str:
    if not amount_text:
        return ""
    # try to parse a float from text like "1,234.56" or "$1,234.56"
    try:
        cleaned = re.sub(r"[^\d.]", "", str(amount_text))
        val = float(cleaned) if cleaned else None
    except Exception:
        val = None
    cur = currency or "USD"
    return f"{cur} {val:,.2f}" if val is not None else f"{cur} {amount_text}"

def subject_for(vendor_name: str, invoice_ids: List[str]) -> str:
    inv = ", ".join(invoice_ids[:3]) + ("…" if len(invoice_ids) > 3 else "")
    name = vendor_name or "Vendor"
    return f"{DEFAULT_SUBJECT_PREFIX}{name} – {inv if inv else 'Payment Inquiry'}"

def build_body_from_supplier_schema(vendor_name: str, row: Optional[Dict], invoice_id: str) -> str:
    greeting = f"Hi {vendor_name or 'Team'},"
    if not row:
        return (
            f"{greeting}\n\n"
            f"Thanks for reaching out. We couldn't find invoice **{invoice_id}** in our records. "
            f"Could you please confirm the invoice number, amount, and date, or attach the invoice copy?\n\n"
            f"Regards,\nAccounts Payable"
        )

    status = (row.get("Status") or "").title()
    amount = fmt_amount_from_text(row.get("Total_Invoice_Amount"), row.get("Currency"))
    invoice_date = row.get("Invoice_Date")
    supplier_invoice_date = row.get("Supplier_Invoice_Date")
    comments = row.get("Comments")
    file_url = row.get("file_url")

    details = []
    if amount: details.append(f"Amount: {amount}")
    if invoice_date: details.append(f"Invoice Date: {invoice_date}")
    if supplier_invoice_date: details.append(f"Supplier Invoice Date: {supplier_invoice_date}")
    if comments: details.append(f"Notes: {comments}")
    if file_url: details.append(f"Invoice File: {file_url}")
    details_str = "\n".join(f"• {d}" for d in details) if details else ""

    if status == "Paid":
        core = (
            f"Invoice **{invoice_id}** shows as **Paid** in our system.\n\n{details_str}\n\n"
            f"If you haven't received the remittance advice, let us know and we'll resend."
        )
    elif status in {"Processing", "Queued"}:
        core = (
            f"Invoice **{invoice_id}** is currently **{status}**.\n\n{details_str}\n\n"
            f"We expect completion soon; we'll notify you once it posts."
        )
    elif status == "On Hold":
        core = (
            f"Invoice **{invoice_id}** is **On Hold** pending additional review.\n\n{details_str}\n\n"
            f"Our team will reach out if further information is required."
        )
    elif status == "Rejected" or status == "Unpaid":
        core = (
            f"Invoice **{invoice_id}** is **{status}**.\n\n{details_str}\n\n"
            f"Please review the above details and let us know if additional information is needed."
        )
    else:
        core = (
            f"We couldn't find invoice **{invoice_id}** in our system. "
            f"Please confirm the invoice number, amount, and date, or attach the invoice."
        )
    return f"{greeting}\n\n{core}\n\nRegards,\nAccounts Payable"

# ---------- EMAIL SENDING ----------
def send_email(
    to_addr: str,
    subject: str,
    body_md: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    host = st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST")
    port = int(st.secrets.get("SMTP_PORT") or os.getenv("SMTP_PORT") or 587)
    user = st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER")
    pwd = st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS")
    from_addr = st.secrets.get("SMTP_FROM") or os.getenv("SMTP_FROM") or user

    if not (host and user and pwd and from_addr):
        return False, "SMTP configuration missing"

    # Basic Markdown to plain text
    body_txt = re.sub(r"\*\*(.*?)\*\*", r"\1", body_md)

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = ", ".join(cc)
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
    st.title("📧 Vendor Payment Auto-Responder (Agentic)")
    st.caption("Read vendor emails (PDF/HTML) from Upload or Supabase Storage, "
               "extract invoice details, look up in Supabase (Supplier_Invoice_No), and draft/send replies.")

    with st.expander("⚙️ Configuration", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            default_cc = st.text_input("Default CC (comma-separated emails)", value="")
            dry_run = st.checkbox("Dry-run (do not send emails)", value=True, help="Generate drafts only.")
            allow_multi = st.checkbox("One reply per invoice (split emails if multiple IDs found)", value=True)
        with col2:
            sb = load_supabase()
            st.write("Supabase connected ✅" if sb else "Supabase not configured ❌ – set SUPABASE_URL and SUPABASE_ANON_KEY")
            smtp_ok = all([
                st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST"),
                st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER"),
                st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS"),
            ])
            st.write("SMTP configured ✅" if smtp_ok else "SMTP not configured ❌ – set SMTP_* secrets to send emails")

    # ---- File Source: Upload or Supabase Storage ----
    source = st.radio("Choose input source", ["Upload", "Supabase Storage"], horizontal=True)
    files_to_process = []  # list of dicts: {name, raw, ext}

    sb = load_supabase()
    if not sb:
        st.warning("Supabase not configured; lookups/storage will be limited.")

    if source == "Upload":
        uploads = st.file_uploader("Upload vendor inquiries (PDF or HTML)", type=["pdf", "html", "htm"], accept_multiple_files=True)
        if uploads:
            for up in uploads:
                files_to_process.append({"name": up.name, "raw": up.read(), "ext": (up.name.split(".")[-1] or "").lower()})
        else:
            st.info("Upload files or switch to Supabase Storage.")
    else:
        bucket_default = st.secrets.get("BUCKET_NAME") or os.getenv("BUCKET_NAME") or "vendor-inquiries"
        bucket = st.text_input("Supabase bucket", value=bucket_default)
        prefix = st.text_input("Folder/prefix (optional)", value="")
        listing = []
        if sb and bucket:
            try:
                listing = sb.storage.from_(bucket).list(prefix)
            except Exception as e:
                st.error(f"Storage list error: {e}")
        st.write("Found:", len(listing) if listing else 0)
        names = [
            (prefix + ("/" if prefix and not prefix.endswith("/") else "") + f["name"])
            for f in (listing or []) if f.get("name")
        ]
        selected = st.multiselect("Select files to process", names, default=names[:5])
        if selected and sb and bucket:
            for path in selected:
                try:
                    blob = sb.storage.from_(bucket).download(path)
                    ext = (path.split(".")[-1] or "").lower()
                    files_to_process.append({"name": os.path.basename(path), "raw": blob, "ext": ext})
                except Exception as e:
                    st.error(f"Download failed for {path}: {e}")

    if not files_to_process:
        return

    cc_list = [e.strip() for e in (default_cc or "").split(",") if e.strip()]
    results_log: List[Dict] = []

    for f in files_to_process:
        st.subheader(f"📎 {f['name']}")
        ext = f["ext"]
        raw = f["raw"]

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

        # Lookup by Supplier_Invoice_No
        lookup = lookup_invoices_by_supplier_invoice_no(sb, invoice_ids) if sb and invoice_ids else {}

        # Build drafts
        if allow_multi and invoice_ids:
            to_process = [(iid, lookup.get(iid)) for iid in invoice_ids]
        else:
            iid = invoice_ids[0] if invoice_ids else "(not provided)"
            to_process = [(iid, lookup.get(iid))]

        for invoice_id, row in to_process:
            vendor_name = (row or {}).get("Supplier_Name") or (vendor_email.split("@")[0].title() if vendor_email else "Vendor")
            subject = subject_for(vendor_name, [invoice_id] if invoice_ids else [])
            body = build_body_from_supplier_schema(vendor_name, row, invoice_id)

            st.markdown(f"**Subject:** {subject}")
            st.code(body)

            action_col1, action_col2 = st.columns([1, 2])
            with action_col1:
                send_now = st.button(
                    f"Send to {vendor_email or '—'}",
                    key=f"send_{f['name']}_{invoice_id}",
                    disabled=(not vendor_email)
                )
            with action_col2:
                st.download_button(
                    label="Download .eml draft",
                    data=f"Subject: {subject}\nTo: {vendor_email or ''}\nCc: {', '.join(cc_list)}\n\n{body}",
                    file_name=f"reply_{invoice_id or 'inquiry'}.eml",
                    mime="message/rfc822",
                    key=f"dl_{f['name']}_{invoice_id}"
                )

            status_msg = "Draft"
            if send_now and not dry_run and vendor_email:
                ok, info = send_email(vendor_email, subject, body, cc=cc_list)
                status_msg = "Sent" if ok else f"Failed – {info}"
                st.success("Email sent." if ok else f"Failed to send: {info}")
            elif send_now and dry_run:
                st.info("Dry-run enabled. Email not sent.")

            results_log.append({
                "file": f['name'],
                "vendor_email": vendor_email,
                "supplier_invoice_no": invoice_id,
                "status": (row or {}).get("Status") if row else "Not Found",
                "amount_text": (row or {}).get("Total_Invoice_Amount"),
                "currency": (row or {}).get("Currency"),
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

    st.caption(
        "Secrets needed: SUPABASE_URL, SUPABASE_ANON_KEY, optional BUCKET_NAME; "
        "for sending email also set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM."
    )

if __name__ == "__main__":
    main()
