# Pacific Yew BDR v3

## Purpose

BDR v3 implements a governed business-development employee rather than a bulk-email script:

```text
Find account
→ verify company and contact
→ detect a relevant business problem or buying signal
→ score fit, timing, authority, evidence, and risk
→ select the correct Pacific Yew offer
→ retain source evidence for every material claim
→ create a controlled sequence
→ require human approval
→ send through one guarded delivery service
→ classify replies
→ suppress, pause, draft, escalate, or create an opportunity
→ offer the booking path when explicitly safe
→ record commercial outcomes for campaign learning
```

The legacy Google Sheets worker is not removed by this pull request. V3 is deliberately isolated behind `BDR_V3_ENABLED=true` and a manual GitHub workflow until its Supabase migration, credentials, and shadow-run results are reviewed.

## Architecture

### Pure policy layer

- `bdr_v3/models.py`: typed contracts and state enums.
- `bdr_v3/policies.py`: normalization, prompt-injection neutralization, deterministic scoring, package routing, sequence definitions, business-time scheduling, and reply classification.

### Transactional operating layer

- `bdr_v3/repository.py`: repository protocol. `memory_repository.py` provides deterministic test storage; the `postgres_*` modules provide the transactional Supabase implementation.
- `migrations/001_bdr_v3_schema.sql`, `002_bdr_v3_claims.sql`, and `003_bdr_v3_delivery.sql`: accounts, contacts, evidence, mailboxes, campaigns, sequence steps, enrollments, touches, messages, suppressions, replies, opportunities, outcomes, audit events, and transactional functions.
- `bdr_v3/delivery.py`: the only v3 route to a mail provider. It rechecks approval, consent evidence, suppression, business-email status, score, risk, mailbox state, daily quota, campaign conflict, and idempotency.
- `bdr_v3/replies.py`: pauses sequences on human replies, processes unsubscribes and bounces, creates opportunities, and restricts automatic replies to explicit policy flags.

### Adapters and orchestration

- `bdr_v3/adapters.py`: legacy discovery/scraping integration, HTTP verification, deterministic research, optional evidence-constrained OpenRouter research, Zoho delivery, reply sending, and IMAP ingestion.
- `bdr_v3/orchestrator.py`: coordinates the complete account-to-outcome loop.
- `v3_agent.py`: guarded CLI.
- `app_v3.py`: approval and opportunity console. It has no direct-send button.

## Critical invariants

1. **No v3 interface sends directly.** `GuardedDeliveryService` is the only sequence-delivery entry point.
2. **Unapproved touches remain scheduled.** They are not claimed or marked failed.
3. **Temporary capacity failures defer.** Disabled mailboxes and exhausted daily quotas release touches for a later run.
4. **Provider uncertainty is terminal pending reconciliation.** A timeout after possible acceptance becomes `uncertain`; the worker does not retry automatically.
5. **Every touch has a durable idempotency key.** A second reservation cannot create another message.
6. **Every human reply pauses the sequence before classification actions occur.**
7. **Unsubscribe and hard-bounce events persist to suppression.**
8. **Positive and meeting-request replies create opportunities.**
9. **Referral addresses are not automatically contacted.** They require a new consent and relevance review.
10. **Scraped website text is untrusted input.** Instruction-like content is neutralized, delimited, and prevented from controlling delivery tools.
11. **Supabase client roles cannot invoke the SECURITY DEFINER worker functions.** Execute permission is reserved for `service_role`.

## Package routing

The deterministic router produces one internal recommendation:

- `AI Team Enablement`
- `Workflow Automation`
- `Intake & Routing System`
- `Connected Operations`
- `Outbound Pipeline System`

The package is an internal operating decision. Outreach copy should describe the business problem and smallest useful finish line rather than forcing package terminology into every email.

## Initial rollout

### 1. Apply the migration

Run `001_bdr_v3_schema.sql`, then `002_bdr_v3_claims.sql`, then `003_bdr_v3_delivery.sql` in Supabase with a privileged migration role.

Do not expose the tables or functions to anonymous or authenticated client roles. The migration enables RLS and revokes execution of the transactional worker functions from public client roles.

### 2. Install v3 dependencies

```bash
pip install -r requirements-v3.txt
```

