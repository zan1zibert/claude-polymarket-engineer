# Market-Detail Selector Sort & Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the market-detail dashboard's `$market_id` selector sort markets by belief-update count or divergence and filter to markets with ≥1 belief update.

**Architecture:** Presentation-only change to one Grafana dashboard file. Two new custom template variables (`$activity`, `$sort`) feed a rewritten `$market_id` query that LEFT JOINs a belief-update count and the latest price, filters on status + activity, and orders via a `CASE`-guarded `ORDER BY`. No schema/service/datasource changes.

**Tech Stack:** Grafana (provisioned dashboard, Postgres datasource uid `postgres`), Postgres 16, Docker Compose. Verification via `psql` (SQL correctness), `python3 -m json.tool` (JSON validity), and the local Grafana UI (render/interaction).

## Global Constraints

- Only file touched: `part-3/monitoring/grafana/dashboards/market-detail.json`.
- **Verify against the LOCAL Grafana at `http://127.0.0.1:3000` (IPv4 literal).** Do NOT use `http://localhost:3000` — `localhost` may resolve to IPv6 `[::1]`, which on this machine is an SSH tunnel to the remote **prod** Grafana. (See memory `grafana-localhost-is-ssh-tunnel`.) Never modify the remote.
- Divergence is magnitude only: `abs(m.current_score - <latest yes_price>)`.
- Every SQL string uses datasource uid `postgres`. The `market_id` variable keeps `"sort": 0` so the query's `ORDER BY` wins (no Grafana-side re-sort).
- The `market_id` variable's `query` AND `definition` fields must be set to the identical SQL string.
- Defaults: `$sort` = `updates_desc` (`Belief updates ↓`), `$activity` = `active` (`Has belief updates`).
- Local Postgres access: `docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm` (run from `part-3/`).

---

## Conventions used by verification steps

Run from `part-3/`:

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
psqlc() { docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA "$@"; }
```

Local Grafana login is `admin` / `admin` (fresh volume) unless a `GF_SECURITY_ADMIN_PASSWORD` is set in the shell.

---

## Task 1: Seed deterministic fixtures and validate the selector SQL

**Files:**
- None modified (test-data setup + SQL validation only).

**Interfaces:**
- Produces: a local fixture set that deterministically exercises the filter and both sorts, and a verified SQL string reused verbatim in Task 2.

- [ ] **Step 1: Ensure the stack is up and note current market count**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
docker compose -f docker-compose.yml up -d postgres grafana >/dev/null 2>&1
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c "SELECT count(*) FROM markets;"
```

Expected: a number ≥ 0 (local dev DB; likely just the `verify-*` fixtures).

- [ ] **Step 2: Seed fixtures that vary update-count and divergence**

Idempotent inserts (zero-vector embedding satisfies the NOT NULL `vector(1024)` column; it is irrelevant to this feature). This creates: a 0-update market with large divergence, and a 1-update market — complementing the existing 2-update `verify-open`/`verify-resolved`.

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm <<'SQL'
CREATE OR REPLACE FUNCTION pg_temp.zvec() RETURNS vector LANGUAGE sql AS
  $$ SELECT ('[' || rtrim(repeat('0,',1024),',') || ']')::vector $$;

-- 0 belief updates, big divergence (belief 0.50 vs price 0.90 -> 0.40)
INSERT INTO markets (id, question, description, end_date, current_score, seed_price, slug, closed, embedding)
VALUES ('verify-noupd', 'Zero-update fixture', 'no belief updates', now() + interval '10 days',
        0.50, 0.50, 'verify-zero-update-market', false, pg_temp.zvec())
ON CONFLICT (id) DO UPDATE SET current_score=EXCLUDED.current_score, slug=EXCLUDED.slug, closed=EXCLUDED.closed, end_date=EXCLUDED.end_date;
DELETE FROM market_prices WHERE market_id='verify-noupd';
INSERT INTO market_prices (market_id, ts, yes_price) VALUES ('verify-noupd', now() - interval '1 day', 0.90);

