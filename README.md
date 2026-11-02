# deskcage

> Project status：vibe-coding, not fully reviewed

Profile based sandbox manager built around bubblewrap, with long lived
instances and a private session bus.

[中文文档](README.zh-CN.md)

A *profile* describes what one container may see: home directory, extra
bind mounts, devices, desktop sockets, environment and how its D-Bus session
works.  A *sandbox* is one running container built from a profile.  Several
processes can share a sandbox (`dcage start` then `dcage exec`, or an
application that talks to its peers over the private bus).  Sandboxes are
owned by a small per-user daemon, `dcaged`, so they survive the command that
started them.

The D-Bus boundary depends on the profile's `bus` mode:

* `isolated` gives the sandbox its own private session bus, unrelated to the
  host bus, so sandboxed applications can talk to each other without
  restrictions;
* `bridge` uses that same private bus and additionally lets notifications, the
  tray (StatusNotifierItem) and MPRIS cross into the host through `bus-bridge`;
* `none` provides no session bus at all.

No mode has portals, and the host bus is never mounted, so no other host
service is visible.

## Requirements

* Linux with user namespaces (bubblewrap)
* `bwrap`, `dbus-daemon`, Python 3.11+
* PyGObject (`gi`) for the daemon, CLI and GUI
* PyQt6 for the GUI (`pip install PyQt6`, or the `gui` extra)
* `bus-bridge` installed with pipx or from the distribution, for `bus = "bridge"` profiles

`dcage` finds `bus-bridge` through `$DESKCAGE_BUS_BRIDGE`, `[bus] bridge` in
`~/.config/dcage/config.toml`, or `$PATH` (pipx and distribution packages
both install the command there).

## Installation

From the `dcage` directory:

```sh
python3 -m venv --system-site-packages ~/.venvs/deskcage
. ~/.venvs/deskcage/bin/activate
pip install '.[gui]'      # drop [gui] for CLI only
```

`--system-site-packages` lets the venv use the distro's PyGObject.
`bus-bridge` is installed separately with pipx or from a distribution package,
and is only needed for `bus = "bridge"` profiles.  Installed, `dcage` looks
for bus-bridge through `$DESKCAGE_BUS_BRIDGE` and `[bus] bridge`, then on
`$PATH`.

## Quick start

```sh
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # usually set in a desktop session
dcage doctor

# Profiles are plain TOML; nothing is created automatically.
dcage profile create work --command firefox
dcage profile show work

# Sandboxes are environments, not single apps: reuse keeps adding processes
# to the same instance, creating it on first use.
dcage start work --instance browser --reuse -- firefox --new-instance
dcage start work --instance browser --reuse -- gimp

# Or start a sandbox that stays alive, and add processes later.
id=$(dcage start work -- sleep infinity)
dcage ps
dcage exec "$id" -- bash -l
dcage logs -f "$id"
dcage keep "$id" off
dcage stop "$id"
```

The daemon starts automatically on the session bus as
`org.deskcage.Manager1`.  `dcage daemon` keeps it in the foreground for debugging;
a background daemon logs to `$XDG_RUNTIME_DIR/dcage/dcaged.log` (tmpfs, nothing
on persistent disk).

## Commands

| Command | Purpose |
| --- | --- |
| `dcage daemon [--detach] [--stop] [--replace]` | run, start or stop the manager daemon |
| `dcage profile list/show/path` | inspect profiles |
| `dcage profile command ID` | print the bwrap command, one option group per line (`--one-line`, `--json`) |
| `dcage profile create <id> [--from P] [--command CMD] [--bus MODE]` | create a profile |
| `dcage profile duplicate <id> [new-id]` | copy a profile |
| `dcage profile rename <id> <new-id>` | rename a profile; running instances follow the new name |
| `dcage profile edit <id>` | edit the TOML in `$EDITOR` |
| `dcage profile delete <id>` | remove a profile |
| `dcage start <profile> [--instance NAME] [--keep\|--no-keep] [--reuse] [-- cmd...]` | long lived sandbox, prints its id; `--reuse` adds the command to a running instance of that name |
| `dcage exec <instance> [--detach] -- cmd...` | start another process inside |
| `dcage ps` | running instances |
| `dcage inspect <instance>` | JSON description, including processes |
| `dcage stop <instance>` / `--all` | destroy sandboxes |
| `dcage keep <instance> on\|off` | keep the sandbox after its last process |
| `dcage logs [-f] [instance]` | process output, all instances or one by name; each file is announced on stderr |
| `dcage doctor` | check the environment and the daemon |
| `dcage completion bash\|zsh` | print the completion script for that shell |

