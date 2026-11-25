# CHANGES

Requirement changes during the round: what came in, what I did, what I chose not to do, and why.

---

## 14:20 · Ananya: "Tonight's run + a kill switch"

She wants to run about 200 real tickets tonight, in parallel, with four asks.

### 1) Parallel runs under a company-wide billing limit of 5 calls/s · **done**

**The risk:** the limit is per company, not per agent. Ten agents each pacing themselves to 5/s add up to 50/s. If
billing throttles us hard, it blocks us for an hour, which would end the night.

**What I built:**
- **One `RateLimiter` per process, shared by every run** (`gateway.py`). Every billing call, from any agent, reserves
  the next evenly spaced slot. The default is **4/s**, leaving headroom under 5 (`BILLING_PER_SECOND`).
- **Billing tools are found from the manifest** ("Billing API: rate limited" in the description), not hard-coded.
  Today that means `list_payments` and `refund_payment`.
- **A 429 slows everyone down.** When any agent gets a 429, the shared limiter pushes the next free slot past the
  `Retry-After`, so the other agents stop hammering billing too, not only the one that hit it.
- **Runs go in parallel:** `python -m flightrec run --all` and the viewer use a worker pool (`WORKERS`, default 4).
- **Proof:** a test runs 8 agents × 5 billing calls on threads and checks that no 1-second window goes over the rate.

**Not done:**
- **The limiter is in-process.** Several worker processes would need a shared limiter, for example Redis. For tonight,
  run one process. The limit is marked as a `shortcut:` in the code.
- **200 tickets can't just all start at once.** Each run averages about 2 billing calls, so billing allows roughly
  100 tickets/minute, which is fine. The **LLM provider's own rate limit** is the real ceiling: on the free Gemini tier
  that's tens of requests per minute. Tonight needs a paid key or a lower `WORKERS`. I'd rather say that now than have
  it fail at 2 a.m.

### 2) Kill switch · **done**

**What I built:** one switch, `POST /api/kill` (a button in the viewer, or `python -m flightrec stop-all "reason"`):
- **Running agents stop at their next safe point**, between steps, and are never cut off mid-HTTP-call. The run ends as
  `stopped`, with the reason.
- **Nothing half-done:**
  - A note or email whose call failed is not retried once the switch is on. The audit log has already confirmed it
    didn't land.
  - A **refund in flight keeps resolving with its idempotency key.** That retry is safe, because it can't pay twice,
    and it's the only way to know whether the money moved.
- **Runs waiting for approval stop immediately.** Their pending approvals become `cancelled`, so nobody can approve a
  refund after the switch is pulled.
- **Every stopped run gets a money report** in its summary, for example `Money: refund PAY-8117 INR 4999: DONE (refund
  RF-0001)` or `...: not made (approval cancelled)`. That answers "if a refund was in flight, did it happen?"
- **The switch persists** in the database, so it survives a restart. While it's on, no new runs start and no approvals
  are accepted. `POST /api/kill/release` turns it off. Stopped runs stay stopped; a person decides what to re-run.

**Not done:** no "pause and resume later". Stopping is final for those runs, which is simpler and safer for tonight.

### 3) Cost per run in rupees and a daily total · **done**

- **Each run shows its cost in ₹**, and `/api/config` returns today's total (`today.inr`, number of runs, tokens).
- **Conversion uses `USD_INR`**, default 84.0. Finance should set it to the day's rate. Token prices are set with
  `LLM_PRICE_IN_PER_M` and `LLM_PRICE_OUT_PER_M`.
- **Caveat:** cost is estimated from token counts and list prices. It isn't the provider's invoice.

### 4) Big model for hard tickets, cheap model for easy ones · **partly**

**Done:** the model is chosen **per run**, from an allowlist (`LLM_MODELS=cheap,strong`; the first is the default). It
is recorded on the run and shown in the viewer, so cost and outcome can be compared per model.

