# DEFENSE.md

Answers to the three questions, against commit 89d757d.

## 1. The 504 refund, the kill-switch report, and the second refund attempt

The retry path: the runner stamps every refund with `idempotency_key = run:payment_id:amount` before
validation. When the world answers 504 after moving the money, the gateway retries with the exact same
arguments and the same key, so the world answers `replayed: true` and the customer is never charged twice.
That part I trust.

The harder case exposes a real gap. If the world then stops answering and all six attempts time out, the
tool step records `error: still failing after 6 attempts`, and `money_report` maps that to "NOT made".
That is not guaranteed to be true: the first 504 may already have moved the money. The honest state is
unknown. So the report Ananya sees at that moment is overconfident. What I would change: any refund whose
attempts end in ambiguous failures should be reported as "unknown, possibly completed, check the audit",
and the run should finish `needs_human` instead of assuming the money is safe.

If the model calls refund_payment again on its next turn, two things stop a second refund: the new call
needs its own approval record, and the key is rebuilt identically, so the world would answer replayed.
But the 4999.0 case breaks this. The key is a plain string built from the amount, so 4999.0 and 4999
produce different keys, and a second refund could execute. Fix: canonicalise the arguments before
building the key (normalise numbers, or hash the normalised JSON), so logically identical calls always
map to the same key.

## 2. Replay of a stopped run, and what a replay should depend on

The live run ended `stopped`, either on the 300-second wall clock or on the kill switch. The replay deps
set `max_seconds` to infinity and `obey_kill` to false, so neither of those exists in the replay. Every
recorded step replays fine, and then the replay goes one step past where the live run ended and makes one
more LLM call. ReplayLLM has no recorded reply for it, raises ReplayDivergence, and the run ends
`failed: run aborted`. So the first step to differ is that extra LLM call, exactly where the original
trace has the `end: stopped` step. The replay ends `failed` instead of `stopped`, and `compare()` reports
first_difference at that index with a status mismatch.

Other inputs not in the trace that can change a replay: `max_steps` and `max_tokens` (the replay still
reads them from today's config, so editing the config between the live run and the replay changes the
path), the token prices, and the allowed model list. The fix I would make: snapshot the effective budget
and config into the trace when the run starts, and have replay read everything from the trace, so the
recorded trace is the only thing a replay depends on. Wall-clock time is the one input a replay genuinely
cannot reproduce, which is why I excluded it rather than fake it.

## 3. The orphaned run after a container restart

On restart, lifespan re-queues every run stuck at `running`. But the dead worker's lease is still fresh:
it was taken with the default 600s ttl. The new process calls `claim()`, it fails because the lease still
belongs to the dead process, and `advance()` simply returns `running` without doing anything. lifespan
runs exactly once at startup, and nothing retries after the lease expires. So over the next hour Ananya
sees that run sitting at `running` with no new steps, and unless someone calls /resume, nobody picks it
up. The run is silently orphaned.

My test misses this because `test_lease_of_a_dead_worker_expires` claims the run with ttl=-1, an already
expired lease. That only proves an expired lease can be taken over. It never tests the real case: a fresh
lease plus an immediate restart, where the takeover is refused and no reaper exists to retry later.

The test that would catch it: worker A claims with the default ttl, a fake clock, worker B restarts one
second later and must fail to advance the run; then the clock moves past the ttl and a periodic sweep
must pick the run up and finish it. The code change: a background reaper that periodically re-queues
`running` runs whose lease has expired, or have lifespan keep retrying until the lease clears. I would
ship the reaper, since it also covers the case where the restart happens hours later.
