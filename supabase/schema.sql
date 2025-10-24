create table if not exists public.invoices (
  Supplier Name text primary key,
  Invoice Date text,
  Total Invoice Amount text,
  Currency text default 'USD',,
  status text check (status in ('Paid','Unpaid','Queued','On Hold','Rejected','Not Found')),
  Supplier Invoice No. text,
  Comments text,
  Supplier Invoice Date date,
  last_updated timestamptz default now()
);
