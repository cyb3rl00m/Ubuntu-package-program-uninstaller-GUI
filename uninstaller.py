#!/usr/bin/env python3
"""Uninstaller — list and completely remove APT/DNF, Snap and Flatpak software (Ubuntu and Fedora).

GTK 4 + libadwaita.  https://github.com/cyb3rl00m/uninstaller
"""
import glob
import os
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field

APP_ID = "io.github.cyb3rl00m.Uninstaller"
VERSION = "1.1.0"
REPO = "https://github.com/cyb3rl00m/uninstaller"

if "--version" in sys.argv:
    print(f"Uninstaller {VERSION}")
    sys.exit(0)

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402


# =========================================================================== backend

HOME = os.path.expanduser("~")

# Ubuntu meta-packages whose removal usually means "half the desktop goes away"
CRITICAL_META = {
    "ubuntu-desktop", "ubuntu-desktop-minimal", "ubuntu-minimal", "ubuntu-standard",
    "ubuntu-session", "gnome-shell", "gdm3", "snapd", "apt", "dpkg", "sudo", "sudo-rs",
    "network-manager", "pkexec", "polkitd", "systemd", "linux-generic",
}

# Folder names in ~/.config etc. that are shared and must never be offered for deletion
SHARED_DIRS = {
    "applications", "icons", "mime", "fonts", "themes", "autostart", "trash",
    "keyrings", "dconf", "systemd", "gtk-3.0", "gtk-4.0", "environment.d",
    "user-dirs.dirs", "local", "config", "cache", "share", "flatpak", "snap",
}

DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/snapd/desktop/applications",
    "/var/lib/flatpak/exports/share/applications",
    os.path.join(HOME, ".local/share/flatpak/exports/share/applications"),
]


@dataclass
class Package:
    source: str                 # "apt" | "dnf" | "snap" | "flatpak"
    name: str                   # apt/rpm package / snap name / flatpak app id
    version: str = ""
    summary: str = ""
    size_bytes: int = 0
    display_name: str = ""
    desktop_files: list = field(default_factory=list)
    is_app: bool = False        # has a visible launcher in the app grid
    manual: bool = True         # apt/dnf: installed by you (not pulled in as a dependency)
    system: bool = False        # essential / required / snap base -> extra warning
    residual: bool = False      # apt: removed, but config files still on disk
    installation: str = ""      # flatpak: "user" / "system" / custom

    @property
    def title(self):
        return self.display_name or self.name


# --------------------------------------------------------------------------- helpers

