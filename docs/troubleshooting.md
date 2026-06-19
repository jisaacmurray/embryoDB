# Troubleshooting embryoDB

## X11 rendering issues over SSH (especially Mac / XQuartz)

Symptoms:
- Black rectangles where windows or popups should be
- New windows open completely black until you resize them
- Dropdowns "don't open" (popup is rendered black or off-screen)
- Resizing has to be wiggled before content appears

These are X11-server compatibility problems, not embryoDB bugs. The fixes
below are stacked: try them in order, stopping once it's stable.

### 1. Use PyQt5, not PySide6

PySide6 6.5+ wants `libxcb-cursor0`, which is not installed on the lab
cluster. Force PyQt5 (already on the system):

```bash
export QT_API=pyqt5
embryodb-gui
```

This alone is the most important fix for the lab cluster.

### 2. Disable the MIT-SHM extension

XQuartz's MIT-SHM implementation is buggy on macOS Sonoma / Sequoia. Qt
falls back to plain X drawing when MIT-SHM is disabled, which renders
correctly but slightly slower.

```bash
export QT_X11_NO_MITSHM=1
embryodb-gui
```

### 3. Force indirect GLX (Mac side)

In Terminal **on the Mac** (not the SSH session):

```bash
defaults write org.xquartz.X11 enable_iglx -bool true
# then restart XQuartz
```

### 4. Force software rendering for Qt

If GPU compositing is the culprit (XQuartz forwarding GL contexts often
goes wrong):

```bash
export LIBGL_ALWAYS_INDIRECT=1
export QT_QUICK_BACKEND=software
embryodb-gui
```

### 5. Use `-Y` not `-X` when SSH-ing

`-Y` (trusted forwarding) skips some authorization checks that occasionally
break with XQuartz. Less secure, more reliable.

```bash
ssh -Y user@penticton.murrlab.lab
```

If you can, use a wired connection on the Mac side — flaky Wi-Fi causes
sporadic "frozen until I wiggle the window" symptoms with X11 forwarding.

### 6. Best, but heavier: x2go or NoMachine

For real responsiveness over a WAN connection, X11-over-SSH is not the
right tool. Both x2go and NoMachine forward only window deltas, with
proper compression and caching. They install on the Linux server and on
the Mac client; the embryoDB GUI runs exactly the same — only the display
transport changes.

If the lab settles on one of these, the rest of this doc becomes moot.

### Combined recipe for the lab cluster

If you're a Mac user SSH-ing into `penticton.murrlab.lab` (or similar),
the recipe that's known to work for several people is:

```bash
# On the Mac, once:
defaults write org.xquartz.X11 enable_iglx -bool true
# restart XQuartz

# In every SSH session you use embryoDB-gui from:
ssh -Y user@penticton.murrlab.lab
export QT_API=pyqt5
export QT_X11_NO_MITSHM=1
embryodb-gui
```

If that combination still leaves you with black-fill bugs, please
report the OS versions involved (macOS, XQuartz, Linux distro) so we
can extend this doc.

## AceTree-Py (napari) window is blank white / crashes XQuartz — use VNC

Symptoms (specific to **AceTree-Py**, the napari rewrite — *not* the Java
AceTree or `embryodb-gui`):
- The window opens but is plain **white** (not black), never drawing the image.
- Closing it crashes XQuartz (`The X11 connection broke`, SIGSEGV).
- The terminal log shows `OpenGL version 2.1 or higher recommended, got 1.4`
  followed by `Shader compilation error in GL_VERTEX_SHADER`.

