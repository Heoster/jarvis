# Prebuilt plugin bundles (offline cache)

JARVIS installs Obsidian community plugins by downloading the **release files
Obsidian itself uses** — `main.js` (the built bundle), `manifest.json` and, when
the release has one, `styles.css`. Nothing is compiled, no npm, no build step:

```bash
python -m jarvis obsidian plugins --install-prebuilt all            # download
python -m jarvis obsidian plugins --install-prebuilt "dataview, tasks"
python -m jarvis obsidian plugins --install-prebuilt all --offline  # from here
python -m jarvis obsidian plugins --update                          # newest releases
python -m jarvis obsidian plugins --list-prebuilt --checked         # what's newer
```

This folder is the **offline cache** for that. Populate it once on a machine with
internet:

```bash
python scripts/fetch_prebuilt_plugins.py            # all ten plugins
python scripts/fetch_prebuilt_plugins.py --only dataview,obsidian-tasks-plugin
```

It writes one folder per plugin (`<plugin-id>/main.js`, `manifest.json`, …), a
`SHA256SUMS` file and `ATTRIBUTION.md` listing every project and its repository.
`--install-prebuilt all --offline` then works with no network at all, and any
download that fails at install time automatically falls back to a bundle here.

Layout:

```
obsidian-plugin/prebuilt/
├── dataview/
│   ├── main.js
│   ├── manifest.json
│   ├── styles.css
│   └── .jarvis-provenance.json     repo · tag · urls · sha256 of every file
├── obsidian-spaced-repetition/
├── …
├── SHA256SUMS
└── ATTRIBUTION.md
```

Bundles are the unmodified artefacts of each project and stay under that
project's own licence — check the repository listed in `ATTRIBUTION.md` before
redistributing. Nothing here is committed by default; add the folders you want
to keep if you would rather not download them again.

The catalogue (ids, repos, why each plugin matters for studying) lives in
`jarvis/tools/obsidian/prebuilt.py`.
