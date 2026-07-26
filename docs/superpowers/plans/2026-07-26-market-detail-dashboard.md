# Per-Market Detail Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Grafana dashboard that shows, for one selected Polymarket market (open or resolved), how the market price and our internal belief evolve toward resolution, plus a threshold-colored "edge" panel for resolved markets.

**Architecture:** A new provisioned Grafana dashboard (`market-detail.json`) queries Postgres directly (tables `markets`, `belief_updates`, `market_prices`, `forecast_scores`). It is auto-loaded by the existing folder provisioner. The single-market drill-down is removed from `accuracy.json`, leaving that dashboard purely aggregate. No service, schema, or code changes — presentation layer only.

**Tech Stack:** Grafana (provisioned dashboards + Postgres datasource, uid `postgres`), Postgres 16 + pgvector, Docker Compose. Verification via `psql` (SQL correctness), `python3 -m json.tool` (JSON validity), and the Grafana HTTP API / browser (rendering).

## Global Constraints

- Dashboard file: `part-3/monitoring/grafana/dashboards/market-detail.json`, `uid: pm-market-detail`, title `Polymarket — Market Detail`.
- Datasource in every panel/variable: `{ "type": "postgres", "uid": "postgres" }`.
- All work happens under `part-3/`. Verification uses the **dev** compose only: `docker compose -f docker-compose.yml ...` (never the prod overlay).
- Grafana at `http://127.0.0.1:3000`, login `admin` / `${GF_SECURITY_ADMIN_PASSWORD:-admin}`. Provisioner reloads every 30s; use `docker compose -f docker-compose.yml restart grafana` for a deterministic reload.
- Never edit an applied DB migration; this plan touches no SQL migrations.
- Edge sign convention (must not be inverted): `edge = |market − outcome| − |belief − outcome|`; **positive = our belief is closer to the outcome than the market**.
- Belief is a step function: the `our belief` series uses `stepAfter` interpolation. Never linear-interpolate belief.

---

## Conventions used by every verification step

Run all commands from `part-3/`. These shell helpers are assumed defined in each task's shell:

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
dc()    { docker compose -f docker-compose.yml "$@"; }
psqlc() { dc exec -T postgres psql -U pm -d pm "$@"; }
GF_URL=http://127.0.0.1:3000
GF_AUTH="admin:${GF_SECURITY_ADMIN_PASSWORD:-admin}"
```

---

## Task 1: Bring the stack up and confirm the "No data" root cause

**Files:**
- None modified. Produces a confirmed diagnosis that later tasks rely on.

**Interfaces:**
- Produces: a captured open-market id (`$OPEN_MID`) and, if one exists, a resolved-market id (`$RES_MID`) used as test inputs in Tasks 3–5.

- [ ] **Step 1: Bring the dev stack up**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
docker compose -f docker-compose.yml up -d --remove-orphans
```

Expected: `postgres`, `migrate` (exits 0), `syncer`, `grafana` etc. come up. Wait for postgres healthy:

```bash
docker compose -f docker-compose.yml ps
```

- [ ] **Step 2: Reproduce the bug — confirm the current variable returns an empty list when no market is resolved**

Run the **current** `accuracy.json` variable query verbatim:

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT id, question FROM markets WHERE closed AND resolved_outcome IS NOT NULL ORDER BY resolved_at DESC LIMIT 200;"
```

Expected (the bug): `(0 rows)` — an empty dropdown, which is why every panel shows "No data". If it returns rows, note that the "No data" cause is instead the time range or datasource (see Step 4) and record which.

- [ ] **Step 3: Confirm the fix direction — the new variable query returns markets**

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT id AS __value, COALESCE(slug, question) AS __text FROM markets ORDER BY (end_date IS NULL), end_date DESC LIMIT 500;"
```

Expected: **≥1 row** (open markets included). This is the evidence that listing open markets fixes the empty dropdown. If this is also empty, the DB has no markets at all — run the syncer / `python db/seed_markets.py` until markets exist before continuing.

- [ ] **Step 4: Rule out the secondary suspects**

Datasource auth (should already be wired — compose sets `POSTGRES_PASSWORD: pm` on grafana):

```bash
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -i "postgres\|datasource" | tail -20
```

Expected: no auth/connection errors. Note anything suspicious.

- [ ] **Step 5: Capture test market ids for later tasks**

