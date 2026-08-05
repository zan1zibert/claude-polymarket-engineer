# Market-Detail Selector "Last Update" Sort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the market-detail dashboard's `$market_id` selector sort markets by the timestamp of their most recent belief update (`Last update ↓/↑`), in addition to the existing sort criteria.

**Architecture:** Presentation-only change to one Grafana dashboard file. Extends the existing per-market belief-update subquery (already aliased `u`, already computing `count(*) AS n`) to also compute `max(ts) AS last_update`. Adds two new `$sort` options (`upd_ts_desc`, `upd_ts_asc`) and two new `CASE`-guarded `ORDER BY` terms consuming that column, following the exact pattern already used by every other sort option. No schema/service/datasource changes.

**Tech Stack:** Grafana (provisioned dashboard, Postgres datasource uid `postgres`), Postgres 16, Docker Compose. Verification via `psql` (SQL correctness), `python3 -m json.tool` (JSON validity), and the local Grafana UI (render/interaction).

## Global Constraints

- Only file touched: `part-3/monitoring/grafana/dashboards/market-detail.json`.
- **Verify against the LOCAL Grafana at `http://127.0.0.1:3000` (IPv4 literal).** Do NOT use `http://localhost:3000` — `localhost` may resolve to IPv6 `[::1]`, which on this machine is an SSH tunnel to the remote **prod** Grafana. Never modify the remote.
- The `market_id` variable's `query` AND `definition` fields must be set to the identical SQL string (they must match after every edit).
- Do not change the meaning of the existing `Recent` (`end_date`) or `Belief updates ↓/↑` (count) options.
- Local Postgres access: `docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm` (run from `part-3/`).

---

## Conventions used by verification steps

Run from `part-3/`:

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
docker compose -f docker-compose.yml up -d postgres grafana >/dev/null 2>&1
```

Local Grafana login is `admin` / `admin` (fresh volume) unless a `GF_SECURITY_ADMIN_PASSWORD` is set in the shell.

---

## Task 1: Validate the `last_update` ORDER BY term against fixtures

**Files:**
- None modified (fixture setup + SQL validation only).

**Interfaces:**
- Produces: fixtures with distinct `belief_updates.ts` values, and a verified `ORDER BY` term reused verbatim in Task 2.

- [ ] **Step 1: Seed fixtures with distinct, known last-update timestamps**

Reuses the existing `verify-open`/`verify-resolved` (2 updates each) and `verify-oneupd` (1 update) fixtures from the prior sort/filter feature, plus `verify-noupd` (0 updates). This step only needs to pin down *when* each fixture's belief updates happened, so `last_update` ordering is deterministic:

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm <<'SQL'
-- verify-oneupd: last (only) update 5 days ago -> oldest of the updated fixtures
UPDATE belief_updates SET ts = now() - interval '5 days'
WHERE market_id = 'verify-oneupd';

-- verify-open: push its most recent update to 1 hour ago -> newest
UPDATE belief_updates SET ts = now() - interval '1 hour'
WHERE market_id = 'verify-open'
  AND ts = (SELECT max(ts) FROM belief_updates WHERE market_id = 'verify-open');

-- verify-resolved: push its most recent update to 2 days ago -> middle
UPDATE belief_updates SET ts = now() - interval '2 days'
WHERE market_id = 'verify-resolved'
  AND ts = (SELECT max(ts) FROM belief_updates WHERE market_id = 'verify-resolved');

SELECT market_id, max(ts) AS last_update
FROM belief_updates
WHERE market_id IN ('verify-open','verify-resolved','verify-oneupd')
GROUP BY market_id
ORDER BY last_update DESC;
SQL
```

Expected: three rows, ordered `verify-open` (~1 hour ago), `verify-resolved` (~2 days ago), `verify-oneupd` (~5 days ago). `verify-noupd` has no `belief_updates` rows at all.

- [ ] **Step 2: Validate `upd_ts_desc` (most-recently-updated first) over all markets**

This is the rewritten `u` subquery plus the two new `ORDER BY` terms, with `$sort='upd_ts_desc'` and `$activity='all'` substituted:

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c "
SELECT COALESCE(m.slug, m.question) AS slug, u.last_update
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n, max(ts) last_update FROM belief_updates GROUP BY market_id) u
       ON u.market_id = m.id
