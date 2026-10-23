# n8n setup

n8n only schedules, sends email, waits for people and routes. It never touches
the database or the LLM: every step that reads data, calls a model or changes a
run's state is an HTTP call to the API. The API enforces the run's state
machine, so a workflow bug can't skip a step (e.g. send before approval): the
call just returns `409`.

```
collecting -> asking -> assembling -> awaiting_approval -> sent
                                 ^             |
                                 +- rejected <-+
any open state -> needs_attention -> asking | assembling
```

## Before you start

- **API base URL.** Inside Docker Compose, n8n reaches the API at
  `http://api:8000`, set as `KPI_API_URL` on the n8n container. HTTP nodes use
  `{{ $env.KPI_API_URL }}/runs/start`. If your n8n version blocks `$env` in
  nodes, set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` or type the URL directly.
- **Every request carries `client_id`.** Runs are client-scoped (row-level
  security); a `run_id` with the wrong `client_id` returns `404`.
- **The API writes every email; n8n only sends them.** Endpoints return ready
  emails as `{to, subject, body}`; Send Email nodes map those three fields and add
  nothing of their own.
- **Test mode for email.** The synthetic recipients use the reserved `.example`
  domain and can't receive mail. Set `TEST_EMAIL_OVERRIDE=you@gmail.com` in
  `.env` and restart the API: every email then goes to a plus-alias of your inbox
  (`ceo@brightline.example` -> `you+ceo.brightline@gmail.com`) and starts with
  "Test mode: originally addressed to ...". Every recipient field the API
  returns is rewritten too, so no workflow can reach a real address by mistake.
- **Email goes out through SMTP.** Every email step is n8n's **Send Email**
  node (SMTP), sending through Gmail with an app password, not the Gmail node
  or Gmail OAuth. Map *To*, *Subject* and *Text* from the API's email object.
- **Turn off *Retry On Fail* on every Send Email node.** A retried send can't
  be detected by the API; the packet send is protected by `claim-send` instead.
- **Error workflow.** In each of WF1-WF5: *Settings > Error workflow > WF6*.
- **Every API call needs the API key.** Create one n8n *Header Auth*
  credential (name `X-API-Key`, value = `API_KEY` from `.env`) and select it
  in every HTTP Request node (*Authentication: Generic > Header Auth*). A
  missing or wrong key gets `401`. That includes `/health`: any health check
  or ping sends the key too. The API won't start without a key of at least 32
  characters.
- **Ports are bound to 127.0.0.1** (API 8000, n8n 5678, Postgres 5432), so
  nothing is reachable from your network. n8n reaches the API over the Compose
  network at `http://api:8000`.
- **API docs** (`/docs`) are off. For development, set `DOCS_ENABLED=true` in
  `.env` and restart the API; the docs pages then open without the key, so
  never enable them anywhere else.

## Credentials you'll need

| Credential (n8n type) | Used by | Setup |
|---|---|---|
| **Header Auth** (`X-API-Key`) | every HTTP Request node | Name `X-API-Key`, value `API_KEY` from `.env`. |
| **SMTP** (Gmail app password) | every Send Email node | On the Google account you send from: turn on 2-Step Verification, then create an app password (Google Account > Security > App passwords). In n8n: host `smtp.gmail.com`, port `465`, SSL/TLS on, user = that Gmail address, password = the 16-character app password. Use a test account, not your main one. |
| ClickUp API (Phase 6) | WF1, WF4 | Personal API token. Not needed yet. |
| QuickBooks Online OAuth2 (Phase 6) | WF1 | Intuit developer sandbox app. Not needed yet. |

No database, LLM or Azure credentials go into n8n; the API key is its only way in.

## Endpoints