```bash
docker compose -f docker-compose.yml exec -T postgres psql -tA -U pm -d pm -c \
"SELECT id FROM markets WHERE NOT closed ORDER BY end_date NULLS LAST LIMIT 1;"     # -> use as OPEN_MID
docker compose -f docker-compose.yml exec -T postgres psql -tA -U pm -d pm -c \
"SELECT id FROM markets WHERE closed AND resolved_outcome IS NOT NULL LIMIT 1;"      # -> use as RES_MID (may be empty)
```

Record both values in the task notes. `RES_MID` may be empty; Tasks 4 and the resolved-market checks are then verified later once a market resolves, but the SQL is still validated against the schema.

- [ ] **Step 6: Commit the diagnosis** (docs only — no code yet)

```bash
git commit --allow-empty -m "chore: confirm market-detail 'No data' root cause (empty resolved-only variable)"
```

---

## Task 2: Scaffold the dashboard — base, variables, resolution annotation, overview stat row

**Files:**
- Create: `part-3/monitoring/grafana/dashboards/market-detail.json`

**Interfaces:**
- Produces: dashboard `uid: pm-market-detail` with template variables `$status` and `$market_id`, a dashboard annotation `resolution`, and a stat row (panel ids 1–6). Later tasks append panels with ids 10, 11, 12 to the `panels` array.

- [ ] **Step 1: Validate every stat/variable query against Postgres first (the "test")**

Using `OPEN_MID` from Task 1, confirm each query returns a sane value:

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
MID=<OPEN_MID from Task 1>
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT id AS __value, COALESCE(slug, question) AS __text FROM markets WHERE (NOT closed) ORDER BY (end_date IS NULL), end_date DESC LIMIT 500;"      # $market_id (Open filter)
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT current_score FROM markets WHERE id='$MID';"                                                       # current belief
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT yes_price FROM market_prices WHERE market_id='$MID' ORDER BY ts DESC LIMIT 1;"                     # current market price
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT EXTRACT(epoch FROM (end_date - now()))/86400 AS days FROM markets WHERE id='$MID';"               # days to resolution
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT count(*) FROM belief_updates WHERE market_id='$MID';"                                              # updates
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT 1 - brier_belief/NULLIF(brier_baseline,0) AS skill FROM forecast_scores WHERE market_id='$MID';"  # brier skill (may be 0 rows)
```

Expected: each of the first five returns a value (price may be 0 rows if the syncer hasn't recorded a price yet — acceptable). The last may be `(0 rows)` for an open market — acceptable.

- [ ] **Step 2: Create the dashboard file**

Create `part-3/monitoring/grafana/dashboards/market-detail.json` with exactly this content:

```json
{
  "annotations": {
    "list": [
      {
        "name": "resolution",
        "datasource": { "type": "postgres", "uid": "postgres" },
        "enable": true,
        "iconColor": "red",
        "target": {
          "rawSql": "SELECT end_date AS time, 'resolution' AS text FROM markets WHERE id = '$market_id' AND end_date IS NOT NULL",
          "format": "table"
        }
      }
    ]
  },
  "editable": true,
  "graphTooltip": 1,
  "refresh": "1m",
  "schemaVersion": 39,
  "tags": ["polymarket", "market-detail"],
  "templating": {
    "list": [
      {
        "name": "status",
        "label": "Status",
        "type": "custom",
        "query": "All,Open,Resolved",
        "current": { "text": "All", "value": "All" },
        "options": [
          { "text": "All", "value": "All", "selected": true },
          { "text": "Open", "value": "Open", "selected": false },
          { "text": "Resolved", "value": "Resolved", "selected": false }
        ]
      },
      {
        "name": "market_id",
        "label": "Market",
        "type": "query",
        "datasource": { "type": "postgres", "uid": "postgres" },
        "definition": "SELECT id AS __value, COALESCE(slug, question) AS __text FROM markets WHERE ('$status' = 'All') OR ('$status' = 'Open' AND NOT closed) OR ('$status' = 'Resolved' AND closed AND resolved_outcome IS NOT NULL) ORDER BY (end_date IS NULL), end_date DESC LIMIT 500",
        "query": "SELECT id AS __value, COALESCE(slug, question) AS __text FROM markets WHERE ('$status' = 'All') OR ('$status' = 'Open' AND NOT closed) OR ('$status' = 'Resolved' AND closed AND resolved_outcome IS NOT NULL) ORDER BY (end_date IS NULL), end_date DESC LIMIT 500",
        "refresh": 1,
        "sort": 0,
        "includeAll": false,
        "multi": false,
        "current": {}
      }
    ]
  },
  "time": { "from": "now-90d", "to": "now+30d" },
  "timepicker": {},
  "title": "Polymarket — Market Detail",
  "uid": "pm-market-detail",
  "version": 1,
  "panels": [
    {
      "id": 1,
      "title": "Overview",
      "type": "row",
      "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 0 },
      "panels": []
    },
    {
      "id": 2, "title": "Current belief", "type": "stat",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 4, "w": 5, "x": 0, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "percentunit", "decimals": 3 } },
      "targets": [ { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
        "rawSql": "SELECT current_score FROM markets WHERE id = '$market_id'" } ]
    },
    {
      "id": 3, "title": "Market price (latest)", "type": "stat",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 4, "w": 5, "x": 5, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "percentunit", "decimals": 3 } },
      "targets": [ { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
        "rawSql": "SELECT yes_price FROM market_prices WHERE market_id = '$market_id' ORDER BY ts DESC LIMIT 1" } ]
    },
    {
      "id": 4, "title": "Days to resolution", "type": "stat",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 4, "w": 5, "x": 10, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "none", "decimals": 1 } },
      "targets": [ { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
        "rawSql": "SELECT EXTRACT(epoch FROM (end_date - now()))/86400 AS days FROM markets WHERE id = '$market_id'" } ]
    },
    {
      "id": 5, "title": "Belief updates", "type": "stat",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 4, "w": 4, "x": 15, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "none", "decimals": 0 } },
      "targets": [ { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
        "rawSql": "SELECT count(*) FROM belief_updates WHERE market_id = '$market_id'" } ]
    },
    {
      "id": 6, "title": "Brier skill (resolved)", "type": "stat",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 4, "w": 5, "x": 19, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "none", "decimals": 3,
        "thresholds": { "mode": "absolute", "steps": [ { "color": "red", "value": null }, { "color": "yellow", "value": 0 }, { "color": "green", "value": 0.05 } ] } },
        "color": { "mode": "thresholds" } },
      "targets": [ { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
        "rawSql": "SELECT 1 - brier_belief/NULLIF(brier_baseline,0) AS skill FROM forecast_scores WHERE market_id = '$market_id'" } ]
    }
  ]
}
```

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
```