Instance ids may be abbreviated to any unique prefix; `dcage ps`, `exec`,
`stop` and `keep` only ever see live instances, while `dcage logs` also accepts
a released name that still has logs.  `--env KEY=VALUE` and `--cwd DIR` are
accepted by `start` and `exec`.

An instance is released the moment it exits or is stopped: its name is free
again and `instances/<id>` is removed.  Its logs stay in the global log
directory, so `dcage logs <id>` still works afterwards.  `--reuse` is about the
sandbox as an environment: it never creates a second sandbox, it never changes
the keep-alive flag of the one it finds, and a name that belongs to another
profile is refused instead of silently landing in the wrong environment.

`dcage logs -f` prints the files that appear and exits when the instance ends;
without an instance it follows every instance and exits when none is left.  A
missing daemon ends the follow as well, so it cannot wait forever.

## Shell completion

Installing the package is enough: the scripts land in
`/usr/share/bash-completion/completions/dcage` and
`/usr/share/zsh/site-functions/_dcage`, which both shells pick up by
themselves.  Without a package, source the same file by hand:

```sh
# bash, current session only
source <(dcage completion bash)
# bash, for every session
dcage completion bash > ~/.local/share/bash-completion/completions/dcage

# zsh (a directory that is in $fpath and is writable)
dcage completion zsh > "${fpath[1]}/_dcage"
```

Both scripts are thin wrappers around the hidden `dcage _complete` command, so
they complete from the real parser instead of a hand-written copy: subcommands,
options, `--bus`/`--home`/`keep` values, profile names, and instance names for
`exec`, `inspect`, `stop`, `keep` and `logs` (`logs` also knows instances that
already exited, because their logs are still there).  `start --instance` only
suggests existing names once `--reuse` is on the line, since without it the
argument means "make a new sandbox with this name".  `--env` completes
`KEY=` (the shells keep the word open for the value), `--cwd` completes
directories and keeps the trailing slash, and after `--` the shell completes
file names as usual.

Completion never starts the daemon: it reads the instance list over D-Bus only
if a daemon is already running, and otherwise falls back to the log directory.

## Profiles

Profiles live in `~/.config/dcage/profiles/<id>.toml`
(`$XDG_CONFIG_HOME` is honoured).  They are plain TOML; the CLI, the GUI and
your editor all edit the same file.

```toml
description = "Firefox with a temporary home"
command = [
    "firefox",
    "--new-instance",
]
keep_alive = true
network = true
home = "tmp"                 # tmp | bind | ro | tmpbind
hostname = ""                # default: deskcage-<id>
extra_args = []

[bus]
mode = "bridge"              # none | isolated | bridge
pid_hint = false

[files]
readonly = [
    "~/.config/fontconfig",
    "~/.local/share/fonts",
]
readwrite = [
    "~/Downloads",
    "~/.config/dconf",
]
tmpfs = []
# host content read-only, writes discarded on exit
tmpbind = [
    "~/.config/QQ",
]
tmpbind_strict = false       # true: refuse to start when a source is missing

[devices]
dri = true                   # /dev/dri + /sys/dev/{char,devices}
snd = true                   # /dev/snd
video = false                # /dev/video*
nvidia = false               # /dev/nvidia* (CUDA)

[desktop]
wayland = true               # $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY, read-only
x11 = true                   # /tmp/.X11-unix + $XAUTHORITY
pipewire = true              # pipewire-0 / pipewire-0-manager
pulse = true                 # $XDG_RUNTIME_DIR/pulse
a11y = false                 # at-spi accessibility bus

[env-preset]
xdg = true                   # inherit the XDG directory variables
session = true               # locale, session type, shell, TERM

[env]
MOZ_ENABLE_WAYLAND = "1"     # string: literal value
```

Generated profiles write every non-empty list one element per line and keep
empty lists as `[]`, so adding or removing a path is a one-line edit.  Both
that style and compact `["a", "b"]` arrays are valid when you edit by hand.

### Environment

The sandbox starts from an empty environment.  Only `HOME`, `PATH`,
`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` and the display variables for the
enabled desktop channels (`DISPLAY`/`XAUTHORITY`, `WAYLAND_DISPLAY`,
`PULSE_SERVER`, `PIPEWIRE_*`) are set automatically.

`[env-preset]` switches whole groups of host variables on (`xdg`, `session`,
`theme`, `proxy`); new profiles enable `xdg` and `session`.  Everything else
comes from `[env]`: a string is used verbatim, and `true` inherits the value
from the process that starts the sandbox — `dcage` or `dcage-gui`, not the
long-running `dcaged` (a variable that process does not define is simply
skipped).  The display variables and the `~`/`$VAR` expansion of profile paths
come from the same snapshot, so the daemon can never supply a stale value.

A sandbox keeps the environment it was started with; after changing something
such as `WAYLAND_DISPLAY`, start a new instance.

