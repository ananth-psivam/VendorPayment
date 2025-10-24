import os, re, io, smtplib
from email.message import EmailMessage
from typing import List, Dict, Optional, Tuple
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

# Optional GenAI (OpenAI)
OPENAI_AVAILABLE = False
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    pass

APP_TITLE = "Vendor Payment Auto-Responder (Agentic)"
DEFAULT_SUBJECT_PREFIX = "Re: Payment Inquiry – "

# ------------- Regex helpers -------------
INVOICE_REGEXES = [
    r"invoice\s*(?:#|no\.?|id\:?)\s*([A-Z0-9\-_\/]{4,})",
    r"\bINV[-_\/]?(\d{4,})\b",
    r"\b([A-Z]{2,5}\d{4,})\b",
]
EMAIL_REGEX = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
CURRENCY_REGEX = r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY|SGD|AED)\b"
AMOUNT_REGEX = r"(?i)(?:amount|total|payment)\s*(?:due|paid|)?\s*[:$]?\s*([\$€£₹]?\s*\d{1,3}(?:[, \s]\d{3})*(?:\.\d{1,2})?)"

def parse_amount(text:str)->Optional[str]:
    m = re.search(AMOUNT_REGEX, text)
    return m.group(1).replace(" ","") if m else None

def extract_invoice_ids(text:str)->List[str]:
    found = []
    low = text.lower()
    for rx in INVOICE_REGEXES:
        for m in re.finditer(rx, low, flags=re.I):
            val = m.group(1).upper().strip(".,;: )(")
            if len(val)>=4 and val not in found:
                found.append(val)
    return found

def extract_emails(text:str)->List[str]:
    return sorted(set(re.findall(EMAIL_REGEX, text)))

def read_pdf(b:bytes)->str:
    if not pdfplumber: return ""
    buf = io.BytesIO(b)
    out=[]
    with pdfplumber.open(buf) as pdf:
        for p in pdf.pages:
            out.append(p.extract_text() or "")
    return "\n".join(out)

def read_html(b:bytes)->str:
    if not BeautifulSoup: return ""
    html = b.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True)

# ------------- Supabase -------------
def load_supabase()->Optional[Client]:
    url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key: return None
    return create_client(url, key)

def list_storage(sb:Client, bucket:str, prefix:str="", limit:int=1000)->Tuple[List[Dict], str]:
    """
    Returns (files, error). Works for top-level and a single folder level.
    Supabase 'list' is not recursive. We provide a 'Include subfolders' option by iterating one level deeper.
    """
    try:
        files = sb.storage.from_(bucket).list(prefix, { "limit": limit, "offset": 0, "sortBy": {"column":"name","order":"asc"} })
        return files or [], ""
    except Exception as e:
        return [], f"{e}"

def download(sb:Client, bucket:str, path:str)->bytes:
    return sb.storage.from_(bucket).download(path)

def lookup_by_supplier_invoice_no(sb:Client, ids:List[str])->Dict[str,Dict]:
    out={}
    if not (sb and ids): return out
    chunk=50
    for i in range(0,len(ids),chunk):
        part = ids[i:i+chunk]
        resp = sb.table("invoices").select("*").in_("Supplier_Invoice_No", part).execute()
        for row in resp.data or []:
            key = str(row.get("Supplier_Invoice_No") or "").upper()
            out[key]=row
    return out

# ------------- Email + GenAI -------------
def fmt_amount_from_text(amount_text:Optional[str], currency:Optional[str])->str:
    if not amount_text: return ""
    try:
        cleaned = re.sub(r"[^\d.]", "", str(amount_text))
        val = float(cleaned) if cleaned else None
    except Exception:
        val = None
    cur = currency or "USD"
    return f"{cur} {val:,.2f}" if val is not None else f"{cur} {amount_text}"

def subject_for(vendor_name:str, invoice_ids:List[str])->str:
    inv = ", ".join(invoice_ids[:3]) + ("…" if len(invoice_ids)>3 else "")
    name = vendor_name or "Vendor"
    return f"{DEFAULT_SUBJECT_PREFIX}{name} – {inv or 'Payment Inquiry'}"

