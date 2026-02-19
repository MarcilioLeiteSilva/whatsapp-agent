create table if not exists leads (
  id bigserial primary key,
  client_id text not null default 'default',
  instance text not null,
  from_number text not null,

  nome text,
  telefone text,
  assunto text,

  status text not null default 'iniciado',
  origem text not null default 'primeiro_contato',
  intent_detected text,

  first_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  lead_saved boolean not null default false
);

create index if not exists idx_leads_instance_from_created on leads (instance, from_number, created_at desc);
create index if not exists idx_leads_status_created on leads (status, created_at desc);
create index if not exists idx_leads_client_created on leads (client_id, created_at desc);
