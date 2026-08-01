# One-Click Polymarket Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-click "Open on Polymarket" link (showing the market slug) to the market-detail dashboard, in its own thin row.

**Architecture:** Presentation-only change to one Grafana dashboard file. A new stat panel queries the selected market's `slug` and carries a Grafana data link to `https://polymarket.com/market/<slug>` (new tab). No schema/service/datasource/template-variable changes.

**Tech Stack:** Grafana (provisioned dashboard, Postgres datasource uid `postgres`), Docker Compose. Verification via `python3 -m json.tool` (JSON validity) and the local Grafana UI (rendered data-link href).

## Global Constraints

- Only file touched: `part-3/monitoring/grafana/dashboards/market-detail.json`.
- Link base is `https://polymarket.com/market/` + `markets.slug` (verified: `/market/<slug>` resolves; `/event/<slug>` 404s). Data link must open in a new tab (`targetBlank: true`).
- The new panel is its **own thin full-width row** — do NOT alter the existing 5-tile overview stat row (ids 2–6).
- **Verify against the LOCAL Grafana at `http://127.0.0.1:3000` (IPv4 literal).** Do NOT use `http://localhost:3000` — it may resolve to IPv6 `[::1]`, an SSH tunnel to the remote **prod** Grafana. (See memory `grafana-localhost-is-ssh-tunnel`.) Never modify the remote.
- Datasource uid `postgres`; new panel query format `table`.

---

## Conventions

Run from `part-3/`:

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
```

Local Grafana login `admin` / `admin` (unless `GF_SECURITY_ADMIN_PASSWORD` is set in the shell). The local stack should be up (`docker compose -f docker-compose.yml up -d postgres grafana`). Local fixtures include `verify-open` (slug `team-a-championship-open`) and `verify-resolved` (slug `merger-close-q2-resolved`).

---

## Task 1: Add the "Open on Polymarket" stat panel in its own row

**Files:**
- Modify: `part-3/monitoring/grafana/dashboards/market-detail.json`

**Interfaces:**
- Consumes: existing `$market_id` template variable; `markets.slug` column.
- Produces: stat panel id 7 at `y:5 h:3 w:24` with a data link to Polymarket; the three panels below (ids 10, 11, 12) shift down by 3.

- [ ] **Step 1: Confirm the slug query returns a bare slug**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
docker compose -f docker-compose.yml exec -T postgres psql -U pm -d pm -qtA -c \
"SELECT slug FROM markets WHERE id = 'verify-open';"
```

Expected: `team-a-championship-open` (a bare slug, no decoration). This is the value the data link will append to the Polymarket base URL.

- [ ] **Step 2: Insert the new panel and shift the panels below**

In `market-detail.json`'s `panels` array, insert this object immediately after the panel with `"id": 6` (Brier skill) and before `"id": 10`:

```json
    {
      "id": 7,
      "title": "↗ Open on Polymarket",
      "description": "Opens the selected market on Polymarket in a new tab. The value shown is the market slug (also selectable/copyable). URL: https://polymarket.com/market/<slug>.",
      "type": "stat",
      "datasource": { "type": "postgres", "uid": "postgres" },
      "gridPos": { "h": 3, "w": 24, "x": 0, "y": 5 },
      "fieldConfig": {
        "defaults": {
          "color": { "mode": "fixed", "fixedColor": "blue" },
          "links": [
            { "title": "Open on Polymarket", "url": "https://polymarket.com/market/${__data.fields.slug}", "targetBlank": true }
          ]
        },
        "overrides": []
      },
      "options": {
        "colorMode": "none",
        "graphMode": "none",
        "textMode": "value",
        "justifyMode": "auto",
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false }
      },
      "targets": [
        { "refId": "A", "datasource": { "type": "postgres", "uid": "postgres" }, "format": "table",
          "rawSql": "SELECT slug FROM markets WHERE id = '$market_id'" }
      ]
    },
```

Then update the `gridPos.y` of the three panels below (leave every other field unchanged):
- panel `"id": 10` (Belief vs market price vs outcome): `"y": 5` → `"y": 8`
- panel `"id": 11` (Edge over the market): `"y": 15` → `"y": 18`
- panel `"id": 12` (Reasoning log): `"y": 21` → `"y": 24`

- [ ] **Step 3: Validate JSON and layout**

```bash
cd /Users/zzibert/personal/claude-polymarket-engineer/part-3
python3 -m json.tool monitoring/grafana/dashboards/market-detail.json > /dev/null && echo VALID
python3 -c "
import json; d=json.load(open('monitoring/grafana/dashboards/market-detail.json'))
byid={p['id']:p for p in d['panels']}
p7=byid[7]
assert p7['type']=='stat' and p7['gridPos']=={'h':3,'w':24,'x':0,'y':5}, p7['gridPos']
assert p7['fieldConfig']['defaults']['links'][0]['url']=='https://polymarket.com/market/\${__data.fields.slug}'
assert p7['fieldConfig']['defaults']['links'][0]['targetBlank'] is True
assert p7['targets'][0]['rawSql']=='SELECT slug FROM markets WHERE id = \'\$market_id\''
assert byid[10]['gridPos']['y']==8 and byid[11]['gridPos']['y']==18 and byid[12]['gridPos']['y']==24
# overview stat row untouched
assert byid[2]['gridPos']['y']==1 and byid[6]['gridPos']['y']==1
print('OK layout + link')
"
```

Expected: `VALID` then `OK layout + link`.

- [ ] **Step 4: Provision and verify the rendered data-link href**

```bash
docker compose -f docker-compose.yml restart grafana >/dev/null 2>&1
for i in $(seq 1 30); do s=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:3000/api/health"); [ "$s" = "200" ] && break; sleep 2; done
docker compose -f docker-compose.yml logs grafana 2>&1 | grep -iE "market-detail|provision.*error|invalid" | grep -iv "up to date" | tail -10
echo "(no error lines above = good)"
```

Then open **`http://127.0.0.1:3000/d/pm-market-detail?var-market_id=verify-open&var-status=All&var-activity=all`** (IPv4 literal — NOT `localhost`). Confirm:
- A thin "↗ Open on Polymarket" panel appears on its own row, directly below the five overview stat tiles and above the "Belief vs market price vs outcome" chart.
- The panel shows the slug `team-a-championship-open`.
- Inspect the panel's link (hover the value / use the browser's element inspector or the page accessibility tree) and confirm the href is exactly `https://polymarket.com/market/team-a-championship-open` and opens in a new tab.
- The overview tiles, trajectory, edge, and reasoning-log panels all still render and don't overlap.

If the href shows the literal `${__data.fields.slug}` or an empty/wrong value instead of the slug, change the data-link URL in the panel to use `https://polymarket.com/market/${__value.text}` and repeat Steps 3–4. Use whichever token produces the correct bare-slug href.

- [ ] **Step 5: Commit**

```bash
git add part-3/monitoring/grafana/dashboards/market-detail.json
git commit -m "feat(grafana): add one-click Polymarket link to market-detail dashboard"
```

---

## Final verification

- [ ] `python3 -m json.tool part-3/monitoring/grafana/dashboards/market-detail.json > /dev/null` passes.
- [ ] On local Grafana `pm-market-detail`: the "↗ Open on Polymarket" row renders with the selected market's slug and a working new-tab data link to `https://polymarket.com/market/<slug>`; switching `$market_id` updates the slug/link; no other panel regresses.
