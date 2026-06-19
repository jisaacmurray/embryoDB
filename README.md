# embryoDB v1 — safe-mirror

A Python rewrite of the Murray Lab's Java `EmbryoDB.jar`. v1 is intentionally
narrow: it imports every legacy XML file, lets you browse / filter / edit them
in a Qt GUI, and writes back to a separate export directory. The original
`/murrlab/gpfs/fs0/l/murr/embryoDB/` is never modified.

The legacy Java GUI keeps running against `source-dir`, so the two systems
coexist while trust is built. See `/home/jmurr/.claude/plans/ok-as-you-see-staged-gizmo.md`
for the broader roadmap (v1.5 through v4).

---

## Documentation

- **[docs/overview.md](docs/overview.md)** — quick map of the analysis tiers
  (import → lineaging → AceTree curation → phenotyping) and the main GUI/CLI
  option at each. Start here if you just want to know "what do I run?".
- [docs/troubleshooting.md](docs/troubleshooting.md) — X11/XQuartz rendering
  fixes, the AceTree-Py VNC path, DB connection issues, pipeline failures.
- [docs/data_access.md](docs/data_access.md) — read surface for separate
  consumer programs.
- [docs/portability.md](docs/portability.md) — what a port to another lab's
  environment would need.

---

## Install

End-to-end walkthrough for a fresh machine. Read top to bottom; nothing is
assumed except `sudo` on the DB host. The Mac client section at the bottom
covers the differences for a laptop connecting over SSH.

### Prerequisites

