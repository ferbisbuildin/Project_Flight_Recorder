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

## Reply sent to Ananya

> Hi Ananya,
>
> Here's what you can have today:
>
> 1. **Parallel runs that respect the company-wide billing limit.** Every agent shares one limiter, set to 4/s to
>    leave headroom under 5/s. If billing ever pushes back, every agent backs off, not just one. One caveat: the AI
>    provider's own rate limit is the real ceiling for 200 tickets, so tonight needs a paid key or fewer workers.
> 2. **A kill switch.** One button stops every agent at its next safe point. Anything waiting for approval is
>    cancelled. A refund already in flight is resolved safely, and every stopped run says exactly what happened to
>    money ("refund PAY-… DONE" / "not made").
> 3. **Cost per run in rupees, plus a daily total.** It's estimated from token usage at a rate finance can set.
> 4. **Model choice per run.** No automatic "hard vs easy" routing yet: getting that wrong on a refund ticket is
>    exactly the risk you described. I suggest the cheap model by default, re-running escalated tickets on the strong
>    one, and deciding on automation once we've seen a week of results.
>
> Every refund and customer email still waits for one of your people.
>
> Vardan
