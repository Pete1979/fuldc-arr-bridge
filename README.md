# fuldc-arr-bridge

**Request movies & TV in Seerr / Jellyseerr / Overseerr and have them download
automatically over Direct Connect using [FulDC++](https://fuldcpp.net).**

When a request is approved, the bridge searches your DC hubs via the FulDC++
Web API, picks the best release, and downloads it straight into your share — or,
if nobody is sharing it right now, it creates a **FulDC++ AutoSearch** item so
the client grabs it automatically as soon as it appears.

It's the "Radarr/Sonarr experience" for Direct Connect — which normally isn't
possible, because Radarr/Sonarr only drive Usenet and BitTorrent clients.

> **Private and self-contained.** The bridge only talks to your **local** FulDC++
> Web API. It never touches your files, never weakens DC's encryption, and never
> calls out to any third-party service. Everything stays on your machine.

## How it works

```
 someone requests a movie/show in Seerr
        │  (approved / auto-approved)
        ▼  webhook
 fuldc-arr-bridge ──► FulDC++ Web API ──► searches your hubs, ranks releases
        │                                  ├─ available now → download best release
        │                                  └─ not shared yet → AutoSearch (auto-grab later)
        ▼
 lands in  S:\dc\movies\...   or   S:\dc\series\<Show>\S01\...
        │  (FulDC++ re-shares it on the hub)
        ▼
 your media server (Plex/Jellyfin/Emby) scans → request shows as Available
```

The bridge is **stateless and API-only** — it just tells FulDC++ what to search
for and where to save it. No volume mounts, no database.

## Requirements

- **[FulDC++](https://fuldcpp.net)** (Windows) with the **Web UI enabled** and a
  **web user** that has **search**, **download** and **settings edit**
  permissions. Settings-edit is what AutoSearch item creation requires — without
  it every "nobody's sharing this yet" fallback fails with a 403.
- A request frontend: **[Seerr](https://seerr.dev)** / **Jellyseerr** /
  **Overseerr**, connected to your media server (Plex, Jellyfin, or Emby).
- **Docker** — Docker Desktop on Windows works great; run it on the same PC as
  FulDC++.

## Install (Docker Compose — recommended)

```bash
# 1. In FulDC++: enable the Web UI (Settings) and create a web user.
# 2. Grab the files
git clone https://github.com/Pete1979/fuldc-arr-bridge
cd fuldc-arr-bridge
cp .env.example .env        # then edit .env (FULDC_PASS at minimum)
# 3. Start it
docker compose up -d
```

`docker compose logs -f` will show requests as they come in.

If FulDC++ runs on the **same Windows PC**, the default `FULDC_URL` of
`http://host.docker.internal:5600` just works. Otherwise set it to the FulDC++
host's LAN IP, e.g. `http://192.168.0.22:5600`.

## Configure the Seerr / Jellyseerr / Overseerr webhook

**Settings → Notifications → Webhook**:

| field | value |
|---|---|
| Webhook URL | `http://<docker-host>:8080/` (if Seerr runs in Docker on the same host, `http://host.docker.internal:8080/`). If you set `WEBHOOK_TOKEN`, append `?token=<value>`. |
| JSON Payload | leave the **default** |
| Notification Types | enable **Request Approved** *and* **Request Automatically Approved** |

> ⚠️ **Enable both approval types.** The server **owner's own requests are
> auto-approved**, which fires `MEDIA_AUTO_APPROVED` — not `MEDIA_APPROVED`. If
> you only tick "Request Approved", your own requests won't trigger anything.
> Leave **Request Pending Approval** off (you don't want to grab before approving).

Then request something → approve it → watch `docker compose logs -f`.

## Configuration

| env | default | notes |
|---|---|---|
| `FULDC_URL` | `http://host.docker.internal:5600` | FulDC++ Web API address |
| `FULDC_USER` | `admin` | FulDC++ web user |
| `FULDC_PASS` | — | **required** |
| `DC_ROOT` | *(required)* | **your** DC share root on the FulDC++ host, a Windows path (e.g. `S:\dc`, `D:\Media`). `movies→DC_ROOT\movies\`, `series→DC_ROOT\series\<Show>\S<NN>\` |
| `MOVIES_DIR` / `SERIES_DIR` | *(from DC_ROOT)* | optional full-path overrides for non-standard layouts |
| `MOVIES_ONLY` | `0` | `1` = only movies, `0` = movies + TV |
| `QUALITY` | *(any)* | e.g. `1080p` — preferred quality (chosen when available, else falls back to the best/untagged release); TV: baked into the `%[inc]` episode monitor |
| `KIDS_ROUTING` | `1` | route kids titles to `kids.movies` / `kids.series` (needs a metadata source below; `0` disables) |
| `TMDB_API_KEY` | — | metadata source for kids routing — a free TMDB API key |
| `SEERR_URL` / `SEERR_API_KEY` | — | alternative metadata source: reuse your Seerr/Jellyseerr/Overseerr |
| `KIDS_MOVIES_DIR` / `KIDS_SERIES_DIR` | *(from DC_ROOT)* | override kids folders (full Windows paths) |
| `KIDS_GENRES` | `Kids,Family` | genres that mark a title as kids (Animation alone is **not** kids) |
| `SEASON_CHECK_HOURS` | `0` | auto new-season detection: every N hours, add a `%[inc]` monitor for a newly-aired season of a show you already follow (needs a metadata source; `0` = off) |
| `MEDIASERVER` | `none` | optional post-download refresh: `plex` \| `jellyfin` \| `webhook` \| `none` |
| `WEBHOOK_TOKEN` | *(empty)* | shared secret for the webhook endpoint. Blank = **anyone who can reach the port can queue downloads** — set it unless the port is strictly LAN-internal |
| `PORT` | `8080` | webhook listen port |

Optional media-server refresh (most servers scan periodically anyway):
`plex` → `PLEX_URL` + `PLEX_TOKEN`; `jellyfin` → `JELLYFIN_URL` + `JELLYFIN_TOKEN`;
`webhook` → `NOTIFY_WEBHOOK`.

## What it does with a request

- **Movies** → best-ranked release into `DC_ROOT\movies\`.
- **TV** → per requested season into `DC_ROOT\series\<Show>\S<NN>\`. A shared
  **season pack** is grabbed straight away (the ranker prefers packs over single
  episodes).
  - **Ongoing show** (status *Returning*) → if no pack is shared yet, a `%[inc]`
    per-episode AutoSearch monitor keeps grabbing new episodes as they air
    (Sonarr-style).
  - **Ended/canceled show** → grabs each season as a pack (a `%[inc]` monitor
    would never match, since past seasons ship as packs). Needs a metadata
    source (`TMDB_API_KEY` or `SEERR_URL`+`SEERR_API_KEY`).
- **Available now** → downloads immediately. **Not shared yet** → creates an
  AutoSearch item so FulDC++ grabs it when it appears.
- Ranking uses title/year/quality/language and skips CAM/TS/sample and content
  you already have. DC has no metadata like torrent indexers, so matching is
  heuristic — tune with the CLI (below) before trusting it fully.

## CLI (for testing / manual use)

The image also ships the `bridge.py` CLI:

```bash
docker compose run --rm fuldc-arr-bridge python bridge.py search "Dune" --year 2021
docker compose run --rm fuldc-arr-bridge python bridge.py grab "Dune" --year 2021 --grab
docker compose run --rm fuldc-arr-bridge python bridge.py grab "Severance" --kind series --season 2 --grab
```

`search` is read-only; `grab` is a dry run unless you add `--grab`.

## Development

```bash
python -m unittest -v test_bridge     # stdlib only, no network, ~1ms
```

The tests fake the FulDC++ API at the transport boundary and assert the exact
request bodies. Several of them guard behaviour the API requires but doesn't
enforce — a missing `use_params`, for example, produces an AutoSearch item that
looks fine in the UI and silently never matches anything.

## Experimental: full Radarr/Sonarr integration

> ⚠️ **Beta.** The Seerr flow above already covers most needs (and for re-share
> setups it's usually the better fit). This adds a **Torznab indexer** + a
> **qBittorrent-compatible download client** so Radarr/Sonarr can use FulDC++
> directly. The API integration works; the *import* side (remote-path mapping +
> how Radarr treats RAR content) still needs real-world tuning.

Enable it (set `TORZNAB_APIKEY` in `.env` first — the arr server refuses to
start without one, since this port can queue downloads):
```bash
docker compose --profile arr up -d      # starts fuldc-arr on :9117
```
Then in Radarr/Sonarr:
- **Indexer** → Generic Torznab, URL `http://<host>:9117/torznab`, API key = `TORZNAB_APIKEY`.
- **Download client** → qBittorrent, host `<host>`, port `9117`, username/password
  = `QBIT_USER`/`QBIT_PASS` (set them; blank means no authentication).

Note the release → magnet mapping is in-memory: after a restart, a grab from a
pre-restart search reports as failed and Radarr re-queries on its next cycle.

Feedback welcome — see [DESIGN.md](DESIGN.md) for the architecture.

## Advanced: Kubernetes

Manifests for a Talos/k8s deployment (Namespace + Deployment + Service) are in
[`k8s/`](k8s/), covering the **Seerr webhook flow only** — there is no manifest
for the experimental arr server. Point `FULDC_URL` at your FulDC++ host and
create the secret:

```bash
kubectl create secret generic fuldc-bridge-secret \
  --from-literal=FULDC_PASS=... \
  --from-literal=WEBHOOK_TOKEN=...     # optional
```

If you use an external secret manager, supply the same keys however you
normally do — the Deployment just reads `fuldc-bridge-secret`.

Note the Deployment uses `strategy: Recreate`. The release→magnet map is
in-process, so two pods behind one Service answer with different state.

## Notes & limitations

- Release **matching is heuristic** — DC filenames carry no structured metadata.
- **Don't expose these ports to the internet.** The bridge assumes a trusted LAN.
  Set `WEBHOOK_TOKEN` (and `QBIT_USER`/`QBIT_PASS` for the arr profile) if the
  ports are reachable by anything you don't control.
- Requires **FulDC++** specifically (it exposes the `auto_search` / `rss` core API
  modules that the upstream AirDC++ webclient does not).
- Roadmap: full Radarr/Sonarr integration via a Torznab indexer + a
  qBittorrent-compatible download-client shim (see [DESIGN.md](DESIGN.md)).

## Credits

Built on the [AirDC++ Web API](https://airdcpp.docs.apiary.io/). Thanks to the
**FulDC++** team and to **[KhwanTosawat8](https://github.com/KhwanTosawat8)** for
confirming the `auto_search` / RSS API surface.

Special thanks to **[KhwanTosawat8](https://github.com/KhwanTosawat8)** — an
AirDC++ developer — for substantial code contributions: AutoSearch `%[inc]`
incrementation (`use_params`), search priority, duplicate handling,
download-path safety, HTTP hardening, and the test suite + CI, all grounded in
the AirDC++ source. See the
[contributors](https://github.com/Pete1979/fuldc-arr-bridge/graphs/contributors).

## License

[MIT](LICENSE)