**Not done: automatic routing.** Deciding what counts as "hard" is itself a judgement that can be wrong. A misrouted
refund ticket handled by the cheap model is exactly the "wrong thing done confidently" we are trying to prevent, and we
have no evaluation data yet to set the threshold. What I'd do next:
1. Run the cheap model by default.
2. Re-run tickets that end `needs_human` or `failed` with the strong model, under human approval as usual.
3. Measure outcome and cost per model on real tickets, then decide whether automatic routing pays for itself.

---

## 14:50 · Feedback from the operator testing the viewer

| Feedback | What I did | Why |
|---|---|---|
| "I wrote 'make it polite' in the approval note, and the email went out unchanged" | Added **Request changes** next to Approve and Reject. Nothing runs; the note goes back to the agent, which revises and asks again. Approve still runs **exactly** the arguments shown, and its note is passed to the agent too | An approval must cover exactly what the person saw. If a note could silently change the action, the approval wouldn't mean anything |
| "A *needs human* run shows no options" | Escalated, failed and stopped runs now show the reason, the notes the agent left, and two actions. **Hand back to agent** starts a follow-up run with your guidance. **Mark handled by me** records that a person resolved it | A follow-up is a new run instead of an edited old one, so every trace still replays exactly |
| Dark theme | A toggle in the top bar, remembered per browser | — |
| Security review: DNS rebinding and cross-site POSTs to the local viewer | Only requests addressed to `localhost` / `127.0.0.1` are accepted, and state-changing requests from another site are refused (tested) | The viewer has no login, so the browser is the attack path |
| Suggestion: rebuild the viewer on CopilotKit | **Not today** | It's a React framework with its own agent runtime. That means a build step, and the brief rules out agent frameworks for the loop. It's a good fit later for a "why did this run escalate?" chat over the traces |

---

## 15:20 · Second round of operator feedback, and preparing for 200 tickets

| Change | Why |
|---|---|
| **T-103 phishing is enforced in code (KB-5).** An injected instruction in the ticket text blocks closing, refunding and finishing `succeeded`; the agent must note it and escalate | In the first final run the model ignored the injection but **closed the ticket**. Right action, wrong outcome. The final run was redone from a clean reset |
| **"Mark handled" changes the headline status to "Resolved by you"** (and the run leaves *Needs attention*; clicking twice does nothing) | The operator clicked it 4 times because the status still read "Needs human" |
| **Recorded traces load on startup**; `run.bat` for a Windows double-click; `run.sh` falls back to the system Python when a virtualenv can't be built | A fresh clone with no key must still start and replay. Verified: all six final runs replay identically with no key |
| **Janitor:** runs whose worker died are re-queued once their lease expires | Found while answering defence Q3: after a crash, the run stayed "Running" forever |
| **One active run per ticket** | Two agents on one ticket is how a customer gets refunded twice |

**Not done:** a per-ticket idempotency key. It's stronger, but it changes the arguments of the already-recorded final
runs, so their replays would diverge. Doing it means re-recording the final run. It's listed under next steps.


---

## 16:00 · Final testing pass

What I checked:
- **The full offline suite:** 78 tests.
- **Every API endpoint**, plus the security blocks: another host gets 400, a cross-site POST gets 403.
- **The kill switch end to end:** engaging it refuses new runs (409), and releasing it allows them again.
- **All six final runs**, rendered in the viewer with no script errors.
- **No-key startup and replay:** run earlier with `./run.sh`; all six final runs replay identically.

Issues found and fixed:

| Issue | Fix |
|---|---|
| Marking a run handled without a note showed "handled by a person: handled by a person" | The resolution text only adds the note when there is one |

Known and not fixed before the code freeze (also in the README honest status and DEFENSE.md):
- A refund that 504s and then only times out is reported as "not made". It should be "unknown", checked against the audit.
- Runs stopped by a budget or the kill switch don't replay as identical, because stop decisions aren't recorded as steps yet.
- `run.bat` hasn't been tested by double-clicking. `run.sh` has, including with no key.
