# Catalyst Center → Devolutions RDM Sync

Pulls your switch inventory from Cisco Catalyst Center, stages it in a small
database, lets you review and fix the structure in a web UI, and exports a
**Devolutions RDM**-ready CSV organised as **Region → Site → Device**.

No SSH credentials are ever stored or exported. You set one SSH credential at the
top of the tree in RDM and every switch below inherits it.

```
  ┌─────────────────┐   token    ┌──────────────┐   upsert    ┌──────────────┐
  │ Catalyst Center │ ─────────▶ │  Sync engine │ ──────────▶ │ SQLite (stage)│
  │   REST API      │  devices   │  + mapping   │  overrides  │   /data       │
  └─────────────────┘  + sites   └──────────────┘  preserved  └──────┬───────┘
                                                                      │
                       ┌──────────────────────────────────────┐      │
                       │  Web UI: tree · sites · devices ·      │ ◀────┘
                       │  conflicts · fix overrides             │
                       └───────────────────┬──────────────────-┘
                                           │  /export/devolutions.csv
                                           ▼
                                 ┌────────────────────┐
                                 │  Devolutions RDM   │  ← one SSH credential
                                 │  (CSV import)      │     at the root, inherited
                                 └────────────────────┘
```

## How the mapping works

Catalyst Center's site hierarchy maps directly onto the RDM folder tree:

| Catalyst Center            | This tool | RDM result            |
|----------------------------|-----------|-----------------------|
| Area under Global (`EMEA`) | Region    | Top-level folder      |
| Building (`Munich-Plant`)  | Site      | Sub-folder (has address + coordinates) |
| Floor                      | rolled up to its building | — |
| Switch                     | Device    | SSH session entry     |

* **Region** is the hierarchy element after `Global` (configurable via
  `REGION_HIERARCHY_LEVEL`). `Global/EMEA/Munich-Plant` → region `EMEA`.
* **Buildings become sites** because they carry the street address and lat/long,
  which is what you want to see "where it is".
* Switches assigned to a **floor** are rolled up to the parent building.
* Only devices in the families listed in `SWITCH_FAMILIES` are imported
  (default `Switches and Hubs`), so routers/APs/WLCs are skipped.

## Overrides survive re-syncs

Anything you change in the UI is stored in separate columns and is **never**
overwritten by a sync:

* `region override` / `name override` on a site
* `site override` on a device (pin an unassigned or mis-placed switch)
* `hostname override` (disambiguate duplicates / set a nicer RDM name)
* `exclude` a device from the export

Re-run a sync as often as you like — Catalyst-sourced fields refresh, your fixes stay.

## Configuration

You can configure everything two ways, and they layer:

1. **Environment / `.env`** — the bootstrap defaults (copy `.env.example` to `.env`).
2. **The Settings page in the UI** (`/settings`) — every value below is editable
   there, stored in the database, and **overrides the environment**. Because it
   lives on the `/data` volume, your changes survive restarts and image rebuilds.
   Secrets are write-only (leave a password blank to keep the current one), and
   **Test connection** authenticates against Catalyst Center and reports back
   before you save. `DATABASE_URL` is the one exception — it's needed to open the
   database itself, so it's shown read-only and can only be set via the environment.

| Variable | Default | Notes |
|---|---|---|
| `CATALYST_BASE_URL` | — | e.g. `https://dnac.example.com` |
| `CATALYST_USERNAME` / `CATALYST_PASSWORD` | — | read-only account is enough (see below) |
| `CATALYST_VERIFY_SSL` | `true` | set `false` only for lab appliances with self-signed certs |
| `CATALYST_TIMEOUT` | `30` | per-request timeout in seconds |
| `SWITCH_FAMILIES` | `Switches and Hubs` | comma-separated families to keep |
| `REGION_HIERARCHY_LEVEL` | `1` | hierarchy index after `Global` that is the region |
| `DATABASE_URL` | `sqlite:////data/catalyst_rdm.db` | leave as-is in the container |
| `SYNC_INTERVAL_MINUTES` | `0` | `0` = manual only; otherwise background sync cadence |
| `SYNC_ON_STARTUP` | `false` | run one sync when the container starts |
| `WEB_USERNAME` / `WEB_PASSWORD` | empty | set both to require HTTP Basic auth on the UI |
| `SSH_CONNECTION_TYPE` | `SSHShell` | RDM connection type written to the CSV |
| `EXPORT_UNSORTED_GROUP` | `_Review` | folder for devices with no resolved region/site |

