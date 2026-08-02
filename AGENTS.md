# AGENTS.md — AI Agent Guide for bootc-installer

This document describes the architecture, dev workflow, and key commands needed
to work on this project as an AI agent. Read it before making changes.

---

## Skill docs

Task-specific knowledge lives in [`docs/skills/`](docs/skills/). Load the
relevant file instead of reading this whole guide:

| Need | Load |
|---|---|
| What lives in `docs/skills/` and what belongs there | [`INDEX.md`](docs/skills/INDEX.md) |
| The working procedure for this repo | [`SKILL.md`](docs/skills/SKILL.md) |
| Known traps, before debugging or changing the build | [`PITFALLS.md`](docs/skills/PITFALLS.md) |

---

## Repository layout

```
bootc-installer/               ← this repo (projectbluefin/bootc-installer)
├── bootc_installer/          ← Python GTK4/Adwaita GUI (the Flatpak app)
│   └── views/
│       ├── progress.py       ← fisherman launcher, log-file watcher, progress JSON parser
│       ├── done.py           ← final screen (reboot / log viewer)
│       └── confirm.py        ← confirmation screen before install
├── fisherman/                ← git submodule → projectbluefin/fisherman (Go backend)
│   └── fisherman/
│       ├── cmd/fisherman/main.go          ← install pipeline (steps 1-9)
│       └── internal/
│           ├── disk/         ← partition, format, mount, finalize
│           ├── luks/         ← LUKS format, open, TPM2 enrol
│           ├── install/      ← bootc install to-filesystem (podman run)
│           ├── post/         ← hostname, flatpak copy, bluetooth, wifi, audio, OEM, caches
│           ├── progress/     ← JSON-line progress emitter
│           ├── recipe/       ← recipe.go schema + Validate()
│           ├── slurp/        ← Windows data migration (wallpapers, scan, extract)
│           └── runner/       ← Run() helper (exec + set-x logging)
├── flatpak/
│   └── org.bootcinstaller.Installer.json   ← Flatpak manifest (GNOME 50 runtime)
├── data/                     ← GSchema, desktop file, icons
├── po/                       ← translations
└── .github/workflows/flatpak.yml   ← CI: builds + publishes "continuous" pre-release
```

---

## Two-component architecture

### fisherman (Go, submodule)

fisherman is a root-level CLI that reads a JSON recipe and executes the full
disk install pipeline. It emits newline-delimited JSON progress to stdout:

```json
{"type":"step","step":2,"total_steps":9,"step_name":"Formatting EFI partition"}
{"type":"substep","message":"Pulling container image"}
{"type":"info","message":"Writing hostname: bootcos"}
{"type":"complete","message":"Installation complete!"}
```

**Install pipeline (main.go):**

| Step | Action |
|------|--------|
| 1 | Partition disk (`sgdisk` via `disk.Partition` / `disk.PartitionEncrypted`) |
| 2 | Format EFI (`mkfs.fat -F32`) and optionally /boot (`mkfs.ext4`) |
| 3 | Set up LUKS (optional: `cryptsetup luksFormat` + `luksOpen`) |
| 4 | Format root filesystem (`mkfs.xfs` or `mkfs.btrfs`) |
| 5 | Mount everything at `/mnt/fisherman-target` |
| 6 | `bootc install to-filesystem` via `podman run --privileged` |
| 7 | Copy system Flatpaks (`/var/lib/flatpak` → target) |
| 8 | Write `/etc/hostname` into the ostree deployment |
| 9 | Finalize: fstrim → remount ro → fsfreeze/thaw |

**Key design decisions:**
- `--skip-finalize` is passed to bootc so the target stays writable for step 8.
  Step 9 manually replicates `bootc`'s internal `finalize_filesystem()`.
- Scratch space for bootc blob downloads is `/var/fisherman-tmp` (disk-backed),
  bind-mounted to `/var/tmp` on the host. Do NOT change this to `/run/*` —
  `/run` is a tmpfs (~50% RAM) and too small for large images.
