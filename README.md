# Nasaq — Intelligent Leave & Workforce Coordination

*Time off, operations on.*

Built for the **Misk × HUMAIN Future AI Builders Hackathon — Challenge 3: Autonomous Enterprise**.

> Naming: the product is **Nasaq**. The Python package, database file and environment variables keep their original
> `shiftrescue` names.

## What it does

When someone asks for time off, the real work is coordination: who is qualified to cover, who is rested and under their
weekly hours, whether the clinic still meets its minimum staffing, and whether anyone has to be asked again after a "no".
Nasaq hands that coordination to a **Claude agent** and keeps the decisions that matter with people and deterministic code.

Nasaq is designed for any shift-based workforce where cover must be arranged around absences. This repository demonstrates
it on a **synthetic clinic** (20 fictional staff in four roles, two weeks of shifts); no real employee data is used.

- **Annual leave, 1–7 consecutive days** (or a partial day). Hours come from the employee's real scheduled shifts, so a
  Mon–Fri request for someone who works 3 shifts deducts 3 shifts' worth of hours, not 40.
- **Sick absence** (today or a future date, one or several days). Recorded immediately as *Reported*: no approval step,
  no annual-balance deduction, no medical documents. The agent still assesses and arranges cover; an accepted replacement
  is written to the schedule right away.
- **Autonomous coverage coordination.** The agent analyses staffing per day and per shift, finds eligible replacements,
  sends offers, reacts to declines by choosing the next candidate, and verifies coverage after changes. Different
  shifts and days can have different replacements, each answered independently.
- **A staffing shortage never denies leave.** If nobody can cover, the uncovered period is escalated to the manager with a
  summary, and only the manager decides. Separately, a request that is *invalid* is rejected at submission by validation
  (annual leave for a past date, more than 7 days, no scheduled shift in the period, overlap with an existing request, or
  insufficient annual balance).
- **Manager approval, then one atomic update.** After approval the leave record, schedule changes and balance are
  applied in a single database transaction, then verified for every affected date.
- **Full audit trail** of requests, offers, responses, decisions, schedule changes and tool calls (SQLite).

## What makes it agentic

The agent runs a manual tool-use loop against the Anthropic API (`shiftrescue/agent.py`). It is not a script with an LLM
call bolted on: the model chooses which tools to call, in what order, and what to do next based on what each tool returns.

Tools: `get_request_context`, `get_policies`, `analyze_staffing`, `find_candidates`, `send_coverage_offer`,
`submit_to_manager`, `escalate_to_manager`, `verify_coverage`, `send_notification`.

| The LLM agent decides | Deterministic code enforces (`shiftrescue/rules.py`, `services.py`) |
|---|---|
| which tools to call and in what order | role qualification, availability, overlaps, approved leave |
| which eligible candidate to offer first, and why | 10 h minimum rest, 12 h/day, weekly-hours cap, no double-booking |
| what to do after a decline or an acceptance | minimum staffing per role, leave balance |
| when to escalate, how to word notifications | max 4 offers per need, one pending offer at a time, approval ordering, atomic apply with rollback |

Every tool call is re-validated in code. A call that breaks a rule is refused and returned to the model as an error, and
it appears in the timeline as *Safeguard blocked*. **The agent has no tool to approve or deny leave or to edit the
schedule.**

## What is real and what is simulated

| Real | Simulated (clearly labeled in the UI) |
|---|---|
| Claude tool calling and every agent decision in the timeline | An employee's **Accept / Decline** on a coverage offer |
| Rule checks, staffing analysis, candidate filtering | The manager's **Approve / Reject** click |
| Database writes, atomic apply, rollback, verification | Nothing is sent outside the app: notifications are in-app only |
| Audit log of the above | Demo data is synthetic; there are no HR, payroll or calendar integrations |

The simulated buttons stand in for people who are not in the room during a demo. The agent's reaction to them is real.

## Setup (Windows, PowerShell)

Requires Python 3.10+.