WHERE ('all'='all' OR COALESCE(u.n,0) > 0)
ORDER BY
  CASE WHEN 'upd_ts_desc'='upd_ts_desc' THEN u.last_update END DESC NULLS LAST,
  CASE WHEN 'upd_ts_desc'='upd_ts_asc'  THEN u.last_update END ASC  NULLS LAST,
  (m.end_date IS NULL), m.end_date DESC
LIMIT 500;"
```

Expected: `verify-open` first, then `verify-resolved`, then `verify-oneupd`; `verify-noupd` (and any other zero-update market) appears **after** all three, at the bottom (NULL `last_update`, `NULLS LAST`).

- [ ] **Step 3: Validate `upd_ts_asc` (least-recently-updated first) over active markets only**

Same query with `$sort='upd_ts_asc'`, `$activity='active'` (i.e. `COALESCE(u.n,0) > 0` filter applied):

```bash
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c "
SELECT COALESCE(m.slug, m.question) AS slug, u.last_update
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n, max(ts) last_update FROM belief_updates GROUP BY market_id) u
       ON u.market_id = m.id
WHERE ('active'='all' OR COALESCE(u.n,0) > 0)
ORDER BY
  CASE WHEN 'upd_ts_asc'='upd_ts_desc' THEN u.last_update END DESC NULLS LAST,
  CASE WHEN 'upd_ts_asc'='upd_ts_asc'  THEN u.last_update END ASC  NULLS LAST,
  (m.end_date IS NULL), m.end_date DESC
LIMIT 500;"
```

Expected: `verify-noupd` is **absent** (0 updates filtered out by `$activity='active'`); remaining rows ordered `verify-oneupd` (oldest, ~5 days ago) first, then `verify-resolved`, then `verify-open` last.

- [ ] **Step 4: Commit the fixture note** (no dashboard code yet)

```bash
git commit --allow-empty -m "chore: validate last-update sort ORDER BY term against local fixtures"
```

---

## Task 2: Add the `Last update` sort options, verify in Grafana

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/market-detail.json` (extend the `u` subquery in the `market_id` `query`/`definition`; add two options to the `sort` variable; add two `ORDER BY` terms)

**Interfaces:**
- Consumes: the SQL validated in Task 1 (`max(ts) last_update` subquery column, `upd_ts_desc`/`upd_ts_asc` `ORDER BY` terms).
- Produces: the finished selector; no downstream tasks.

- [ ] **Step 1: Add the two options to the `sort` variable, right after `Recent`**

In `market-detail.json`, the `sort` variable's `query` string currently is:

```
Recent : recent,Belief updates ↓ : updates_desc,Belief updates ↑ : updates_asc,Divergence ↓ : div_desc,Divergence ↑ : div_asc,Days to resolution ↓ : res_desc,Days to resolution ↑ : res_asc
```

Change it to (inserting the two new options immediately after `Recent : recent`):

```
Recent : recent,Last update ↓ : upd_ts_desc,Last update ↑ : upd_ts_asc,Belief updates ↓ : updates_desc,Belief updates ↑ : updates_asc,Divergence ↓ : div_desc,Divergence ↑ : div_asc,Days to resolution ↓ : res_desc,Days to resolution ↑ : res_asc
```

And insert these two entries into the `sort` variable's `options` array, immediately after the `{ "text": "Recent", "value": "recent", "selected": false }` entry:

```json
          { "text": "Last update ↓", "value": "upd_ts_desc", "selected": false },
          { "text": "Last update ↑", "value": "upd_ts_asc", "selected": false },
```

Leave `"current"` unchanged (`{ "text": "Belief updates ↓", "value": "updates_desc" }` stays the default).

- [ ] **Step 2: Extend the `u` subquery to compute `last_update`**

In BOTH the `query` and `definition` fields of the `market_id` variable, replace:

```
LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u ON u.market_id = m.id
```

with:

```
LEFT JOIN (SELECT market_id, count(*) n, max(ts) last_update FROM belief_updates GROUP BY market_id) u ON u.market_id = m.id
```

- [ ] **Step 3: Add the two `ORDER BY` terms to the `market_id` query AND definition**

In BOTH the `query` and `definition` fields, insert these two terms immediately before `CASE WHEN '$sort'='updates_desc' ...` (i.e. right after `ORDER BY`, before the existing terms):