- Partition layout: always **3-partition** (EFI + `/boot` ext4 + root). The
  separate ext4 `/boot` is required for two reasons: (1) GRUB's built-in XFS
  driver cannot read el10 XFS features (`nrext64`, `exchange`, `rmapbt`), so
  GRUB must only ever read ext4; (2) for encrypted installs, `bootupctl` (inside
  its bwrap sandbox) must be able to find the `/boot` UUID from a raw block
  device rather than a LUKS mapper. Both `Partition()` and
  `PartitionEncrypted()` produce the same 3-partition GPT table; the difference
  is that encrypted installs additionally set up LUKS on p3.

### bootc-installer (Python, GTK4/Adwaita)

The GUI collects user choices and writes a recipe JSON, then launches fisherman
and tails its JSON log output (via a `GLib.timeout_add` polling loop in
`progress.py`).

**Flatpak sandbox constraints:**
- fisherman is staged to `~/.cache/bootc-installer/fisherman` (host-visible via
  `--filesystem=host`) by `_stage_fisherman_on_host()` in `progress.py`.
- fisherman runs on the **host** via `flatpak-spawn --host pkexec <path>`.
- `systemctl reboot` must be called as `flatpak-spawn --host systemctl reboot`
  from inside the sandbox (see `done.py`).
- The installer log is written to `~/.cache/bootc-installer/fisherman-output.log`.

**Recipe JSON written by the GUI:**

```json
{
  "disk": "/dev/nvme0n1",
  "filesystem": "xfs",
  "btrfsSubvolumes": false,
  "encryption": {
    "type": "tpm2-luks-passphrase",
    "passphrase": "hunter2"
  },
  "image": "ghcr.io/projectbluefin/bootcos:latest",
  "targetImgref": "ghcr.io/projectbluefin/bootcos:latest",
  "selinuxDisabled": true,
  "hostname": "bootcos",
  "flatpaks": ["org.mozilla.firefox", "..."]
}
```

Encryption types: `"none"`, `"luks-passphrase"`, `"tpm2-luks"`, `"tpm2-luks-passphrase"`.

---

## Development workflow

Work directly on `projectbluefin/bootc-installer` — there is no fork.
The single remote is `origin → git@github.com:projectbluefin/bootc-installer.git`.

```bash
# Standard branch-and-PR workflow
git checkout -b my-feature origin/dev
# ... make changes ...
git push origin my-feature
gh pr create --base dev
```

### Making changes to fisherman

fisherman lives at `fisherman/` and is a **git submodule** pointing to
`github.com/projectbluefin/fisherman`. You must commit and push changes there
**separately** before updating the parent repo's submodule pointer.

```bash
# 1. Edit files inside fisherman/fisherman/
cd fisherman/fisherman
# ... make changes ...
go build ./cmd/fisherman/   # quick compile check
go vet ./...                # lint

# 2. Commit + push fisherman
git add -A && git commit -m "fix: describe the change"
git push

# 3. Update the submodule pointer in the parent repo
cd /path/to/bootc-installer
git add fisherman
git commit -m "chore: update fisherman submodule (describe the change)"
git push
```

### Making changes to the Python GUI

```bash
cd /path/to/bootc-installer
# edit bootc_installer/views/*.py or other files
git add -A && git commit -m "fix: describe the change"
git push
```

### Building and deploying the Flatpak locally

```bash
cd /path/to/bootc-installer

# Build and install locally (takes ~10 min first time; cached after)
flatpak run org.flatpak.Builder \
  --force-clean --user --install \
  _build flatpak/org.bootcinstaller.Installer.json

# Bundle for deployment to a remote machine
flatpak build-bundle \
  ~/.local/share/flatpak/repo \
  org.bootcinstaller.Installer.flatpak \
  org.bootcinstaller.Installer

# Deploy to a remote machine
scp org.bootcinstaller.Installer.flatpak user@<machine>:~
ssh user@<machine> \
  "flatpak uninstall --user -y org.bootcinstaller.Installer; \
   flatpak install --user --bundle -y ~/org.bootcinstaller.Installer.flatpak"
```

### Running the installer (on a live machine)

```bash
flatpak run org.bootcos.Installer
# Or with a local fisherman binary (dev/test):
BOOTC_FISHERMAN_PATH=/path/to/fisherman flatpak run org.bootcos.Installer
```

