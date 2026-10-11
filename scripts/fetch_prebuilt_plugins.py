#!/usr/bin/env python3
"""Vendor the prebuilt Obsidian plugin bundles into ``obsidian-plugin/prebuilt/``.

Run this **once on a machine with internet**.  It downloads the exact release
files Obsidian itself would install (``main.js``, ``manifest.json`` and, when the
release has one, ``styles.css``) for every plugin in JARVIS's catalogue, verifies
the sha256 digests GitHub publishes, and writes them next to the project:

    python scripts/fetch_prebuilt_plugins.py                 # everything
    python scripts/fetch_prebuilt_plugins.py --only dataview,tasks
    python scripts/fetch_prebuilt_plugins.py --tag 0.5.70    # pin a release
    python scripts/fetch_prebuilt_plugins.py --out /other/prebuilt

After that, any vault can be filled offline:

    python -m jarvis obsidian plugins --install-prebuilt all --offline

Each bundle gets JARVIS's provenance file (repo, tag, urls, sha256 of every
file) plus a shared ``ATTRIBUTION.md`` and ``SHA256SUMS`` for auditing.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from jarvis.tools.obsidian.prebuilt import (PREBUILT_PLUGINS, PROVENANCE_FILE,  # noqa: E402
                                            ObsidianError, resolve_selection,
                                            vendor_bundle)

DEFAULT_OUT = REPO_ROOT / "obsidian-plugin" / "prebuilt"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(root: Path) -> Path:
    lines = []
    for folder in sorted(root.glob("*/")):
        for item in sorted(folder.rglob("*")):
            if item.is_file():
                lines.append(f"{sha256_file(item)}  {item.relative_to(root).as_posix()}")
    target = root / "SHA256SUMS"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_attribution(root: Path, entries: list[dict]) -> Path:
    lines = [
        "# Prebuilt Obsidian plugin bundles",
        "",
        "Vendored by `scripts/fetch_prebuilt_plugins.py` so JARVIS can install them",
        "offline (`python -m jarvis obsidian plugins --install-prebuilt all --offline`).",
        "",
        f"Fetched {datetime.now(timezone.utc).isoformat(timespec='seconds')}.",
        "",
        "| plugin | version | repository | licence |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(f"| {entry['name']} | {entry['version']} | "
                     f"https://github.com/{entry['repo']} | see repository (free/open source) |")
    lines += [
        "",
        "These are the unmodified release artefacts of each project, redistributed",
        "for convenience. Every project keeps its own licence and copyright — see",
        "the linked repository before redistributing further.",
        "",
        "Integrity: `sha256sum -c SHA256SUMS`, and each folder carries",
        f"`{PROVENANCE_FILE}` with the release tag and per-file hashes.",
        "",
    ]
    target = root / "ATTRIBUTION.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="all",
                        help='which plugins: "all" or ids/names, comma separated')
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="vendor directory")
    parser.add_argument("--tag", default="", help="pin a release tag for every plugin")
    parser.add_argument("--timeout", type=float, default=120.0, help="per file, seconds")
    args = parser.parse_args()

    selection, unknown = resolve_selection(args.only)
    for name in unknown:
        print(f"⚠ unknown plugin: {name}")
        matches = [p.id for p in PREBUILT_PLUGINS if name.lower() in p.id]
        if matches:
            print(f"   did you mean: {', '.join(matches)}")
    if not selection:
        parser.error("nothing selected")

    root = Path(args.out).resolve()
    print(f"vendoring {len(selection)} plugin(s) → {root}")
    entries: list[dict] = []
    failures = 0
    for plugin in selection:
        try:
            staged = vendor_bundle(plugin, root=root, tag=args.tag, timeout=args.timeout)
        except ObsidianError as exc:
            failures += 1
            print(f"  ⚠ {plugin.name}: {exc}")
            continue
        entries.append({"name": staged["name"], "version": staged["version"],
                        "repo": plugin.repo})
        kib = staged["bytes"] / 1024
        print(f"  ✅ {staged['name']} v{staged['version']} ({kib:.0f} KiB, "
              f"{len(staged['files'])} files)")

    if entries:
        checksums = write_checksums(root)
        attribution = write_attribution(root, entries)
        print(f"\n{len(entries)} bundle(s) ready in {root}")
        print(f"  {checksums.name} + {attribution.name} written")
        print("  commit them to keep offline installs working, or leave them out "
              "(JARVIS will download on demand)")
    if failures and not entries:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