Expected: `VALID`.

- [ ] **Step 4: Reload Grafana and verify the dashboard provisioned without error**

```bash
docker compose -f docker-compose.yml restart grafana
sleep 5
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -i "market-detail\|provisioning error\|invalid" | tail -20
curl -s -u "admin:${GF_SECURITY_ADMIN_PASSWORD:-admin}" "http://127.0.0.1:3000/api/dashboards/uid/pm-market-detail" | python3 -m json.tool | head -5
```

Expected: no provisioning errors in logs; the `curl` returns a JSON dashboard object (not a 404 `{"message":"Dashboard not found"}`).

- [ ] **Step 5: Visual check**

Open `http://127.0.0.1:3000/d/pm-market-detail`. Expected: the **Market** dropdown is populated with slugs, the **Status** dropdown offers All/Open/Resolved, and the five stat tiles show values (not "No data") for a selected open market. This is the concrete confirmation that the original "No data" bug is fixed.

- [ ] **Step 6: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): scaffold per-market detail dashboard (slug selector + stats), fixes No-data"
```

---

## Task 3: Trajectory panel — belief vs market price vs outcome

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/market-detail.json` (append panel id 10 to the `panels` array)

**Interfaces:**
- Consumes: variable `$market_id`, the `resolution` annotation from Task 2.
- Produces: timeseries panel id 10 at `y: 5`.