### Invoking fisherman directly (for testing)

```bash
# Build fisherman
cd fisherman/fisherman
go build -o /tmp/fisherman ./cmd/fisherman/

# Run with a recipe (as root — fisherman needs root for disk ops)
sudo /tmp/fisherman /path/to/recipe.json

# Watch the log on a remote machine
ssh james@192.168.0.119 "tail -f ~/.cache/bootc-installer/fisherman-output.log"
```

---

## CI / releases

- **Every push to `dev`** triggers `.github/workflows/flatpak.yml` which builds
  the Flatpak and publishes it as the `continuous-dev` pre-release on GitHub (pushes to `prod` publish the `continuous` pre-release).
- **`.github/workflows/python-test.yml`** runs on every push: 210+ unit tests
  (no display) + 14 GTK UI integration tests (Xvfb).
- **Tagged pushes** (`v*`) publish a named release.
- Container: `ghcr.io/flathub-infra/flatpak-github-actions:gnome-50`
- The submodule is checked out recursively by CI (`submodules: recursive`).

Always verify CI passes after pushing both submodule + parent repo commits.

---

## Testing

### Test suite layout

```
tests/
├── unit/
│   ├── test_processor.py       ← 153+ pure-Python tests for processor.py (no display)
│   ├── test_confirm_helpers.py ← 21 tests for confirm.py pure logic (_ENC_LABELS, quotes)
│   ├── test_slurp_helpers.py   ← 23 tests for slurp.py pure logic (_fmt_bytes, get_finals, etc.)
│   ├── test_defaults_misc.py   ← tests for conn_check, vm, nvidia, theme, network defaults
│   ├── test_done.py            ← tests for views/done.py: D-Bus reboot contract, apply_icon, warmup_registry
│   ├── test_recipe_loader.py   ← tests for utils/recipe loader logic incl. Flatpak live-ISO path
│   ├── test_run_async.py       ← tests for utils/run_async helpers
│   └── test_recovery_key.py    ← tests for views/recovery_key pure logic
└── ui/
    ├── conftest.py          ← GResource loader + Adw.init() for headless GTK
    └── test_wizard.py       ← 14 GTK integration tests (real widgets via Xvfb)
```

Run unit tests:
```bash
pytest tests/unit/ -v
```

Run UI tests (requires a display — use Xvfb in CI or a live X session locally):
```bash
xvfb-run -a pytest tests/ui/ -v
```

### Rules for keeping tests in sync with UI changes

**When you change `bootc_installer/utils/processor.py`:**
- Update `tests/unit/test_processor.py` to cover new fields or changed logic.
- Every new recipe field emitted by `processor.py` should have at least one
  parametrized test asserting the correct JSON value in the output recipe.

**When you change `bootc_installer/utils/finals.py`:**
- Update `tests/unit/test_done.py::TestMainWindowIconExtraction` — it imports
  `_extract_icon_and_name` directly and tests all edge cases (non-dict entries,
  fields split across dicts, first-occurrence wins, empty list).

**When you change a wizard step's `get_finals()` output (e.g. `defaults/image.py`,
`defaults/disk.py`, `defaults/encryption.py`, `defaults/user.py`):**
- Update `tests/ui/test_wizard.py` if the changed step is covered there.
- If a new `get_finals()` key is added, add an assertion for it in the
  relevant `TestXxxStep` class.

**When you add a new wizard step:**
- Add the step to `_SYS_RECIPE["steps"]` in `tests/ui/test_wizard.py` only if
  its template widgets are available in the CI libadwaita version (Ubuntu 24.04
  ships libadwaita 1.5.x — `Adw.ButtonRow` and other ≥ 1.6 widgets will fail).
  If in doubt, leave the step out of the test recipe and test via unit tests only.
- Add unit test coverage in `test_processor.py` for any new recipe fields the
  step produces.

**When you change `fisherman/fisherman/internal/recipe/recipe.go`:**
- Update `fisherman/fisherman/internal/recipe/recipe_test.go` — add valid and
  invalid cases for any new fields or validation rules.

---