```powershell
git clone <your-repo-url>
cd <repo-folder>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then retry.

In `.env`, set `ANTHROPIC_API_KEY` to your own key (create one at https://console.anthropic.com). The default model is
`claude-opus-5-5`; override it with `ANTHROPIC_MODEL`. `.env` is git-ignored: never commit it.

macOS/Linux: the same steps work with `source .venv/bin/activate` and `cp .env.example .env`.

## Run

```powershell
streamlit run app.py
```

Opens http://localhost:8501. The SQLite database is created at `data/shiftrescue.db` and seeded on first run. Each agent
run makes real API calls and uses your key.

## Test (no API key, no network)

```powershell
python -m pytest -q
```

57 tests: scheduling constraints, approval sequencing, atomic updates and rollback, coverage verification, multi-day and
sick-absence logic, the agent loop's guardrails (using a stub model, for protocol testing only), and Streamlit UI smoke
tests on temporary databases. A test guard blocks creation of a real Anthropic client, so the suite cannot spend money.

## Using the app

| Page | Purpose |
|---|---|
| Overview | KPIs, items needing attention, 7-day staffing check, recent requests |
| My Leave | Employee portal: annual leave or sick absence, dates and hours, balance, status, notifications |
| Manager Workspace | Action required (approvals, escalations), ongoing coverage, reported sick absences, activity history |
| Workforce Calendar | Colour-coded shifts, leave, sick absence, replacement cover and below-minimum days, with date and role filters |
| AI Activity | Readable per-request timeline; full audit trail and tool calls under *Technical details* |
| Demo Scenarios | The five scenarios below and a collapsed *Admin / Troubleshooting* section (agent re-run, data reset) |

## Demo scenarios (page **Demo Scenarios**)

1. **First replacement declines** — the agent offers to a candidate; click *Decline* (simulated) and it picks the next
   eligible one. *Accept*, then approve in the Manager Workspace.
2. **Partial-day leave** — a lab technician, 12:00–16:00. After approval the Workforce Calendar shows the shift split
   and the replacement's cover shift.
3. **No qualified replacement** — the only pharmacist on shift. The agent escalates; the manager must explicitly accept
   the gap. The request is never auto-denied.
4. **Multi-day annual leave (Mon–Fri)** — three separate coverage needs, answered independently. Decline the only
   Friday candidate to see just that day escalate. Approval applies everything in one transaction and verifies each date.
5. **Sick absence, 2 days (Thu–Fri)** — reported instantly, no approval, no balance deduction. The agent arranges cover or
   escalates the shortage; the absence is never blocked and never marked "approved".

Also try: a requester with only 4 h of balance (rejected by the balance validation), or a receptionist (staffing stays
satisfied, so no replacement is needed).

## Limitations

- Prototype with **synthetic data**; no authentication or roles — one browser session plays employee and manager.
- Leave spans at most 7 consecutive calendar days; multi-day leave is full days only (hourly leave is single-day).
- One replacement per affected shift segment.
- Sick absence has no medical certificates, HR eligibility rules or payroll. The "no approval, no balance deduction"
  policy is a demo policy, not a statement of any labour law.
- Notifications are in-app only; no email, chat or calendar integrations.
- Agent runs need an Anthropic API key and network access; results can vary between runs. Rules are enforced in code
  regardless of what the model proposes.
- Existing databases are migrated automatically (adds `end_date`). *Reset demo data* (under *Admin / Troubleshooting*)
  deletes all requests and restores the original sample data.

## Layout

```
app.py                   Streamlit UI (6 pages)
shiftrescue/db.py        SQLite schema, migration, transactions
shiftrescue/rules.py     deterministic scheduling constraints
shiftrescue/services.py  workflow state machine, atomic apply, verification, audit
shiftrescue/agent.py     Claude tool-calling loop and tool definitions
shiftrescue/seed.py      synthetic clinic data and policies
shiftrescue/scenarios.py the five demo scenarios
shiftrescue/ui.py        UI helpers
tests/                   pytest suite (no API access)
.env.example             template for local configuration
```