- [ ] **Step 1: Validate the three series queries against Postgres (the "test")**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
MID=<OPEN_MID from Task 1>
# market price
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT ts AS \"time\", yes_price AS \"market price\" FROM market_prices WHERE market_id='$MID' ORDER BY ts LIMIT 5;"
# our belief (seed + step updates)
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT time, \"our belief\" FROM ( SELECT (SELECT min(ts) FROM market_prices WHERE market_id='$MID') AS time, (SELECT seed_price FROM markets WHERE id='$MID') AS \"our belief\" UNION ALL SELECT ts, new_score FROM belief_updates WHERE market_id='$MID' ) b ORDER BY time LIMIT 5;"
# outcome (empty for open markets, one/two rows for resolved)
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT resolved_outcome AS outcome FROM markets WHERE id='$MID';"
```

Expected: market price + belief return rows (belief always has ≥1 row from the seed). Outcome is NULL for an open market — correct (the series draws nothing).

- [ ] **Step 2: Append panel id 10 to the `panels` array**

Insert this object as the last element of the `panels` array in `market-detail.json` (add a comma after the current last panel — id 6):

```json
    {
      "id": 10,
      "title": "Belief vs market price vs outcome",
      "description": "Blue 'our belief' is a step function — it moves only when the worker re-evaluates on news. Orange 'market price' is Polymarket's own price. Gray dashed 'outcome' is the 0/1 the market settled at (absent until resolved). The red annotation marks end_date: the gap between the last point and that line is the time left to resolution.",
      "type": "timeseries",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 10, "w": 24, "x": 0, "y": 5 },
      "fieldConfig": {
        "defaults": {
          "unit": "percentunit", "min": 0, "max": 1,
          "custom": { "drawStyle": "line", "fillOpacity": 0, "lineWidth": 2, "showPoints": "never", "lineInterpolation": "linear" }
        },
        "overrides": [
          { "matcher": { "id": "byName", "options": "our belief" }, "properties": [
            { "id": "custom.lineInterpolation", "value": "stepAfter" },
            { "id": "color", "value": { "mode": "fixed", "fixedColor": "blue" } } ] },
          { "matcher": { "id": "byName", "options": "market price" }, "properties": [
            { "id": "color", "value": { "mode": "fixed", "fixedColor": "orange" } } ] },
          { "matcher": { "id": "byName", "options": "outcome" }, "properties": [
            { "id": "custom.lineStyle", "value": { "dash": [10, 10], "fill": "dash" } },
            { "id": "color", "value": { "mode": "fixed", "fixedColor": "gray" } } ] }
        ]
      },
      "options": { "legend": { "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "multi" } },
      "targets": [
        { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "time_series",
          "rawSql": "SELECT ts AS \"time\", yes_price AS \"market price\" FROM market_prices WHERE market_id = '$market_id' AND $__timeFilter(ts) ORDER BY ts" },
        { "refId": "B", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "time_series",
          "rawSql": "SELECT time, \"our belief\" FROM ( SELECT (SELECT min(ts) FROM market_prices WHERE market_id = '$market_id') AS time, (SELECT seed_price FROM markets WHERE id = '$market_id') AS \"our belief\" UNION ALL SELECT ts AS time, new_score AS \"our belief\" FROM belief_updates WHERE market_id = '$market_id' ) belief WHERE $__timeFilter(time) ORDER BY time" },
        { "refId": "C", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "time_series",
          "rawSql": "SELECT $__timeFrom() AS \"time\", resolved_outcome AS \"outcome\" FROM markets WHERE id = '$market_id' UNION ALL SELECT $__timeTo(), resolved_outcome FROM markets WHERE id = '$market_id' ORDER BY time" }
      ]
    }
```

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
```

Expected: `VALID`.

- [ ] **Step 4: Reload and verify**

```bash
docker compose -f docker-compose.yml restart grafana && sleep 5
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -i "market-detail\|error" | tail -10
```

Open `http://127.0.0.1:3000/d/pm-market-detail`, select an **open** market. Expected: orange price line and blue stepped belief line render; belief holds flat to the right edge; a red vertical `resolution` line appears at `end_date`; no outcome line; no "No data". If a resolved market is available (`RES_MID`), select it and confirm a gray dashed outcome line appears.

- [ ] **Step 5: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): add belief-vs-price trajectory panel with resolution marker"
```

---

## Task 4: Edge panel — who's closer to the outcome (resolved markets)

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/market-detail.json` (append panel id 11)

**Interfaces:**
- Consumes: variable `$market_id`.
- Produces: timeseries panel id 11 at `y: 15`.

- [ ] **Step 1: Validate the edge query against Postgres (the "test")**