## Key files to know

| File | Purpose |
|------|---------|
| `fisherman/fisherman/cmd/fisherman/main.go` | Install pipeline, step ordering, totalSteps, `scan` subcommand |
| `fisherman/fisherman/internal/disk/format.go` | `FinalizeFilesystem`, `FormatBoot`, `MountEFI`, `BindMount` |
| `fisherman/fisherman/internal/disk/partition.go` | `Partition` (2-part), `PartitionEncrypted` (3-part) |
| `fisherman/fisherman/internal/luks/luks.go` | LUKS format, open, close, `EnrollTPM2` |
| `fisherman/fisherman/internal/install/install.go` | `BootcInstall` → podman command |
| `fisherman/fisherman/internal/post/post.go` | `WriteHostname`, `CopyFlatpaks`, `CopyBluetoothPairings`, `CopyWiFiConnections`, `EnablePrintServices`, `Cleanup` |
| `fisherman/fisherman/internal/post/audio.go` | WirePlumber friendly device names, hide S/PDIF, live+persist |
| `fisherman/fisherman/internal/post/caches.go` | `WarmCaches` — pre-generate 8 system caches for instant first boot |
| `fisherman/fisherman/internal/post/oem.go` | OEM vendor detection (ASUS/Framework/TUXEDO), first-boot brew packages |
| `fisherman/fisherman/internal/slurp/wallpaper.go` | NTFS detect, wallpaper extraction/injection, thumbnail generation |
| `fisherman/fisherman/internal/slurp/scan.go` | `Scan()` enumerates Windows user data by category, `ScanJSON()` for CLI |
| `fisherman/fisherman/internal/slurp/data.go` | `ExtractData`/`InjectData` with RAM budget enforcement |
| `fisherman/fisherman/internal/recipe/recipe.go` | Recipe struct, `SlurpSpec`, `Validate()` |
| `bootc_installer/views/progress.py` | Video player, fisherman launch, JSON progress parsing |
| `bootc_installer/views/recovery_key.py` | Recovery key screen (post-encrypted-install) |
| `bootc_installer/views/done.py` | Final screen, reboot button, log viewer, `warmup_registry()` (post-install skopeo warmup) |
| `bootc_installer/defaults/conn_check.py` | Connection check — skipped when offline_install=True |
| `bootc_installer/windows/main_window.py` | Wizard, `_is_offline_install()`, context builder, `update_finals()` |
| `bootc_installer/utils/processor.py` | Recipe assembly: slurpWallpapers, additionalImageStores |
| `bootc_installer/utils/finals.py` | `_extract_icon_and_name()` — pure helper used by `main_window.update_finals()` |
| `bootc_installer/defaults/slurp.py` | Windows data slurp wizard step: async fisherman scan, category checkboxes, budget warning |
| `flatpak/org.bootcinstaller.Installer.Devel.json` | Devel Flatpak manifest (GNOME 50 runtime) |
| `.github/workflows/flatpak.yml` | CI build + publish workflow |
| `.github/workflows/python-test.yml` | CI unit + GTK UI integration tests |
| `tests/unit/test_processor.py` | 153+ unit tests for processor, progress, disks (no display) |
| `tests/unit/test_slurp_helpers.py` | 23 unit tests for slurp.py pure logic (no display) |
| `tests/unit/test_done.py` | D-Bus reboot contract, apply_icon, warmup_registry, icon extraction |
| `tests/ui/conftest.py` | GResource loader + `Adw.init()` for headless GTK tests |
| `tests/ui/test_wizard.py` | GTK integration tests (image step finals, E2E recipe gen) |
| `tests/ui/test_should_show.py` | Tests for should_show() step visibility pattern |

---

## Known issues / in-progress work

- **`bootc install finalize` is a no-op upstream**: We replicate the real finalization
  ops in `disk.FinalizeFilesystem()` ourselves (fstrim, remount ro, fsfreeze/thaw).
- **Windows data slurp GUI**: Fully implemented (#22, closed). Backend (`fisherman scan`,
  `ExtractData`, `InjectData`) and GUI wizard step (`bootc_installer/defaults/slurp.py`)
  are both complete. The step runs `fisherman scan` asynchronously, presents per-user
  category checkboxes with size estimates, enforces a RAM budget warning, and emits a
  `slurp` recipe key for fisherman to consume.
