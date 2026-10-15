# Changelog

## 1.1.0 - 2026-10-15
- **Fedora support.** RPM packages are listed next to Snaps and Flatpaks and removed with `dnf remove`,
  including unused dependencies and leftover `.rpmsave` config files.
- Dry-run preview of every package dnf will remove; Fedora's protected packages get the core-package warning.
- "Dependency" labels and the "Hide dependencies" filter work on Fedora too.
- Fedora Atomic (Silverblue, Kinoite…) is detected: only Flatpaks and Snaps are listed there.
- Installer installs the right dependencies on Fedora (`dnf`) and Ubuntu (`apt`).
- Flatpak data in `~/.var/app` is no longer offered twice (`--delete-data` already removes it).

## 1.0.0 - 2026-10-13
- First public release.
- One searchable list of APT, Snap and Flatpak software with Uninstall buttons.
- Complete removal: purge + unused dependencies, snap `--purge`, Flatpak data and unused runtimes.
- Finds leftover folders in the home directory and moves the chosen ones to the Trash.
- Dry-run preview for APT removals; extra confirmation for core system packages.
- Leftover-config packages can be purged.
