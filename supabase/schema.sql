create table if not exists public.invoices (
  Supplier_Name text primary key,
  Invoice_Date text,
  Total_Invoice Amount text,
  Currency_text default 'USD',,
  status_text check (status in ('Paid','Unpaid','Queued','On Hold','Rejected','Not Found')),
  Supplier_Invoice_No. text,
  Comments_text,
  Supplier_Invoice Date date,
  );