| Method and path | Body | Returns |
|---|---|---|
| `POST /runs/start` | `client_id, week_start` | `run_id, state, created, red_kpis[]` (each with `owner_name`, `owner_email`). Safe to call twice; never resets a run. |
| `GET /runs/{id}?client_id=` | | `state, status ("sending" while a send is claimed), packet_md, approved, controller_email, leadership_emails, red_kpis[] (answered), unanswered_kpis[]`, plus emails: `packet_email` (to leadership; only once approved, else `null`) and `reminder_emails[]` (one per unanswered KPI) |
| `POST /runs/{id}/observations` | `client_id` | `messages[]`: `kpi_id, owner_name, owner_email, message, email {to, subject, body}, source (llm / llm_retry / template)` |
| `POST /runs/{id}/responses` | `client_id, kpi_id, what_changed, why, owner_of_fix, actions` | `response_id` |
| `POST /runs/{id}/packet` | `client_id` | `state, packet_md, faithfulness {passed, problems[]}, unanswered_kpis[]` |
| `POST /runs/{id}/approval` | `client_id, approver, decision (approved / rejected), edits` | `approval_id, state`. `approver` must be the client's Controller (`403` otherwise). |
| `POST /runs/{id}/claim-send` | `client_id, idempotency_key` | `claimed, repeated, packet_email {to, subject, body}`. Call **before** emailing. Same key again returns the same claim; another key gets `409`. `409` too unless the current packet version is approved, or if it was already sent. |
| `POST /runs/{id}/mark-sent` | `client_id, idempotency_key` | `state: sent, already_sent`. Needs the claim's key (`409` otherwise). On an already-sent run: `200` with `already_sent: true`, so a retry is safe. |
| `POST /runs/{id}/resolve-claim` | `client_id, resolution (sent / not_sent), resolved_by, note` | `state`. Settles a stuck claim; recorded with who and when. |
| `POST /runs/{id}/needs-attention` | `client_id, reason` | `state: needs_attention`, `alert_email` (to the Controller) |

`409` always means "the run's state doesn't allow this step"; the `detail` says why.
`401` means the `X-API-Key` header is missing or wrong.

## WF1 Collect & compute (Mon 06:00)

| # | Node | Calls / does |
|---|---|---|
| 1 | Schedule Trigger | Mondays 06:00 (`GENERIC_TIMEZONE`) |
| 2 | Set: clients | `[{client_id: "brightline"}, {client_id: "northgate"}]` (one item each) |
| 3 | Code: week_start | This Monday as `YYYY-MM-DD` (for the synthetic data, a fixed week such as `2026-08-03`) |
| 4 | HTTP Request | `POST /runs/start {client_id, week_start}` |
| 5 | IF | `red_kpis` is not empty |
| 6 | Execute Workflow | WF2 with `{client_id, run_id}` (true branch) |

Pulling QuickBooks and creating the ClickUp task come in Phase 6; until then
the data is the seeded synthetic set.

## WF2 Investigate & ask (sub-workflow)

| # | Node | Calls / does |
|---|---|---|
| 1 | Execute Workflow Trigger | Input: `client_id, run_id` |
| 2 | HTTP Request | `POST /runs/{run_id}/observations {client_id}` |
| 3 | Split Out | `messages` (one item per red KPI) |
| 4 | Send Email (SMTP) | *Interim until the API-hosted reply form (next step) replaces send-and-wait.* To `owner_email`, subject "Week of ...: {kpi_name} needs your input", body `message`. Response type **Custom form** with four required text fields: *What changed*, *Why*, *Who owns the fix*, *What's being done*. Limit the wait to Tuesday 12:00 so it ends before the packet is built. |
| 5 | HTTP Request | `POST /runs/{run_id}/responses` with `client_id`, `kpi_id` and the four form fields (`what_changed`, `why`, `owner_of_fix`, `actions`) |

A reply that arrives after the packet is built gets `409` (the run has moved
on); the packet already marks that KPI as unanswered.

## WF3 Reminder (Tue 09:00)

| # | Node | Calls / does |
|---|---|---|
| 1 | Schedule Trigger | Tuesdays 09:00 |
| 2 | Set: clients + Code: week_start | As in WF1 |
| 3 | HTTP Request | `POST /runs/start` (returns the existing run unchanged) |
| 4 | HTTP Request | `GET /runs/{run_id}?client_id=` |
| 5 | Split Out | `unanswered_kpis` |
| 6 | Send Email (SMTP) | To `owner_email`: "Reminder: please answer yesterday's email about {name} by 12:00." The form link stays in the original email (WF2 is still waiting on it). |

## WF4 Assemble & approve (Tue 12:00)

