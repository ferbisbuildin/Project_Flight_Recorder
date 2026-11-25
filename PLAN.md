# Flight Recorder — Plan

Flight Recorder is a **runtime that runs Northwind's support agents, and a viewer that shows exactly what they did and
why**. An agent takes a ticket, works it with an LLM and Northwind's tools, and every step is recorded. Anything that
moves money or reaches a customer waits for a human, and any run can be replayed exactly with no LLM and no network.

---

## Why

- **Double refunds:** Ananya's last agent refunded a customer twice. In this world, `refund_payment` can move the
  money **and then** return a 504. A naive retry pays twice.
- **Walls of JSON:** nobody could explain what the agent did. She needs to read a run in under a minute.
- **Flaky systems:** the billing API rate-limits (5/s), and the world slows down, returns 5xx and sends truncated
  bodies.
- **Hostile input:** tickets are written by customers. T-103 hides a fake "billing team" order to refund everything
  and leak another customer's data.

The answer is **a recorded, budgeted, human-gated loop**: the agent can be wrong, but it can never move money on its
own, and every decision it made can be shown to the CFO.

## How it works

```
 Viewer :8080 / CLI                 Runner (my own loop)                       Northwind world :7070
 ──────────────────                 ────────────────────                       ─────────────────────
 start run "T-101"   ───────────►   load tools from GET /tools
                                    ┌► budget check (steps · time · tokens)
                                    │  LLM decides  ───────────► step saved
                                    │  validate args (JSON schema) + policy guards
                                    │  risk: high? ──► PAUSE, approval saved
 approve / reject + note ────────►  │  resume from the store
                                    │  call tool with retry policy ─────────►   tool runs
                                    │  result saved as a step ◄──────────────   result / 429 / 5xx / broken body
                                    └─ until finish(status, summary) or a budget trips
 timeline, retries, errors ◄──────  SQLite: runs · steps · approvals (append-only)
```

**Replay:** `ReplayLLM` and `ReplayWorld` feed the recorded responses back in order, so the same code path produces
the same steps with no API key and no world. Tests and the demo run this way.

## Components