Use a resolved market id if available; otherwise run against `OPEN_MID` to confirm the SQL is valid (it returns 0 rows because `resolved_outcome IS NULL` — expected).

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
MID=<RES_MID if available, else OPEN_MID>
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT mp.ts AS \"time\", abs(m.resolved_outcome - mp.yes_price) - abs(m.resolved_outcome - COALESCE(b.belief, m.seed_price)) AS \"edge\" FROM market_prices mp JOIN markets m ON m.id = mp.market_id LEFT JOIN LATERAL ( SELECT new_score AS belief FROM belief_updates bu WHERE bu.market_id = mp.market_id AND bu.ts <= mp.ts ORDER BY bu.ts DESC LIMIT 1 ) b ON true WHERE mp.market_id='$MID' AND m.resolved_outcome IS NOT NULL ORDER BY mp.ts LIMIT 10;"
```

Expected: for a resolved market, rows with an `edge` value roughly in `[-1, 1]` (positive = we beat the market at that instant). For an open market, `(0 rows)` — correct.

- [ ] **Step 2: Append panel id 11**

Insert as the last element of the `panels` array (comma after panel id 10):

```json
    {
      "id": 11,
      "title": "Edge over the market (belief error advantage)",
      "description": "edge = |market − outcome| − |belief − outcome|, sampled at each price observation using the belief in effect at that moment. Positive (green) = our belief was closer to the truth than the market; negative (red) = the market was closer. Empty until the market resolves.",
      "type": "timeseries",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 6, "w": 24, "x": 0, "y": 15 },
      "fieldConfig": {
        "defaults": {
          "unit": "none", "decimals": 3,
          "custom": { "drawStyle": "line", "lineWidth": 2, "fillOpacity": 25, "gradientMode": "scheme", "showPoints": "never", "lineInterpolation": "linear", "thresholdsStyle": { "mode": "line" } },
          "color": { "mode": "thresholds" },
          "thresholds": { "mode": "absolute", "steps": [ { "color": "red", "value": null }, { "color": "green", "value": 0 } ] }
        },
        "overrides": []
      },
      "options": { "legend": { "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single" } },
      "targets": [
        { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "time_series",
          "rawSql": "SELECT mp.ts AS \"time\", abs(m.resolved_outcome - mp.yes_price) - abs(m.resolved_outcome - COALESCE(b.belief, m.seed_price)) AS \"edge\" FROM market_prices mp JOIN markets m ON m.id = mp.market_id LEFT JOIN LATERAL ( SELECT new_score AS belief FROM belief_updates bu WHERE bu.market_id = mp.market_id AND bu.ts <= mp.ts ORDER BY bu.ts DESC LIMIT 1 ) b ON true WHERE mp.market_id = '$market_id' AND m.resolved_outcome IS NOT NULL AND $__timeFilter(mp.ts) ORDER BY mp.ts" }
      ]
    }
```

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
```

Expected: `VALID`.

- [ ] **Step 4: Reload and verify**

```bash
docker compose -f docker-compose.yml restart grafana && sleep 5
```

Open the dashboard. For a **resolved** market: the edge line is green where above 0, red where below 0, with gradient fill and a zero threshold line. For an **open** market: the panel is empty (no outcome yet) — expected, not a bug. If no resolved market exists yet, note that the color rendering is verified once one resolves; the SQL and thresholds are already confirmed.

- [ ] **Step 5: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): add threshold-colored edge panel (belief vs market, resolved)"
```

---

## Task 5: Reasoning log table

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/market-detail.json` (append panel id 12)

**Interfaces:**
- Consumes: variable `$market_id`.
- Produces: table panel id 12 at `y: 21`.