-- 1 belief update, small divergence (belief 0.30 vs price 0.35 -> 0.05)
INSERT INTO markets (id, question, description, end_date, current_score, seed_price, slug, closed, embedding)
VALUES ('verify-oneupd', 'One-update fixture', 'one belief update', now() + interval '20 days',
        0.30, 0.25, 'verify-one-update-market', false, pg_temp.zvec())
ON CONFLICT (id) DO UPDATE SET current_score=EXCLUDED.current_score, slug=EXCLUDED.slug, closed=EXCLUDED.closed, end_date=EXCLUDED.end_date;
DELETE FROM market_prices WHERE market_id='verify-oneupd';
INSERT INTO market_prices (market_id, ts, yes_price) VALUES ('verify-oneupd', now() - interval '1 day', 0.35);
DELETE FROM belief_updates WHERE market_id='verify-oneupd';
INSERT INTO belief_updates (ts, market_id, market_title, previous_score, new_score, article_url, reasoning)
VALUES (now() - interval '5 days', 'verify-oneupd', 'One-update fixture', 0.25, 0.30, 'https://example.com/o1', 'single update');

SELECT m.id, COALESCE(u.n,0) AS updates, m.current_score,
       (SELECT yes_price FROM market_prices p WHERE p.market_id=m.id ORDER BY ts DESC LIMIT 1) AS price
FROM markets m LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u ON u.market_id=m.id
WHERE m.id LIKE 'verify-%' ORDER BY updates DESC;
SQL
```

Expected: lists the `verify-*` markets with their update counts (e.g. `verify-open`/`verify-resolved` = 2, `verify-oneupd` = 1, `verify-noupd` = 0).

- [ ] **Step 3: Validate — `Belief updates ↓` sort over active markets (the primary view)**

This is the exact query from Task 2 with `$status='All'`, `$activity='active'`, `$sort='updates_desc'` substituted:

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c "
SELECT m.id AS __value,
       COALESCE(m.slug, m.question)
         || '  · ' || COALESCE(u.n,0) || ' upd'
         || CASE WHEN px.yes_price IS NOT NULL AND m.current_score IS NOT NULL
                 THEN '  · '||chr(916)|| to_char(abs(m.current_score - px.yes_price),'FM0.00')
                 ELSE '' END AS __text
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u ON u.market_id = m.id
LEFT JOIN LATERAL (SELECT yes_price FROM market_prices p WHERE p.market_id = m.id ORDER BY ts DESC LIMIT 1) px ON true
WHERE (('All'='All') OR ('All'='Open' AND NOT m.closed) OR ('All'='Resolved' AND m.closed AND m.resolved_outcome IS NOT NULL))
  AND ('active'='all' OR COALESCE(u.n,0) > 0)
ORDER BY
  CASE WHEN 'updates_desc'='updates_desc' THEN COALESCE(u.n,0) END DESC NULLS LAST,
  CASE WHEN 'updates_desc'='updates_asc'  THEN COALESCE(u.n,0) END ASC  NULLS LAST,
  CASE WHEN 'updates_desc'='div_desc'     THEN abs(m.current_score - px.yes_price) END DESC NULLS LAST,
  CASE WHEN 'updates_desc'='div_asc'      THEN abs(m.current_score - px.yes_price) END ASC  NULLS LAST,
  (m.end_date IS NULL), m.end_date DESC
LIMIT 500;"
```

Expected: `verify-noupd` is **absent** (0 updates filtered out); remaining rows ordered by update count descending (the two 2-update markets first, then `verify-oneupd`); each label ends with `· N upd` and a `· Δx.xx` (the `chr(916)` is `Δ`).

- [ ] **Step 4: Validate — `Divergence ↓` over all markets**

