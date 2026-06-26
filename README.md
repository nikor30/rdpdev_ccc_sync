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

The export builds a six-level RDM folder tree:

```
<Root> \ Region \ Country \ Site (CODE) \ Building \ Device
Webasto \ EMEA   \ Sweden  \ Stockholm (STO) \ HQ    \ SSTO014CIS
```

| Level    | Source | Notes |
|----------|--------|-------|
| **Root** | `EXPORT_ROOT` (default `Webasto`) | Blank it if you import under an existing root folder. |
| **Region** | Catalyst hierarchy, level after `Global` (`REGION_HIERARCHY_LEVEL`) | `Global/EMEA/...` → `EMEA`. |
| **Country** | The building's street **address** (last component) | Override per site if the guess is wrong. |
| **Site (CODE)** | The building's **parent area** + a 3-letter code | Code comes from the device **hostname** via `SITE_CODE_REGEX` (`SSTO010CIS` → `STO`). Rendered `Area (CODE)`. |
| **Building** | Catalyst **building** name | Floors roll up into their building. |
| **Device** | Switch | SSH session entry; discovered asset info goes in its Description. |

* Only devices in the families listed in `SWITCH_FAMILIES` are imported
  (default `Switches and Hubs`), so routers/APs/WLCs are skipped.
* A switch is only placed when it has a region, country, site code **and**
  building; otherwise it lands in the `EXPORT_UNSORTED_GROUP` review folder and
  shows up under **Conflicts** telling you exactly what's missing.
* **Credentials inherit from the root** — the CSV carries no username/password,
  so set one SSH credential on the top folder and every switch below inherits it.

### Asset information

Each device entry's **Description** is populated from what Catalyst Center
discovered: model/platform, series, IOS/software version, serial number, role
and reachability — e.g. `Model: C9300-48P | IOS: 17.9.4 | S/N: FOC2531 | Role: ACCESS`.

## Overrides survive re-syncs

Anything you change in the UI is stored in separate columns and is **never**
overwritten by a sync:

* `region override` / `name override` / `country override` on a site
* `site override` (pin a switch to a building) and `site-code override` on a device
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
| `SITE_CODE_REGEX` | `^[A-Za-z]?([A-Za-z]{3})` | one capture group; pulls the 3-letter site code from the hostname (`SSTO010CIS` → `STO`) |
| `DATABASE_URL` | `sqlite:////data/catalyst_rdm.db` | leave as-is in the container |
| `SYNC_INTERVAL_MINUTES` | `0` | `0` = manual only; otherwise background sync cadence |
| `SYNC_ON_STARTUP` | `false` | run one sync when the container starts |
| `WEB_USERNAME` / `WEB_PASSWORD` | empty | set both to require HTTP Basic auth on the UI |
| `SSH_CONNECTION_TYPE` | `SSHShell` | RDM connection type written to the CSV |
| `EXPORT_ROOT` | `Webasto` | top-level RDM folder; blank to import under an existing root |
| `EXPORT_UNSORTED_GROUP` | `_Review` | folder for devices that can't be placed |

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
docker compose up -d --build   # or: podman-compose up -d
```

**No `.env` is needed** — the compose file doesn't reference one, so it starts on
any Compose version. Bring the stack up and configure everything in the UI under
**Settings** (persisted in the `/data` volume). If you'd rather seed config from a
file, create it (`cp .env.example .env`) and uncomment the two `env_file:` lines
in `docker-compose.yml`.

The SQLite staging DB lives on the `catalyst_rdm_data` volume, so it persists
across restarts and image rebuilds.

## Suggested workflow

1. Open **Settings**, fill in the Catalyst Center connection, and click
   **Test connection** to confirm the credentials work. Save.
2. **Sync now** on the dashboard.
3. Open **Conflicts**. Errors (red) block clean export; warnings/info don't.
   * *Unassigned device* → open it, set a **site (building) override** (or exclude it).
   * *Device can't be placed* → it tells you what's missing (region / country / site
     code); fix via the site overrides, or set a **site-code override** on the device.
   * *Site missing geo data* → open the site, set a **region / country override**.
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
     `Webasto\EMEA\Sweden\Stockholm (STO)\HQ` becomes the full tree)
   * `ConnectionType` → set entries as **SSH Shell** (map the column, or pick the
     SSH Shell template in the wizard)
   * `Description` → entry **Description** (the discovered asset info)
4. Import under the top folder from step 1 so inheritance applies. If you blanked
   `EXPORT_ROOT`, import under your own `Webasto` root; otherwise the CSV already
   carries `Webasto\…` as the first level.

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