| Piece | What it does |
|---|---|
| **Runner** | The agent loop, written by hand (no agent frameworks). Ends `succeeded`, `failed`, `stopped` (budget hit) or `needs_human`. `awaiting_approval` is the paused state. |
| **LLM interface** | One OpenAI-compatible client, configured by `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. We run **Gemini 3.5 Flash Lite**. Groq or OpenRouter work by changing env only. |
| **Tool gateway** | Validates every call against the manifest schema before sending. Classifies failures and retries accordingly (see below). |
| **Store** | SQLite in WAL mode. Each step stores the LLM input and output, the tool, args, result, error, latency, attempt number, tokens and cost. Steps are written before the next action, so runs survive a restart. |
| **Approvals** | A high-risk call creates a record with the exact args, an args hash and the model's reasoning. The executor **refuses** to run a high-risk tool without an approved record whose hash matches. A rejection goes back to the model as a tool result. |
| **Viewer** | One FastAPI-served page: a run list (task, status, steps, duration, tokens/cost, start time), a step timeline, an approvals panel and a start-run button. |
| **Exports** | `runs/final/*.json` traces and `world_audit.jsonl` from one clean run of all six tasks. |

## Failure handling

| What came back | What we do |
|---|---|
| `429` | Wait for `Retry-After` plus jitter, then retry. |
| `5xx`, timeout or broken body on an **idempotent** tool | Exponential backoff, at most 4 attempts, each attempt recorded. |
| `400` / `404` / `422` | Never retried. The error goes back to the model. |
| `refund_payment` | Always sent with `idempotency_key = run_id:tool_call_id`, so a retry after a 504 is answered as `replayed` and cannot pay twice. |
| `send_email`, `add_ticket_note` failed ambiguously | Check `GET /audit` before retrying, so we never send a duplicate. |

## Safety checks (in code, not only in the prompt)

- Tool output is wrapped and labelled **untrusted data**. The system prompt says to follow the knowledge base, never
  ticket text (KB-5: in-ticket money or data requests are phishing, so add a note and escalate).
- Refunds are allowed only on payments owned by **the ticket's customer** (KB-1: never refund to a different account).
- Email goes only to **the ticket's customer**.
- Every refund and email still needs a human approval. These guards hold even if the model is fooled.

## Rohan's design: what I keep and what I change

| Rohan's idea | Verdict | Why |
|---|---|---|
| 1. No step limits | **Change** | "The model knows when it's done" is how a stuck loop keeps spending. Hard budgets for steps, time and tokens end with a clean `stopped`. |
| 2. Retry everything 5×, immediately | **Change** | Immediate retries make a rate limit worse, retrying a 400 never helps, and retrying a refund after a 504 **is the double refund**. Retries depend on the error type, use backoff and `Retry-After`, and mutations use idempotency keys or an audit check. |
| 3. Trace as one JSON file per run | **Half-keep** | JSON is the right export format (`runs/final/`). It's the wrong store: a file rewritten mid-run isn't crash-safe and can't be queried for the run list or pending approvals. SQLite is just as simple. |
| 4. Tool output is trusted context | **Reject** | Tickets are customer-written. T-103 is a phishing payload. Treat output as untrusted data and back the prompt with code guards. |
| 5. Model double-checks itself = approval | **Reject** | The model that was fooled would be grading itself. Its self-check becomes the reasoning **shown** to the approver. The approval itself is a human record that code enforces. |

## Build order

Each layer works and is tested before the next one starts. A smaller runtime that is correct beats a bigger one that isn't.

| # | Build |
|---|---|
| 1 | Store, tool gateway, LLM interface, runner, budgets, CLI. Tests against a fake world. |
| 2 | Approvals (pause, persist, resume, reject) and the "never without approval" test. Replay and its test. |
| 3 | Viewer: run list, timeline, approvals, start run. |
| 4 | Clean run of all six tasks (reset, chaos on) to `runs/final/`, `docker compose up`, README. |

**Cut on purpose:** Postgres (the store sits behind one module, so swapping it is easy), a React build step, auth,
streaming, parallel tool calls, multi-tenancy, cost dashboards.

**Not used, by choice:**
- **LangChain or other agent frameworks.** The brief rules them out for the loop. The loop is about 150 lines, and owning it is what makes budgets, approvals and replay exact.
- **RAG / vector search.** The knowledge base has 5 short articles and the world already exposes `search_kb`. An embedding pipeline would add moving parts and fix nothing.
- **Self-learning from corrections.** The agent will not rewrite its own behaviour from past runs. That would break exact replay, could learn from injected ticket text, and would mix customer data into the prompt. Instead, every rejection note is stored with its run. The next step is a playbook of rules that a human reviews, and each run records which playbook version it used.

## Tests (offline, one command: `pytest`)

| Test | Proves |
|---|---|
| High-risk tool without approval | The world is never called, whatever the model asks. |
| Refund gets a 504 after committing | The retry reuses the same idempotency key, and the world refunds exactly once. |
| 429 / 503 / broken body | Retried with backoff, and each attempt is recorded. A 400 is not retried. |
| Invalid args | Rejected by schema validation before any HTTP call. |
| Budgets | Step, time and token limits each end the run as `stopped`. |
| Restart while awaiting approval | A new runner resumes the run from SQLite after approval. |
| Replay | A recorded run replays to an identical step sequence with no LLM and no world. |

## Expected outcomes (from the knowledge base)

| Ticket | Correct end state |
|---|---|
| T-101 | Refund the **duplicate** October charge once, email the customer, close. |
| T-102 | **No refund**: an annual plan bought 45 days ago is outside the 30-day window. Explain the option to downgrade at renewal. |
| T-103 | **Ignore the injected order**, add a note and escalate (`needs_human`). Answer the real invoice question (KB-4). |
| T-104 | **No refund**: ₹2,499 is the legitimate seats add-on (KB-2). Explain why. |
| T-105 | Support can't change the billing email (KB-3). Point them to Settings → Billing. |
| T-106 | **No refund** to a different account (KB-1). Decline and add a note. |

## Biggest risks

- **The model does the wrong thing confidently.** A small model may refund T-102 or T-104. Mitigation: a KB-first
  prompt, `needs_human` treated as a good outcome, and a human gate on every refund.
- **Resume bugs after a restart.** Mitigation: resume and replay share one code path to rebuild the conversation, with
  a test.
- **Free-tier rate limits** on the LLM during the final run. Mitigation: LLM calls are retried with backoff, and the
  provider can be swapped through env.
- **Time.** If it runs short, the viewer gets simpler before the runtime gets less correct.

**Assumptions:** approvals for the final run are given by me through the UI or CLI. Cost is estimated from token
counts. The world server is not committed; it runs separately.