Substitute `$status='All'`, `$activity='all'`, `$sort='div_desc'`:

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c "
SELECT COALESCE(m.slug,m.question) AS slug, COALESCE(u.n,0) AS upd,
       round(abs(m.current_score - px.yes_price)::numeric,3) AS divergence
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u ON u.market_id = m.id
LEFT JOIN LATERAL (SELECT yes_price FROM market_prices p WHERE p.market_id = m.id ORDER BY ts DESC LIMIT 1) px ON true
WHERE ('all'='all' OR COALESCE(u.n,0) > 0)
ORDER BY
  CASE WHEN 'div_desc'='updates_desc' THEN COALESCE(u.n,0) END DESC NULLS LAST,
  CASE WHEN 'div_desc'='updates_asc'  THEN COALESCE(u.n,0) END ASC  NULLS LAST,
  CASE WHEN 'div_desc'='div_desc'     THEN abs(m.current_score - px.yes_price) END DESC NULLS LAST,
  CASE WHEN 'div_desc'='div_asc'      THEN abs(m.current_score - px.yes_price) END ASC  NULLS LAST,
  (m.end_date IS NULL), m.end_date DESC
LIMIT 500;"
```

Expected: `verify-noupd` is **present** (activity=all) and appears **first** (divergence ≈ 0.400, the largest); markets with NULL divergence (no price/belief) sort to the bottom.

- [ ] **Step 5: Validate — `Recent` sort matches the old ordering**

Substitute `$sort='recent'`, `$activity='all'`, `$status='All'`; confirm it orders by `end_date DESC` with NULLs first-guarded, i.e. same as the pre-change behavior:

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c "
SELECT COALESCE(m.slug,m.question) AS slug, m.end_date
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u ON u.market_id=m.id
WHERE ('all'='all' OR COALESCE(u.n,0)>0)
ORDER BY
  CASE WHEN 'recent'='updates_desc' THEN COALESCE(u.n,0) END DESC NULLS LAST,
  CASE WHEN 'recent'='updates_asc'  THEN COALESCE(u.n,0) END ASC  NULLS LAST,
  (m.end_date IS NULL), m.end_date DESC
LIMIT 500;"
```

Expected: ordered by `end_date` descending, NULL end_dates last. No error.

- [ ] **Step 6: Commit the fixture note** (no code yet)

```bash
git commit --allow-empty -m "chore: validate market-selector sort/filter SQL against local fixtures"
```

---

## Task 2: Add the variables, rewrite the query, verify in Grafana

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/market-detail.json` (add `activity` + `sort` template variables; replace the `market_id` `query`/`definition`; new defaults)

**Interfaces:**
- Consumes: the SQL validated in Task 1 (used verbatim as the `market_id` query).
- Produces: the finished selector; no downstream tasks.

- [ ] **Step 1: Add the `activity` and `sort` variables**

In `market-detail.json`, `templating.list` currently holds `status` then `market_id`. Insert these two objects **between** `status` and `market_id`:

```json
    {
      "name": "activity",
      "label": "Activity",
      "type": "custom",
      "query": "All markets : all,Has belief updates : active",
      "current": { "text": "Has belief updates", "value": "active" },
      "options": [
        { "text": "All markets", "value": "all", "selected": false },
        { "text": "Has belief updates", "value": "active", "selected": true }
      ]
    },
    {
      "name": "sort",
      "label": "Sort",
      "type": "custom",
      "query": "Recent : recent,Belief updates ↓ : updates_desc,Belief updates ↑ : updates_asc,Divergence ↓ : div_desc,Divergence ↑ : div_asc",
      "current": { "text": "Belief updates ↓", "value": "updates_desc" },
      "options": [
        { "text": "Recent", "value": "recent", "selected": false },
        { "text": "Belief updates ↓", "value": "updates_desc", "selected": true },
        { "text": "Belief updates ↑", "value": "updates_asc", "selected": false },
        { "text": "Divergence ↓", "value": "div_desc", "selected": false },
        { "text": "Divergence ↑", "value": "div_asc", "selected": false }
      ]
    },
