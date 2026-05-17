# Mac-side claude-code prompt: investigate XQuartz black-fill bug

Paste the prompt below into a claude-code session running on your Mac
(not on the Linux server). The agent should investigate XQuartz
configuration on the Mac side; it does not need access to the embryoDB
codebase.

---

I'm running a PyQt5 application (`embryodb-gui`) over SSH X11 forwarding
from a Linux server (Ubuntu 20.04 / Anaconda Python 3.12 / PyQt5 5.15.10
/ Qt 5.15) to my Mac via XQuartz. Symptoms:

- Sporadic black rectangles filling parts or all of newly-opened
  windows.
- Dropdowns / popups sometimes don't appear when triggered, or appear
  black.
- Resizing the window with the mouse held down usually causes content
  to eventually render — but it's intermittent.

A screenshot example: the upper portion of an embryoDB window
("Detail / Dataset" tab) renders correctly; the body of the window is
black until I wiggle the resize handle.

This is almost certainly an XQuartz/Quartz-Wm rendering issue, not an
application bug. The Linux side already runs with
`QT_X11_NO_MITSHM=1`, `QT_API=pyqt5`, `LIBGL_ALWAYS_INDIRECT=1`,
`QT_QUICK_BACKEND=software`. I SSH with `-Y`.

**Please investigate Mac-side XQuartz configuration** to make this
stable. Specifically:

1. **Audit current XQuartz settings:**
   ```bash
   defaults read org.xquartz.X11
   xdpyinfo | grep -i 'extension\|version\|name of display'
   xrandr --listproviders 2>/dev/null
   ```
   Report which extensions are present (especially MIT-SHM, GLX,
   RENDER, Composite) and the XQuartz version + macOS version. Note
   any current settings that diverge from defaults.

2. **Try the standard XQuartz fixes** (one at a time, restarting
   XQuartz between each) and report which one (if any) eliminates
   the black-fill bug:

   - `defaults write org.xquartz.X11 enable_iglx -bool true`
   - `defaults write org.xquartz.X11 enable_render_extension -bool true`
   - `defaults write org.xquartz.X11 enable_test_extensions -bool true`
   - `defaults write org.xquartz.X11 no_quartz_wm -bool true`
     (uses the X11 window manager instead of macOS Quartz — sometimes
     fixes compositing bugs at the cost of native-looking decorations)
   - Disable `localhost_indirect`:
     `defaults write org.xquartz.X11 localhost_indirect -bool false`
     (forces TCP forwarding instead of unix sockets — sometimes helps
     with intermittent rendering)

3. **Check whether the SSH client is doing something exotic:**
   ```bash
   grep -E '^(ForwardX11|XAuthLocation|ForwardX11Trusted|ForwardX11Timeout)' \
       ~/.ssh/config /etc/ssh/ssh_config 2>/dev/null
   ```
   In particular, `ForwardX11Timeout 0` (or `ForwardX11Timeout 596h`) is
   the standard workaround for the bug where forwarded X11 sessions
   silently drop after 20 minutes.

4. **Reproduce with a known-good X11 app first.** Install xeyes,
   xclock, xterm via XQuartz, and try them over the same SSH session.
   If `xeyes` also shows black-fill behaviour, the problem is purely
   XQuartz/SSH. If `xeyes` is perfect but Qt apps misbehave, the
   problem is Qt-specific and there are different fixes (Qt
   integration plugin selection on the Linux side).

5. **Consider switching to alternatives** if XQuartz keeps fighting:
   - **x2go** — both server and client are free, much better for
     WAN/intermittent. Linux side: `apt install x2goserver`. Mac side:
     install x2goclient. Works essentially identically to SSH+X11 but
     with proper compression and session persistence.
   - **NoMachine** — commercial but free for personal use. Better
     out-of-the-box experience than x2go.
   - **VNC over SSH tunnel** — universal fallback; lower performance
     than x2go but works everywhere.

Report:
- macOS version + XQuartz version
- Which fix(es) helped, if any
- Whether the issue persists with `xeyes` alone
- Recommendation: stick with patched XQuartz, switch to x2go/NoMachine,
  or accept the bug

If a configuration change is needed, **show me the commands first**
before applying them. XQuartz settings are sticky across reboots and
some affect security posture (e.g. `localhost_indirect false` enables
TCP-listening X11). Don't apply anything without me confirming.
