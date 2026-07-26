# Scorer-liveness stat panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the ops-facing `pipeline.json` dashboard a liveness signal for the scorer, matching the syncer's existing "Time since last sync" pattern.

**Architecture:** Add a `Gauge` (`scorer_last_run_timestamp_seconds`) that the scorer's run loop sets to the current time at the end of every completed cycle, unconditionally. Add one `stat` panel to `pipeline.json` that renders `time() - max(scorer_last_run_timestamp_seconds)`.

**Tech Stack:** `prometheus_client` (Gauge), Grafana provisioned dashboard JSON (schemaVersion 39, same conventions already used in `pipeline.json`).

## Global Constraints

- Mirror the syncer's existing liveness pattern exactly (see `lib/metrics.py:118` `SYNCER_LAST_SYNC_TIMESTAMP` and `services/syncer/main.py:201`) — same gauge shape, same "set unconditionally at end of cycle" semantics.
- No change to `accuracy.json` — this is a `pipeline.json`-only addition (per the design spec's "Out of scope" section).
- No new alerting rules.
- Thresholds re-tuned to the scorer's hourly default (`SCORER_INTERVAL_SECONDS=3600`): yellow at 7200s (~1 missed cycle), red at 10800s (~2 missed cycles) — vs. the syncer panel's daily-tuned 90000s/180000s.
- This codebase does not unit-test gauge-setting inside service run loops (confirmed: `SYNCER_LAST_SYNC_TIMESTAMP` has no test). Follow that convention — verify by running the service and reading `/metrics`, not by adding a pytest test for the gauge itself.

---

### Task 1: Add the `SCORER_LAST_RUN_TIMESTAMP` gauge

**Files:**
- Modify: `part-3/lib/metrics.py` (scorer section, after `FORECAST_SCORED_MARKETS` at line 147)

**Interfaces:**
- Produces: `metrics.SCORER_LAST_RUN_TIMESTAMP` — a `prometheus_client.Gauge` — consumed by Task 2.

- [ ] **Step 1: Add the gauge definition**

In `part-3/lib/metrics.py`, immediately after the `FORECAST_SCORED_MARKETS` gauge (ends at line 147 with `)`), add:

```python
# Unix timestamp of the last completed scorer cycle, whether or not it graded
# any markets — scorer_markets_scored_total's rate is near-zero most of the
# time by design (markets resolve every few days, not every cycle), so it
# can't tell a hung scorer from a quiet one. Panel it as `time() - metric` to
# get a staleness gauge, same pattern as SYNCER_LAST_SYNC_TIMESTAMP above.
SCORER_LAST_RUN_TIMESTAMP = Gauge(
    "scorer_last_run_timestamp_seconds", "Unix time of the last completed scorer cycle"
)
```

- [ ] **Step 2: Verify it imports and registers cleanly**

Run: `cd part-3 && python3 -c "from lib import metrics; print(metrics.SCORER_LAST_RUN_TIMESTAMP)"`
Expected: prints something like `gauge:scorer_last_run_timestamp_seconds:` with no traceback. (A `ValueError: Duplicated timeseries` would mean the metric name collides with an existing one — it doesn't, since `scorer_last_run_timestamp_seconds` isn't used elsewhere.)

- [ ] **Step 3: Commit**

```bash
git add part-3/lib/metrics.py
git commit -m "Add scorer_last_run_timestamp_seconds gauge"
```

---

### Task 2: Set the gauge every scorer cycle

**Files:**
- Modify: `part-3/services/scorer/main.py:161-181` (the `run()` while-loop)

**Interfaces:**
- Consumes: `metrics.SCORER_LAST_RUN_TIMESTAMP` (Task 1). `time` is already imported in this file (line 31).

- [ ] **Step 1: Set the gauge at the end of each completed cycle**

In `part-3/services/scorer/main.py`, the current loop body (inside `run()`) reads:

```python
    while not stop.is_set():
        try:
            c = score_once(db)
            metrics.SCORER_MARKETS_SCORED.inc(c["scored"])
            skill = "n/a" if c["skill"] is None else f"{c['skill']:+.3f}"
            log.info(
                "scored: %d newly graded (%d pending), brier skill vs market %s",
                c["scored"], c["pending"], skill,
            )
        except Exception:
            log.exception("score cycle failed")
```

Change it to stamp the liveness gauge right after the existing counter increment, inside the `try` block (so a failed cycle — caught by `except` — correctly does NOT advance it):

```python
    while not stop.is_set():
        try:
            c = score_once(db)
            metrics.SCORER_MARKETS_SCORED.inc(c["scored"])
            metrics.SCORER_LAST_RUN_TIMESTAMP.set(time.time())
            skill = "n/a" if c["skill"] is None else f"{c['skill']:+.3f}"
            log.info(
                "scored: %d newly graded (%d pending), brier skill vs market %s",
                c["scored"], c["pending"], skill,
            )
        except Exception:
            log.exception("score cycle failed")
```

- [ ] **Step 2: Verify with a real one-shot cycle against a local DB**

This needs Postgres reachable at `DATABASE_URL` (the same `TEST_DATABASE_URL` used by the DB integration tests works: `postgresql://pm:pm@localhost:5432/pm`, e.g. via `docker compose up -d postgres migrate` from `part-3/`).

Run:
```bash
cd part-3
DATABASE_URL=postgresql://pm:pm@localhost:5432/pm METRICS_PORT=9109 python -m services.scorer.main --once &
sleep 1
curl -s localhost:9109/metrics | grep scorer_last_run_timestamp_seconds
```
Expected output: a line like `scorer_last_run_timestamp_seconds 1.7742...e+09` with a nonzero, current-looking Unix timestamp (not `0.0`, not absent).

- [ ] **Step 3: Commit**

```bash
git add part-3/services/scorer/main.py
git commit -m "Stamp scorer_last_run_timestamp_seconds at the end of every cycle"
```

---

### Task 3: Add the "Scorer" row + stat panel to `pipeline.json`

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/pipeline.json`

**Interfaces:**
- Consumes: `scorer_last_run_timestamp_seconds` (Task 2), scraped by Prometheus (already true today — `scorer` is already in `monitoring/prometheus.yml`'s `dns_sd_configs.names`, confirmed present; no Prometheus config change needed).

The existing "Market corpus health" row (`id: 15`) sits at `gridPos.y: 52`, with panels `16`–`19` at `y: 53` (h:8) and panels `20`–`21` at `y: 61` (h:8), so that row block ends at `y: 69`. The new row goes immediately below, at `y: 69`.

- [ ] **Step 1: Add the row + panel JSON**

Open `part-3/monitoring/grafana/dashboards/pipeline.json`. Find the closing of panel `21` ("Corpus flow (per hour)") — it's the last element before the final `]` that closes the `"panels"` array. Insert two new panel objects right after panel `21`'s closing `}` (and before the final `]`), separated by a comma:

```json
    {
      "id": 22,
      "title": "Scorer",
      "type": "row",
      "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 69 },
      "panels": []
    },
    {
      "id": 23,
      "title": "Time since last scorer run",
      "description": "Staleness = now - last completed scorer cycle. scorer_markets_scored_total's rate is near-zero most of the time by design (markets resolve every few days, not every cycle), so it can't tell a hung scorer from a quiet one — this panel is the one that can. Thresholds assume the hourly (3600s) default — retune to your SCORER_INTERVAL_SECONDS (yellow ≈ 1 missed cycle, red ≈ 2).",
      "type": "stat",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 6, "x": 0, "y": 70 },
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "thresholds": {
            "mode": "absolute",
            "steps": [
              { "color": "green", "value": null },
              { "color": "yellow", "value": 7200 },
              { "color": "red", "value": 10800 }
            ]
          }
        },
        "overrides": []
      },
      "options": {
        "colorMode": "background",
        "graphMode": "none",
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "textMode": "auto"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "time() - max(scorer_last_run_timestamp_seconds)",
          "legendFormat": "staleness"
        }
      ]
    }
