# bootc-installer Pitfalls Reference

Distilled engineering gotchas for `projectbluefin/bootc-installer`.  
Load when debugging test failures, import errors, or GTK/GIO issues.

---

## GTK Unit Testing Without a Display

### The `__new__` + attribute injection pattern

GTK subclasses (`Adw.Bin`, `Adw.ActionRow`, etc.) call `Gtk.Template` machinery in `__init__`, which requires a display. Bypass with:

```python
obj = MyClass.__new__(MyClass)
obj._MyClass__private_attr = value    # Python name-mangling for private attrs
obj.some_child_widget = MagicMock()   # mock Template.Child() widgets
```

### Building gi stubs

Every test file that imports GTK code must stub `gi.repository.*` before importing. The canonical pattern:

```python
def _build_gi_stubs():
    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = MagicMock()
    repo = types.ModuleType("gi.repository")
    # ... stub Gtk, Adw, GLib, Gio, etc.
    sys.modules.update({"gi": gi_mod, "gi.repository": repo, ...})

def _import_MyClass_fresh():
    _build_gi_stubs()                          # ALWAYS call first
    pkg = sys.modules.get("bootc_installer.defaults")
    if pkg and hasattr(pkg, "mymodule"):
        delattr(pkg, "mymodule")               # clear stale attribute cache
    sys.modules.pop("bootc_installer.defaults.mymodule", None)
    return importlib.import_module("bootc_installer.defaults.mymodule")
```

**Rules:**
- Call `_build_gi_stubs()` INSIDE `_import_X_fresh()`, not once at module level
- Pop both `sys.modules[full.path]` AND `delattr(parent_pkg, "module")` — Python caches module attributes separately from `sys.modules`
- Use `importlib.import_module()` not bare `import` after clearing sys.modules

### gi stub cross-contamination

When multiple test files each call `_build_gi_stubs()` at module level, pytest's alphabetical collection order determines which stub wins. `test_builder.py` runs early and loads the **real** GTK C-extensions via `bootc_installer.utils.builder`. Any test file that runs after it and calls `patch("...Gio.some_method")` may be patching the real C object — the patch silently fails.

**Symptom:** Test passes when run alone, fails in the full suite.  
**Diagnosis:** `pytest tests/unit/test_builder.py tests/unit/test_my_file.py::TestMyClass -q` — if it fails, test_builder.py is contaminating.  
**Fix:** Use `_import_X_fresh()` to reload the module with clean stubs before every test class.

### Patching `Gio.resources_lookup_data` after real Gio is loaded

`patch("bootc_installer.defaults.image.Gio.resources_lookup_data", return_value=x)` silently fails when the real Gio C-extension is already loaded. The patch target is the C method object, not a Python-wrappable attribute.

**Fix:** Reload with `_import_image_fresh()`, then set directly:
```python
fresh = _import_image_fresh()
fresh.Gio.resources_lookup_data = MagicMock(return_value=x)
```

See `tests/unit/test_image_helpers.py::TestLoadManifestOverrides` for the canonical pattern.

### Dialog stub staleness

Each `_build_gi_stubs()` call creates a **new** `BootcDialog = MagicMock()` stored in `sys.modules["bootc_installer.windows.dialog"]`. Code that already ran `from bootc_installer.windows.dialog import BootcDialog` holds the **old** object. Asserting on the new stub: `sys.modules["bootc_installer.windows.dialog"].BootcDialog.assert_called_once()` will fail — it was never called.

**Fix:** Always assert on the module's own attribute:
```python
assert _yn_mod.BootcDialog.call_count == 1  # not sys.modules[...].BootcDialog
```
See `tests/unit/test_layouts.py` for the canonical pattern.

---

## Ruff / Python Quality

### Intentional out-of-order imports (GTK)

GTK apps must call `gi.require_version()` before importing widgets. These imports always appear after the version pin and get `# noqa: E402`:
```python
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402
```
Do NOT restructure these — they are intentional.

### Common F821 patterns to watch

- `encryption.py`: `_()` used for i18n without `from gettext import gettext as _`
- `done.py`: `logger.debug()` called with `import logging` present but no `logger = logging.getLogger(...)` — silent at import time, crashes at runtime

### Check before committing

```bash
python3 -m ruff check bootc_installer/ tests/
```

The codebase is ruff-clean. All new code must pass.

---

## Coverage Gate

### Never raise above measured

```bash
pytest tests/unit/ -q --cov=bootc_installer --cov-report=term-missing 2>&1 | tail -5
```