Cause: napari/vispy needs **OpenGL ≥ 2.1** shaders. Forwarded X11 (XQuartz)
only negotiates indirect GLX **1.4**, so the canvas never renders. **None of
the X11 fixes above help** — this isn't a 2D drawing bug, the GL context
itself is too old. (The Java AceTree and `embryodb-gui` are fine over X11
because they don't need modern OpenGL.)

Fix: render inside a server-side VNC display, so OpenGL runs on the server
(software Mesa / llvmpipe, GL 3.3) and only pixels travel to your Mac. The
`acetree-py-vnc` launcher in the acetree_py repo does this end-to-end.

**One-time, server (root):** `sudo apt install -y tigervnc-standalone-server`
**One-time, per user:** `vncpasswd`   (sets `~/.vnc/passwd`)

**Each session, on the server:**

```bash
acetree-py-vnc /path/to/<series>.xml
```

It starts a private VNC display, launches AceTree-Py into it, and prints the
exact SSH-tunnel command + `vnc://localhost:<port>` line. On the Mac: open the
tunnel, then Finder → Cmd-K → `vnc://localhost:<port>` (built-in viewer, no
install) and enter your `vncpasswd` password.

**Through a firewall/bastion:** if you reach the server via a jump host and
`-J` fails with `administratively prohibited`, the bastion blocks ProxyJump
forwarding. Use **nested local forwards** instead (each hop forwards only to
its own localhost). Set these once (e.g. in `~/.bashrc` on the server) and the
launcher prints the correct nested command for you every run:

```bash
export ACETREE_PY_SSH_JUMP=user@bastion.fqdn      # the firewall host
export ACETREE_PY_HOSTNAME=server.fqdn            # this server's resolvable name
```

The printed nested form looks like:

```bash
ssh -L 5911:localhost:5911 -t user@bastion.fqdn "ssh -L 5911:localhost:5911 user@server.fqdn"
```

Quit the napari window (or Ctrl-C the launcher) when done to free the display.

## Database connection issues

Symptoms:
- `embryodb-gui` exits immediately with "Database error"
- `pg_hba.conf` rejection messages
- `connection refused`

### SQLite (development)

```bash
export EMBRYODB_DB_URL='sqlite:////absolute/path/to/embryodb.db'
embryodb init-db
```

The path must be **absolute** (note the 4 slashes after `sqlite:`).

### PostgreSQL (deployment)

Default URL: `postgresql+psycopg://embryodb@localhost/embryodb`. Override:

```bash
export EMBRYODB_DB_URL='postgresql+psycopg://user:pass@host:5432/dbname'
```

If you get `connection refused`, the PostgreSQL server is down or not
listening on the expected port. If you get `password authentication
failed`, the URL is wrong.

## Audit-import failures

Symptoms:
- `embryodb audit-import` reports byte diffs that didn't exist before

Possible causes (most likely first):

1. **Someone modified files in source-dir.** Check `git log` /
   `stat -c%y` on the failing files. Source-dir is supposed to be
   read-only for existing files.
2. **A new acquisition was added.** Confirm with
   `embryodb find-duplicates` — files present in source-dir but no DB row
   means the v2 import flow wrote to source-dir as designed.
3. **Schema migration broke something.** Re-run `embryodb init-db`
   shouldn't matter (`create_all` is additive) but if you dropped tables
   for a v1 reset, the provenance fields (`raw_xml` especially) need to
   be repopulated by re-importing.

## Pipeline import failures

Symptoms:
- `embryodb pipeline import-acquisition` raises mid-run
- Some series end up half-staged

Each pipeline step records its own `PipelineStepRun` row, so partial
state is visible in the GUI's Pipeline column (or `embryodb show
<series>`). Common cases:

- **`stage_images` fails halfway**: usually disk-full or permissions.
  Check `df -h` on the target filesystem.
- **`write_embryodb_xml` fails with "refusing to overwrite"**: that
  series_name already has a legacy XML in source-dir. Either rename the
  new acquisition or move the conflicting XML out of source-dir
  manually (and remember why source-dir is supposed to be append-only).
- **All steps after `stage_metadata` fail with KeyErrors**: the per-position
  `Properties.xml` wasn't found. Confirm the metadata files are present
  in the source directory alongside the raw TIFs (or in the staged dir
  if you ran the legacy pipeline first).

Re-running the import on the same source is safe — steps are idempotent
and skip already-complete work unless you pass `--overwrite`.