- [ ] **Step 1: Validate the query (the "test")**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
MID=<OPEN_MID from Task 1>
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c \
"SELECT ts AS \"time\", previous_score AS \"from\", new_score AS \"to\", article_url AS \"article\", reasoning FROM belief_updates WHERE market_id='$MID' ORDER BY ts DESC LIMIT 5;"
```

Expected: rows for a market the worker has evaluated (0 rows if it was never touched — pick a market with `n_updates > 0` from `SELECT market_id, count(*) FROM belief_updates GROUP BY 1 ORDER BY 2 DESC LIMIT 1;` to see a populated table).

- [ ] **Step 2: Append panel id 12**

Insert as the last element of the `panels` array (comma after panel id 11):

```json
    {
      "id": 12,
      "title": "Reasoning log — why the belief moved",
      "description": "Every belief update for the selected market, newest first: the score before and after, the article that triggered it, and the model's reasoning.",
      "type": "table",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 9, "w": 24, "x": 0, "y": 21 },
      "fieldConfig": {
        "defaults": { "custom": { "align": "left", "cellOptions": { "type": "auto", "wrapText": true } } },
        "overrides": [
          { "matcher": { "id": "byName", "options": "from" }, "properties": [ { "id": "unit", "value": "percentunit" }, { "id": "decimals", "value": 3 }, { "id": "custom.width", "value": 90 } ] },
          { "matcher": { "id": "byName", "options": "to" }, "properties": [ { "id": "unit", "value": "percentunit" }, { "id": "decimals", "value": 3 }, { "id": "custom.width", "value": 90 } ] },
          { "matcher": { "id": "byName", "options": "article" }, "properties": [ { "id": "custom.cellOptions", "value": { "type": "markdown" } }, { "id": "custom.width", "value": 260 } ] }
        ]
      },
      "options": { "showHeader": true, "sortBy": [ { "displayName": "time", "desc": true } ] },
      "targets": [
        { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
          "rawSql": "SELECT ts AS \"time\", previous_score AS \"from\", new_score AS \"to\", '[link](' || article_url || ')' AS \"article\", reasoning FROM belief_updates WHERE market_id = '$market_id' ORDER BY ts DESC" }
      ]
    }
```

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
```

Expected: `VALID`.

- [ ] **Step 4: Reload and verify**

```bash
docker compose -f docker-compose.yml restart grafana && sleep 5
```

Open the dashboard, select a market with belief updates. Expected: a table of updates newest-first, `from`/`to` as percentages, `article` as a clickable link, `reasoning` wrapped.

- [ ] **Step 5: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): add reasoning-log table to market-detail dashboard"
```

---

## Task 6: Remove the single-market drill-down from accuracy.json

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/accuracy.json` (remove the row panel id 7 and timeseries panel id 8, and the now-unused `market_id` template variable)

**Interfaces:**
- Consumes: nothing.
- Produces: `accuracy.json` reduced to aggregate-only panels.

- [ ] **Step 1: Confirm what to remove**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
grep -n '"id": 7\|"id": 8\|Market detail\|convergence\|Our belief vs market' monitoring/grafana/dashboards/accuracy.json
```

Expected: locates the row panel (id 7, title "Market detail — convergence race") and the timeseries (id 8, title "Our belief vs market price vs outcome").

- [ ] **Step 2: Remove both panel objects**

Edit `monitoring/grafana/dashboards/accuracy.json`: delete the two panel objects with `"id": 7` and `"id": 8` from the `panels` array (the row and the timeseries following it), and delete the trailing comma so the array stays valid JSON. Also remove the now-unused `market_id` entry from `templating.list` (accuracy.json's aggregate panels use Prometheus, not this variable).

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool monitoring/grafana/dashboards/accuracy.json > /dev/null && echo VALID
```

Expected: `VALID`.

- [ ] **Step 4: Reload and verify accuracy.json still loads and no longer has the drill-down**

```bash
docker compose -f docker-compose.yml restart grafana && sleep 5
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -i "accuracy\|error" | tail -10
curl -s -u "admin:${GF_SECURITY_ADMIN_PASSWORD:-admin}" "http://127.0.0.1:3000/api/dashboards/uid/pm-accuracy" | grep -c "convergence"
```

Expected: no provisioning errors; the `grep -c` prints `0` (the drill-down is gone). Open `http://127.0.0.1:3000/d/pm-accuracy` and confirm the aggregate panels still render.

- [ ] **Step 5: Commit**

```bash
git add monitoring/grafana/dashboards/accuracy.json
git commit -m "refactor(grafana): move per-market drill-down out of accuracy dashboard"
```

---

## Final verification (after all tasks)

- [ ] Both dashboards present and error-free:

```bash
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -i "provisioning error\|invalid dashboard" ; echo "exit: $?"
curl -s -u "admin:${GF_SECURITY_ADMIN_PASSWORD:-admin}" "http://127.0.0.1:3000/api/search?query=Polymarket" | python3 -m json.tool
```

Expected: no error lines; search lists both "Polymarket Forecast Accuracy" and "Polymarket — Market Detail".

- [ ] Manual pass: open `pm-market-detail`, switch the Status filter, pick an open and (if available) a resolved market, confirm every panel behaves per the spec's "Testing / verification" section.