def _run(cmd):
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def human_size(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(text):
    """'1.2 GB' / '512.0 kB' (flatpak, 1000-based) -> bytes."""
    m = re.search(r"([\d.,]+)\s*([kKMGT]?B|bytes)", text.replace("\xa0", " "))
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    mult = {"B": 1, "bytes": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    return int(num * mult.get(m.group(2), 1))


def parse_desktop_file(path):
    """Tiny .desktop parser -> dict of keys in [Desktop Entry]."""
    data, in_entry = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    in_entry = line == "[Desktop Entry]"
                    continue
                if in_entry and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    data.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return data


def is_visible_app(d):
    return (d.get("Type") == "Application"
            and d.get("NoDisplay", "").lower() != "true"
            and d.get("Hidden", "").lower() != "true")


def all_desktop_files():
    out = []
    for d in DESKTOP_DIRS:
        out.extend(sorted(glob.glob(os.path.join(d, "*.desktop"))))
    return out


def _attach_desktop(pkg, paths):
    for p in paths:
        d = parse_desktop_file(p)
        pkg.desktop_files.append(p)
        if is_visible_app(d) and not pkg.is_app:
            pkg.is_app = True
            pkg.display_name = d.get("Name", "") or pkg.display_name


# --------------------------------------------------------------------------- APT

def _apt_desktop_map():
    """desktop-file path -> owning dpkg package (read straight from dpkg's file lists)."""
    owner = {}
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-5]
        try:
            with open(lst, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "/share/applications/" in line and line.rstrip().endswith(".desktop"):
                        owner[line.rstrip("\n")] = pkg
        except OSError:
            pass
    by_pkg = {}
    for path, pkg in owner.items():
        by_pkg.setdefault(pkg, []).append(path)
        by_pkg.setdefault(pkg.split(":")[0], []).append(path)
    return by_pkg


def list_apt():
    fmt = ("${binary:Package}\t${Version}\t${Installed-Size}\t${Priority}\t"
           "${Essential}\t${db:Status-Abbrev}\t${binary:Summary}\n")
    rc, out, _ = _run(["dpkg-query", "-W", "-f=" + fmt])
    if rc != 0:
        return []
    _, manual_out, _ = _run(["apt-mark", "showmanual"])
    manual = set(manual_out.split())
    desktop = _apt_desktop_map()

    pkgs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, ver, size, prio, essential, status, summary = parts[:7]
        state = status[1:2] if len(status) > 1 else ""
        if state in ("n", ""):          # not installed
            continue
        p = Package(
            source="apt", name=name, version=ver, summary=summary,
            size_bytes=int(size) * 1024 if size.isdigit() else 0,
            manual=(name in manual or name.split(":")[0] in manual),
            system=(essential == "yes" or prio in ("required", "important")),
            residual=(state == "c"),
        )
        if not p.residual:
            _attach_desktop(p, sorted(set(desktop.get(name, []))))
        pkgs.append(p)
    return pkgs


def simulate_apt_purge(name):
    """Dry-run. Returns (list_of_packages_that_would_go, error_text)."""
    rc, out, err = _run(["apt-get", "-s", "purge", "--autoremove", name])
    if rc != 0:
        return [], (err or out).strip()
    removed = []
    for line in out.splitlines():
        if line.startswith(("Purg ", "Remv ")):
            removed.append(line.split()[1])
    return removed, ""


# --------------------------------------------------------------------------- DNF / RPM (Fedora)

# Fedora packages whose removal breaks the desktop or the package manager itself
FEDORA_CRITICAL = {
    "fedora-release", "fedora-release-common", "fedora-release-workstation", "fedora-release-kde",
    "fedora-release-identity-basic", "fedora-release-identity-workstation", "fedora-release-identity-kde",
    "gnome-shell", "gdm", "sddm", "plasma-workspace", "dnf", "dnf5", "rpm", "libdnf5", "python3-dnf",
    "systemd", "sudo", "polkit", "glibc", "bash", "coreutils", "NetworkManager", "kernel",
    "kernel-core", "grub2-common", "shim-x64", "dracut", "selinux-policy", "selinux-policy-targeted",
}
RPM_ARCHES = {"x86_64", "noarch", "i686", "i386", "aarch64", "ppc64le", "s390x", "armv7hl", "src"}


def is_ostree():
    """Fedora Atomic (Silverblue, Kinoite…): the base system is read-only, dnf can't remove from it."""
    return os.path.exists("/run/ostree-booted")


def _dnf_protected():
    names = set()
    for f in glob.glob("/etc/dnf/protected.d/*.conf"):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                names |= {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
        except OSError:
            pass
    return names


def _dnf_userinstalled():
    """Names of packages you installed yourself, or None if dnf can't tell us."""
    rc, out, _ = _run(["dnf", "-q", "repoquery", "--userinstalled", "--disablerepo=*",
                       "--queryformat", "%{name}\n"])
    names = set(out.split()) if rc == 0 else set()
    return names or None


def _rpm_desktop_map():
    """desktop-file path -> owning rpm package name."""
    files = [f for f in glob.glob("/usr/share/applications/*.desktop")
             + glob.glob("/usr/local/share/applications/*.desktop")]
    if not files:
        return {}
    rc, out, _ = _run(["rpm", "-qf", "--qf", "[%{FILENAMES}\t%{=NAME}\n]", *files])
    wanted, owner = set(files), {}
    for line in out.splitlines():
        path, _, name = line.partition("\t")
        if name and path in wanted:
            owner.setdefault(name, []).append(path)
    return owner


def list_dnf():
    if is_ostree():
        return []
    fmt = "%{NAME}\t%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\t%{ARCH}\t%{SIZE}\t%{SUMMARY}\n"
    rc, out, _ = _run(["rpm", "-qa", "--qf", fmt])
    if rc != 0:
        return []
    rows = [ln.split("\t") for ln in out.splitlines()]
    rows = [r for r in rows if len(r) >= 5 and r[0] != "gpg-pubkey"]
    counts = {}
    for r in rows:
        counts[r[0]] = counts.get(r[0], 0) + 1
    manual = _dnf_userinstalled()
    protected = _dnf_protected()
    desktop = _rpm_desktop_map()

    pkgs = []
    for name, ver, arch, size, summary in (r[:5] for r in rows):
        # several versions/arches of one name (kernels, i686 libs): address each one exactly
        ident = name if counts[name] == 1 else f"{name}-{ver.split(':')[-1]}.{arch}"
        p = Package(
            source="dnf", name=ident, version=ver, summary=summary,
            size_bytes=int(size) if size.isdigit() else 0,
            manual=True if manual is None else name in manual,
            system=name in protected or name in FEDORA_CRITICAL,
        )
        if ident != name:
            p.display_name = name
        _attach_desktop(p, sorted(desktop.get(name, [])))
        pkgs.append(p)
    return pkgs


def parse_dnf_transaction(out):
    """Package names from the 'Removing…' sections of dnf4/dnf5 transaction output."""
    removed, in_section = [], False
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith("transaction summary"):
            break
        if s.endswith(":"):                 # section header: "Removing:", "Removing unused dependencies:"…
            in_section = s.startswith("Removing")
            continue
        if not in_section or set(s) <= set("=- "):
            continue
        first = s.split()[0]
        if first in RPM_ARCHES or first == "replacing" or not re.fullmatch(r"[\w.+-]+", first):
            continue                        # continuation of a wrapped line / not a package row
        removed.append(first)
    return list(dict.fromkeys(removed))


def simulate_dnf_remove(name, use_pkexec=False):
    """Dry-run. Returns (packages, error_text, cancelled).

    dnf refuses to resolve a removal without admin rights, so if the unprivileged
    try is refused this is repeated through pkexec (one password prompt)."""
    cmd = ["dnf", "remove", "--assumeno", name]
    rc, out, err = _run((["pkexec"] if use_pkexec else []) + cmd)
    text = out + "\n" + err
    if use_pkexec and rc in (126, 127):
        return [], "", True
    removed = parse_dnf_transaction(text)
    if removed:
        return removed, "", False
    if not use_pkexec and re.search(r"superuser privileges", text, re.I):
        return simulate_dnf_remove(name, use_pkexec=True)
    lines = [ln for ln in text.strip().splitlines() if "Operation aborted" not in ln]
    return [], "\n".join(lines[-15:]) or "dnf could not work out what to remove.", False


def rpm_config_files(names):
    """Config files owned by these packages (to clean up *.rpmsave copies after removal)."""
    rc, out, _ = _run(["rpm", "-qc", *names])
    return sorted({ln for ln in out.splitlines() if ln.startswith("/")})


# --------------------------------------------------------------------------- distro

def _os_release():
    data = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                data[k] = v.strip('"')
    except OSError:
        pass
    return data


def detect_native():
    """'apt' on Ubuntu/Debian, 'dnf' on Fedora/RHEL family, else whatever tool exists."""
    rel = _os_release()
    ids = (rel.get("ID", "") + " " + rel.get("ID_LIKE", "")).split()
    if any(i in ("fedora", "rhel", "centos") for i in ids):
        return "dnf"
    if any(i in ("debian", "ubuntu") for i in ids):
        return "apt"
    if subprocess.run(["sh", "-c", "command -v dpkg-query"], capture_output=True).returncode == 0:
        return "apt"
    if subprocess.run(["sh", "-c", "command -v rpm"], capture_output=True).returncode == 0:
        return "dnf"
    return None


NATIVE = detect_native()
NATIVE_LABEL = {"apt": "APT", "dnf": "DNF"}.get(NATIVE, "")
DISTRO_NAME = _os_release().get("NAME", "your system")


# --------------------------------------------------------------------------- Snap

SNAP_SYSTEM_NOTES = ("base", "core", "snapd", "gadget", "kernel")


def list_snap():
    rc, out, _ = _run(["snap", "list"])
    if rc != 0:
        return []
    pkgs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        name, ver = parts[0], parts[1]
        publisher = parts[4] if len(parts) >= 6 else ""
        notes = parts[-1]
        size = sum(os.path.getsize(f) for f in glob.glob(f"/var/lib/snapd/snaps/{name}_*.snap")
                   if os.path.isfile(f))
        p = Package(
            source="snap", name=name, version=ver,
            summary=f"Snap by {publisher.rstrip('✓*✪')}" if publisher and publisher != "-" else "Snap package",
            size_bytes=size,
            system=any(n in notes.split(",") for n in SNAP_SYSTEM_NOTES) or name == "snapd",
        )
        _attach_desktop(p, sorted(glob.glob(f"/var/lib/snapd/desktop/applications/{name}_*.desktop")))
        pkgs.append(p)
    return pkgs


# --------------------------------------------------------------------------- Flatpak

def list_flatpak():
    rc, out, _ = _run(["flatpak", "list", "--app",
                       "--columns=application,name,description,version,size,installation"])
    if rc != 0:
        return []
    pkgs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        app_id, name, desc, ver, size, inst = parts[:6]
        p = Package(source="flatpak", name=app_id, display_name=name, summary=desc,
                    version=ver, size_bytes=parse_size(size), installation=inst.strip())
        cands = [f"/var/lib/flatpak/exports/share/applications/{app_id}.desktop",
                 os.path.join(HOME, f".local/share/flatpak/exports/share/applications/{app_id}.desktop")]
        _attach_desktop(p, [c for c in cands if os.path.exists(c)])
        p.display_name = p.display_name or name
        p.is_app = True                 # --app only lists applications
        pkgs.append(p)
    return pkgs


# --------------------------------------------------------------------------- all

def load_all():
    pkgs = []
    native = {"apt": [list_apt], "dnf": [list_dnf]}.get(NATIVE, [])
    for fn in (*native, list_snap, list_flatpak):
        try:
            pkgs.extend(fn())
        except Exception as e:          # one broken source must not kill the list
            print(f"[uninstaller] {fn.__name__} failed: {e}")
    return pkgs


# --------------------------------------------------------------------------- removal

def user_data_paths(pkg):
    """Folders in your home directory that belong to this app (exact-name matches only)."""
    names = {pkg.name, pkg.name.split(":")[0]}
    if pkg.display_name:
        names |= {pkg.display_name, pkg.display_name.replace(" ", ""),
                  pkg.display_name.replace(" ", "-")}
    for d in pkg.desktop_files:
        base = os.path.basename(d)[:-8]                  # strip .desktop
        if pkg.source == "snap":
            base = base.split("_", 1)[-1]
        names.add(base)
        if base.count(".") >= 2:                         # org.gnome.Foo -> Foo
            names.add(base.rsplit(".", 1)[-1])
    names = {n.lower() for n in names if len(n) >= 3 and n.lower() not in SHARED_DIRS}

    found = []
    roots = [os.path.join(HOME, r) for r in (".config", ".local/share", ".local/state", ".cache")]
    for root in roots:
        try:
            for entry in os.listdir(root):
                if entry.lower() in names:
                    found.append(os.path.join(root, entry))
        except OSError:
            pass
    try:
        for entry in os.listdir(HOME):
            if entry.startswith(".") and entry[1:].lower() in names:
                found.append(os.path.join(HOME, entry))
    except OSError:
        pass
    snap_home = os.path.join(HOME, "snap", pkg.name)
    if pkg.source == "snap" and os.path.exists(snap_home):
        found.append(snap_home)
    # Flatpak's ~/.var/app/<id> is already deleted by `flatpak uninstall --delete-data`
    return sorted(set(found))


def _flatpak_scope(pkg):
    if pkg.installation in ("user", "system"):
        return [f"--{pkg.installation}"]
    return [f"--installation={pkg.installation}"] if pkg.installation else []


def uninstall_commands(pkg, config_files=()):
    """Commands that remove the package + its system-wide config/data."""
    if pkg.source == "apt":
        return [["pkexec", "env", "DEBIAN_FRONTEND=noninteractive",
                 "apt-get", "purge", "--autoremove", "-y", pkg.name]]
    if pkg.source == "dnf":
        # dnf removes unused dependencies by default (clean_requirements_on_remove).
        # RPM keeps edited config files as *.rpmsave; delete those too, like apt's purge.
        script = "dnf remove -y -- " + shlex.quote(pkg.name)
        if config_files:
            script += ('\nfor f in "$@"; do if [ -e "$f.rpmsave" ]; then '
                       'rm -f -- "$f.rpmsave" && echo "Deleted leftover config: $f.rpmsave"; fi; done')
        return [["pkexec", "sh", "-ec", script, "uninstaller", *config_files]]
    if pkg.source == "snap":
        return [["pkexec", "snap", "remove", "--purge", pkg.name]]
    if pkg.source == "flatpak":
        scope = _flatpak_scope(pkg)
        return [["flatpak", "uninstall", "-y", "--noninteractive", "--delete-data", *scope, pkg.name],
                ["flatpak", "uninstall", "-y", "--noninteractive", "--unused", *scope]]
    return []


# =========================================================================== GUI


CSS = """
.badge { border-radius: 99px; padding: 1px 9px; font-size: 0.78em; font-weight: 700; }
.badge-apt     { background: alpha(#e95420, 0.18); color: #e95420; }
.badge-dnf     { background: alpha(#3c6eb4, 0.20); color: #3c6eb4; }
.badge-snap    { background: alpha(#3fa37a, 0.20); color: #3fa37a; }
.badge-flatpak { background: alpha(#4a90d9, 0.20); color: #4a90d9; }
.badge-warn    { background: alpha(#e01b24, 0.18); color: #e01b24; }
.badge-dim     { background: alpha(currentColor, 0.10); }
.pkg-row { padding: 8px 14px; }
.mono { font-family: monospace; font-size: 0.9em; }
"""


class Item(GObject.Object):
    def __init__(self, pkg):
        super().__init__()
        self.pkg = pkg
        self.blob = " ".join([pkg.name, pkg.display_name, pkg.summary]).lower()


class PkgRow(Gtk.Box):
    """One row: icon | name + description | badges | version | size | Uninstall."""

    _icon_cache = {}

    def __init__(self, on_uninstall):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("pkg-row")
        self.item = None

        self.icon = Gtk.Image(pixel_size=32)
        self.append(self.icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        text.append(self.title)
        text.append(self.subtitle)
        self.append(text)

        self.badges = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.append(self.badges)

        self.version = Gtk.Label(width_chars=14, max_width_chars=14, xalign=1,
                                 ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.version.add_css_class("dim-label")
        self.version.add_css_class("caption")
        self.append(self.version)

        self.size = Gtk.Label(width_chars=9, xalign=1)
        self.size.add_css_class("dim-label")
        self.size.add_css_class("numeric")
        self.append(self.size)

        self.button = Gtk.Button(label="Uninstall", valign=Gtk.Align.CENTER, width_request=104)
        self.button.add_css_class("destructive-action")
        self.button.connect("clicked", lambda *_: self.item and on_uninstall(self.item.pkg))
        self.append(self.button)

    def _badge(self, text, css, tooltip=None):
        lbl = Gtk.Label(label=text, tooltip_text=tooltip)
        lbl.add_css_class("badge")
        lbl.add_css_class(css)
        self.badges.append(lbl)

    def _gicon(self, pkg):
        for path in pkg.desktop_files:
            if path in self._icon_cache:
                return self._icon_cache[path]
            info = Gio.DesktopAppInfo.new_from_filename(path)
            icon = info.get_icon() if info else None
            self._icon_cache[path] = icon
            if icon:
                return icon
        name = "application-x-executable" if pkg.is_app else "package-x-generic"
        return Gio.ThemedIcon.new_with_default_fallbacks(name)

    def bind(self, item):
        self.item = item
        p = item.pkg
        self.icon.set_from_gicon(self._gicon(p))
        self.title.set_text(p.title)
        sub = p.summary
        if p.display_name and p.display_name != p.name:
            sub = f"{p.name} · {sub}" if sub else p.name
        self.subtitle.set_text(sub)
        self.subtitle.set_tooltip_text(sub)
        self.version.set_text(p.version)
        self.version.set_tooltip_text(p.version)
        self.size.set_text(human_size(p.size_bytes))

        while (c := self.badges.get_first_child()):
            self.badges.remove(c)
        self._badge({"apt": "APT", "dnf": "DNF", "snap": "Snap", "flatpak": "Flatpak"}[p.source],
                    f"badge-{p.source}")
        if p.system:
            self._badge("System", "badge-warn", f"Core system package — removing it can break {DISTRO_NAME}")
        if p.residual:
            self._badge("Leftover config", "badge-dim", "Already uninstalled, only config files remain")
        elif p.source in ("apt", "dnf") and not p.manual:
            self._badge("Dependency", "badge-dim", "Installed automatically as a dependency")
        if p.source == "flatpak" and p.installation == "user":
            self._badge("User", "badge-dim")
        self.button.set_label("Purge" if p.residual else "Uninstall")


class Window(Adw.ApplicationWindow):
    # dropdown labels -> source keys (native package manager first)
    SOURCES = [None] + ([NATIVE_LABEL] if NATIVE_LABEL else []) + ["Snap", "Flatpak"]
    SOURCE_KEYS = [None] + ([NATIVE] if NATIVE_LABEL else []) + ["snap", "flatpak"]

    def __init__(self, app):
        super().__init__(application=app, title="Uninstaller", icon_name=APP_ID, default_width=1000, default_height=720)
        self.busy = False
        self.loading = True

        # ---- model: store -> filter -> sort -> listview
        self.store = Gio.ListStore(item_type=Item)
        self.filter = Gtk.CustomFilter.new(self._match, None)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorter = Gtk.CustomSorter.new(self._compare, None)
        sorted_model = Gtk.SortListModel(model=self.filtered, sorter=self.sorter)
        self.filtered.connect("items-changed", lambda *_: self._update_count())

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda f, li: li.set_child(PkgRow(self.on_uninstall)))
        factory.connect("bind", lambda f, li: li.get_child().bind(li.get_item()))
        self.listview = Gtk.ListView(model=Gtk.NoSelection(model=sorted_model), factory=factory,
                                     show_separators=True)

        # ---- header: search in the middle, refresh on the left
        header = Adw.HeaderBar()
        self.search = Gtk.SearchEntry(placeholder_text="Search apps and packages…", width_request=380)
        self.search.connect("search-changed", lambda *_: self._refilter())
        self.search.set_key_capture_widget(self)        # just start typing anywhere
        header.set_title_widget(self.search)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload list")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_start(refresh)
        menu = Gio.Menu()
        menu.append("About Uninstaller", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", lambda *_: self.show_about())
        self.add_action(about)

        # ---- filter bar
        bar = Gtk.Box(spacing=10, margin_start=14, margin_end=14, margin_top=8, margin_bottom=8)
        self.view_dd = Gtk.DropDown.new_from_strings(["Apps", "All packages"])
        self.source_dd = Gtk.DropDown.new_from_strings(["All sources", *self.SOURCES[1:]])
        self.sort_dd = Gtk.DropDown.new_from_strings(["Sort: Name", "Sort: Size"])
        self.manual_cb = Gtk.CheckButton(label="Hide dependencies")
        self.manual_cb.set_tooltip_text(f"Hide {NATIVE_LABEL or 'system'} packages that were installed automatically")
        for w in (self.view_dd, self.source_dd):
            w.connect("notify::selected", lambda *_: self._refilter())
        self.sort_dd.connect("notify::selected", lambda *_: self.sorter.changed(Gtk.SorterChange.DIFFERENT))
        self.manual_cb.connect("toggled", lambda *_: self._refilter())
        self.count = Gtk.Label(hexpand=True, xalign=1)
        self.count.add_css_class("dim-label")
        for w in (self.view_dd, self.source_dd, self.sort_dd, self.manual_cb, self.count):
            bar.append(w)

        # ---- stack: loading / list / empty
        self.stack = Gtk.Stack(vexpand=True)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        loading.append(spinner)
        loading.append(Gtk.Label(label="Reading installed software…"))
        self.stack.add_named(loading, "loading")
        self.stack.add_named(Gtk.ScrolledWindow(child=self.listview), "list")
        self.stack.add_named(Adw.StatusPage(icon_name="system-search-symbolic", title="Nothing found",
                                            description="Try a different search or filter"), "empty")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if NATIVE == "dnf" and is_ostree():
            content.append(Adw.Banner(revealed=True, title="Fedora Atomic: the base system is managed by "
                                      "rpm-ostree, so only Flatpaks and Snaps are listed here."))
        content.append(bar)
        content.append(Gtk.Separator())
        content.append(self.stack)

        self.toasts = Adw.ToastOverlay(child=content)
        tv = Adw.ToolbarView(content=self.toasts)
        tv.add_top_bar(header)
        self.set_content(tv)
        self.reload()

    # ------------------------------------------------------------ list
    def reload(self):
        self.loading = True
        self.stack.set_visible_child_name("loading")
        threading.Thread(target=lambda: GLib.idle_add(self._populate, load_all()),
                         daemon=True).start()

    def _populate(self, pkgs):
        self.loading = False
        self.store.splice(0, self.store.get_n_items(), [Item(p) for p in pkgs])
        self._update_count()
        return False

    def _match(self, item, *_):
        p = item.pkg
        if self.view_dd.get_selected() == 0 and not p.is_app:
            return False
        src = self.SOURCE_KEYS[self.source_dd.get_selected()]
        if src and p.source != src:
            return False
        if self.manual_cb.get_active() and p.source == NATIVE and not p.manual and not p.is_app:
            return False
        q = self.search.get_text().strip().lower()
        return all(word in item.blob for word in q.split()) if q else True

    def _compare(self, a, b, *_):
        if self.sort_dd.get_selected() == 1:
            d = b.pkg.size_bytes - a.pkg.size_bytes
            if d:
                return -1 if d < 0 else 1
        ta, tb = a.pkg.title.lower(), b.pkg.title.lower()
        return (ta > tb) - (ta < tb)

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _update_count(self):
        n = self.filtered.get_n_items()
        self.count.set_text(f"{n} shown · {self.store.get_n_items()} installed")
        if not self.loading:
            self.stack.set_visible_child_name("list" if n else "empty")

    def show_about(self):
        Adw.AboutDialog(application_name="Uninstaller", application_icon=APP_ID, version=VERSION,
                        developer_name="cyb3rl00m", website=REPO, issue_url=REPO + "/issues",
                        license_type=Gtk.License.MIT_X11,
                        comments="Completely remove apps and packages: APT, DNF, Snap and Flatpak, "
                                 "including the leftover files in your home folder.").present(self)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    # ------------------------------------------------------------ uninstall
    def on_uninstall(self, pkg):
        if self.busy:
            self.toast("Wait for the current uninstall to finish")
            return
        self.busy = True

        if pkg.source == "dnf":
            self.toast("Checking what will be removed… (dnf may ask for your password)")

        def work():
            config = []
            if pkg.source == "apt":
                removed, err = simulate_apt_purge(pkg.name)
            elif pkg.source == "dnf":
                removed, err, cancelled = simulate_dnf_remove(pkg.name)
                if cancelled:
                    GLib.idle_add(self._cancelled)
                    return
                config = rpm_config_files(removed) if removed else []
            else:
                removed, err = [pkg.name], ""
            paths = user_data_paths(pkg)
            GLib.idle_add(self._confirm, pkg, removed, err, paths, config)
        threading.Thread(target=work, daemon=True).start()

    def _cancelled(self):
        self.busy = False
        self.toast("Cancelled — authentication was not given")
        return False

    def _confirm(self, pkg, removed, err, paths, config=()):
        if err:
            self.busy = False
            d = Adw.AlertDialog(heading=f"Can't remove {pkg.title}", body=err)
            d.add_response("ok", "OK")
            d.present(self)
            return False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        removed_set = set(removed)
        critical = FEDORA_CRITICAL | _dnf_protected() if pkg.source == "dnf" else CRITICAL_META
        danger = sorted(n for n in removed_set if n.split(":")[0] in critical)
        if pkg.system:
            danger.insert(0, pkg.name)

        if pkg.source in ("apt", "dnf"):
            extra = len(removed) - 1
            verb = "purged" if pkg.source == "apt" else "removed"
            box.append(self._heading(
                f"{len(removed)} package{'s' if len(removed) != 1 else ''} will be {verb}"
                + (f" ({extra} unused dependenc{'ies' if extra != 1 else 'y'})" if extra > 0 else "")))
            lst = Gtk.Label(label="\n".join(sorted(removed)), xalign=0, selectable=True, wrap=True)
            lst.add_css_class("mono")
            box.append(Gtk.ScrolledWindow(child=lst, min_content_height=min(150, 20 * len(removed) + 10),
                                          max_content_height=200, propagate_natural_height=True))

        checks = []
        if paths:
            box.append(self._heading("Your personal data (moved to Trash):"))
            for path in paths:
                cb = Gtk.CheckButton(label=path.replace(HOME, "~", 1), active=not pkg.system)
                checks.append((cb, path))
                box.append(cb)

        d = Adw.AlertDialog(heading=f"Uninstall {pkg.title}?",
                            body="Removes the program, its system files and settings."
                            if not pkg.residual else "Removes the leftover configuration files.")
        d.add_response("cancel", "Cancel")
        d.add_response("go", "Purge" if pkg.residual else "Uninstall")
        d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")

        if danger:
            warn = Gtk.Label(xalign=0, wrap=True, label=
                             "⚠ This touches core system packages: " + ", ".join(dict.fromkeys(danger))
                             + f". {DISTRO_NAME} may stop working properly.")
            warn.add_css_class("error")
            box.prepend(warn)
            ok = Gtk.CheckButton(label="I understand, remove it anyway")
            ok.connect("toggled", lambda b: d.set_response_enabled("go", b.get_active()))
            box.append(ok)
            d.set_response_enabled("go", False)

        d.set_extra_child(box)

        def on_response(_d, resp):
            if resp != "go":
                self.busy = False
                return
            self._run(pkg, [p for cb, p in checks if cb.get_active()], config)
        d.connect("response", on_response)
        d.present(self)
        return False

    @staticmethod
    def _heading(text):
        lbl = Gtk.Label(label=text, xalign=0, wrap=True)
        lbl.add_css_class("heading")
        return lbl

    def _run(self, pkg, trash_paths, config=()):
        # progress dialog with live log
        dlg = Adw.Dialog(title=f"Removing {pkg.title}", content_width=640, content_height=420,
                         can_close=False)
        buf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=buf, editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=8, bottom_margin=8, left_margin=10, right_margin=10)
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True)
        status = Gtk.Label(label="Working… (you may be asked for your password)", margin_top=8,
                           margin_bottom=8, wrap=True)
        close = Gtk.Button(label="Close", sensitive=False, halign=Gtk.Align.CENTER, margin_bottom=12)
        close.add_css_class("pill")
        close.connect("clicked", lambda *_: dlg.close())
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for w in (scroller, status, close):
            body.append(w)
        tv = Adw.ToolbarView(content=body)
        tv.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False))
        dlg.set_child(tv)
        dlg.present(self)

        def log(text):
            def _append():
                buf.insert(buf.get_end_iter(), text)
                view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
                return False
            GLib.idle_add(_append)

        def finish(ok, msg):
            def _done():
                status.set_text(msg)
                status.add_css_class("success" if ok else "error")
                close.set_sensitive(True)
                dlg.set_can_close(True)
                self.busy = False
                self.toast(msg)
                if ok:
                    self.reload()
                return False
            GLib.idle_add(_done)

        def work():
            env = dict(os.environ, LC_ALL="C")
            for cmd in uninstall_commands(pkg, config):
                if cmd[:2] == ["pkexec", "sh"]:      # dnf: show the command, not the helper script
                    log("$ pkexec " + cmd[3].splitlines()[0] + "\n")
                else:
                    log("$ " + " ".join(cmd) + "\n")
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1, env=env)
                except FileNotFoundError as e:
                    return finish(False, f"Failed: {e}")
                for line in proc.stdout:
                    log(line)
                rc = proc.wait()
                if rc in (126, 127) and cmd[0] == "pkexec":
                    return finish(False, "Cancelled — authentication was not given")
                if rc != 0:
                    return finish(False, f"Failed (exit code {rc}) — see log above")
            for path in trash_paths:
                try:
                    Gio.File.new_for_path(path).trash(None)
                    log(f"Moved to Trash: {path}\n")
                except GLib.Error as e:
                    log(f"Could not trash {path}: {e.message}\n")
            finish(True, f"{pkg.title} was removed")

        threading.Thread(target=work, daemon=True).start()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            css = Gtk.CssProvider()
            css.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = Window(self)
            focus = Gtk.ShortcutController()
            focus.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                                Gtk.CallbackAction.new(lambda *_: win.search.grab_focus() or True)))
            win.add_controller(focus)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
