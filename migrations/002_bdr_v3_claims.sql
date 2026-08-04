create or replace function claim_bdr_touches(
    p_worker_id text,
    p_limit integer,
    p_now timestamptz default now()
)
returns table (
    touch_id uuid,
    enrollment_id uuid,
    campaign_id uuid,
    account_id uuid,
    contact_id uuid,
    mailbox_id uuid,
    sequence_step_id uuid,
    step_position integer,
    subject text,
    body text,
    requires_approval boolean,
    approved_at timestamptz,
    idempotency_key text
)
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_limit < 1 or p_limit > 100 then
        raise exception 'p_limit must be between 1 and 100';
    end if;

    return query
    with candidates as (
        select t.id
        from bdr_touches t
        join bdr_enrollments e on e.id = t.enrollment_id
        join bdr_sequence_steps s on s.id = t.sequence_step_id
        where t.status = 'scheduled'
          and t.scheduled_for <= p_now
          and e.status = 'active'
          and (not s.requires_approval or t.approved_at is not null)
        order by t.scheduled_for, t.created_at
        for update of t skip locked
        limit p_limit
    ), claimed as (
        update bdr_touches t
        set status = 'claimed', claimed_at = p_now, claimed_by = p_worker_id, updated_at = now()
        from candidates c
        where t.id = c.id
        returning t.*
    )
    select
        c.id,
        c.enrollment_id,
        e.campaign_id,
        e.account_id,
        e.contact_id,
        e.mailbox_id,
        c.sequence_step_id,
        s.position,
        c.subject,
        c.body,
        s.requires_approval,
        c.approved_at,
        c.idempotency_key
    from claimed c
    join bdr_enrollments e on e.id = c.enrollment_id
    join bdr_sequence_steps s on s.id = c.sequence_step_id
    order by c.scheduled_for, s.position;
end;
$$;

create or replace function get_bdr_dispatch_context(p_touch_id uuid)
returns table (
    touch_id uuid,
    enrollment_id uuid,
    campaign_id uuid,
    account_id uuid,
    contact_id uuid,
    mailbox_id uuid,
    sequence_step_id uuid,
    step_position integer,
    subject text,
    body text,
    requires_approval boolean,
    approved_at timestamptz,
    idempotency_key text,
    account_name text,
    account_domain text,
    contact_email text,
    consent_type text,
    consent_source_url text,
    no_contact_statement boolean,
    verified_business_email boolean,
    fit_score integer,
    timing_score integer,
    authority_score integer,
    evidence_score integer,
    risk_score integer,
    total_score integer,
    eligible boolean,
    score_reasons jsonb,
    suppressed boolean,
    mailbox_enabled boolean,
    mailbox_daily_limit integer,
    mailbox_sent_today bigint,
    conflicting_active_enrollment boolean
)
language sql
stable
security definer
set search_path = public
as $$
    select
        t.id,
        t.enrollment_id,
        e.campaign_id,
        e.account_id,
        e.contact_id,
        e.mailbox_id,
        t.sequence_step_id,
        s.position,
        t.subject,
        t.body,
        s.requires_approval,
        t.approved_at,
        t.idempotency_key,
        a.name,
        a.domain,
        c.email,
        c.consent_type,
        c.consent_source_url,
        c.no_contact_statement,
        c.verified_business_email,
        a.fit_score,
        a.timing_score,
        a.authority_score,
        a.evidence_score,
        a.risk_score,
        a.total_score,
        a.eligible,
        a.score_reasons,
        exists(select 1 from bdr_suppressions x where lower(x.email) = lower(c.email)),
        m.enabled and m.health_status not in ('blocked','failed'),
        m.daily_limit,
        (
            select count(*)
            from bdr_messages sent
            where sent.mailbox_id = m.id
              and sent.status = 'sent'
              and sent.accepted_at >= date_trunc('day', now() at time zone 'UTC') at time zone 'UTC'
        ),
        exists(
            select 1
            from bdr_enrollments other
            where other.contact_id = e.contact_id
              and other.id <> e.id
              and other.status = 'active'
        )
    from bdr_touches t
    join bdr_enrollments e on e.id = t.enrollment_id
    join bdr_sequence_steps s on s.id = t.sequence_step_id
    join bdr_accounts a on a.id = e.account_id
    join bdr_contacts c on c.id = e.contact_id
    join bdr_mailboxes m on m.id = e.mailbox_id
    where t.id = p_touch_id;
$$;