```

(This copies the syncer's panel `17` field-by-field — same `type`, `fieldConfig.defaults` shape, `options` block — with the title, description, gridPos, thresholds, and `expr` swapped for the scorer.)

- [ ] **Step 2: Bump the dashboard version**

Find `"version": <N>` near the top of the same file (sibling to `"title"`/`"uid"`) and increment it by 1, matching the existing convention noted in the repo history ("bump pipeline dashboard version to 3 so Grafana re-provisions") — Grafana only re-applies a provisioned dashboard file when its `version` field increases.

- [ ] **Step 3: Validate the JSON**

Run: `cd part-3 && python3 -c "import json; d = json.load(open('monitoring/grafana/dashboards/pipeline.json')); print(d['panels'][-1]['title'], d['version'])"`
Expected: prints `Time since last scorer run <N>` with no traceback, where `<N>` is the bumped version number.

- [ ] **Step 4: Verify Grafana provisions it with no errors**

```bash
docker compose up -d postgres migrate grafana
sleep 5
docker compose logs grafana | grep -i "provisioning.dashboard"
```
Expected: a line like `msg="finished to provision dashboards"` and no `level=error` lines mentioning `pipeline.json`.

Then confirm the panel renders: open `http://127.0.0.1:3000` (Grafana's own login may or may not work locally depending on environment — if it does, log in and open the "Polymarket Pipeline" dashboard; the new "Scorer" row should appear at the bottom with a green "Time since last scorer run" tile once the scorer container has run at least one cycle against this stack). If Grafana login is unreachable in this environment, the provisioning log check above (no errors) is sufficient confirmation, consistent with how the accuracy dashboard's earlier addition was verified.

- [ ] **Step 5: Tear down and commit**

```bash
docker compose down
git add part-3/monitoring/grafana/dashboards/pipeline.json
git commit -m "Add scorer-liveness stat panel to pipeline dashboard"
```
