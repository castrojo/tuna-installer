"""Drift gate for the wizard step-template registry.

A wizard step reaches the user only if its class is listed in the ``templates``
dict in ``bootc_installer/utils/builder.py``. That dict is the single source of
truth for step reachability, but four other surfaces independently declare that
a step exists:

  1. ``bootc_installer/defaults/meson.build`` sources  — installs the module
  2. ``bootc_installer/meson.build`` blueprint list    — compiles its .blp
  3. ``bootc_installer/bootc-installer.gresource.xml`` — embeds its .ui
  4. ``recipe.json`` ``steps.<key>.template``          — asks for it by name

Nothing made those surfaces agree with ``builder.templates``, so step modules
accumulated that ship, translate, and look production-ready but are never
constructed. The reverse drift is worse: registering a class whose .ui is not
in the gresource, or shipping a recipe naming a template that is not in
``templates``, only fails on the live ISO. ``Builder.__load`` guards with
``if step["template"] in templates:``, so an unknown template name drops the
step silently with no error at all.

These checks are deliberately **static** — they parse source text and never
import ``bootc_installer.defaults.*``. The ``unit`` CI job installs only pytest
and has no ``gi``/GTK, so importing a step module there would fail.

See issue #210.
"""

import ast
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PKG_ROOT = _REPO_ROOT / "bootc_installer"
_BUILDER = _PKG_ROOT / "utils" / "builder.py"
_GRESOURCE = _PKG_ROOT / "bootc-installer.gresource.xml"
_STEP_DIRS = ("defaults", "layouts")

# Step classes that exist on disk but are intentionally NOT reachable from
# builder.templates. Every entry here is dead weight the installer still ships;
# an entry may be removed (by deleting or registering the module) but adding a
# new one requires a deliberate edit, which is the point of this gate.
#
# keyboard/language/timezone are tracked by #187 and removed by PR #194 —
# listed so this gate is green both before and after that PR lands.
_UNREGISTERED_STEP_CLASSES = {
    "BootcDefaultKeyboard",  # #187 / PR #194
    "BootcDefaultLanguage",  # #187 / PR #194
    "BootcDefaultTimezone",  # #187 / PR #194
    "BootcDefaultNvidia",  # #210
    "BootcDefaultVm",  # #210
    "BootcDefaultNetwork",  # #210
    "BootcDefaultTheme",  # #210
    "BootcLayoutPreferences",  # #210
}

# Wizard-page resources embedded in the gresource with no Python consumer.
_UNCONSUMED_PAGE_RESOURCES = {
    "gtk/default-hardware.ui",  # #210 — no resource_path references it
    "gtk/default-keyboard.ui",  # #187 / PR #194
    "gtk/default-language.ui",  # #187 / PR #194
    "gtk/default-timezone.ui",  # #187 / PR #194
}


def _builder_tree() -> ast.Module:
    return ast.parse(_BUILDER.read_text())


def _registered_templates() -> dict[str, str]:
    """Return ``{template_name: ClassName}`` from builder.py's ``templates``."""
    for node in ast.walk(_builder_tree()):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "templates" for t in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Dict), "templates is not a dict literal"
        return {
            key.value: value.id
            for key, value in zip(node.value.keys, node.value.values)
            if isinstance(key, ast.Constant) and isinstance(value, ast.Name)
        }
    pytest.fail(f"No `templates` assignment found in {_BUILDER}")


def _builder_imports() -> dict[str, str]:
    """Return ``{ImportedName: module}`` for builder.py's module-level imports."""
    imports: dict[str, str] = {}
    for node in _builder_tree().body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module
    return imports


def _module_path(dotted: str) -> Path:
    return _REPO_ROOT / Path(*dotted.split(".")).with_suffix(".py")


_RESOURCE_RE = re.compile(
    r"@Gtk\.Template\(\s*resource_path\s*=\s*[\"']([^\"']+)[\"']\s*\)\s*\n"
    r"class\s+(\w+)"
)
_GRESOURCE_PREFIX = "/org/bootcinstaller/Installer/"


def _class_resources() -> dict[str, str]:
    """Map ``ClassName -> gresource path`` for every ``@Gtk.Template`` class."""
    found: dict[str, str] = {}
    for py in sorted(_PKG_ROOT.rglob("*.py")):
        for resource, cls in _RESOURCE_RE.findall(py.read_text()):
            found[cls] = resource
    return found