- A Linux host running Pop!_OS / Ubuntu 20.04+ or RHEL/Fedora/Rocky/Alma.
  (Macs work as clients but can't host the DB in this setup.)
- **Python ≥ 3.11.** The code uses `enum.StrEnum`, which arrived in 3.11.
  Ubuntu 20.04 / Pop!_OS focal ships 3.8 by default, so step 1 below adds
  3.12 from the deadsnakes PPA. Conda environments with 3.11+ also work.
- `sudo` on every machine where embryoDB will be installed (system-wide
  install lives in `/opt`), plus on the DB host for PostgreSQL setup.
- Access to the legacy XML corpus at `/murrlab/gpfs/fs0/l/murr/embryoDB/`
  (read-only) — required for the v1 safe-mirror import.

### Step 1 — System packages

The Qt GUI links against a set of X11 libraries that aren't part of the
default Ubuntu/Pop!_OS install. PostgreSQL needs server + client packages on
the DB host; other workstations only need to be able to install Python deps.

**Ubuntu / Pop!_OS / Debian (DB host):**

```bash
# On Ubuntu 20.04 / Pop!_OS focal, default python3 is 3.8 — too old.
# Add the deadsnakes PPA for python3.12. (Skip if you're on 22.04+ where
# python3.12 is in the default repos.)
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update

sudo apt install -y \
    python3.12 python3.12-venv python3-pip git \
    postgresql postgresql-contrib \
    libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0
```

The `postgresql` package installs and auto-starts the server. Verify:

```bash
sudo systemctl status postgresql        # should be "active (exited)"
python3.12 --version                    # should print "Python 3.12.x"
```

**Workstations / non-DB Linux hosts:** drop `postgresql postgresql-contrib`
from the apt line above. Everything else is the same:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y \
    python3.12 python3.12-venv python3-pip git \
    libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0
```

**RHEL / Fedora / Rocky / Alma (DB host):**

```bash
sudo dnf install -y \
    python3.12 python3-pip git \
    postgresql-server postgresql-contrib \
    libxcb libxkbcommon-x11
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

### Step 2 — Get the source

The lab keeps the package on shared GPFS so a `git pull` by anyone updates
the install for every user (the install in step 3 is editable mode). On the
GPFS-mounted host:

```bash
# Use the shared tree if you have GPFS access:
SRC=/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB
```

Otherwise clone fresh:

```bash
git clone https://github.com/jisaacmurray/embryoDB.git ~/embryoDB
SRC=~/embryoDB
cd "$SRC"
```

### Step 3 — Python dependencies (system-wide venv, recommended)

One venv per machine, under `/opt/embryodb/venv`, owned by root. All users
on the machine share it. Editable mode (`-e "$SRC"`) means a `git pull` in
the shared source tree picks up automatically; you only re-run `pip
install` when a dependency changes.

```bash
sudo mkdir -p /opt/embryodb
sudo python3.12 -m venv /opt/embryodb/venv
sudo /opt/embryodb/venv/bin/pip install -e "$SRC"
sudo /opt/embryodb/venv/bin/pip install pyqt5         # Qt binding (qtpy auto-selects)
```

Expose the entry-point scripts in `/usr/local/bin` so they're on every
user's `$PATH` automatically (no per-user `~/.bashrc` edit needed):

```bash
for cmd in embryodb embryodb-gui embryodb-open embryodb-worker; do
    sudo ln -sf "/opt/embryodb/venv/bin/$cmd" "/usr/local/bin/$cmd"
done

which embryodb-gui     # should print /usr/local/bin/embryodb-gui
```

If you prefer wrapper scripts over symlinks (e.g. to inject extra env
vars), substitute each symlink with:

```bash
sudo tee "/usr/local/bin/$cmd" >/dev/null <<EOF
#!/bin/bash
exec /opt/embryodb/venv/bin/$cmd "\$@"
EOF
sudo chmod +x "/usr/local/bin/$cmd"
```

**Upgrading later.** When dependencies change in `pyproject.toml`, re-run
just the install line (no need to recreate the venv):

```bash
sudo /opt/embryodb/venv/bin/pip install -e "$SRC"
```

**Alternative: per-user pip install.** For solo development or machines
where you don't have sudo, use `pip install --user -e "$SRC"` + `pip
install --user pyqt5` and add `~/.local/bin` to your `$PATH`. The
per-user path doesn't scale well to multi-user lab machines because each
account needs its own copy of every dependency.

### Step 4 — PostgreSQL: create role and database (DB host only, first install)

The `postgres` OS user has peer auth for local socket connections, so this
works without prompting:

```bash
sudo -u postgres psql <<SQL
CREATE ROLE embryodb LOGIN PASSWORD 'PICK_A_PASSWORD';
CREATE DATABASE embryodb OWNER embryodb;
SQL
```

### Step 5 — PostgreSQL: allow network connections (DB host only, first install)

Skip this step if **every** user will run embryoDB on the DB host itself.
Otherwise:

```bash
PGHBA=$(sudo -u postgres psql -tA -c "SHOW hba_file;")
PGCONF=$(sudo -u postgres psql -tA -c "SHOW config_file;")

# pg_hba.conf — allow the lab subnet (substitute your real CIDR).
echo 'host  embryodb  embryodb  10.0.0.0/8  scram-sha-256' \
  | sudo tee -a "$PGHBA"

# postgresql.conf — listen on the network, not just localhost.
sudo sed -i "s/^#listen_addresses.*/listen_addresses = '*'/" "$PGCONF"

sudo systemctl restart postgresql
```

If a firewall is active, open port 5432 for the lab subnet:

```bash
# Ubuntu/Pop!_OS (ufw — check `sudo ufw status` first; on Pop it's inactive by default)
sudo ufw allow from 10.0.0.0/8 to any port 5432 proto tcp

# RHEL/Fedora (firewalld)
sudo firewall-cmd --add-service=postgresql --permanent && sudo firewall-cmd --reload
```

### Step 6 — Configure the DB URL (once per machine)

With the system-wide venv install from step 3, the DB URL belongs in
`/etc/profile.d/embryodb.sh` rather than each user's `~/.bashrc`. Every
login shell sources `/etc/profile.d/*.sh`, so all users on the machine
pick up the same configuration:

```bash
sudo tee /etc/profile.d/embryodb.sh >/dev/null <<'EOF'
export EMBRYODB_DB_URL='postgresql+psycopg://embryodb:PICK_A_PASSWORD@DB_HOST/embryodb'
EOF
sudo chmod 0644 /etc/profile.d/embryodb.sh
# Re-source so the current shell picks it up without logging out:
source /etc/profile.d/embryodb.sh
```

- Replace `DB_HOST` with `localhost` if Postgres is on the same machine,
  otherwise the DB host's hostname (e.g. `penticton.lab.local`).
- URL-encode special characters in the password if any: `@` → `%40`,
  `:` → `%3A`, `/` → `%2F`, `#` → `%23`, `?` → `%3F`, `%` → `%25`,
  space → `%20`.
- `EMBRYODB_USER` is intentionally **not** set here — it defaults to
  `$USER` per login, so `updated_by` / `imported_by` columns get each
  lab member's real username automatically.
- If a specific user wants to override the URL (e.g. point at a test
  DB), they add their own `export EMBRYODB_DB_URL=…` line to `~/.bashrc`
  — login shells source `/etc/profile.d/*` first and `~/.bashrc` after,
  so the personal override wins.

Smoke-test that the URL parses and reaches Postgres:

```bash
psql "postgresql://embryodb:PICK_A_PASSWORD@DB_HOST/embryodb" \
  -c "SELECT current_user, current_database();"
```

Should print `embryodb | embryodb`. If it doesn't, see Troubleshooting below.

### Step 7 — Populate the DB from legacy XMLs (one-time, first user only)

```bash
embryodb-open
```

This is the "one command that does everything": creates the schema,
imports every legacy XML from source-dir, loads dataset list files, seeds
Stellaris protocols, then opens the GUI. Takes about 30 seconds on a quiet
GPFS read. Safe to re-run any time — it's idempotent and only re-processes
changed XMLs.

### Step 8 — Day-to-day use

```bash
embryodb-gui     # open the GUI without re-importing
```

`embryodb-gui` is the lightweight everyday command. The GUI still runs
schema migrations on startup (idempotent), so additive schema changes from
a `git pull` apply automatically. Use `embryodb-open` whenever you want to
re-sync the DB from the source XMLs (e.g. after a new acquisition lands).

### Step 9 — Verify

```bash
embryodb audit-import           # round-trip the whole corpus; exit 0 = safe-mirror holds
pytest tests/                   # 59 tests, in-memory SQLite, <1s
```

### Step 10 — Nightly backups (DB host only; strongly recommended)

```bash
sudo install -d -o postgres -g postgres /var/backups/embryodb
sudo tee /etc/cron.d/embryodb-backup >/dev/null <<'EOF'
SHELL=/bin/bash
PATH=/usr/bin:/bin
0 2 * * * postgres pg_dump -Fc embryodb > /var/backups/embryodb/embryodb-$(date +\%F).pgdump 2>>/var/log/embryodb-backup.log && find /var/backups/embryodb -name 'embryodb-*.pgdump' -mtime +30 -delete && mkdir -p /murrlab3/backups/embryodb && cp /var/backups/embryodb/embryodb-$(date +\%F).pgdump /murrlab3/backups/embryodb/ 2>>/var/log/embryodb-backup.log && find /murrlab3/backups/embryodb -name 'embryodb-*.pgdump' -mtime +90 -delete
EOF
sudo install -o postgres -g postgres -m 0644 /dev/null /var/log/embryodb-backup.log
```

Cron runs `pg_dump` nightly at 2 AM, logs any errors to
`/var/log/embryodb-backup.log`, and keeps the most recent 30 days
locally. **Why the second copy:** PGDATA *and* `/var/backups/embryodb`
both live on the DB host's local disk, so a disk failure would take the
database and its backups together. The tail of the cron line mirrors each
dump off-host to `/murrlab3/backups/embryodb/` (different file server,
ample space) with 90-day retention. Create that dir sticky+world-write
once so the `postgres` cron user can drop files into it:
`mkdir -p /murrlab3/backups/embryodb && chmod 1777 /murrlab3/backups/embryodb`.
The dump is tiny (~2 MB compressed; ≤3 MB projected at 10-year growth),
so the mirror is essentially free.

Test it without waiting:

```bash
sudo -u postgres bash -c 'pg_dump -Fc embryodb > /var/backups/embryodb/embryodb-test-$(date +%s).pgdump'
ls -lh /var/backups/embryodb/
```

Expected size after a full import: 50-150 MB. An empty schema dumps to
~20 KB (useful sanity check).

---

## Adding more users on the same host

With the system-wide install from steps 1, 3, and 6, **no per-user setup
is needed**. Every login shell already has `embryodb-gui` on `$PATH`
(symlinks in `/usr/local/bin`) and `EMBRYODB_DB_URL` exported (sourced
from `/etc/profile.d/embryodb.sh`). A new lab member just logs in and
runs `embryodb-gui`.

All users share the `embryodb` Postgres role, but `EMBRYODB_USER`
defaults to `$USER`, so `updated_by` / `imported_by` columns record
each lab member's actual username — no need for separate Postgres
roles unless you want per-user access controls.

---

## Mac (or off-network Linux) client — one command

The native GUI runs on macOS without X11 forwarding. The lab Postgres is reached
through an SSH tunnel, and a single launcher opens it for you:

```bash
embryodb-remote
```

It opens (or reuses) a **nested bastion→penticton** tunnel to Postgres, reads
the DB password from a chmod-600 file, and launches `embryodb-gui` locally
(native speed). In this mode heavy pipeline jobs run on a penticton-resident
worker, not the Mac. Image/AceTree access assumes lab paths resolve locally via
your filesystem mount + root symlinks (`/murrlab3` etc.), so no path remapping is
needed.

**Full setup (mount + root symlinks, the DB-URL file, `remote.conf`, the
bastion/nested-forward details) lives in [`docs/remote_access.md`](docs/remote_access.md).**

> The note that the firewall blocks `ssh -J` (ProxyJump) and the use of nested
> local forwards is the key gotcha — see that doc.

A FastAPI/HTTPS tier (connect with no SSH tunnel) remains a deferred option; the
tunnel + launcher covers the single-user Mac case with no lab-side server.

---

## Configuration reference

All settings come from env vars (prefix `EMBRYODB_`) or a `.env` file:

| Var | Default | Purpose |
|---|---|---|
| `EMBRYODB_SOURCE_DIR` | `/murrlab/gpfs/fs0/l/murr/embryoDB` | Read-only legacy XMLs |
| `EMBRYODB_EXPORT_DIR` | `/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB_exports` | Where DB→XML writes land |
| `EMBRYODB_DB_URL` | `postgresql+psycopg://embryodb@localhost/embryodb` | SQLAlchemy URL (also accepts `sqlite:////path` for local dev) |
| `EMBRYODB_USER` | `$USER` if set, else `anonymous` | Recorded in `imported_by` / `updated_by` columns |
| `EMBRYODB_TOOLS3_DIR` | `/gpfs/fs0/l/murr/tools3` | Location of `matlab_SN_cluster.pl` + `acebatch3.jar` for the pipeline worker |
| `EMBRYODB_WORKER_PIDFILE_DIR` | `/tmp` | Where the worker writes its per-host pidfile |
| `EMBRYODB_REMOTE` | `0` | Off-network client mode (set by `scripts/embryodb-remote`); suppresses local worker spawn so heavy jobs run on penticton. See [`docs/remote_access.md`](docs/remote_access.md) |
| `QT_API` | (auto) | Force a specific Qt binding: `pyqt5` / `pyqt6` / `pyside6` |

For quick local dev without PostgreSQL:

```bash
export EMBRYODB_DB_URL='sqlite:////tmp/embryodb-dev.db'
```

SQLite is **only safe for one user at a time**. See Multi-user notes below.

---

## CLI cheatsheet

| Command | What it does |
|---|---|
| `embryodb-gui` | Launch the GUI |
| `embryodb-open` | First-time bootstrap (init + import-xml + import-lists + seed-protocols) + open GUI; idempotent |
| `embryodb-worker` | Run the background pipeline worker (usually spawned by the import wizard) |
| `embryodb-remote` | From an off-network Mac: open the SSH tunnel + launch the GUI in remote mode ([`docs/remote_access.md`](docs/remote_access.md)) |
| `embryodb init-db` | Create the schema only |
| `embryodb import-xml [dir]` | Bulk-import XMLs from source-dir (read-only) |
| `embryodb export-xml [series\|all]` | Write back to export-dir |
| `embryodb audit-import` | Round-trip the corpus; exit 1 on any diff |
| `embryodb compare-with-source <series>` | Single-series diff |
| `embryodb find-duplicates` | Name collisions, case-fold, file↔row symmetric diff |
| `embryodb validate-paths [--dataset N]` | Check image_loc / annot_loc / acetree_config on disk |
| `embryodb missing-images` / `missing-annots` | Coverage report variants |
| `embryodb list [--gene G] [--person P] [--status S] [--since YYYYMMDD] [--text T]` | Filter |
| `embryodb show <series>` | All fields + provenance |
| `embryodb stats` | Count by status |
| `embryodb dataset create <name> [--series A --series B]` | Create a named collection |
| `embryodb dataset add <name> <series...>` | Add to a collection |
| `embryodb dataset export-list <name> --output FILE` | Write `/murr/lists/`-style text file |
| `embryodb gc-deleted [--older-than 30] [--apply]` | Purge image dirs for series marked-deleted past the grace period (dry-run by default) |

---

## GUI

```bash
embryodb-gui                # auto-selects whichever Qt binding is installed
QT_API=pyqt5 embryodb-gui   # explicit
```

Works over SSH X11 forwarding. Layout:

- **Filter bar** — person / strain / reporter / treatments / editor / status (multi-select dropdowns), date-before / date-after, text search with "comments-only" toggle. Filters compose live.
- **Dataset bar** — current dataset dropdown; create / add-selected / remove-selected / export-list buttons.
- **Browser table** — sortable, multi-select. Right-click selected row(s) for "Select all from same acquisition", "Bulk edit metadata", "Re-run pipeline". Right-click column headers to show/hide any column.
- **Detail panel** — 16 XML fields plus a Provenance section showing `version`, `updated_at`, source XML hash, and a Pipeline section showing per-step status with log viewers. Save uses optimistic locking — concurrent edits surface a conflict dialog with reload/cancel.

---

## Multi-user concurrency

The schema supports multiple lab members editing concurrently via per-row
optimistic locking — every Series, Dataset, Protocol, and Acquisition row
carries a `version` counter. When the GUI saves, it checks that the loaded
`version` still matches the DB; if not, you get a "Save conflict" dialog
with Reload / Cancel buttons. No silent overwrites.

PostgreSQL is the supported backend for multi-user use. SQLite is fine for
single-user local dev but unsafe for concurrent writers over a network
filesystem (GPFS, NFS) — its locking model relies on POSIX advisory locks
that distributed filesystems implement loosely.

---

## Safety guarantees

- `source-dir` is opened read-only. No code path writes to it. The exporter, audits, and round-trip tests all confirm this.
- `audit-import` round-trips all 11,021 corpus XMLs to byte-identical output today (0 diffs).
- Optimistic locking: every editable row carries `version`. A stale-version save surfaces a conflict dialog rather than silently overwriting another user's edits.
- Raw XML preserved on every row (`raw_xml`, `xml_hash`, `xml_mtime`, `xml_source_path`, `imported_at`, `imported_by`) so we can detect source drift after import.
- All pipeline file writes go through `embryodb.fsutil.safe_write` (umask 0002, mode 0664 / 0775, group `users`).

---

## Troubleshooting

### `Could not load the Qt platform plugin "xcb" … even though it was found`

The plugin file is present but a system library it depends on is missing.
This is the standard symptom on a clean Ubuntu 20.04 / Pop!_OS focal
install. Step 1 of Install installs the libs that fix it; if you still hit
this after step 1, diagnose with:

```bash
QT_DEBUG_PLUGINS=1 embryodb-gui 2>&1 | head -60
```

Look for `Cannot load library .../libqxcb.so: (libNNN.so: cannot open
shared object file)`. Install the named library with `sudo apt install`.

### `FATAL: no pg_hba.conf entry for host …`

Postgres rejected the connection because no rule in `pg_hba.conf` matches
the client IP / database / role combination. Temporary debugging trick:
add `host all all <client-ip>/32 trust` to `pg_hba.conf`, reload, and
verify the connection works. Then tighten back to a proper subnet +
`scram-sha-256` rule.

### `password authentication failed for user "embryodb"`

Either the password in `EMBRYODB_DB_URL` is wrong, or it contains
characters that need URL-encoding (`@`, `:`, `/`, `#`, `?`, `%`, space).
Reset the role's password if unsure:

```bash
sudo -u postgres psql -c "ALTER ROLE embryodb PASSWORD 'newpw';"
```

### `value too long for type character varying(N)` during import-xml

Legacy data contains rows whose values exceed an old VARCHAR cap that
SQLite never enforced but PostgreSQL does. Fixed in v0.1; if you see it
on an older install, `git pull` and re-run `embryodb-open` to apply the
ALTER COLUMN migration that widens those columns to TEXT.

### `Connection refused` on `localhost:5432`

Postgres isn't listening on TCP, only the Unix socket. Check:

```bash
sudo -u postgres psql -c "SHOW listen_addresses;"
```

It should be `localhost` or `*`. If it's just an empty string, edit
`postgresql.conf` (step 5) and restart.

### SSH X11 forwarding doesn't work after `su <other-user>`

`su` doesn't propagate the xauth cookie. Either SSH directly as that user
(`ssh -Y azach@host`), or manually copy the cookie:

```bash
# In the original user's shell, before su:
xauth list | grep "$(hostname)/unix:$(echo $DISPLAY | sed 's/.*://;s/\..*//')"
# Copy the line. Then in the target user's shell:
xauth add penticton/unix:NN MIT-MAGIC-COOKIE-1 <cookie>
export DISPLAY=$DISPLAY    # inherit value
```

### `libGL error: failed to load driver: swrast` (cosmetic)

Harmless. SSH X11 forwarding doesn't pass GLX, so Qt falls back to
software rendering. Suppress with `LIBGL_ALWAYS_INDIRECT=1 embryodb-gui`
if it's noisy.

---

## Source layout

```
embryodb/
  models.py         SQLAlchemy ORM (Series, Dataset, dataset_series, Status, …)
  database.py       Engine + session_scope context manager + additive migrations
  config.py         Pydantic-settings (source_dir, export_dir, db_url, tools3_dir, …)
  xml_format.py     The 16-field schema (single source of truth)
  parsers/          xml.py (lenient parser + serializer), filename.py, leica_metadata.py, matlab_params.py
  importers/        xml_importer.py, list_importer.py
  exporters/        xml_exporter.py
  queries/          series.py, datasets.py
  audits.py         audit-import, find-duplicates, validate-paths, …
  pipeline/         orchestrate.py (the 9 steps), worker.py, subprocess_steps.py, stage.py, …
  cli.py            Typer entry point
  fsutil.py         safe_write* helpers (umask + chmod + chgrp)
  gui/              app.py, main_window.py, filter_bar.py, detail_panel.py,
                    dataset_panel.py, models.py (Qt table model), import_wizard.py,
                    rerun_dialog.py, bulk_edit_dialog.py, acetree_config_dialog.py,
                    auxinfo_dialog.py
```

---

## v2 Pipeline import

Replaces the Perl `Stellaris_tif_pipeline*.pl` + `standard_pipeline.sh`
orchestration with Python. The Java analysis JARs themselves (RedBkgComp7,
Measure, SeriesSulstonizer) stay as subprocess invocations — those are
rewritten in v3.

### CLI

```bash
# One-time: seed Protocols from /gpfs/fs0/l/murr/parameters/Stellaris_*
embryodb pipeline seed-protocols

# Inspect available protocols
embryodb pipeline list-protocols

# Import one acquisition end-to-end (channel routing per the protocol)
embryodb pipeline import-acquisition \
    /murrlab3/Images/20250527_JIM783_efl-3 \
    --protocol Stellaris_JIM113 \
    --person azach \
    --strain JIM783 \
    --perturbation 'efl-3 RNAi'

# Register pre-existing on-disk acquisitions (no re-processing)
embryodb pipeline backfill /murrlab3/azach/images/

# Run the background worker (usually started by the GUI wizard)
embryodb pipeline worker
```

Most lab members will use the **File → Import acquisition…** wizard in the
GUI rather than the CLI form; the wizard previews positions, lets you set
per-series parameter overrides, and spawns the worker automatically.

### What `import-acquisition` does

For each TileScan position discovered in the source dir:

1. **stage_images** — LZW-compress + rename raw TIFs into `tif/` / `tifR/` /
   optional `DIC/` (channel routing per `Protocol.channel_map`); `tifC<n>/`
   for any additional channels. This is the slow step (hours per acquisition
   for full TileScans); the import wizard's "Delay (hours)" option defers it
   to off-hours via the worker.
2. **stage_metadata** — copy the per-position Leica `Properties.xml` to
   `dats/` and parse it into a `MicroscopyMetadata` row (voxel sizes,
   objective, NA, channels, pinhole, stage position, scan settings).
3. **write_acetree_config** — `<image_loc>/dats/<series>.xml` with
   resolution sourced from the Leica metadata.
4. **write_embryodb_xml** — legacy 16-field XML written to source-dir so
   the unmodified Java GUI sees the new series immediately. **Refuses to
   overwrite** an existing file (the safe-mirror property holds).
5. **create_alias_symlink** — `/murrlab/<user>/images/<series>` →
   `/murrlab3/<user>/images/<series>` for tools that use the alias path.
6. **write_matlab_params** — copies the protocol's parameter file, applies
   `--set-param k=v` overrides, sets `xyres` / `zres` / `slices` /
   `end_time` from the Leica metadata so AceTree XML and matlabParams
   can't drift.
7. **run_starrynite** — wraps the legacy Matlab StarryNite pipeline.
8. **run_red_extract** — `acebatch3.jar RedExtractor1` wrapper.
9. **run_measure** — `acebatch3.jar Measure1` wrapper.

Steps 7-9 (and stage_images, if delayed) run in the background worker;
their progress is visible in the Pipeline column of the browser.

### Browser GUI

- **Pipeline** column shows per-series step state, e.g.
  `6/9  stage✓ meta✓ cfg✓ xml✓ lnk✓ prm✓ SN. red. meas.`
- **Edit AceTree config…** and **Edit AuxInfo…** buttons on the detail
  panel for tweaking the per-series XML and Measure-output CSV.
- **Re-run pipeline…** button + context-menu entry for re-queuing any
  subset of steps with optional parameter overrides.

---

## Beyond v2

- **v2.5** — LineagePhenotyping YAML bridge, FastAPI tier for off-network access
- **v3** — Python port of RedBkgComp7, Measure, SeriesSulstonizer, GetACD
- **v4** — acetree_py launch, archive/delete lifecycle, image tile streaming
