# n8n setup

n8n only schedules and sends emails. It never touches the database or the LLM,
and it never waits for people: every step that reads data, calls a model or
changes a run's state is an HTTP call to the API, and people act through pages
the API hosts (the Controller's approval page, owner reply forms), reached from
signed links in the emails. The API enforces the run's state machine, so a
workflow bug can't skip a step (e.g. send before approval): the call just
returns `409`.

```
collecting -> asking -> assembling -> awaiting_approval -> sent
                                 ^             |
                                 +- rejected <-+
any open state -> needs_attention -> asking | assembling
needs_attention -> sent | awaiting_approval   (only by resolving a stuck send claim)
```

## Before you start

- **API base URL.** Inside Docker Compose, n8n reaches the API at
  `http://api:8000`, set as `KPI_API_URL` on the n8n container. HTTP nodes use
  `{{ $env.KPI_API_URL }}/runs/start`. If your n8n version blocks `$env` in
  nodes, set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` or type the URL directly.
- **Every API call needs the API key.** Create one n8n *Header Auth*
  credential (name `X-API-Key`, value = `API_KEY` from `.env`) and select it
  in every HTTP Request node (*Authentication: Generic > Header Auth*). A
  missing or wrong key gets `401`, including `/health`. The API won't start
  without a key of at least 32 characters.
- **Every request carries `client_id`.** Runs are client-scoped (row-level
  security); a `run_id` with the wrong `client_id` returns `404`.
- **The API writes every email; n8n only sends them.** Endpoints return ready
  emails as `{to, subject, body}`; the approval and leadership emails also have
  `html`. In each Send Email node:
  - *To*: `{{ $json.<email>.to.join(', ') }}` (`to` is a list),
  - *Subject*: `subject`; *Text*: `body`,
  - for emails with `html` (approval, leadership): *Email Format* **Both** and
    *HTML*: `html`; owner emails are text only,
  - *Options > Append n8n Attribution*: **off**, so nothing is added to the
    API's text.
- **Email goes out through SMTP**: n8n's **Send Email** node, sending through
  Gmail with an app password (not the Gmail node or Gmail OAuth).
- **Turn off *Retry On Fail* on every Send Email node.** A retried send can't
  be detected by the API; the packet send is protected by `claim-send` instead.
- **Test mode for email.** The synthetic recipients use the reserved `.example`
  domain and can't receive mail. With `TEST_EMAIL_OVERRIDE=you@gmail.com` in
  `.env`, every email goes to a plus-alias of your inbox
  (`ceo@brightline.example` -> `you+ceo.brightline@gmail.com`) and starts with
  "Test mode: originally addressed to ...". The API refuses to start until you
  choose test mode or `EMAIL_MODE=live` explicitly.
- **Links in emails point at `PUBLIC_BASE_URL`** (`http://localhost:8000`), so
  open them on the machine running Docker. Each link works once and expires
  after `LINK_TTL_HOURS` (72). Opening a link only shows the page; nothing
  happens until the form is submitted, so email link scanners are harmless.
- **Error workflow.** In each of WF1-WF5: *Settings > Error workflow > WF6*.
- **Ports are bound to 127.0.0.1** (API 8000, n8n 5678, Postgres 5432).
- **API docs** (`/docs`) are off. For development, set `DOCS_ENABLED=true` and
  restart the API; the docs then open without the key, so development only.

## Credentials you'll need

| Credential (n8n type) | Used by | Setup |
|---|---|---|
| **Header Auth** (`X-API-Key`) | every HTTP Request node | Name `X-API-Key`, value `API_KEY` from `.env`. |
| **SMTP** (Gmail app password) | every Send Email node | On the Google account you send from: turn on 2-Step Verification, then create an app password (Google Account > Security > App passwords). In n8n: host `smtp.gmail.com`, port `465`, SSL/TLS on, user = that Gmail address, password = the 16-character app password. Use a test account, not your main one. |
| ClickUp API (Phase 6) | WF1, WF4 | Personal API token. Not needed yet. |
| QuickBooks Online OAuth2 (Phase 6) | WF1 | Intuit developer sandbox app. Not needed yet. |

No database, LLM or Azure credentials go into n8n; the API key is its only way in.

## Endpoints (API key required)

| Method and path | Body | Returns |
|---|---|---|
| `POST /runs/start` | `client_id, week_start` | `run_id, state, created, red_kpis[]`. Safe to call twice; never resets a run. |
| `GET /runs/{id}?client_id=` | | `state`, `status` (`"sending"` while a send is claimed), `packet_md`, `approved`, `unanswered_kpis[]`, recipients, and `packet_email` once approved (else `null`). Read-only: never creates links. |
| `GET /runs/ready-to-send?client_id=` | | `runs[]` whose current packet is approved and whose send isn't claimed: the send workflow's poll. |
| `POST /runs/{id}/observations` | `client_id` | `messages[]`, one per red KPI: `email {to, subject, body}` (with the owner's reply link), `source` (llm / llm_retry / template). |
| `POST /runs/{id}/reminders` | `client_id` | `reminder_emails[]` for red KPIs with no reply yet, each with a fresh reply link. |
| `POST /runs/{id}/packet` | `client_id` | `state`, `packet_md`, `faithfulness {passed, problems[]}`, `unanswered_kpis[]`, and `approval_email` (to the Controller, with the approval link) when the packet passed. |
| `POST /runs/{id}/approval-email` | `client_id` | A fresh `approval_email`, e.g. if the first was lost or expired. |
| `POST /runs/{id}/claim-send` | `client_id, idempotency_key` | `claimed, repeated, packet_email`. Call **before** emailing. Same key again returns the same claim; another key gets `409`, as does an unapproved or already-sent run. |
| `POST /runs/{id}/mark-sent` | `client_id, idempotency_key` | `state: sent, already_sent`. Needs the claim's key. An already-sent run returns `200` with `already_sent: true`, so a retry is safe. |
| `POST /runs/{id}/resolve-claim` | `client_id, resolution (sent / not_sent), resolved_by, note` | Settles a stuck claim; recorded with who and when. |
| `POST /runs/{id}/needs-attention` | `client_id, reason` | `state: needs_attention`, `alert_email` (to the Controller). |
| `POST /ingest/qbo-ar-aging` | `client_id, week_start, realm_id, report` | Loads a QuickBooks AgedReceivables report (sent raw, exactly as QuickBooks returned it) as that week's AR over 60 facts. `403` unless `realm_id` is the client's own QuickBooks company (only Sandbox Co has one); `422` if the report doesn't check out (nothing is written). |
| `POST /runs/{id}/responses` | `client_id, kpi_id, what_changed, why, owner_of_fix, actions` | Saves a reply. Owners normally use the reply form instead; this is for manual entry. |

`409` means "the run's state doesn't allow this step" (the `detail` says why);
`401` means the API key is missing or wrong.

## Pages people open (no API key; the signed link is the credential)

| Page | Opened from | What happens |
|---|---|---|
| `/approve?token=...` | The Controller's approval email | Shows the packet (the same HTML as the leadership email: KPI table with colored status, then each red KPI's observation and the owner's answers) with *Approve* / *Reject* and a notes box. The decision is recorded as the client's Controller (from the database), for exactly that packet version. A rebuilt packet makes older links stop working. |
| `/reply?token=...` | An owner's request or reminder email | The four questions: what changed, why, who owns the fix, what's being done. The answers go into the packet word for word. |

A used, expired, forged or outdated link shows an error page and changes
nothing. Approving by sending an address to the API is not possible.

## Workflow status

| Workflow | Status | Export |
|---|---|---|
| WF1 Collect & compute | **Built** | `n8n/workflows/wf1_collect_compute.json` |
| WF2 Ask the owners | **Built** | `n8n/workflows/wf2_ask_owners.json` |
| WF3 Reminder | Planned, not built | |
| WF4 Build packet & request approval | **Built** (manual trigger) | `n8n/workflows/wf4_build_packet.json` |
| WF4b Send approved packets | **Built** (manual trigger; poll planned) | `n8n/workflows/wf4b_send_approved.json` |
| WF5 Remember | Planned, not built (needs `/runs/{id}/remember`) | |
| WF6 Error handler | Planned, not built | |
| QBO AR pull (Sandbox Co) | Planned, next | |

Until WF6 exists, no workflow has an error workflow set; failures show in n8n's
execution list. The HTTP nodes use `http://api:8000` directly (the API's name on
the Compose network).

## WF1 Collect & compute (built)

| # | Node | Calls / does |
|---|---|---|
| 1 | Schedule Trigger + Manual Trigger | Mondays 06:00 (`GENERIC_TIMEZONE`), or by hand from the editor |
| 2 | Code: *Clients for week* | One item per client with a fixed `WEEK_START` (`2026-08-03`): the synthetic data has fixed weeks; change it to demo another week |
| 3 | HTTP Request: *Start run* | `POST /runs/start {client_id, week_start}` |
| 4 | IF: *Any red KPIs?* | `{{ $json.red_kpis.length }}` > 0 |
| 5 | Execute Workflow | WF2 with `{client_id, run_id}` (true branch) |

Pulling QuickBooks and creating the ClickUp task come in Phase 6; until then
the data is the seeded synthetic set.

## WF2 Ask the owners (built, sub-workflow)

| # | Node | Calls / does |
|---|---|---|
| 1 | Execute Workflow Trigger | Input: `client_id, run_id` |
| 2 | HTTP Request: *Get owner messages* | `POST /runs/{run_id}/observations {client_id}` |
| 3 | Split Out | `messages` (one item per red KPI) |
| 4 | Send Email (SMTP) | Text: *To* `{{ $json.email.to.join(', ') }}`, *Subject* / *Text* from `email`; the body carries the owner's reply link. Retry On Fail off, attribution off. |

Owners answer on the reply form; the API saves the reply. No workflow waits.

## WF3 Reminder (planned, not built)

Tuesdays 09:00: `POST /runs/start` (returns the existing run), then
`POST /runs/{run_id}/reminders {client_id}`, Split Out `reminder_emails`, and
send each (text) with its fresh reply link.

## WF4 Build packet & request approval (built, manual trigger)

| # | Node | Calls / does |
|---|---|---|
| 1 | Manual Trigger | Run from the editor (a Tuesday 12:00 schedule is planned) |
| 2 | Code: *Clients for week* | As in WF1 |
| 3 | HTTP Request: *Start run* | `POST /runs/start` (to get `run_id`) |
| 4 | HTTP Request: *Build packet* | `POST /runs/{run_id}/packet {client_id}`: builds the packet and returns `approval_email`. **Note:** the current export calls `/approval-email` here, which only works when the packet already exists (`409` on a fresh run); change the URL to `/packet`. Use `/approval-email` only to resend. |
| 5 | IF: *Packet Passed?* | `{{ $json.approval_email }}` exists (the packet passed the faithfulness check). Otherwise the run is `needs_attention`: stop. |
| 6 | Send Email (SMTP) | **Both**: *To* `{{ $json.approval_email.to.join(', ') }}`, *Subject*, *Text* (`body`), *HTML* (`html`). Attribution off. |

The Controller approves or rejects on the approval page. On *reject*, fix what
the notes say, then run WF4 again: rebuilding bumps the packet version and
sends a new approval link.

## WF4b Send approved packets (built, manual trigger)

| # | Node | Calls / does |
|---|---|---|
| 1 | Manual Trigger | Run from the editor right after approving (handy in a demo). The planned Schedule Trigger polls every `SEND_POLL_INTERVAL_MINUTES` (`.env`; `1` in development): use the expression `{{ $env.SEND_POLL_INTERVAL_MINUTES }}`, or type the number if your n8n version doesn't accept it. |
| 2 | Code: *Clients* | One item per client |
| 3 | HTTP Request: *Ready to send* | `GET /runs/ready-to-send?client_id=` |
| 4 | Split Out | `runs` |
| 5 | HTTP Request: *Claim Send* | `POST /runs/{run_id}/claim-send {client_id, idempotency_key: "wf4b-exec-{{ $execution.id }}"}`. The prefix is needed because the API requires 8+ characters and a bare execution id is shorter; it also says where the claim came from. On `409`, stop: another execution owns the send, or it already went out. |
| 6 | Send Email (SMTP): *Email leadership* | **Both**: *To* `{{ $json.packet_email.to.join(', ') }}`, *Subject*, *Text* (`body`), *HTML* (`html`). **Retry On Fail off**, attribution off. |
| 7 | HTTP Request: *Mark Sent* | `POST /runs/{run_id}/mark-sent {client_id, idempotency_key: "wf4b-exec-{{ $execution.id }}"}` (the same key as node 5), only after node 6 succeeds. Retry is safe (`already_sent: true`). |

The key must be identical in nodes 5 and 7, character for character: the API
compares it exactly, so a stray space in one of them makes `mark-sent` return
`409`. (The current export has two leading spaces in both, so they match; it's
safer to remove them from both.)

Why the claim: if this runs twice for the same run (an overlapping poll, a
retry, someone clicking *Retry*), the second execution has a different id, so
`claim-send` refuses it and leadership gets the packet once. If an execution
stops after claiming but before `mark-sent`, nothing re-sends it automatically:
a person settles it with `resolve-claim` (see WF6).

## QBO AR pull (planned, next)

One live pull from the QuickBooks sandbox into the third client, **Sandbox Co**
(`sandbox_co`); Brightline and Northgate stay on synthetic data.

1. Put the sandbox Company ID in `.env` as `QBO_REALM_ID=...`, then
   `python scripts/seed_db.py` (stores it on Sandbox Co) and
   `docker compose up -d n8n` (n8n reads it as `$env.QBO_REALM_ID`).
2. **Manual Trigger.**
3. **Code: *Week***: `week_start` (this Monday) and `report_date` (today, or this
   Sunday): the report's date must fall inside the week.
4. **HTTP Request: *Get AR aging***: GET
   `https://sandbox-quickbooks.api.intuit.com/v3/company/{{ $env.QBO_REALM_ID }}/reports/AgedReceivables`,
   *Authentication*: Predefined Credential Type, QuickBooks Online OAuth2 API,
   **QuickBooks Sandbox**; query `report_date`, `minorversion=75`; header
   `Accept: application/json`. (Keep the default aging settings: the API expects
   the "61 - 90" and "91 and over" columns.)
5. **HTTP Request: *Ingest***: POST `http://api:8000/ingest/qbo-ar-aging` with
   the Header Auth credential; body (JSON, expression):
   `{{ JSON.stringify({ client_id: "sandbox_co", week_start: $('Week').item.json.week_start, realm_id: $env.QBO_REALM_ID, report: $json }) }}`
6. **HTTP Request: *Start run***: POST `/runs/start {client_id: "sandbox_co", week_start}`.
7. Check the ingest response's `totals` against *Reports > A/R Aging Summary* in
   the sandbox for the same date.

## WF5 Remember (planned, not built)

After a run is sent: `POST /runs/{run_id}/remember {client_id}` (the endpoint
isn't built yet) will embed the run's owner replies into `doc_chunks` as
`past_explanation`s, so next year's retrieval finds them.

## WF6 Error handler (planned, not built)

| # | Node | Calls / does |
|---|---|---|
| 1 | Error Trigger | Fires when any workflow that names WF6 as its error workflow fails |
| 2 | Code: find the run | Read `client_id` and `run_id` from the failed execution's data; if there's no run yet, skip node 3 |
| 3 | HTTP Request | `POST /runs/{run_id}/needs-attention {client_id, reason: "<workflow>: <node>: <error message>"}` (`409` means the run was already sent, which is fine) |
| 4 | Send Email (SMTP) | The `alert_email` from node 3. If the run had a claimed send, it explains how to settle it with `resolve-claim`. |

To resume a `needs_attention` run, re-run the step that failed: `packet`,
`observations` and `reminders` all work from `needs_attention`.

**A stuck send** (claimed, never marked sent) is settled by a person, not
retried: check the leadership inbox, then call
`POST /runs/{id}/resolve-claim {client_id, resolution, resolved_by, note}` with
`"sent"` (it went out: the run becomes sent) or `"not_sent"` (it didn't: the
claim is released and the next send run sends it). Who resolved it, and when,
is recorded. While a claim is open, `GET /runs/{id}` shows `status: "sending"`.

## Export

Build each workflow in the editor, then *Download* it as JSON into
`n8n/workflows/` (`wf1_collect_compute.json`, ...; n8n adds `.json`, so don't type it twice). Before committing, check the
JSON for hardcoded secrets (credentials are referenced by ID, never inlined),
missing error-workflow settings, and any request without `client_id`.