def draft_email_template(vendor_name:str, row:Optional[Dict], invoice_id:str)->str:
    greeting = f"Hi {vendor_name or 'Team'},"
    if not row:
        return (f"{greeting}\n\n"
                f"Thanks for reaching out. We couldn't find invoice **{invoice_id}** in our records. "
                f"Could you please confirm the invoice number, amount, and date, or attach the invoice copy?\n\n"
                f"Regards,\nAccounts Payable")
    status = (row.get("Status") or "").title()
    amount = fmt_amount_from_text(row.get("Total_Invoice_Amount"), row.get("Currency"))
    inv_date = row.get("Invoice_Date")
    sup_inv_date = row.get("Supplier_Invoice_Date")
    comments = row.get("Comments")
    file_url = row.get("file_url")

    details=[]
    if amount: details.append(f"Amount: {amount}")
    if inv_date: details.append(f"Invoice Date: {inv_date}")
    if sup_inv_date: details.append(f"Supplier Invoice Date: {sup_inv_date}")
    if comments: details.append(f"Notes: {comments}")
    if file_url: details.append(f"Invoice File: {file_url}")
    details_str = "\n".join(f"• {d}" for d in details) if details else ""

    if status=="Paid":
        core = (f"Invoice **{invoice_id}** shows as **Paid** in our system.\n\n{details_str}\n\n"
                f"If you haven't received the remittance advice, let us know and we'll resend.")
    elif status in {"Processing","Queued"}:
        core = (f"Invoice **{invoice_id}** is currently **{status}**.\n\n{details_str}\n\n"
                f"We expect completion soon; we'll notify you once it posts.")
    elif status=="On Hold":
        core = (f"Invoice **{invoice_id}** is **On Hold** pending additional review.\n\n{details_str}\n\n"
                f"Our team will reach out if further information is required.")
    elif status in {"Rejected","Unpaid"}:
        core = (f"Invoice **{invoice_id}** is **{status}**.\n\n{details_str}\n\n"
                f"Please review the above details and let us know if additional information is needed.")
    else:
        core = (f"We couldn't find invoice **{invoice_id}** in our system. "
                f"Please confirm the invoice number, amount, and date, or attach the invoice.")
    return f"{greeting}\n\n{core}\n\nRegards,\nAccounts Payable"

def draft_email_genai(base:str, vendor_name:str, row:Optional[Dict], invoice_id:str)->str:
    """If OPENAI_API_KEY provided, refine the draft for tone/clarity. Fallback to base."""
    if not OPENAI_AVAILABLE: return base
    api_key = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    print ("printing the API key", api_key) 
    if not api_key: return base
    try:
        client = OpenAI(api_key=api_key)
        # Keep prompt short and deterministic for business email
        prompt = f"""
You are an AP specialist. Improve the following draft email: make it concise, polite, and professional.
Keep all facts and sensitive info intact. Always include the invoice number {invoice_id}.
Return plain text only.

Draft:
{base}
"""
        rsp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
        )
        txt = rsp.choices[0].message.content.strip()
        return txt or base
    except Exception:
        return base

def send_email(to_addr:str, subject:str, body_md:str, cc:Optional[List[str]]=None, bcc:Optional[List[str]]=None)->Tuple[bool,str]:
    host = st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST")
    port = int(st.secrets.get("SMTP_PORT") or os.getenv("SMTP_PORT") or 587)
    user = st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER")
    pwd  = st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS")
    from_addr = st.secrets.get("SMTP_FROM") or os.getenv("SMTP_FROM") or user
    if not (host and user and pwd and from_addr):
        return False, "SMTP configuration missing"
    body_txt = re.sub(r"\*\*(.*?)\*\*", r"\1", body_md)
    msg = EmailMessage()
    msg["From"]=from_addr; msg["To"]=to_addr; msg["Subject"]=subject
    if cc: msg["Cc"]=", ".join(cc)
    msg.set_content(body_txt)
    recipients=[to_addr]+(cc or [])+(bcc or [])
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls(); s.login(user,pwd); s.send_message(msg, to_addrs=recipients)
        return True,"Sent"
    except Exception as e:
        return False, f"SMTP error: {e}"