### 3. Bootstrap runtime records

Set:

```text
BDR_V3_ENABLED=true
DATABASE_URL=<Supabase direct PostgreSQL connection string>
```

Then run:

```bash
python v3_agent.py bootstrap \
  --mailbox-email contact@pacificyew.pro \
  --campaign-name "Pacific Yew Local SMB Outreach" \
  --daily-limit 8
```

The command prints:

```text
BDR_V3_MAILBOX_ID=<uuid>
BDR_V3_CAMPAIGN_ID=<uuid>
```

The mailbox is disabled by default. That is intentional.

### 4. Configure environment or GitHub secrets

Required:

```text
BDR_V3_ENABLED=true
DATABASE_URL=
BDR_V3_MAILBOX_ID=
BDR_V3_CAMPAIGN_ID=
GMAIL_USER=contact@pacificyew.pro
GMAIL_APP_PASSWORD=
SENDER_NAME=Pacific Yew Automations
SENDER_INDIVIDUAL=Michael
SENDER_ADDRESS=
SENDER_WEBSITE=https://pacificyew.pro
OPENROUTER_API_KEY=
APIFY_TOKEN=
APIFY_ACTOR=
BDR_BOOKING_URL=https://calendly.com/contact-pacificyew/30min
```

Keep these false during initial rollout:

```text
BDR_AUTO_CONFIRM_UNSUBSCRIBE=false
BDR_AUTO_SEND_BOOKING_LINK=false
```

### 5. Shadow discovery

With the mailbox still disabled:

```bash
python v3_agent.py discover "physiotherapy clinic Vancouver BC"
python v3_agent.py pending-approvals
```

Review:

- account identity and website;
- contact publication URL;
- no-contact detection;
- fit, timing, authority, evidence, and risk scores;
- package route;
- every evidence-supported observation;
- all four sequence messages.

### 6. Use the v3 command center

```bash
streamlit run app_v3.py
```

The command center can approve touches and inspect opportunities. It cannot call SMTP. Do not use the legacy Streamlit `Send Now` path after v3 becomes the operating system.

### 7. Enable one mailbox only after shadow review

```sql
update bdr_mailboxes
set enabled=true, health_status='healthy', updated_at=now()
where id='<BDR_V3_MAILBOX_ID>';

update bdr_campaigns
set status='active', updated_at=now()
where id='<BDR_V3_CAMPAIGN_ID>';
```

Start with one approved touch. Reconcile the provider inbox and database message record before increasing volume.

### 8. Dispatch and process replies

```bash
python v3_agent.py dispatch --worker-id local-review-1
python v3_agent.py replies --limit 50
```

The GitHub `Pacific Yew BDR v3 Manual Worker` workflow provides the same governed operations. It has no schedule in this version.

## Human approval workflow

List pending touches:

```bash
python v3_agent.py pending-approvals --limit 100
```

Approve one after reviewing its evidence and copy:

```bash
python v3_agent.py approve-touch <touch-uuid> --approved-by Michael
```

The dispatcher only claims approved steps when `requires_approval=true`.

## Reply autonomy

Default behavior:

- Unsubscribe: suppress and stop; confirmation remains manual.
- Bounce: suppress and stop.
- Not interested: suppress and stop.
- Positive interest: pause and create a qualified opportunity.
- Meeting request: pause and create a meeting-requested opportunity.
- Pricing, objections, questions, referrals, and ambiguity: draft or escalate for review.
- Out of office: pause.

Automatic unsubscribe confirmations or Calendly replies require explicit environment flags. Pricing, scope, legal, compliance, architecture, and negotiation replies are never auto-sent by the deterministic policy.

## Outcome learning

`BusinessDevelopmentEmployee.learn_from_outcome()` writes immutable outcome events. `bdr_campaign_performance` summarizes enrollments, sent messages, replies, opportunities, wins, and won value. This creates an auditable feedback loop without allowing the model to rewrite its own policy or compliance thresholds.

## Validation

Local deterministic validation for this implementation:

```bash
python -m compileall -q bdr_v3 v3_agent.py app_v3.py
python -m unittest discover -s tests -p 'test_bdr_v3*.py' -v
```

GitHub runs the same checks through `BDR v3 Quality Gates`.
