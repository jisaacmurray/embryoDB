# Mac setup & test brief — remote embryoDB GUI (paste this to your Mac agent)

You are helping me set up and **test** running the embryoDB GUI on this Mac,
talking to the lab database on penticton over an SSH tunnel, with images reached
through a mounted lab filesystem. Everything below is the context you need; the
authoritative reference (already in the repo you'll clone) is
`docs/remote_access.md`. Work through it with me step by step, and stop to ask me
for anything only I can supply (usernames, the DB password, confirming a mount).

---

## What we're testing

A new launcher, `scripts/embryodb-remote`, that:
1. Opens (or reuses) an SSH tunnel from this Mac to the lab Postgres on penticton.
2. Launches `embryodb-gui` **locally** on the Mac (native speed/rendering).
3. Flags the process `EMBRYODB_REMOTE=1` so it does **not** spawn a local pipeline
   worker — heavy jobs (StarryNite/extract/measure) run on penticton instead.

Success = the GUI opens, I can browse and edit metadata (edits land in the lab
Postgres), and right-click → **Open in AceTree** opens an image stack locally.

---

## Key facts about my lab's network (you won't guess these)

- **The firewall blocks SSH ProxyJump.** `ssh -J bastion penticton` fails with
  `administratively prohibited`. So we reach penticton through a **bastion** using
  **nested local forwards** (each hop forwards to its own localhost). The launcher
  already does this; don't try to "simplify" it to `-J`.
- Two hosts are involved (I'll give you exact `user@host`):
  - **bastion** (jump/firewall host) — e.g. `gen-murra-006.med.upenn.edu`
  - **penticton** (the DB host) — e.g. `gen-murra-004.med.upenn.edu`
- Postgres listens on penticton port **5432**.
- The DB password is a **secret**. It goes in a chmod-600 file and must never be
  printed, echoed, or written into shell history or a committed file.

---

## The path assumption (why image viewing works)

The database stores **absolute lab paths** like `/murrlab3/jmurr/images/<series>`
and `/gpfs/fs0/l/murr/...`. embryoDB does **no** path translation. For AceTree to
open images, those exact paths must resolve on this Mac. My setup:

- The lab filesystem (alcatraz/GPFS) is mounted in my home directory (sshfs).
- I have **root-level symlinks** `/murrlab`, `/murrlab2`, `/murrlab3`, `/gpfs`
  pointing at the mount. On modern macOS, root symlinks require
  `/etc/synthetic.conf` (see `man synthetic.conf`).

Help me confirm these exist (`ls -ld /murrlab3 /gpfs`) before we test AceTree. If
they don't, browsing/editing metadata still works; only image viewing will fail.

---

## Setup steps (walk me through these)

```bash
# 1. Python + embryoDB (PySide6 ships its own Qt — no XQuartz/X11 needed)
brew install python@3.12 git
git clone https://github.com/jisaacmurray/embryoDB.git ~/embryoDB
pip install --user -e ~/embryoDB
pip install --user pyside6 qtpy
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

# 2. (If not already mounted) lab filesystem + root symlinks — I may already
#    have this; check `ls -ld /murrlab3 /gpfs` first.
#    brew install macfuse sshfs   # then sshfs mount + /etc/synthetic.conf symlinks

# 3. The DB-URL secret (I will give you the password; keep it OUT of history)
mkdir -p ~/.config/embryodb
# Use a method that doesn't log the password to shell history — e.g. open an
# editor and paste one line:
#   postgresql+psycopg://embryodb:THEPASSWORD@127.0.0.1:5432/embryodb
$EDITOR ~/.config/embryodb/db_url
chmod 600 ~/.config/embryodb/db_url

# 4. First run scaffolds the config, then I edit the two hostnames
~/embryoDB/scripts/embryodb-remote          # writes ~/.config/embryodb/remote.conf, exits
$EDITOR ~/.config/embryodb/remote.conf      # set EMBRYODB_REMOTE_BASTION + _DBHOST

# 5. Optional convenience alias
echo 'alias embryodb-remote="$HOME/embryoDB/scripts/embryodb-remote"' >> ~/.zshrc
```

`~/.config/embryodb/remote.conf` fields to set with me:
- `EMBRYODB_REMOTE_BASTION="user@gen-murra-006.med.upenn.edu"`
- `EMBRYODB_REMOTE_DBHOST="user@gen-murra-004.med.upenn.edu"`  (penticton)
- leave `EMBRYODB_REMOTE_PGPORT=5432`, `EMBRYODB_REMOTE_DBURL_FILE`,
  `EMBRYODB_REMOTE_CHECK_PATHS="/murrlab3 /murrlab /gpfs"` as defaults
- `EMBRYODB_REMOTE_ENSURE_WORKER=0` (set `1` later if we want the Mac to start a
  penticton worker each launch)

---

## Run the test

```bash
embryodb-remote
```

Expected: a line about opening (or reusing) the tunnel, optional warnings if a
`/murrlab*` path doesn't resolve, then the GUI window appears. Verify with me:

1. **Tunnel up:** `ssh -S ~/.ssh/embryodb-remote-*.sock -O check <bastion>` prints
   "Master running".
2. **DB read:** the series table populates in the GUI.
3. **DB write:** I edit a field on one series and Save; we confirm on-lab it
   persisted (I can re-query, or you note the GUI shows the new value after a
   refresh).
4. **No local worker:** `ls /tmp/embryodb-worker-*.pid` should NOT show a new
   pidfile for this Mac (remote mode suppresses local worker spawn).
5. **AceTree (only if the mounts/symlinks are present):** right-click a series →
   **Open in AceTree** → a Java window opens the image stack. Needs `java` on the
   Mac (`brew install --cask temurin` if missing).

---

## Likely problems and what they mean

- **Launcher exits asking me to edit config** — that's the first-run scaffold;
  fill in the two hostnames and the db_url file, run again.
- **"failed to open SSH tunnel" / auth prompts loop** — my SSH keys may not reach
  one of the hops. Test by hand: `ssh <bastion>`, then from there `ssh <penticton>`.
- **GUI "Database error" on startup** — wrong password/URL in `db_url`, or the
  tunnel isn't actually forwarding 5432. Re-check the `-O check` master and the
  `db_url` line (don't print the password; just confirm the file is one non-empty
  line and mode 600).
- **AceTree opens but no image** — a lab path didn't resolve locally; check the
  mount and the `/murrlab3` `/gpfs` root symlinks.
- **macOS won't let me make `/murrlab3` at root** — that's SIP; use
  `/etc/synthetic.conf`. This is the "harder on a new computer" part.

Don't reach for X11/XQuartz — this path is a native Mac GUI; X11 forwarding is a
different (Linux-GUI) option entirely.
