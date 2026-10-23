# local — Invidious on the tailnet, plus a sidecar

Everything in this directory is local-only. `master` stays a pure mirror of
upstream `iv-org/invidious`; all of our files live under `local/` on the `local`
branch, so pulling upstream can never conflict with them.

Nothing here edits an upstream file. Invidious runs as the stock prebuilt
container and is extended through two config hooks (`banner`,
`default_user_preferences.feed_menu`) plus a sidecar service that Caddy routes a
few paths to.

## Topology

```
tailnet --HTTPS--> tailscale serve :8444 --> 127.0.0.1:3080  caddy
                                              |- /feed/smart*, /feed/discover*, /x/*  --> sidecar:8000 (FastAPI + SQLite)
                                              `- everything else                      --> invidious:3000 --> companion:8282
                                                                                                              invidious-db:5432 (postgres 14)
sidecar --HTTP + bearer token--> invidious:3000/api/v1/...   (never touches Invidious's Postgres)
```

Caddy is the only container that publishes a host port, bound to
`127.0.0.1:3080`, so nothing is reachable from the LAN or the internet.

## Files

| Path | Purpose |
| --- | --- |
| `compose.yml` | the whole stack |
| `Caddyfile` | path routing between Invidious and the sidecar |
| `.env` | the only config file; gitignored (see `.env.example`) |
| `update.sh` | backup → pull → restart → health check |
| `sidecar/` | FastAPI service: collector, ranking, pages, tests |

## First start

```bash
cd local
cp .env.example .env      # then fill in the three secrets
chmod 600 .env
docker compose up -d --build
tailscale serve --bg --https=8444 http://127.0.0.1:3080
```

Open `https://steve-server.tail9d4465.ts.net:8444`, register, import
subscriptions (Settings → Import/export), turn watch history on in preferences.

Then close registration and hand the sidecar a token:

1. Set `REGISTRATION_ENABLED=false` in `.env`, `docker compose up -d invidious`.
2. Put your login in `.env` as `INVIDIOUS_TOKEN=`. Either kind works:
   - the `SID` cookie of your logged-in browser session (browser dev tools →
     Storage/Application → Cookies → `SID`). The sidecar sends it as a cookie.
     It stays valid until you log out of that session.
   - an API token from `/token_manager` with scopes
     **feed, subscriptions, history, preferences** (a JSON string). The sidecar
     sends it as `Authorization: Bearer`.
3. `docker compose up -d sidecar` — the collector starts on boot. A container
   keeps the environment it was created with, so after editing `.env` run
   `up -d sidecar` again; a plain restart keeps the old token.

`https://…/x/health` reports the last poll time and row counts.

## Updating

```bash
./update.sh
```

Dumps Postgres to `~/backups/invidious/` (on the ext4 root disk, not the NTFS
source disk), pulls new `invidious` and `companion` images, restarts, and checks
`/css/default.css` and `/x/health`. Companion is the piece that breaks against
YouTube most often, so run this when playback fails.

The sidecar image is built locally: `docker compose build sidecar`.

Restore:

```bash
docker compose exec -T invidious-db psql -U invidious -d invidious \
  < /path/to/dump.sql     # gzip -dc first if it ends in .gz
```

## Keeping the fork current

```bash
git fetch upstream
git switch master && git merge --ff-only upstream/master
git switch local && git merge master          # conflict-free: local/ is disjoint
```

`git diff master local --stat` must list only paths under `local/`.

## Developing the sidecar

```bash
uv venv /tmp/sidecar-venv --python 3.12
uv pip install --python /tmp/sidecar-venv/bin/python -e 'local/sidecar[dev]'
/tmp/sidecar-venv/bin/python -m pytest local/sidecar -q
```

Build the venv on the ext4 disk: the source tree is on an NTFS/FUSE mount.

## Design notes

- **No Invidious source changes.** `banner` renders raw HTML on every page
  (`src/invidious/views/template.ecr`) and `feed_menu.ecr` links every entry of
  a user's `feed_menu` as `/feed/<name>` with no allowlist, which is how
  `/feed/smart` and `/feed/discover` get navigation.
- **Saving Invidious's preferences page rewrites `feed_menu`** from its fixed
  four-option list, dropping Smart and Discover. `/x/settings` has a *Restore
  feed menu* button that re-posts it. It reads the current preferences document
  first and sends it back with only `feed_menu` changed, because
  `POST /api/v1/auth/preferences` rebuilds the whole document and defaults
  anything omitted. That endpoint returns 204 even when the body failed to
  parse, so the button re-reads preferences to confirm the write.
- **Invidious has no likes and no timestamps on history.**
  `GET /api/v1/auth/history` returns a bare array of video IDs, so the sidecar
  timestamps a video when it first sees it. The first cycle is a backfill:
  those rows get `first_seen_at = 0` and `source = 'backfill'`, which ranking
  reads as "seen, date unknown". All feedback (`👍 👎 🚫 ✓`) lives in the
  sidecar's own SQLite file.
- **Request budget.** The collector is throttled to one Invidious request per
  `FETCH_MIN_INTERVAL_SECONDS` (5s) and at most `FETCH_MAX_PER_CYCLE` (20) video
  fetches per cycle. `/api/v1/videos/:id` is the expensive one; related videos
  that already carry a publish date are stored from that response without a
  second request, so a watched video costs roughly one API call.
- **Theme.** Sidecar pages have no session cookie, so the token's own
  `dark_mode` is polled every 5 minutes and applied as the body class that
  Invidious's `default.css` expects. `?dark_mode=dark|light` overrides it per
  request, and the navbar's toggle goes through Invidious.
- **Pages reuse Invidious's own CSS** (`/css/pure-min.css`, `default.css`,
  `ionicons.min.css`) and copy the card markup from
  `src/invidious/views/components/item.ecr`, so cards inherit styling, dark
  mode, and the watched overlay.
- **CSP is `script-src 'self'`** on sidecar pages, matching Invidious. There are
  no inline handlers: filter auto-submit and the feedback buttons live in
  `/x/static/app.js`.

## Deviations from the original plan

- The sidecar image installs from `pyproject.toml` with pip rather than a
  committed `uv.lock`. Reproducibility comes from the pinned ranges; add a lock
  file if that ever matters.
- `tailscale serve` on this host maps `:443 → 127.0.0.1:8090` and
  `:8443 → 127.0.0.1:8000` (not ntfy), and something already owns host port 80,
  which is why the stack publishes `127.0.0.1:3080`.
- Companion is configured entirely by environment variables
  (`SERVER_SECRET_KEY`, `SERVER_BASE_PATH`, `CACHE_DIRECTORY`), read from
  `config/config.example.toml` inside the image.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Pages look unstyled | Invidious must be up: `/css/default.css` is served by it, not the sidecar |
| Smart feed is empty | `/x/health`: `last_feed_poll_age`, `counts.feed_items` |
| Discover is empty | `counts.edges` and `counts.watches`; edges need one harvested watch first |
| Playback fails | `docker compose logs companion`, then `./update.sh` |
| Cards never show watched | `INVIDIOUS_TOKEN` set? The ✓ button POSTs to Invidious's history endpoint |
| Feed menu lost Smart/Discover | Preferences page overwrote it → `/x/settings` → Restore feed menu |