- **Flatpak builder bare repo issue**: git sources in Flatpak manifests fail due to
  `safe.bareRepository=explicit` in the sandbox. Workaround: use `archive` sources
  with SHA256 instead of `git` sources.
- **gi stub contamination in unit tests (fixed)**: When multiple test modules install
  `sys.modules` stubs for `gi.repository.*`, earlier stubs can bleed into later test files
  run in the same process. **Definitive three-part pattern**:
  1. Each `_import_X_fresh()` helper calls `_build_gi_stubs()` before popping and
     reimporting, so the correct stubs are always active at reimport time.
  2. Clear the parent package attribute — pop `sys.modules["bootc_installer.views.done"]`
     **and** `delattr(views_pkg, "done")` (Python can return a stale cached attr even after
     `sys.modules` is cleaned). Use `importlib.import_module()` rather than a plain import.
  3. For `gi` C-extension attrs that can bypass stubs (e.g. `done_mod.Gio`), use a
     structured stub: `gio_stub = MagicMock(); gio_stub.BusType = types.SimpleNamespace(SYSTEM=sentinel)`.
     Assign `done_mod.Gio = gio_stub` in `setUp()` so `patch.object` always targets a
     controllable object and typos like `Gio.BusTyp` surface as failures.
  Fixed in #67 and hardened across `test_done.py` / `test_branding_parity.py`.
