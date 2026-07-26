# Scorer-liveness stat panel — design

## Problem

`pipeline.json` (the ops-facing "is the machine running?" dashboard) has a
liveness/staleness panel for every other singleton service (syncer: "Time
since last sync") but none for the scorer. A hung or crash-looping scorer
currently reads the same as a quiet one — `scorer_markets_scored_total`'s
rate is near-zero most of the time by design (markets resolve every few
days, not every cycle), so it can't be used as a liveness signal.

## Approach

Mirror the syncer's existing pattern exactly, rather than relying on
Prometheus's `up{job="scorer"}` scrape metric (which only proves the
`/metrics` HTTP endpoint answers, not that the scoring loop itself is still
completing cycles).

1. **`lib/metrics.py`** — add a new gauge, grouped with the other scorer
   metrics:

   ```python
   SCORER_LAST_RUN_TIMESTAMP = Gauge(
       "scorer_last_run_timestamp_seconds", "Unix time of the last completed scorer cycle"
   )
   ```

2. **`services/scorer/main.py`** — set this gauge to the current time at the
   end of every completed cycle, unconditionally (whether or not any markets
   were actually graded that cycle) — same spot the other scorer gauges are
   refreshed each cycle.

3. **`monitoring/grafana/dashboards/pipeline.json`** — add a new row titled
   `"Scorer"` below the existing "Market corpus health" row, containing one
   `stat` panel, "Time since last scorer run":

   - `expr: time() - max(scorer_last_run_timestamp_seconds)`
   - `unit: s`, `colorMode: background`
   - Thresholds re-tuned to the scorer's default hourly cadence
     (`SCORER_INTERVAL_SECONDS=3600`): green until ~1 missed cycle (yellow at
     7200s), red at ~2 missed cycles (10800s) — same "yellow ≈ 1 missed
     cycle, red ≈ 2" convention as the syncer's panel, just re-tuned to an
     hourly instead of daily interval.

## Out of scope

- No change to `accuracy.json` — that dashboard already covers "is the
  scorer's output *good*"; this panel is purely about "is the process
  alive".
- No new alerting rules — this is a dashboard-only visibility addition.