The groups are expanded first and the single entries after them, so an entry
such as `LANG = "C"` overrides the value coming from a preset.

In the GUI the presets are plain switches: checking one adds it to
`[env-preset]` without listing its variables in the table, and `Append as
custom entries` copies them into editable rows so a single value can be
overridden.

`dcage start` and `exec` accept `--env KEY` to inherit `KEY` from the invoking
shell, and `--env KEY=VALUE` to set it for that command only.

### File mounts

| Mode | Behaviour | Host after exit |
| --- | --- | --- |
| `readonly` | `--ro-bind-try`, visible read-only | unchanged |
| `readwrite` | `--bind-try`, writable | changes kept |
| `tmpfs` | `--tmpfs`, host content hidden | unchanged |
| `tmpbind` | `--overlay-src PATH --tmp-overlay PATH`, host content visible read-only, writes go to a temporary layer | unchanged |

The four `home` modes mirror this for `$HOME`: empty tmpfs, read-write bind,
read-only bind, and a whole-home `tmpbind` overlay.  `~` and environment
variables are expanded on the host.

Mounts are generated in a fixed order, and a later mount overrides an earlier
one with the same destination:

1. base skeleton (`/dev`, `/proc`, `/tmp`, `/var`, `/usr`, `/etc`, `/run`)
2. home
3. runtime directory and Wayland / PipeWire / Pulse sockets
4. X11
5. devices
6. `[files]`: `readonly` → `readwrite` → `tmpfs` → `tmpbind`
7. `/run/dcage` control channel, then `/run/dcage/logs` inside it (always last,
   in that order, because the second mount lives inside the first)
8. `extra_args`, environment, agent

If the same destination appears in several lists, only the last mount is kept.
`--overlay-src` only applies to the overlay option directly after it, so every
pair stays adjacent.  Missing `tmpbind` sources are skipped by default and the
skip is recorded in the instance's `logs/<id>/sandbox.log`; `tmpbind_strict = true` turns
that into a startup error.  Tmpbind needs overlayfs from the host kernel, and
the module must already be loaded before the sandbox starts (a user namespace
cannot trigger module autoloading); `dcage doctor` reports its status.

### Bus modes

| Mode | What the sandbox sees |
| --- | --- |
| `none` | `DBUS_SESSION_BUS_ADDRESS` unset |
| `isolated` | a private `dbus-daemon`, sandbox-only |
| `bridge` | private bus plus notifications, tray and MPRIS via `bus-bridge` |

When a bus is enabled (`isolated` / `bridge`) the socket is bind mounted at
`/run/dcage/bus`, so the sandbox can always use `unix:path=/run/dcage/bus`;
`none` leaves `DBUS_SESSION_BUS_ADDRESS` unset.

### Layout inside the sandbox

* `/run/dcage` – control channel and bus socket (bind mounted read-write)
* `/run/dcage/agent.py` – PID 1, a small Python process agent
* `/run/dcage/control.sock` – host ↔ agent protocol
* `/run/dcage/logs/` – the instance log directory, one file per process
* `/tmp`, `/var`, `/dev/shm` – fresh tmpfs
* `/usr`, `/etc`, `/opt`, `/sys/module` – read-only host mounts; a symlinked
  `/etc/resolv.conf` is bound to its real target

The agent is PID 1 in the PID namespace.  It reaps orphans, starts processes
on request, reports exit status and keeps the namespace alive until the last
process exits (unless keep-alive is on).  `dcaged` launches bwrap with
`--die-with-parent`, so sandboxes cannot outlive the daemon.

## Logs and disk usage

All logs stay in tmpfs by default and disappear at logout:

| What | Where |
| --- | --- |
| daemon | `$XDG_RUNTIME_DIR/dcage/dcaged.log` |
| instance bwrap output | `$XDG_RUNTIME_DIR/dcage/logs/<id>/sandbox.log` |
| private bus / bus-bridge | `$XDG_RUNTIME_DIR/dcage/logs/<id>/bus.log` |
| sandbox process output | `$XDG_RUNTIME_DIR/dcage/logs/<id>/<ns>-<seq>.log` (what `dcage logs` reads) |

There is one log directory per instance, mounted at `/run/dcage/logs` inside
the sandbox.  Every file above except the daemon log lives there, so nothing is
lost when the sandbox goes away: stopping or losing an instance removes
`instances/<id>` but keeps `logs/<id>`, and starting a new instance with the
same name clears it first.  `dcage logs` prints the process output only;
`bus.log` and `sandbox.log` are next to it for debugging.  Without
`XDG_RUNTIME_DIR` the paths fall back to `/tmp/dcage-<uid>/`.