### Catalyst Center permissions

A read-only API account works. The built-in **OBSERVER-ROLE** is sufficient — the
tool only issues `GET`s against `network-device`, `site`, and `site-member`, plus
the auth-token call. No write/provision scopes are required.

## Run it (Podman)

```bash
cp .env.example .env        # then edit .env
podman build -t catalyst-rdm-sync .
podman volume create catalyst_rdm_data
podman run -d --name catalyst-rdm-sync \
  -p 8080:8080 \
  --env-file .env \
  -v catalyst_rdm_data:/data \
  catalyst-rdm-sync
```

Open <http://localhost:8080>, click **Sync now**, then review the **Tree**.

### Or with compose

```bash
podman-compose up -d     # or: docker compose up -d
```

The SQLite staging DB lives on the `catalyst_rdm_data` volume, so it persists
across restarts and image rebuilds.

## Suggested workflow

1. Open **Settings**, fill in the Catalyst Center connection, and click
   **Test connection** to confirm the credentials work. Save.
2. **Sync now** on the dashboard.
3. Open **Conflicts**. Errors (red) block clean export; warnings/info don't.
   * *Unassigned device* → open it, set a **site override** (or exclude it).
   * *Site has no region* → open the site, set a **region override**.
   * *Duplicate IP / hostname* → fix in Catalyst Center, or set a hostname override.
4. Check the **Tree** — it's a live preview of exactly what RDM will receive.
5. Download the CSV from **Export CSV** (or point RDM at the stable URL
   `http://<host>:8080/export/devolutions.csv`).

## Import into Devolutions RDM

**One-time: set the inherited SSH credential**

1. Create (or pick) the top folder these switches will live under in RDM.
2. Add a **Credential entry** (username/password or your PAM/AnyIdentity source)
   on that folder, and set the folder's session settings to **inherit** it.
   Every imported switch sits below it and inherits the credential — nothing
   secret is in the CSV.

**Import the CSV**

1. **File → Import → Import from CSV** (exact wording varies by RDM version).
2. Choose the exported file (or the `/export/devolutions.csv` URL).
3. Map the columns:
   * `Name` → entry **Name**
   * `Host` → **Host** / Host name
   * `Group` → **Group / Folder** (RDM reads the `\` as folder nesting, so
     `EMEA\Munich-Plant` becomes a two-level tree)
   * `ConnectionType` → set entries as **SSH Shell** (map the column, or pick the
     SSH Shell template in the wizard)
4. Import under the top folder from step 1 so inheritance applies.

To refresh later, re-run the sync and re-import — RDM will update matching
entries and add new ones.

## Notes

* **Air-gapped / OT friendly.** No external CDNs, web fonts, or runtime
  downloads — the UI uses plain HTML forms, vanilla JS, and system fonts. It runs
  on an isolated network exactly as built.
* **SSH only.** Every exported entry is an SSH Shell session; no other protocols
  are written.
* **Security.** Don't expose the UI on an untrusted network without setting
  `WEB_USERNAME`/`WEB_PASSWORD`. Keep `.env` readable only by you
  (`chmod 600 .env`); it holds the Catalyst Center account.
* **Large hierarchies.** The sync fetches site membership per building/floor, so
  very large estates take a little longer; run it on a schedule rather than
  on every page load.

## Layout

```
app/
  config.py        env-driven default settings
  settings_store.py effective settings (env + DB overrides) for the UI
  db.py            SQLAlchemy models (Site, Device, SyncRun, AppSetting)
  catalyst.py      Catalyst Center API client (+ connection probe)
  sync.py          sync engine + hierarchy → region/site mapping
  conflicts.py     data-quality checks
  export.py        Devolutions CSV builder + shared placement logic
  main.py          FastAPI app (UI + settings + export)
  templates/       Jinja2 views
  static/          stylesheet
tests/             pytest suite
Dockerfile  docker-compose.yml  requirements.txt  requirements-dev.txt  .env.example
```
