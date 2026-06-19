# Remote access — running the GUI from an off-network Mac/laptop

The native embryoDB GUI (and Java AceTree) run **locally** on your Mac for full
speed and rendering, while the **database lives on penticton** and **image /
annotation files are reached over your lab filesystem mount**. One command —
`embryodb-remote` — opens the SSH tunnel and launches the GUI; no manual
terminal juggling.

> **Why a tunnel, not a web server?** The GUI runs locally either way, so both
> give native speed. The SSH tunnel needs no lab-side service — just SSH — and
> the launcher opens it invisibly. A FastAPI/HTTPS tier (no tunnel) is a
> possible future option but adds server + auth deployment for no UX gain on a
> single-user Mac; it's deferred. See the v2.5 note in the master plan.

---

## How it fits together

```
  Mac (local)                     bastion (firewall)        penticton (lab)
  ┌──────────────┐  nested SSH    ┌──────────────┐  SSH     ┌──────────────┐
  │ embryodb-gui │──local fwd────▶│  forwards    │─fwd─────▶│  PostgreSQL  │
  │ Java AceTree │  :5432         │  :5432       │  :5432   │  (embryodb)  │
  └──────┬───────┘                └──────────────┘          └──────────────┘
         │  reads /murrlab3/... etc.
         ▼
   lab filesystem mount  (root symlinks /murrlab /murrlab2 /murrlab3 /gpfs)
```

Two independent channels:

1. **Metadata** → Postgres, over the SSH tunnel. The GUI thinks it's talking to
   a local Postgres on `127.0.0.1:5432`; SSH forwards the bytes to penticton.
2. **Image / annotation bytes** → your mounted lab filesystem. The DB stores
   canonical absolute lab paths (`/murrlab3/...`, `/gpfs/fs0/l/murr/...`). As
   long as those paths resolve locally, AceTree opens them unchanged — **no path
   remapping in embryoDB**.

---

## The path-resolution assumption (important)

embryoDB does **not** translate paths for the remote client. It relies on lab
absolute paths resolving on your Mac. The supported setup:

- Mount the lab shared filesystem (alcatraz / GPFS — same `/murrlab`,
  `/murrlab2`, `/murrlab3` content as penticton) somewhere in your home dir via
  sshfs (or your preferred mount).
- Create **root-level symlinks** so the lab absolute paths point at the mount:

  ```bash
  # one-time, on the Mac (root symlinks; on modern macOS this needs
  # /etc/synthetic.conf — see Apple's synthetic.conf(5)):
  /murrlab   → ~/lab-mount/murrlab
  /murrlab2  → ~/lab-mount/murrlab2
  /murrlab3  → ~/lab-mount/murrlab3
  /gpfs      → ~/lab-mount/gpfs
  ```

With those in place, `/murrlab3/jmurr/images/<series>` resolves on the Mac and
Java AceTree (launched by the GUI) finds the stack with no per-machine alias.

> If a machine genuinely cannot provide root symlinks, a configurable
> `EMBRYODB_PATH_MAP` prefix remap is the fallback — **not implemented yet**;
> open an issue if you need it.

---

## One-time setup

```bash
# 1. Python + embryoDB (PySide6 wheel ships its own Qt; no system X libs needed)
brew install python@3.12 git
git clone https://github.com/jisaacmurray/embryoDB.git ~/embryoDB
pip install --user -e ~/embryoDB
pip install --user pyside6 qtpy
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

# 2. Lab filesystem mount + root symlinks (see "path-resolution assumption")
brew install macfuse sshfs   # if using sshfs
# …mount + create /murrlab /murrlab2 /murrlab3 /gpfs symlinks…

# 3. The DB URL secret (chmod 600 — the launcher refuses to print it)
mkdir -p ~/.config/embryodb
printf 'postgresql+psycopg://embryodb:PASSWORD@127.0.0.1:5432/embryodb\n' \
    > ~/.config/embryodb/db_url
chmod 600 ~/.config/embryodb/db_url

# 4. First run scaffolds a config; edit hosts, then run again
~/embryoDB/scripts/embryodb-remote          # writes ~/.config/embryodb/remote.conf
$EDITOR ~/.config/embryodb/remote.conf      # set BASTION + DBHOST
```