To keep the daemon log on disk, point it somewhere explicitly:

```sh
DESKCAGE_DAEMON_LOG=~/.local/state/dcage/dcaged.log dcage daemon --detach
```

`DESKCAGE_DAEMON_LOG=off` (or `-`) discards daemon output; a failed autostart then
says so instead of pointing at a file that does not exist.

## GUI

```sh
dcage-gui
```

The Qt window lists profiles and instances.  Right-click a profile to create,
edit, duplicate, rename, delete or start it, or to show its command; right-click an
instance for logs, inspect, run command, keep-alive and stop.  Instances only
appear while they are alive, so exited ones drop off the list.  Profiles are
read and written directly under `~/.config/dcage/profiles`, so the window
opens and stays fully usable for editing even when `dcaged` cannot be
reached; only instance operations go through the daemon.

On the Files tab every mode is a one-path-per-row list with editable rows and
`Add file...` / `Add directory...` pickers.  Missing Tmpbind sources are shown
in red and summarised on the page.  Tooltips are short natural-language
explanations, not argument dumps.

The text box at the bottom of the editor follows the mouse: hovering an option
shows only the bind arguments that option generates (handy on the Devices tab:
point at `Wayland` to see the socket mount, at `X11` for the X11 mounts, and so
on).  Hovering an option that is currently off previews what turning it on
would add.  With nothing hovered the box just shows a hint, so it never dumps
the whole profile.  `Full command...` and the profile context menu's
`Show command` open the complete bwrap command, one option group per line and
ready to copy
(also available as `dcage profile command ID`).

The interface uses Qt's standard translation machinery: source strings are
wrapped in `self.tr()` / `QCoreApplication.translate()`, translators edit
`src/deskcage/translations/deskcage_zh_CN.ts`, and `lrelease6` compiles it into the
installed `deskcage_zh_CN.qm`.  A `QTranslator` loads the catalogue for
`QLocale.system()` at startup.  The toolbar can switch between "System
language", English and Simplified Chinese, and the choice is remembered with
`QSettings`.  Strings without a translation fall back to the English source.
To use another language for one run, change the locale the usual way:

```sh
LANG=zh_CN.UTF-8 dcage-gui
```

After changing interface strings, update the catalogue with the normal Qt
tools:

```sh
cd src/deskcage/translations
make            # pylupdate6 refreshes the .ts, lrelease6 rebuilds the .qm
```

This needs `pylupdate6` and `lrelease6` from `qt6-tools`; Qt Linguist can edit the
`.ts` file.

New strings appear in English and as unfinished entries in the `.ts`; finish
them and run `make` again.  Switching language rebuilds the window without
touching the daemon or running sandboxes.

## D-Bus interface

`dcaged` owns `org.deskcage.Manager1` at `/org/deskcage/Manager1`:

* profiles: `ListProfiles`, `GetProfile`, `SaveProfile`, `DeleteProfile`,
  `DuplicateProfile`, `RenameProfile`
* instances: `Launch(profile, id, keep, argv, env, cwd, reuse, caller_env)`,
  `ListInstances`, `GetInstance`, `Spawn`, `StopInstance`, `SetKeepAlive`,
  `Shutdown`
* signals: `InstanceChanged(id, state)`, `ProcessExited(instance, pid, status)`

`caller_env` is the environment of the process that launched the sandbox; it
is what `[env] KEY = true` and the presets resolve against.  A daemon started
by an older build cannot honour it, so after an upgrade run
`dcage daemon --replace` once.

`GetInstance` returns `a{sv}`; `processes` is a JSON string with pid, argv,
start time, log path and exit status.  Errors are returned as
`org.deskcage.Manager1.Error`.

## Tests

```sh
tests/run_all.sh
```

Every test creates its own XDG directories and its own private session bus, so
it never touches the developer session.  The suite covers profile
serialization, bwrap argument generation and mount ordering, real sandbox
lifecycle in all three bus modes (including `bus-bridge` name visibility), the
CLI, a headless GUI smoke test with the language switch, `.ts`/`.qm`
completeness and `QTranslator` loading, recovery after the daemon is killed,
a real tmpbind overlay test (skipped when overlayfs is unavailable), and the
installed (site-packages) layout.

## Limitations

* User namespaces are a practical isolation tool, not a security boundary
  against a hostile local attacker.
* The X11 socket is a broad channel; prefer Wayland for real isolation.
* No portals, no arbitrary host services.  `bus = "bridge"` intentionally
  exposes only notifications, tray and MPRIS.
* Profiles are per user; there is no root mode.
* Tmpbind needs overlayfs from the host kernel and the module must already be
  loaded; `dcage doctor` reports it.  Missing sources are skipped unless
  `tmpbind_strict = true`.
