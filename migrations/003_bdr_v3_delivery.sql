create or replace function reserve_bdr_message(p_touch_id uuid, p_idempotency_key text)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_message_id uuid;
    v_mailbox_id uuid;
begin
    select e.mailbox_id into v_mailbox_id
    from bdr_touches t
    join bdr_enrollments e on e.id = t.enrollment_id
    where t.id = p_touch_id and t.status = 'claimed'
    for update of t;

    if v_mailbox_id is null then
        return null;
    end if;

    insert into bdr_messages(touch_id, mailbox_id, idempotency_key, status)
    values (p_touch_id, v_mailbox_id, p_idempotency_key, 'reserved')
    on conflict (idempotency_key) do nothing
    returning id into v_message_id;

    if v_message_id is null then
        return null;
    end if;

    update bdr_touches set status='reserved', updated_at=now() where id=p_touch_id;
    return v_message_id;
end;
$$;

create or replace function complete_bdr_message(
    p_message_id uuid,
    p_provider_message_id text,
    p_accepted_at timestamptz,
    p_provider_payload jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
    v_touch_id uuid;
    v_enrollment_id uuid;
begin
    update bdr_messages
    set status='sent', provider_message_id=p_provider_message_id,
        accepted_at=p_accepted_at, provider_payload=p_provider_payload, updated_at=now()
    where id=p_message_id and status='reserved'
    returning touch_id into v_touch_id;

    if v_touch_id is null then
        raise exception 'message is missing or not reserved';
    end if;

    update bdr_touches
    set status='sent', updated_at=now()
    where id=v_touch_id
    returning enrollment_id into v_enrollment_id;

    if not exists (
        select 1 from bdr_touches
        where enrollment_id=v_enrollment_id
          and status in ('scheduled','claimed','reserved')
    ) then
        update bdr_enrollments set status='completed',updated_at=now() where id=v_enrollment_id;
    end if;
end;
$$;

create or replace function fail_bdr_message(
    p_touch_id uuid,
    p_message_id uuid,
    p_reason text,
    p_uncertain boolean default false
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_message_id is not null then
        update bdr_messages
        set status=case when p_uncertain then 'uncertain' else 'failed' end,
            error=left(coalesce(p_reason,''),2000),updated_at=now()
        where id=p_message_id;
    end if;

    update bdr_touches
    set status=case when p_uncertain then 'uncertain' else 'failed' end,
        last_error=left(coalesce(p_reason,''),2000),updated_at=now()
    where id=p_touch_id;
end;
$$;

create or replace function stop_bdr_enrollment(p_enrollment_id uuid, p_reason text)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    update bdr_enrollments
    set status='stopped',stop_reason=left(coalesce(p_reason,''),1000),updated_at=now()
    where id=p_enrollment_id;

    update bdr_touches
    set status='cancelled',last_error='enrollment stopped',updated_at=now()
    where enrollment_id=p_enrollment_id and status in ('scheduled','claimed');
end;
$$;

create or replace view bdr_campaign_performance as
select
    c.id as campaign_id,
    c.name,
    count(distinct e.id) as enrollments,
    count(distinct m.id) filter (where m.status='sent') as messages_sent,
    count(distinct r.id) as replies,
    count(distinct o.id) filter (where o.stage in ('qualified','meeting_requested','meeting_booked','proposal','won')) as qualified_opportunities,
    count(distinct o.id) filter (where o.stage='won') as wins,
    coalesce(sum(outcome.value_cents) filter (where outcome.outcome='won'),0) as won_value_cents
from bdr_campaigns c
left join bdr_enrollments e on e.campaign_id=c.id
left join bdr_touches t on t.enrollment_id=e.id
left join bdr_messages m on m.touch_id=t.id
left join bdr_replies r on r.enrollment_id=e.id
left join bdr_opportunities o on o.account_id=e.account_id
left join bdr_outcomes outcome on outcome.campaign_id=c.id
group by c.id,c.name;

-- Internal-only data: block anonymous/authenticated Supabase clients unless an
-- explicit policy is added later. The service role bypasses RLS.
alter table bdr_accounts enable row level security;
alter table bdr_contacts enable row level security;
alter table bdr_evidence enable row level security;
alter table bdr_mailboxes enable row level security;
alter table bdr_campaigns enable row level security;
alter table bdr_sequence_steps enable row level security;
alter table bdr_enrollments enable row level security;
alter table bdr_touches enable row level security;
alter table bdr_messages enable row level security;
alter table bdr_suppressions enable row level security;
alter table bdr_replies enable row level security;
alter table bdr_opportunities enable row level security;
alter table bdr_outcomes enable row level security;
alter table bdr_audit_events enable row level security;

-- SECURITY DEFINER functions are not callable by public client roles.
revoke all on function claim_bdr_touches(text, integer, timestamptz) from public;
revoke all on function get_bdr_dispatch_context(uuid) from public;
revoke all on function reserve_bdr_message(uuid, text) from public;
revoke all on function complete_bdr_message(uuid, text, timestamptz, jsonb) from public;
revoke all on function fail_bdr_message(uuid, uuid, text, boolean) from public;
revoke all on function stop_bdr_enrollment(uuid, text) from public;

revoke all on function claim_bdr_touches(text, integer, timestamptz) from anon, authenticated;
revoke all on function get_bdr_dispatch_context(uuid) from anon, authenticated;
revoke all on function reserve_bdr_message(uuid, text) from anon, authenticated;
revoke all on function complete_bdr_message(uuid, text, timestamptz, jsonb) from anon, authenticated;
revoke all on function fail_bdr_message(uuid, uuid, text, boolean) from anon, authenticated;
revoke all on function stop_bdr_enrollment(uuid, text) from anon, authenticated;

grant execute on function claim_bdr_touches(text, integer, timestamptz) to service_role;
grant execute on function get_bdr_dispatch_context(uuid) to service_role;
grant execute on function reserve_bdr_message(uuid, text) to service_role;
grant execute on function complete_bdr_message(uuid, text, timestamptz, jsonb) to service_role;
grant execute on function fail_bdr_message(uuid, uuid, text, boolean) to service_role;
grant execute on function stop_bdr_enrollment(uuid, text) to service_role;