def _step_classes_on_disk() -> dict[str, Path]:
    """Map step class -> defining file.

    A class is a wizard step iff it implements ``get_finals()`` — that is the
    method ``Builder.get_finals`` calls on every registered widget to collect
    the values written into the installer recipe. Helper widgets and modals in
    the same modules (disk rows, partition dialogs) do not implement it.
    """
    found: dict[str, Path] = {}
    for subdir in _STEP_DIRS:
        for py in sorted((_PKG_ROOT / subdir).glob("*.py")):
            for node in ast.parse(py.read_text()).body:
                if not isinstance(node, ast.ClassDef):
                    continue
                if any(
                    isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and m.name == "get_finals"
                    for m in node.body
                ):
                    found[node.name] = py
    return found


def _gresource_files() -> set[str]:
    root = ET.parse(_GRESOURCE).getroot()
    return {f.text.strip() for f in root.iter("file") if f.text}


def test_registered_classes_are_defined_where_builder_imports_them():
    """Every class in ``templates`` resolves to a real class in a real module."""
    imports = _builder_imports()
    missing = []
    for template, cls in _registered_templates().items():
        module = imports.get(cls)
        if module is None:
            missing.append(f"{template!r} -> {cls}: not imported by builder.py")
            continue
        path = _module_path(module)
        if not path.exists():
            missing.append(f"{template!r} -> {module}: {path} does not exist")
            continue
        defined = {
            n.name for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.ClassDef)
        }
        if cls not in defined:
            missing.append(f"{template!r} -> {cls}: not defined in {module}")

    assert not missing, (
        "builder.py `templates` references classes that do not resolve:\n"
        + "\n".join(f"  {m}" for m in missing)
    )


def test_registered_step_resources_are_embedded_in_the_gresource():
    """A registered step whose .ui is not in the gresource crashes the wizard."""
    resources = _class_resources()
    embedded = _gresource_files()
    missing = []
    for template, cls in _registered_templates().items():
        resource = resources.get(cls)
        if resource is None:
            # Not every step is a @Gtk.Template class (e.g. qr_companion builds
            # its page in code); nothing to embed.
            continue
        assert resource.startswith(_GRESOURCE_PREFIX), (
            f"{cls} resource_path {resource!r} is outside {_GRESOURCE_PREFIX}"
        )
        relative = resource[len(_GRESOURCE_PREFIX):]
        if relative not in embedded:
            missing.append(f"{template!r} -> {cls}: {relative}")

    assert not missing, (
        "Registered steps whose resource is missing from "
        f"{_GRESOURCE.relative_to(_REPO_ROOT)}:\n"
        + "\n".join(f"  {m}" for m in missing)
        + "\nAdd the <file> entry, or the wizard fails at construction time."
    )


def test_bundled_recipe_only_names_registered_templates():
    """``Builder.__load`` skips unknown template names silently — catch them here."""
    recipe = json.loads((_REPO_ROOT / "recipe.json").read_text())
    registered = _registered_templates()
    unknown = sorted(
        f"steps.{key}.template = {step['template']!r}"
        for key, step in recipe.get("steps", {}).items()
        if step.get("template") not in registered
    )
    assert not unknown, (
        "recipe.json names templates that builder.py does not register "
        "(these steps are dropped with no error):\n"
        + "\n".join(f"  {u}" for u in unknown)
    )


def test_unregistered_step_classes_are_declared_dead():
    """New step modules must be registered or explicitly declared unreachable."""
    registered = set(_registered_templates().values())
    on_disk = _step_classes_on_disk()
    orphaned = {cls for cls in on_disk if cls not in registered}

    undeclared = orphaned - _UNREGISTERED_STEP_CLASSES
    assert not undeclared, (
        "Step classes that ship but are never constructed by builder.py:\n"
        + "\n".join(
            f"  {cls} ({on_disk[cls].relative_to(_REPO_ROOT)})"
            for cls in sorted(undeclared)
        )
        + "\nRegister it in builder.py `templates`, delete it, or add it to "
        "_UNREGISTERED_STEP_CLASSES with a tracking issue."
    )


def test_wizard_page_resources_have_a_python_consumer():
    """Every embedded ``gtk/default-*.ui`` must be claimed by some class."""
    consumed = {
        r[len(_GRESOURCE_PREFIX):]
        for r in _class_resources().values()
        if r.startswith(_GRESOURCE_PREFIX)
    }
    pages = {f for f in _gresource_files() if f.startswith("gtk/default-")}

    undeclared = pages - consumed - _UNCONSUMED_PAGE_RESOURCES
    assert not undeclared, (
        "Wizard page resources embedded in the gresource with no "
        "`resource_path=` referencing them:\n"
        + "\n".join(f"  {p}" for p in sorted(undeclared))
        + "\nDrop the <file> and blueprint entries, or add to "
        "_UNCONSUMED_PAGE_RESOURCES with a tracking issue."
    )
