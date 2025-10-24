create table if not exists public.invoices (
  Supplier_Name text primary key,
  Invoice_Date text,
  Total_Invoice_Amount text,
  Currency text default 'USD',
  Status text check (status in ('Paid','Unpaid','Queued','On Hold','Rejected','Not Found')),
  Supplier_Invoice_No text,
  Comments text,
  Supplier_Invoice_Date date
  );