Use the integer floor (`47` for `47.57%`). pytest-cov displays rounded values — `47.57%` shows as `48%` in terminal output but the gate uses the raw decimal. Set `--cov-fail-under=47` to be unambiguous.

### When two PRs both change the gate

Keep the higher value. The gate is a ratchet — never lower it. Resolve the conflict by taking `max(a, b)`.

---

## CI

### Missing CI checks on a PR

GitHub Actions silently skips `pull_request` events when a PR branch has merge conflicts. If CI shows "no checks reported":
```bash
gh pr view N --json mergeable   # look for "CONFLICTING"
git rebase origin/dev && git push --force-with-lease
```

### Overlapping test PRs

When multiple PRs add to the same test files (`test_branding_parity.py`, `test_done.py`, `test_slurp_helpers.py`):
1. Rebase each branch onto latest `dev` after every merge
2. Run `pytest tests/unit/ -q` after every conflict resolution before pushing
3. "Keep both sides" of an additive test conflict can silently introduce indentation errors

---

## Flatpak Manifests

### git sources fail in Flatpak sandbox

`"type": "git"` sources fail with `safe.bareRepository=explicit`. Always use:
```json
{"type": "archive", "url": "...", "sha256": "..."}
```

### New .py files must be in meson.build

Every new Python file must appear in `sources = [...]` in its subpackage's `meson.build`. The `tests/unit/test_meson_sources.py` test catches this. Fix the `meson.build`, not the test.

---

## fisherman / Install Pipeline

### Windows data slurp timing

The slurp scan must happen BEFORE partitioning — the source disk is often the target disk.  
RAM scratch: `/run/fisherman-slurp/` (Statfs("/run") minus 2GB reserve).

### Offline install detection

Check `local_imgref` in recipe OR live ISO indicators (`/run/initramfs/live`, `/run/ostree-booted`). `processor.py` passes `additionalImageStores` to fisherman recipe for ISO-baked OCI stores.

### NTFS mounting

Try kernel `ntfs3` (faster, in-kernel since Linux 5.15) then fall back to `ntfs-3g` FUSE.

---

## GStreamer / Video Playback

Defer `set_muted()` and `play()` to the widget's `map` signal. Calling them before `GstPlayer` is constructed triggers `GStreamer-Player-CRITICAL`. Pattern:

```python
self.connect("map", self.__on_map)

def __on_map(self, _widget):
    media_stream = self.video.get_media_stream()
    if media_stream and media_stream.is_prepared():
        media_stream.set_muted(True)
        media_stream.play()
    else:
        media_stream.connect("notify::prepared", self.__on_prepared)
```

---

## Recipe / Image Catalog Override Chain

`RecipeLoader` (`utils/recipe.py`) applies overrides in this priority order:
1. `/etc/bootc-installer/recipe.json` (ISO/system override)
2. `$XDG_CONFIG_HOME/bootc-installer/recipe.json` (user override)
3. Bundled GResource version

Same for `images.json`. The ISO uses `/etc/bootc-installer/images.json` to replace the full multi-distro catalog with a single Dakota entry.

---

## QR Phone Companion

`CompanionServer` in `utils/phone_companion.py` runs `openssl` subprocess and opens a UDP socket to `8.8.8.8`. Both block in CI. Any UI test that navigates past the QR wizard step must mock:

```python
patch("bootc_installer.defaults.qr_companion.CompanionServer")
patch("bootc_installer.defaults.qr_companion.get_local_ip", return_value="127.0.0.1")
```

`GLOBAL_CONFIG = None` inside a method creates a local variable, not a module-level reset. Always add `global GLOBAL_CONFIG` before the assignment.

---

## conn_check.py — Don't Check github.com for Connectivity

**Pattern to avoid:**
```python
urllib.request.urlopen("https://github.com", timeout=5)  # WRONG
```

github.com is blocked in corporate environments and some geographic regions. The installer's actual dependency is `ghcr.io` (OCI registry). Use socket-level checks:

```python
import socket
for host, port in [("ghcr.io", 443), ("8.8.8.8", 53)]:
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        return True  # connected
    except OSError:
        continue
return False  # all failed
```

This probes the real OCI registry first (ghcr.io:443), then falls back to DNS (8.8.8.8:53) as a basic internet check.

---

## fisherman `checkRequiredTools` — Always Include Late-Stage Tools

**Rule:** If a tool is required at any step of the install pipeline, it must be in `checkRequiredTools`, even if it's only used in the very last step.

**Why:** fisherman fails silently late — the disk is already wiped and the OS is already installed by the time a missing tool is discovered. The only safe pattern is checking ALL required tools before touching any disk.