- **`CompanionServer.start()` global reset (fixed in #70)**: `GLOBAL_CONFIG = None`
  inside a method creates a local variable, not a module-level reset. Always add
  `global GLOBAL_CONFIG` before the assignment when resetting module-level state.

---

## Post-install "instant first boot" features

These run automatically during the install pipeline (main.go) and require no user input:

| Feature | File | What it does |
|---------|------|--------------|
| Bluetooth persistence | `post.go` | Copies `/var/lib/bluetooth` → target so paired devices reconnect |
| WiFi persistence | `post.go` | Copies NM `.nmconnection` files → target for auto-reconnect |
| Audio device naming | `audio.go` | WirePlumber rules: rename ugly ALSA names, hide S/PDIF/Pro Audio |
| Live audio fix | `audio.go` | `ApplyAudioConfigLive()` — fixes names in live session immediately |
| OEM detection | `oem.go` | Detects ASUS/Framework/TUXEDO, queues first-boot brew packages |
| Cache warming | `caches.go` | Pre-generates font, icon, pixbuf, GIO, ldconfig, man-db, flatpak caches |
| Wallpaper slurp | `slurp/wallpaper.go` | Extracts Windows wallpapers, injects into target |
| Wallpaper thumbnails | `slurp/wallpaper.go` | Pre-generates GNOME wallpaper capplet thumbnails |
| Data slurp | `slurp/data.go` | Migrates documents/photos/music/bookmarks/fonts from Windows |
| Print services | `post.go` | Enables cups-browsed, avahi-daemon, ipp-usb for USB/AirPrint support |

---

## Useful diagnostic commands (on a remote install target)

```bash
# Watch the live install log
tail -f ~/.cache/bootc-installer/fisherman-output.log

# Check the most recent recipe used
ls -lt ~/.cache/bootc-installer/bootc-recipe-*.json | head -1 | xargs cat

# Inspect the installed disk after install (replace nvme0n1 with actual disk)
sudo lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID /dev/nvme0n1
sudo mount /dev/nvme0n1p2 /tmp/ir && sudo mount /dev/nvme0n1p1 /tmp/ie
cat /tmp/ir/boot/grub2/grub.cfg
cat /tmp/ie/EFI/almalinux/bootuuid.cfg
ls /tmp/ir/boot/loader/entries/
sudo umount /tmp/ie /tmp/ir

# Check EFI boot entries
efibootmgr

# Check bootupd state on installed root
sudo mount /dev/nvme0n1p2 /tmp/ir
cat /tmp/ir/boot/bootupd-state.json
sudo umount /tmp/ir
```

## Future Architectural Considerations

- **Move `images.json` to `fisherman` (Done)**: The image registry (`fisherman/data/images.json`) now lives in the `fisherman` backend. This allows `fisherman` to act as a universal registry of BootC images, containing not just the OCI references but also the specific installation requirements for each image (e.g., whether it requires manual user creation, specific kernel arguments, or filesystem defaults).
- **Universal BootC Registry**: Evolving the image manifest into a standard format that other installers or tools could consume to understand the "metadata" of a BootC image.
- **Dynamic Installation Carousel**: Replaced with video playback (Gtk.Video + AV1/VP9). Distribution can provide a branded video via `/etc/bootc-installer/install-video.webm`.
- **Windows Data Slurp (Done — #22)**: Backend (`fisherman scan`, `ExtractData`, `InjectData`) and GUI wizard step (`bootc_installer/defaults/slurp.py`) are fully implemented. The step runs an async scan, presents per-user category checkboxes with size estimates, and enforces a RAM budget warning. Wallpaper extraction also runs as an always-on easter egg.
- **Offline-first Install (Done — #16)**: `_is_offline_install()` detects live ISO mode; `additionalImageStores` passes pre-baked OCI stores from the ISO to fisherman/podman.
- **GStreamer VP9/AV1 codec validation (landing — #72)**: Validates that required video codecs are present before playback begins, surfacing a clear error instead of a silent blank video.
- **libpastry integration (landing — #71)**: Integrates libpastry for install-time configuration generation.
- **QR soundtrack codes (landing — #73)**: Pre-generates QR codes for soundtrack tracks at build time so they display instantly during the installation carousel.
- **QR Phone Companion MVP (landing — #70)**: Serves a local HTTPS companion server during install; the user can scan a QR code with their phone to follow along. `CompanionServer` in `bootc_installer/utils/phone_companion.py`.
- **DX groups on first install (Done — #74)**: `docker`, `incus-admin`, `libvirt`, and `dialout` added to `_DEFAULT_GROUPS` in `bootc_installer/defaults/user.py` so newly-created users have full developer access from first boot without needing `ujust dx-group`.

---

## Branch strategy

```
feature/xyz  ──►  dev  ──►  prod
```

- **`dev`** is the integration branch. All feature PRs target `dev`.
- **`prod`** is the release branch. It is promoted wholesale from `dev` when `dev` is in a shippable state — no cherry-picks, no partial merges.
- Never open PRs directly against `prod`. Features land on `dev` first.
- The merge queue is enabled for `dev`. Use `gh pr merge --squash <number>` or enqueue via the GitHub UI.

## Verification requirements for installer changes

**Unit tests passing is not sufficient for installer changes that affect the install path.**

Before merging any change to:
- Default filesystem (`disk.py`, `images.json`, `recipe.json`)
- Encryption or bootloader defaults
- fisherman post-install steps (hostname, flatpak copy, user creation)
- `processor.py` recipe generation

You must provide evidence of a **complete installation test** — not just that the UI works or the unit tests pass:

```bash
# In dakota-iso: run a full install E2E test
just plain-e2e dakota   # or luks-e2e for encrypted
```

Or on real hardware: install, reboot, verify the installed system boots.

**"Tests pass" ≠ "install works."** The unit tests mock fisherman. They cannot catch missing host tools (`mkfs.xfs`, `mkfs.btrfs`), wrong filesystem paths, or post-install failures that only occur during a real `bootc install to-filesystem` run. See issue [#190](https://github.com/projectbluefin/bootc-installer/issues/190).

### Querying features in flight (targeting `dev`)

To view active pull requests and their current check status:
1. Run `gh pr list --base dev` in your terminal to see open pull requests targeting the `dev` branch.
2. View the open pull requests directly on the GitHub UI at [GitHub Pull Requests](https://github.com/projectbluefin/bootc-installer/pulls).

---

## GitHub org context

- **`projectbluefin/bootc-installer`** — this repo (work directly here, no fork)
- **`projectbluefin/fisherman`** — Go backend (submodule at `fisherman/`)
- Images are published to `ghcr.io/projectbluefin/`