Optional convenience alias:

```bash
echo 'alias embryodb-remote="$HOME/embryoDB/scripts/embryodb-remote"' >> ~/.zshrc
```

### `~/.config/embryodb/remote.conf`

| Key | Meaning |
|---|---|
| `EMBRYODB_REMOTE_BASTION` | `user@host` of the firewall/jump host you SSH into first |
| `EMBRYODB_REMOTE_DBHOST` | `user@host` of penticton, reached **from** the bastion |
| `EMBRYODB_REMOTE_PGPORT` | Postgres port (default 5432) |
| `EMBRYODB_REMOTE_DBURL_FILE` | path to the chmod-600 DB-URL file |
| `EMBRYODB_REMOTE_CHECK_PATHS` | space-separated lab paths to warn about if they don't resolve locally |
| `EMBRYODB_REMOTE_ENSURE_WORKER` | `1` to start a worker on penticton each launch (default `0`) |

---

## Each session

```bash
embryodb-remote
```

That's it. The launcher:

1. Reuses an existing tunnel if one is alive (idempotent via SSH
   `ControlMaster`/`ControlPersist`), else opens a fresh **nested** one.
2. Reads the secret DB URL from the chmod-600 file (never echoed).
3. Warns if any `CHECK_PATHS` don't resolve locally (AceTree would fail).
4. Launches `embryodb-gui` with `EMBRYODB_REMOTE=1`.

Tear the tunnel down manually (rarely needed — `ControlPersist` keeps it warm
for instant relaunch):

```bash
ssh -S ~/.ssh/embryodb-remote-*.sock -O exit <bastion>
```

---

## The firewall / bastion reality

The lab firewall blocks SSH **ProxyJump** (`ssh -J` fails with
`administratively prohibited`). So you cannot do a single `ssh -L
5432:penticton:5432 -J bastion …`. Instead the launcher uses **nested local
forwards**, where each hop forwards only to its own localhost:

```bash
ssh -L 5432:localhost:5432 -t <bastion> \
    "ssh -N -L 5432:localhost:5432 <penticton>"
```

The launcher wraps this in a multiplexed master connection so it's opened once
and reused.

---

## Heavy jobs stay on penticton

StarryNite, RedExtractor, Measure, and image staging need the lab's compiled
MATLAB/Java stack and shared storage — they must **not** run on the Mac. In
remote mode (`EMBRYODB_REMOTE=1`) the GUI's `spawn_worker()` is a no-op. The
worker is purely DB-driven: it claims PENDING `PipelineStepRun` rows. So:

- From the Mac GUI you can **enqueue** work (e.g. a pipeline rerun) — it writes
  PENDING rows to the shared Postgres.
- A worker **running on penticton** claims and executes those rows with
  canonical lab paths, and the GUI shows progress over the tunnel
  (`claimed_by` = penticton).

Make sure a penticton worker is alive. Either run it as a persistent service
there, or set `EMBRYODB_REMOTE_ENSURE_WORKER=1` so the launcher starts one each
session.

**Remote import-acquisition is out of scope for now**: its inline steps write
files and are better run lab-side. Run `embryodb pipeline import-…` directly on
penticton over SSH instead of from the Mac.

---

## Troubleshooting

- **Tunnel won't open / `connection refused`** — check the two hosts in
  `remote.conf` and that your SSH keys reach both. Test the inner hop by hand:
  `ssh <bastion>` then `ssh <penticton>`.
- **GUI exits with a database error** — the DB URL or password is wrong, or the
  tunnel isn't up. Confirm `127.0.0.1:5432` is forwarded
  (`ssh -S ~/.ssh/embryodb-remote-*.sock -O check <bastion>`).
- **AceTree opens but shows no image** — a lab path didn't resolve locally;
  check your mount and the `/murrlab*` `/gpfs` root symlinks (the launcher warns
  about missing `CHECK_PATHS`).
- **X11 / XQuartz** — not needed for this path; the Mac GUI is native. X11
  forwarding of the *Linux* GUI is a separate option covered in
  `docs/troubleshooting.md`.