```
CASE WHEN '$sort'='upd_ts_desc' THEN u.last_update END DESC NULLS LAST, CASE WHEN '$sort'='upd_ts_asc' THEN u.last_update END ASC NULLS LAST,
```

The `ORDER BY` clause of both strings becomes:

```
ORDER BY CASE WHEN '$sort'='upd_ts_desc' THEN u.last_update END DESC NULLS LAST, CASE WHEN '$sort'='upd_ts_asc' THEN u.last_update END ASC NULLS LAST, CASE WHEN '$sort'='updates_desc' THEN COALESCE(u.n,0) END DESC NULLS LAST, CASE WHEN '$sort'='updates_asc' THEN COALESCE(u.n,0) END ASC NULLS LAST, CASE WHEN '$sort'='div_desc' THEN abs(m.current_score - px.yes_price) END DESC NULLS LAST, CASE WHEN '$sort'='div_asc' THEN abs(m.current_score - px.yes_price) END ASC NULLS LAST, CASE WHEN '$sort'='res_desc' THEN m.end_date END DESC NULLS LAST, CASE WHEN '$sort'='res_asc' THEN m.end_date END ASC NULLS LAST, (m.end_date IS NULL), m.end_date DESC LIMIT 500
```

(The `FROM`/`WHERE` portion of the string is unchanged except for the `u` subquery edit from Step 2.)

- [ ] **Step 4: Validate JSON + consistency**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
python3 -m json.tool monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
python3 -c "
import json; d=json.load(open('monitoring/grafana/dashboards/market-detail.json'))
sortv=[v for v in d['templating']['list'] if v['name']=='sort'][0]
vals=[o['value'] for o in sortv['options']]
assert vals==['recent','upd_ts_desc','upd_ts_asc','updates_desc','updates_asc','div_desc','div_asc','res_desc','res_asc'], vals
mid=[v for v in d['templating']['list'] if v['name']=='market_id'][0]
assert mid['query']==mid['definition'], 'query != definition'
assert 'max(ts) last_update' in mid['query']
assert mid['query'].count('upd_ts_desc')==1 and mid['query'].count('upd_ts_asc')==1
print('OK', vals)
"
```

Expected: `VALID` then `OK ['recent', 'upd_ts_desc', 'upd_ts_asc', 'updates_desc', 'updates_asc', 'div_desc', 'div_asc', 'res_desc', 'res_asc']`.

- [ ] **Step 5: Provision to local Grafana and confirm no error**

```bash
docker compose -f docker-compose.yml restart grafana >/dev/null 2>&1
for i in $(seq 1 30); do s=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:3000/api/health"); [ "$s" = "200" ] && break; sleep 2; done
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -iE "market-detail|provision.*error|invalid" | grep -iv "up to date" | tail -10
echo "(no error lines above = good)"
```

Expected: health 200; no provisioning error for the dashboard.

- [ ] **Step 6: Verify render + interaction in the browser**

Open **`http://127.0.0.1:3000/d/pm-market-detail`** (IPv4 literal — NOT `localhost`). Confirm:
- The `Sort` dropdown now shows `Recent`, `Last update ↓`, `Last update ↑` (in that order) before `Belief updates ↓`, etc.
- With `Activity=All markets` and `Sort=Last update ↓`: `verify-open` appears first, then `verify-resolved`, then `verify-oneupd`, then `verify-noupd` (and any other zero-update market) last.
- With `Activity=Has belief updates` and `Sort=Last update ↑`: `verify-noupd` is absent; `verify-oneupd` appears first, then `verify-resolved`, then `verify-open` last.
- Pick any market: all panels (stats, trajectory, edge, reasoning log) still populate — no regression.

- [ ] **Step 7: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): add last-update sort options to market selector"
```

---

## Final verification

- [ ] `python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null` passes.
- [ ] Local Grafana `pm-market-detail` loads with `Last update ↓/↑` in the `Sort` dropdown, orders correctly in both directions, and every panel still renders for a selected market.
- [ ] (Optional) Reset the fixture timestamps touched in Task 1 if they should not persist:
  `docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -c "SELECT market_id, ts FROM belief_updates WHERE market_id IN ('verify-open','verify-resolved','verify-oneupd') ORDER BY market_id, ts;"` (review before deciding whether to touch — these are shared verification fixtures used by other dashboard features too).
