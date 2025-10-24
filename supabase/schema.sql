create table if not exists public.invoices (
  invoice_id text primary key,
  vendor_email text,
  vendor_name text,
  amount numeric,
  currency text default 'USD',
  status text check (status in ('Paid','Processing','Queued','On Hold','Rejected','Not Found')),
  due_date date,
  paid_date date,
  remittance_ref text,
  last_updated timestamptz default now()
);