| # | Node | Calls / does |
|---|---|---|
| 1 | Schedule Trigger | Tuesdays 12:00 |
| 2 | Set: clients + Code: week_start | As in WF1 |
| 3 | HTTP Request | `POST /runs/start` (to get `run_id`) |
| 4 | HTTP Request | `POST /runs/{run_id}/packet {client_id}` |
| 5 | IF | `state` is `awaiting_approval` (the packet passed the faithfulness check). Otherwise the run is already `needs_attention`: stop and let WF6's alert cover it. |
| 6 | HTTP Request | `GET /runs/{run_id}?client_id=` (for `controller_email`, `leadership_emails`) |
| 7 | Send Email (SMTP) | *Interim until the API-hosted approval page (next step) replaces send-and-wait.* To `controller_email`, body `packet_md`. Response type **Approval** (Approve / Disapprove), plus a free-text field for notes if you want them. |
| 8 | HTTP Request | `POST /runs/{run_id}/approval {client_id, approver: controller_email, decision: "approved" or "rejected", edits: notes}` |
| 9 | IF | `decision` is `approved` |
| 10 | HTTP Request | `POST /runs/{run_id}/claim-send {client_id, idempotency_key: {{ $execution.id }}}` (true branch). On `409`, stop: another execution owns the send, or it already went out. |
| 11 | Send Email (SMTP) | *To*, *Subject*, *Text* from node 10's `packet_email`. **Retry On Fail must be off**: a retried send is the one thing the claim can't detect. |
| 12 | HTTP Request | `POST /runs/{run_id}/mark-sent {client_id, idempotency_key: {{ $execution.id }}}`, only after node 11 succeeds. Retry is safe (`already_sent: true`). |
| 13 | Execute Workflow | WF5 with `{client_id, run_id}` |

Why the claim: if n8n re-runs WF4 (a retry, or someone clicks *Retry*), the new
execution has a different id, so `claim-send` refuses it and leadership gets
the packet once. If an execution stops after claiming but before `mark-sent`,
the run stays claimed and nothing re-sends it automatically: WF6 flags it, and
a person checks the leadership inbox. The design prefers at most once over at
least once.

On *rejected* the run is `rejected`: fix whatever the Controller's notes say
(e.g. ask an owner for a better reply), then run nodes 4-13 again. Rebuilding
bumps the packet version, so the old decision no longer counts and the new
packet needs a fresh approval.

Approval links are not authenticated: anyone with the email could click them.
The API only checks that `approver` is the Controller's address, which WF4
fills in. That's acceptable for the prototype; a real deployment would verify
the approver's identity.

## WF5 Remember (sub-workflow)

| # | Node | Calls / does |
|---|---|---|
| 1 | Execute Workflow Trigger | Input: `client_id, run_id` |
| 2 | HTTP Request | `POST /runs/{run_id}/remember {client_id}`: **not built yet.** It will embed the approved run's owner replies into `doc_chunks` as `past_explanation`s, so next year's retrieval finds them. |

## WF6 Error handler

| # | Node | Calls / does |
|---|---|---|
| 1 | Error Trigger | Fires when any workflow that names WF6 as its error workflow fails |
| 2 | Code: find the run | Read `client_id` and `run_id` from the failed execution's data (each workflow passes them along); if there's no run yet (e.g. WF1 failed before `/runs/start`), skip node 3 |
| 3 | HTTP Request | `POST /runs/{run_id}/needs-attention {client_id, reason: "<workflow>: <node>: <error message>"}` (`409` here means the run was already sent, which is fine) |
| 4 | Send Email (SMTP) | Map the `alert_email` from node 3's response. If the run had a claimed send, the alert explains how to settle it with `resolve-claim` (see below). |

To resume a `needs_attention` run, re-run the step that failed: `packet`
works from `needs_attention`, and so do `observations` and `responses`.

**A stuck send** (claimed, never marked sent) is settled by a person, not
retried: check the leadership inbox, then call
`POST /runs/{id}/resolve-claim {client_id, resolution, resolved_by, note}` with
`resolution: "sent"` (it went out: the run becomes sent) or `"not_sent"` (it
didn't: the claim is released and a new execution can claim and send). Who
resolved it, and when, is recorded. While a claim is open, `GET /runs/{id}`
shows `status: "sending"`.

## Export

Build each workflow in the editor, then *Download* it as JSON into
`n8n/workflows/` (`wf1_collect_compute.json`, ...). Before committing, check the
JSON for hardcoded secrets (credentials are referenced by ID, never inlined),
missing error-workflow settings, and any request without `client_id`.