```

- [ ] **Step 2: Replace the `market_id` `query` and `definition`**

Set BOTH the `"query"` and `"definition"` fields of the `market_id` variable to this exact one-line string (identical for both):

```
SELECT m.id AS __value, COALESCE(m.slug, m.question) || '  · ' || COALESCE(u.n,0) || ' upd' || CASE WHEN px.yes_price IS NOT NULL AND m.current_score IS NOT NULL THEN '  · Δ' || to_char(abs(m.current_score - px.yes_price),'FM0.00') ELSE '' END AS __text FROM markets m LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u ON u.market_id = m.id LEFT JOIN LATERAL (SELECT yes_price FROM market_prices p WHERE p.market_id = m.id ORDER BY ts DESC LIMIT 1) px ON true WHERE (('$status'='All') OR ('$status'='Open' AND NOT m.closed) OR ('$status'='Resolved' AND m.closed AND m.resolved_outcome IS NOT NULL)) AND ('$activity'='all' OR COALESCE(u.n,0) > 0) ORDER BY CASE WHEN '$sort'='updates_desc' THEN COALESCE(u.n,0) END DESC NULLS LAST, CASE WHEN '$sort'='updates_asc' THEN COALESCE(u.n,0) END ASC NULLS LAST, CASE WHEN '$sort'='div_desc' THEN abs(m.current_score - px.yes_price) END DESC NULLS LAST, CASE WHEN '$sort'='div_asc' THEN abs(m.current_score - px.yes_price) END ASC NULLS LAST, (m.end_date IS NULL), m.end_date DESC LIMIT 500
```

Leave `market_id`'s other fields unchanged (`"sort": 0`, `"refresh": 1`, `"current": {}`, datasource, etc.).

- [ ] **Step 3: Validate JSON**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
python3 -m json.tool monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
python3 -c "
import json; d=json.load(open('monitoring/grafana/dashboards/market-detail.json'))
names=[v['name'] for v in d['templating']['list']]
assert names==['status','activity','sort','market_id'], names
mid=[v for v in d['templating']['list'] if v['name']=='market_id'][0]
assert mid['query']==mid['definition'], 'query != definition'
assert \"'\\\$activity'\" in mid['query'] and \"'\\\$sort'\" in mid['query']
print('OK', names)
"
```

Expected: `VALID` then `OK ['status', 'activity', 'sort', 'market_id']`.

- [ ] **Step 4: Provision to local Grafana and confirm no error**

```bash
docker compose -f docker-compose.yml restart grafana >/dev/null 2>&1
for i in $(seq 1 30); do s=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:3000/api/health"); [ "$s" = "200" ] && break; sleep 2; done
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -iE "market-detail|provision.*error|invalid" | grep -iv "up to date" | tail -10
echo "(no error lines above = good)"
```

Expected: health 200; no provisioning error for the dashboard.

- [ ] **Step 5: Verify render + interaction in the browser**

Open **`http://127.0.0.1:3000/d/pm-market-detail`** (IPv4 literal — NOT `localhost`). Confirm:
- Three control dropdowns appear: `Status`, `Activity`, `Sort` (plus `Market`).
- On load (`Activity=Has belief updates`, `Sort=Belief updates ↓`): the `Market` dropdown lists only markets with ≥1 update, highest count first; labels read like `verify-open · 2 upd · Δ0.04`; `verify-noupd` is absent.
- Switch `Activity=All markets`: `verify-noupd` now appears.
- Switch `Sort=Divergence ↓` with `Activity=All markets`: `verify-noupd` (Δ0.40) sorts to the top.
- Switch `Sort=Recent`: order matches end_date-descending.
- Pick any market: all panels (stats, trajectory, edge, reasoning log) still populate — no regression.

- [ ] **Step 6: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): sort/filter market-detail selector by belief updates and divergence"
```

---

## Final verification

- [ ] `python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null` passes.
- [ ] Local Grafana `pm-market-detail` loads with the three new controls and the documented default view, and every panel still renders for a selected market.
- [ ] (Optional) Remove the throwaway fixtures when done:
  `docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c "DELETE FROM belief_updates WHERE market_id IN ('verify-oneupd'); DELETE FROM market_prices WHERE market_id IN ('verify-noupd','verify-oneupd'); DELETE FROM markets WHERE id IN ('verify-noupd','verify-oneupd');"`