**Known gap (now fixed):** `systemd-cryptenroll` for TPM2 encryption types was missing. The install would succeed through 8 steps, then fail at TPM2 enrollment (step 9), leaving the disk wiped with no bootable system.

```go
// checkRequiredTools checklist:
// - All partition tools (sfdisk, mkfs.*)
// - Encryption tools (cryptsetup + systemd-cryptenroll for TPM2)
// - Image tools (skopeo, podman)
// - Any tool called in post-install steps (systemd-cryptenroll, etc.)
```

---

## Loop Devices in Kubernetes Containers

Loop partition nodes (`/dev/loopXpY`) do NOT appear in Kubernetes privileged containers after `sfdisk` repartitions a loop device. The `BLKRRPART` ioctl that sfdisk uses to notify the kernel about partition table changes fails in containers.

fisherman's `loopRescan()` (detach + re-attach with `--partscan`) mitigates this for real hardware/VMs, but does NOT work reliably inside k8s pods.

**Impact on testing:** Automated integration tests using `losetup` + fisherman in Argo pods will fail at `mkfs.fat /dev/loop0p1: No such file or directory`.

**Workaround for tests:** Use a KubeVirt VM for full install testing. The validate step (`fisherman validate recipe.json`) uses `os.Stat(disk)` only and DOES work in containers (13/13 test cases pass).

**Real-world impact:** NONE. fisherman is intended for live ISO installs (bare metal, VMs). Loop device rescanning works correctly on real hardware.

---

## Python Escape Sequences in GTK String Literals

Strings used in GTK markup or display names must use raw strings if they contain backslash sequences that aren't valid Python escapes:

```python
# WRONG — \| is not a valid Python escape; SyntaxWarning in 3.12, SyntaxError in 3.14+
"Czech (with <\|> key)"

# CORRECT — raw string, backslash is literal
r"Czech (with <\|> key)"
```

This affects any string with `\|`, `\%`, `\-` or other non-escape backslash combinations.

---

## `flatpak-builder --run`: `/app/bin` Not in PATH

When running the app via `flatpak run org.flatpak.Builder --run _build manifest.json COMMAND`, the default `PATH` inside the sandbox is:

```
/app/go/bin:/usr/bin:/bin
```

`/app/bin` is **not included**. Invoking `bootc-installer` directly fails with `No such file or directory`.

**Fix:** Always use the full path:

```bash
# Wrong
flatpak run org.flatpak.Builder --run _build manifest.json bootc-installer

# Correct
flatpak run org.flatpak.Builder --run _build manifest.json \
    sh -c 'BOOTC_DEMO=1 /app/bin/bootc-installer'

# Or set PATH explicitly
flatpak run org.flatpak.Builder --run _build manifest.json \
    sh -c 'PATH=/app/bin:$PATH BOOTC_DEMO=1 bootc-installer'
```

See `dev.sh` for the canonical form.

---

## `flatpak-builder --run`: Debug Log Goes to XDG App Cache

When running via `flatpak-builder --run` (not a full install), the app writes its debug log to the Flatpak XDG cache path, **not** `~/.cache/bootc-installer/`:

```
# flatpak-builder --run (dev loop)
~/.var/app/org.bootcinstaller.Installer.Devel/cache/bootc-installer/installer-debug.log

# Full flatpak install
~/.cache/bootc-installer/installer-debug.log   (via XDG_CACHE_HOME redirect)
```

`./dev.sh --logs` tails the correct path automatically.

---

## `DeploymentDirFn` mocking masks real command failures

`DefaultDeploymentDir` calls `ostree admin --print-current-dir` to locate the
deployment directory on the installed target. Every test that exercises
`WriteHostname` (ostree path) replaces `DeploymentDirFn` with a stub that
returns a fake path — meaning `DefaultDeploymentDir` itself is **never called**
in any test.

The real command always exits 1 against a freshly-installed target (never booted,
no booted-deployment state in the kernel). This caused a fatal installer crash
on every real install for weeks while CI passed.

The CI e2e wrapper (`scripts/fisherman-install.sh` in `dakota-iso`) silently
caught the crash and patched the hostname manually. It treated the symptom,
not the cause, and masked the failure from every CI run.

**Fix:** `DefaultDeploymentDir` now falls back to `filepath.Glob` over
`ostree/deploy/*/deploy/*` when `--print-current-dir` fails. Three regression
tests cover the fallback, happy path, and empty-sysroot error — and they call
`DefaultDeploymentDir` directly through `post.Exec`, not via the stub.