# ------------- App -------------
def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="📧", layout="wide")
    st.title("📧 Vendor Payment Auto-Responder (Agentic)")
    st.caption("Read vendor emails (PDF/HTML) from Supabase Storage or Upload, validate against Supabase invoices (Supplier_Invoice_No), and generate a GenAI email reply per vendor.")

    with st.expander("⚙️ Configuration", expanded=True):
        col1,col2 = st.columns(2)
        with col1:
            default_cc = st.text_input("Default CC (comma-separated)", value="")
            dry_run = st.checkbox("Dry-run (do not send emails)", value=True)
            allow_multi = st.checkbox("One reply per each detected invoice ID", value=True)
            use_genai = st.checkbox("Use GenAI to polish email (OpenAI)", value=True)
        with col2:
            sb = load_supabase()
            st.write("Supabase connected ✅" if sb else "Supabase not configured ❌")
            smtp_ok = all([st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST"),
                           st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER"),
                           st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS")])
            st.write("SMTP configured ✅" if smtp_ok else "SMTP not configured ❌")
            if use_genai:
                ok_ai = (st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")) and OPENAI_AVAILABLE
                st.write("GenAI ready ✅" if ok_ai else "GenAI not configured (set OPENAI_API_KEY)")

    source = st.radio("Choose input source", ["Supabase Storage","Upload"], horizontal=True)

    files_to_process=[]
    sb = load_supabase()

    if source=="Supabase Storage":
        bucket_default = st.secrets.get("BUCKET_NAME") or os.getenv("BUCKET_NAME") or "vendor-inquiries"
        bucket = st.text_input("Supabase bucket", value=bucket_default)
        prefix = st.text_input("Folder/prefix (optional, e.g. 'inbox/2025-10')", value="")
        include_sub = st.checkbox("Include one subfolder level", value=True)
        files, err = ([], "Supabase not configured") if not (sb and bucket) else list_storage(sb, bucket, prefix)
        if err: st.error(f"List error: {err}")
        # Build select options (files only)
        names=[]
        for f in files:
            # Heuristic: storage returns folders without 'id' sometimes; files have 'id' and 'metadata'
            # Keep anything that looks like a file (has 'id' or size)
            if f.get("id") or (f.get("metadata") and isinstance(f["metadata"], dict)):
                names.append(f["name"] if not prefix else f"{prefix.rstrip('/')}/{f['name']}")
            elif include_sub:
                # subfolder listing
                sub_prefix = (prefix.rstrip('/') + '/' if prefix else '') + f["name"]
                sub_files, _ = list_storage(sb, bucket, sub_prefix)
                for sf in sub_files or []:
                    if sf.get("id") or (sf.get("metadata") and isinstance(sf["metadata"], dict)):
                        names.append(f"{sub_prefix}/{sf['name']}")

        st.markdown(f"Found: **{len(names)}** file(s)")
        selected = st.multiselect("Select files to process", names, default=names[:10])

        if selected and sb:
            for path in selected:
                try:
                    blob = download(sb, bucket, path)
                    ext = (path.split(".")[-1] or "").lower()
                    files_to_process.append({"name": os.path.basename(path), "raw": blob, "ext": ext})
                except Exception as e:
                    st.error(f"Download failed for {path}: {e}")

    else:
        uploads = st.file_uploader("Upload vendor emails (PDF or HTML)", type=["pdf","html","htm"], accept_multiple_files=True)
        if uploads:
            for up in uploads:
                files_to_process.append({"name": up.name, "raw": up.read(), "ext": (up.name.split(".")[-1] or "").lower()})

    if not files_to_process:
        st.info("No files to process yet.")
        return

    cc_list=[e.strip() for e in (default_cc or "").split(",") if e.strip()]
    results=[]

    # --------- Process sequentially, one-by-one ----------
    for idx, f in enumerate(files_to_process, start=1):
        st.subheader(f"📎 {idx}/{len(files_to_process)} • {f['name']}")
        text = read_pdf(f["raw"]) if f["ext"]=="pdf" else read_html(f["raw"])
        if not text:
            st.error("Could not parse this file. Check that pdfplumber/BeautifulSoup are installed.")
            continue

        with st.expander("Parsed text (preview)"):
            st.text(text[:5000])

        invoice_ids = extract_invoice_ids(text)
        emails_in_text = extract_emails(text)
        vendor_email = emails_in_text[0] if emails_in_text else None

        colA,colB = st.columns(2)
        with colA: st.write("**Detected invoices:**", ", ".join(invoice_ids) if invoice_ids else "—")
        with colB: st.write("**Detected vendor email:**", vendor_email or "—")

        look = lookup_by_supplier_invoice_no(sb, invoice_ids) if (sb and invoice_ids) else {}

        if (not invoice_ids):
            # still provide a generic draft to request details
            subject = subject_for("Vendor", [])
            base = draft_email_template("Vendor", None, "(not provided)")
            polished = draft_email_genai(base, "Vendor", None, "(not provided)") if use_genai else base
            st.markdown(f"**Subject:** {subject}")
            st.code(polished)
            continue

        pairs = [(iid, look.get(iid)) for iid in invoice_ids] if allow_multi else [(invoice_ids[0], look.get(invoice_ids[0]))]

        for inv_id, row in pairs:
            vendor_name = (row or {}).get("Supplier_Name") or (vendor_email.split("@")[0].title() if vendor_email else "Vendor")
            subject = subject_for(vendor_name, [inv_id])
            base = draft_email_template(vendor_name, row, inv_id)
            polished = draft_email_genai(base, vendor_name, row, inv_id) if use_genai else base

            st.markdown(f"**Subject:** {subject}")
            st.code(polished)

            c1,c2 = st.columns([1,2])
            with c1:
                send_now = st.button(f"Send to {vendor_email or '—'}", key=f"send_{f['name']}_{inv_id}", disabled=(not vendor_email))
            with c2:
                st.download_button(
                    "Download .eml draft",
                    data=f"Subject: {subject}\nTo: {vendor_email or ''}\nCc: {', '.join(cc_list)}\n\n{polished}",
                    file_name=f"reply_{inv_id or 'inquiry'}.eml",
                    mime="message/rfc822",
                    key=f"dl_{f['name']}_{inv_id}"
                )

            status_msg = "Draft"
            if send_now and not dry_run and vendor_email:
                ok, info = send_email(vendor_email, subject, polished, cc=cc_list)
                status_msg = "Sent" if ok else f"Failed – {info}"
                st.success("Email sent." if ok else f"Failed to send: " + info)
            elif send_now and dry_run:
                st.info("Dry-run enabled. Email not sent.")

            results.append({
                "file": f['name'],
                "vendor_email": vendor_email,
                "supplier_invoice_no": inv_id,
                "status": (row or {}).get("Status") if row else "Not Found",
                "amount_text": (row or {}).get("Total_Invoice_Amount"),
                "currency": (row or {}).get("Currency"),
                "action": status_msg,
                "timestamp": datetime.utcnow().isoformat()+"Z",
            })

    st.divider()
    st.subheader("Run Log")
    if results:
        import pandas as pd
        df = pd.DataFrame(results)
        st.dataframe(df, use_container_width=True)
        st.download_button("Download CSV log", data=df.to_csv(index=False).encode("utf-8"), file_name="run_log.csv", mime="text/csv")

    st.caption("Secrets: SUPABASE_URL, SUPABASE_ANON_KEY, BUCKET_NAME (optional), "
               "SMTP_* (optional to send), OPENAI_API_KEY (optional for GenAI)")

if __name__ == "__main__":
    main()
