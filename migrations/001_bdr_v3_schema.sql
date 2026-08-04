-- Pacific Yew BDR v3 transactional operating model.
-- Apply with a privileged migration role. Runtime workers should use the
-- Supabase service role or a dedicated PostgreSQL role with explicit grants.

create extension if not exists pgcrypto;

create table if not exists bdr_accounts (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    website text not null,
    domain text not null unique,
    source_url text not null,
    location text not null default '',
    is_operating_business boolean not null default false,
    verification_confidence double precision not null default 0 check (verification_confidence between 0 and 1),
    verification_reason text not null default '',
    metadata jsonb not null default '{}'::jsonb,
    fit_score integer not null default 0 check (fit_score between 0 and 100),
    timing_score integer not null default 0 check (timing_score between 0 and 100),
    authority_score integer not null default 0 check (authority_score between 0 and 100),
    evidence_score integer not null default 0 check (evidence_score between 0 and 100),
    risk_score integer not null default 100 check (risk_score between 0 and 100),
    total_score integer not null default 0 check (total_score between 0 and 100),
    eligible boolean not null default false,
    score_reasons jsonb not null default '[]'::jsonb,
    recommended_offer text,
    route_rationale text not null default '',
    route_confidence double precision not null default 0 check (route_confidence between 0 and 1),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists bdr_contacts (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references bdr_accounts(id) on delete cascade,
    email text not null,
    name text not null default '',
    role text not null default '',
    consent_type text not null default '',
    consent_source_url text not null default '',
    verified_business_email boolean not null default false,
    no_contact_statement boolean not null default false,
    confidence double precision not null default 0 check (confidence between 0 and 1),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists bdr_contacts_email_lower_uidx on bdr_contacts ((lower(email)));
create index if not exists bdr_contacts_account_idx on bdr_contacts(account_id);

create table if not exists bdr_evidence (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references bdr_accounts(id) on delete cascade,
    kind text not null,
    claim text not null,
    source_url text not null,
    excerpt text not null,
    confidence double precision not null check (confidence between 0 and 1),
    observed_at timestamptz not null,
    created_at timestamptz not null default now()
);
create index if not exists bdr_evidence_account_idx on bdr_evidence(account_id);

create table if not exists bdr_mailboxes (
    id uuid primary key default gen_random_uuid(),
    email text not null,
    provider text not null default 'zoho',
    enabled boolean not null default false,
    daily_limit integer not null default 8 check (daily_limit between 0 and 500),
    health_status text not null default 'unknown',
    last_health_check_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists bdr_mailboxes_email_lower_uidx on bdr_mailboxes ((lower(email)));

create table if not exists bdr_campaigns (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    status text not null default 'draft' check (status in ('draft','active','paused','completed')),
    target_description text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists bdr_sequence_steps (
    id uuid primary key default gen_random_uuid(),
    campaign_id uuid not null references bdr_campaigns(id) on delete cascade,
    position integer not null check (position > 0),
    delay_days integer not null check (delay_days >= 0),
    purpose text not null,
    subject_template text not null,
    body_template text not null,
    requires_approval boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (campaign_id, position)
);

create table if not exists bdr_enrollments (
    id uuid primary key default gen_random_uuid(),
    campaign_id uuid not null references bdr_campaigns(id) on delete restrict,
    account_id uuid not null references bdr_accounts(id) on delete cascade,
    contact_id uuid not null references bdr_contacts(id) on delete cascade,
    mailbox_id uuid not null references bdr_mailboxes(id) on delete restrict,
    status text not null default 'active' check (status in ('active','paused','completed','stopped')),
    offer text not null,
    timezone text not null default 'America/Vancouver',
    pause_reason text not null default '',
    stop_reason text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists bdr_enrollments_active_campaign_contact_uidx
    on bdr_enrollments(campaign_id, contact_id)
    where status in ('active','paused');
create index if not exists bdr_enrollments_contact_idx on bdr_enrollments(contact_id);

create table if not exists bdr_touches (
    id uuid primary key default gen_random_uuid(),
    enrollment_id uuid not null references bdr_enrollments(id) on delete cascade,
    sequence_step_id uuid not null references bdr_sequence_steps(id) on delete restrict,
    scheduled_for timestamptz not null,
    status text not null default 'scheduled'
        check (status in ('scheduled','claimed','reserved','sent','failed','uncertain','cancelled')),
    idempotency_key text not null unique,
    subject text not null,
    body text not null,
    approved_at timestamptz,
    approved_by text not null default '',
    claimed_at timestamptz,
    claimed_by text not null default '',
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists bdr_touches_due_idx on bdr_touches(status, scheduled_for);
create index if not exists bdr_touches_enrollment_idx on bdr_touches(enrollment_id);

create table if not exists bdr_messages (
    id uuid primary key default gen_random_uuid(),
    touch_id uuid not null unique references bdr_touches(id) on delete restrict,
    mailbox_id uuid not null references bdr_mailboxes(id) on delete restrict,
    idempotency_key text not null unique,
    status text not null default 'reserved'
        check (status in ('reserved','sent','failed','uncertain')),
    provider_message_id text not null default '',
    accepted_at timestamptz,
    error text not null default '',
    provider_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists bdr_messages_mailbox_accepted_idx on bdr_messages(mailbox_id, accepted_at);

create table if not exists bdr_suppressions (
    id uuid primary key default gen_random_uuid(),
    email text not null,
    reason text not null,
    source text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists bdr_suppressions_email_lower_uidx on bdr_suppressions ((lower(email)));

create table if not exists bdr_replies (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references bdr_contacts(id) on delete cascade,
    enrollment_id uuid references bdr_enrollments(id) on delete set null,
    sender_email text not null,
    recipient_email text not null,
    subject text not null default '',
    body_text text not null default '',
    provider_message_id text not null unique,
    received_at timestamptz not null,
    intent text not null,
    confidence double precision not null check (confidence between 0 and 1),
    summary text not null,
    action text not null,
    headers jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists bdr_opportunities (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references bdr_accounts(id) on delete cascade,
    contact_id uuid not null references bdr_contacts(id) on delete cascade,
    offer text not null,
    stage text not null check (stage in ('new','qualified','meeting_requested','meeting_booked','proposal','won','lost','nurture')),
    source_reply_id uuid references bdr_replies(id) on delete set null,
    summary text not null,
    next_action text not null,
    next_action_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists bdr_opportunities_stage_idx on bdr_opportunities(stage, next_action_at);

create table if not exists bdr_outcomes (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references bdr_accounts(id) on delete cascade,
    campaign_id uuid references bdr_campaigns(id) on delete set null,
    enrollment_id uuid references bdr_enrollments(id) on delete set null,
    outcome text not null,
    occurred_at timestamptz not null,
    value_cents bigint not null default 0,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists bdr_audit_events (
    id bigserial primary key,
    event_type text not null,
    entity_type text not null,
    entity_id uuid,
    payload jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null default now()
);
create index if not exists bdr_audit_events_entity_idx on bdr_audit_events(entity_type, entity_id, occurred_at desc);