**Rule:** When a function is injected via a package-level `var Fn = Default...`
pattern, the default implementation must have at least one test that exercises
it end-to-end. Stub-only coverage is no coverage.

---

## `fisherman-install.sh` wrapper: symptom workaround, not a fix

The `scripts/fisherman-install.sh` wrapper in `dakota-iso` was written to patch
around the hostname-write crash at the CI level. It detects the specific
`"ostree admin --print-current-dir"` error in the log, re-mounts the installed
disk, and writes `/etc/hostname` directly.

This pattern is dangerous: it lets broken releases ship because CI appears green.
If you see a wrapper script that catches a specific fatal error and patches
around it, treat it as a **bug report in disguise** — find and fix the root
cause in fisherman, then remove the workaround.

The wrapper is still present in `scripts/fisherman-install.sh` as a safety net
but is no longer load-bearing now that `DefaultDeploymentDir` has the glob fallback.

---

## Branch Protection: Rulesets vs Classic Protection

This repo uses GitHub **repository rulesets** (not classic branch protection). The REST API endpoint is different:

```bash
# List rulesets
gh api repos/projectbluefin/bootc-installer/rulesets

# Delete a ruleset (removes all its rules including required status checks)
gh api --method DELETE repos/projectbluefin/bootc-installer/rulesets/<id>

# Classic branch protection (does NOT apply here — returns 404)
gh api repos/projectbluefin/bootc-installer/branches/dev/protection
```

If direct pushes to `dev` are blocked with "2 of 2 required status checks expected", the block comes from a ruleset, not classic protection. Delete the ruleset to allow direct pushes.

---

## Changing default filesystem requires live-env tool check (2026-06-15)

**Symptom:** `fisherman: fatal: missing required host tool: "mkfs.xfs" not found in PATH`
immediately after clicking Install. Affects every user on the new ISO.

**Cause:** When the installer default changed from `btrfs` to `xfs`, fisherman's preflight
check for `mkfs.xfs` became active. The live ISO environment (GnomeOS/freedesktop-sdk base)
did not have `xfsprogs` installed — only `btrfs-progs` was present.

**Fix in dakota-iso:** Copy `mkfs.xfs` from the Debian build stage into the final live image:
```dockerfile
COPY --from=initramfs-builder /usr/sbin/mkfs.xfs /usr/sbin/mkfs.xfs
```

**Rule:** Any change to the default filesystem (in `images.json`, `recipe.json`, or
`defaults/disk.py`) MUST be followed by a full E2E install test in the target live
environment. Unit tests mock fisherman — they cannot catch missing host tools.
The check lives in `fisherman/cmd/fisherman/main.go` (the `checkRequiredTools` slice).
Before changing a default, verify every tool in that slice is present in the live squashfs.

**How to check:** `podman unshare bash -c "M=\$(podman image mount <installer-image>); ls \$M/usr/sbin/mkfs.xfs || echo MISSING"`

See: https://github.com/ublue-os/bluefin/discussions/4754

---

## The fisherman submodule tracked the wrong branch (2026-08-01)

`.gitmodules` declared `branch = dev`, but fisherman's active line is **`main`** — on
2026-08-01 `main` was 25 commits ahead of `dev`. fisherman's *default* branch is `dev`,
so a PR opened there with the default base lands on the branch nobody ships, and
`git submodule update --remote` pulled from a stale line.

The pointer had not moved since **2026-06-23**. Everything that shipped in between —
the flatpak in every stable dakota ISO — was built from that June commit. A user hit an
ENOSPC install failure whose fix had been merged to fisherman `main` and still could not
reach them, because this pointer is the only bridge.

**Rules:**
- fisherman PRs target `main`. `.gitmodules` now tracks `main`.
- Bumping this pointer is what *ships* a fisherman fix. Merging in fisherman is not shipping.
- Check the gap before assuming a fix is live:

```bash
gh api repos/projectbluefin/bootc-installer/contents/fisherman -q .sha
gh api repos/projectbluefin/fisherman/compare/<that-sha>...main -q '{ahead:.ahead_by,behind:.behind_by}'
```

**Delivery chain** — three hops, each independently stale-able:

```
fisherman main → bootc-installer submodule → flatpak GitHub release → dakota ISO
```

The ISO downloads the bundle from a *release asset*, not from the branch:
`releases/latest/download/org.bootcinstaller.Installer.flatpak` for the stable channel,
`releases/download/latest-dev/...Devel.flatpak` for dev. So a merge to `prod` alone does
not reach a stable ISO — a `v*` release must be cut.
