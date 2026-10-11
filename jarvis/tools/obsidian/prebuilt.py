"""Prebuilt Obsidian plugins — ready-to-run community bundles, installed by JARVIS.

An Obsidian plugin ships as a handful of plain files:

    main.js        the built bundle — this is the "prebuilt" part, nothing is
                   compiled on your machine
    manifest.json  id, name, version, minAppVersion
    styles.css     optional

Those exact files are attached to every release of the plugin's GitHub repo, so
JARVIS installs a plugin the same way Obsidian's own community store does — no
npm, no bundler, no Obsidian account, no build step::

    python -m jarvis obsidian plugins --install-prebuilt all

Per plugin, the best available source is used:

1. a bundle vendored in ``obsidian-plugin/prebuilt/<id>/`` — offline, instant;
   populate that folder once with ``scripts/fetch_prebuilt_plugins.py``
2. otherwise the pinned tag (``tag=``) or the latest GitHub release

Downloads are never a black box: when the release publishes a sha256 digest it
must match what arrives, and every install writes ``.jarvis-provenance.json``
next to the plugin with the repo, tag, asset URLs, file hashes and timestamp.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from .vault import ObsidianError

log = get_logger("tools.obsidian.prebuilt")

#: asset names Obsidian itself looks for inside a plugin folder
ASSET_NAMES: Tuple[str, ...] = ("manifest.json", "main.js", "styles.css")
REQUIRED_ASSETS: Tuple[str, ...] = ("manifest.json", "main.js")

DEFAULT_API_BASE = "https://api.github.com"
USER_AGENT = "JARVIS-Obsidian/1.0 (+https://github.com/heostertech-cyber/jarvis)"
MAX_ASSET_BYTES = 48 * 1024 * 1024        # sanity cap — the big ones are ~5 MB
PROVENANCE_FILE = ".jarvis-provenance.json"


class PluginFetchError(ObsidianError):
    """A prebuilt plugin could not be fetched, verified or installed."""


# ── the catalogue ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PrebuiltPlugin:
    """One ready-to-run community plugin (all free, all open source)."""

    id: str                      # folder name inside <vault>/.obsidian/plugins/
    name: str
    repo: str                    # owner/name on GitHub
    why: str                     # what it does for a student
    pinned_tag: str = ""         # "" = follow the latest release
    caveat: str = ""             # honest note when something is unusual

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.repo}/releases"


PREBUILT_PLUGINS: Tuple[PrebuiltPlugin, ...] = (
    PrebuiltPlugin("dataview", "Dataview", "blacksmithgu/obsidian-dataview",
                   "live tables of tasks, flashcards and exam countdowns"),
    PrebuiltPlugin("obsidian-spaced-repetition", "Spaced Repetition",
                   "st3v3nmw/obsidian-spaced-repetition",
                   "drills the `Question :: Answer` cards JARVIS writes"),
    PrebuiltPlugin("obsidian-tasks-plugin", "Tasks",
                   "obsidian-tasks-group/obsidian-tasks",
                   "due dates, priorities and queries for every `- [ ]`"),
    PrebuiltPlugin("calendar", "Calendar", "liamcain/obsidian-calendar-plugin",
                   "one click into any daily note"),
    PrebuiltPlugin("templater-obsidian", "Templater", "SilentVoid13/Templater",
                   "{{date}} / {{title}} templates with scripts"),
    PrebuiltPlugin("obsidian-git", "Git", "Vinzent03/obsidian-git",
                   "auto-backup the vault (free, offline, version history)"),
    PrebuiltPlugin("obsidian-excalidraw-plugin", "Excalidraw",
                   "zsviczian/obsidian-excalidraw-plugin",
                   "diagrams for physics and chemistry"),
    PrebuiltPlugin("obsidian-kanban", "Kanban", "mgmeyers/obsidian-kanban",
                   "board view of the syllabus and the revision plan"),
    PrebuiltPlugin("obsidian-admonition", "Admonition", "javalent/admonitions",
                   "pretty callouts for formulas and warnings"),
    PrebuiltPlugin("cm-editor-syntax-highlight-obsidian", "Editor Syntax Highlight",
                   "deathau/cm-editor-syntax-highlight-obsidian",
                   "legible code blocks",
                   caveat="older plugin — if its releases carry no bundle, take the build "
                          "from the repository instead"),
)

#: convenience alias: every id, in catalogue order
ALL_PREBUILT = tuple(plugin.id for plugin in PREBUILT_PLUGINS)


def find_prebuilt(name: str) -> Optional[PrebuiltPlugin]:
    """Look a plugin up by id, display name or unambiguous prefix."""
    wanted = (name or "").strip().lower().replace(" ", "-")
    if not wanted:
        return None
    for plugin in PREBUILT_PLUGINS:
        if wanted in (plugin.id.lower(), plugin.name.lower().replace(" ", "-")):
            return plugin
    for plugin in PREBUILT_PLUGINS:
        if plugin.id.lower().startswith(wanted) or wanted.startswith(plugin.id.lower()):
            return plugin
    for plugin in PREBUILT_PLUGINS:
        if wanted in plugin.id.lower():
            return plugin
    return None


def resolve_selection(spec: str) -> Tuple[List[PrebuiltPlugin], List[str]]:
    """Turn ``"all"`` / ``"dataview, tasks"`` into plugins, plus unknown names."""
    text = (spec or "").strip()
    if not text or text.lower() in ("all", "*", "recommended", "everything"):
        return list(PREBUILT_PLUGINS), []
    chosen: List[PrebuiltPlugin] = []
    unknown: List[str] = []
    for raw in text.replace(";", ",").split(","):
        name = raw.strip()
        if not name:
            continue
        plugin = find_prebuilt(name)
        if plugin is None:
            unknown.append(name)
        elif plugin not in chosen:
            chosen.append(plugin)
    return chosen, unknown


# ── where bundles live ──────────────────────────────────────────────────────
def vendored_root() -> Path:
    """``obsidian-plugin/prebuilt/`` inside this project."""
    return Path(__file__).resolve().parents[3] / "obsidian-plugin" / "prebuilt"


def vendored_bundle(plugin_id: str, *, root: Optional[Path] = None) -> Optional[Path]:
    folder = (Path(root) if root else vendored_root()) / plugin_id
    if (folder / "manifest.json").is_file() and (folder / "main.js").is_file():
        return folder
    return None


def vendored_plugins(*, root: Optional[Path] = None) -> Dict[str, Path]:
    base = Path(root) if root else vendored_root()
    found: Dict[str, Path] = {}
    if base.is_dir():
        for folder in sorted(base.iterdir()):
            if folder.is_dir() and vendored_bundle(folder.name, root=base):
                found[folder.name] = folder
    return found


def github_api_base() -> str:
    """GitHub API root — override with ``JARVIS_GITHUB_API`` (mirrors, tests)."""
    return (os.environ.get("JARVIS_GITHUB_API") or DEFAULT_API_BASE).rstrip("/")


# ── tiny HTTP layer (stdlib only, proxy-aware, token-aware) ─────────────────
def _request(url: str, *, timeout: float, accept: str = "") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept or "*/*"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if token and url.startswith(DEFAULT_API_BASE):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    last: Optional[Exception] = None
    for _attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_ASSET_BYTES + 1)
            if len(data) > MAX_ASSET_BYTES:
                raise PluginFetchError(f"{url} is larger than {MAX_ASSET_BYTES} bytes")
            return data
        except PluginFetchError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise PluginFetchError(f"{url} → HTTP {exc.code}") from exc
            last = exc
        except Exception as exc:               # URLError, timeouts, TLS, DNS
            last = exc
    raise PluginFetchError(f"cannot reach {url}: {last}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ReleaseInfo:
    tag: str
    published_at: str = ""
    assets: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def url(self, name: str) -> str:
        return str(self.assets.get(name, {}).get("url") or "")


def resolve_release(plugin: PrebuiltPlugin, *, tag: str = "", timeout: float = 30,
                    api_base: str = "") -> ReleaseInfo:
    """Read the release's asset list (the prebuilt bundle) from GitHub."""
    base = (api_base or github_api_base()).rstrip("/")
    path = (f"/repos/{plugin.repo}/releases/tags/{tag}" if tag
            else f"/repos/{plugin.repo}/releases/latest")
    try:
        payload = json.loads(_request(base + path, timeout=timeout,
                                      accept="application/vnd.github+json"))
    except json.JSONDecodeError as exc:
        raise PluginFetchError(f"{plugin.repo}: release metadata was not JSON") from exc
    if not isinstance(payload, dict) or payload.get("message"):
        raise PluginFetchError(f"{plugin.repo}: {payload.get('message', 'no release')}")

    assets: Dict[str, Dict[str, Any]] = {}
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        if name not in ASSET_NAMES:
            continue
        assets[name] = {"url": asset.get("browser_download_url") or asset.get("url") or "",
                        "size": int(asset.get("size") or 0),
                        "digest": str(asset.get("digest") or "")}
    info = ReleaseInfo(tag=str(payload.get("tag_name") or tag or ""),
                       published_at=str(payload.get("published_at") or ""),
                       assets=assets)
    missing = [name for name in REQUIRED_ASSETS if name not in assets]
    if missing:
        seen = sorted(str(a.get("name")) for a in (payload.get("assets") or []))
        raise PluginFetchError(
            f"{plugin.repo} release {info.tag or tag or 'latest'} has no prebuilt bundle "
            f"(missing {', '.join(missing)}; found {', '.join(seen) or 'nothing'}) — "
            "install this one from Obsidian's community store instead")
    return info


# ── staging (download or copy, verify, move into place) ─────────────────────
def _read_manifest(data: bytes, plugin: PrebuiltPlugin) -> Dict[str, Any]:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise PluginFetchError(f"{plugin.id}: manifest.json unreadable ({exc})") from exc
    if not isinstance(manifest, dict) or not manifest.get("id"):
        raise PluginFetchError(f"{plugin.id}: manifest.json has no plugin id")
    if str(manifest["id"]) != plugin.id:
        log.warning(f"{plugin.id}: release manifest says id={manifest['id']} — "
                    "installing under the manifest's own id")
    if not manifest.get("version"):
        log.warning(f"{plugin.id}: manifest has no version")
    return manifest


def _finalise(tmp: Path, destination: Path, plugin: PrebuiltPlugin,
              manifest: Dict[str, Any], *, source: str, tag: str,
              urls: Dict[str, str]) -> Dict[str, Any]:
    """Move a staged bundle into the vault and write its provenance."""
    hashes: Dict[str, str] = {}
    files: List[str] = []
    for item in sorted(tmp.rglob("*")):
        if item.is_file():
            relative = item.relative_to(tmp).as_posix()
            files.append(relative)
            hashes[relative] = _sha256(item.read_bytes())

    provenance = {
        "installed_by": "jarvis",
        "plugin_id": str(manifest.get("id") or plugin.id),
        "name": str(manifest.get("name") or plugin.name),
        "version": str(manifest.get("version") or ""),
        "source": source,                       # "github-release" | "vendored"
        "repo": plugin.repo,
        "tag": tag,
        "urls": urls,
        "sha256": hashes,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "licence": "free/open-source community plugin — see the repository",
    }
    (tmp / PROVENANCE_FILE).write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    if PROVENANCE_FILE not in files:
        files.append(PROVENANCE_FILE)

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.move(str(tmp), str(destination))
    return {"plugin_id": provenance["plugin_id"], "name": provenance["name"],
            "version": provenance["version"], "source": source, "tag": tag,
            "path": str(destination), "files": files, "sha256": hashes,
            "bytes": sum((destination / f).stat().st_size for f in files)}


def _stage_from_vendor(plugin: PrebuiltPlugin, vendor: Path, destination: Path) -> Dict[str, Any]:
    manifest_path = vendor / "manifest.json"
    if not manifest_path.is_file() or not (vendor / "main.js").is_file():
        raise PluginFetchError(f"{vendor} is not a runnable plugin (needs manifest.json + main.js)")
    tmp = Path(tempfile.mkdtemp(dir=str(destination.parent), prefix=f".{plugin.id}-"))
    try:
        for item in sorted(vendor.rglob("*")):
            if item.is_dir() or item.name.startswith("."):
                continue
            target = tmp / item.relative_to(vendor)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        manifest = _read_manifest((vendor / "manifest.json").read_bytes(), plugin)
        return _finalise(tmp, destination, plugin, manifest, source="vendored",
                         tag=manifest.get("version", ""), urls={"vendored": str(vendor)})
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _stage_from_release(plugin: PrebuiltPlugin, destination: Path, *, tag: str,
                        timeout: float, api_base: str) -> Dict[str, Any]:
    release = resolve_release(plugin, tag=tag, timeout=timeout, api_base=api_base)
    tmp = Path(tempfile.mkdtemp(dir=str(destination.parent), prefix=f".{plugin.id}-"))
    try:
        blob = _request(release.url("manifest.json"), timeout=timeout)
        manifest = _read_manifest(blob, plugin)
        (tmp / "manifest.json").write_bytes(blob)

        for name in ASSET_NAMES:
            if name == "manifest.json" or name not in release.assets:
                continue
            asset = release.assets[name]
            data = _request(str(asset.get("url") or ""), timeout=timeout)
            digest = str(asset.get("digest") or "")
            if digest.startswith("sha256:"):
                expected = digest.split(":", 1)[1].strip().lower()
                actual = _sha256(data)
                if expected and expected != actual:
                    raise PluginFetchError(
                        f"{plugin.id}: sha256 mismatch for {name} "
                        f"(release says {expected[:16]}…, got {actual[:16]}…) — aborted")
            (tmp / name).write_bytes(data)
            log.info(f"{plugin.id}: {name} {len(data) / 1024:.0f} KiB ok")
        urls = {name: release.url(name) for name in ASSET_NAMES if name in release.assets}
        return _finalise(tmp, destination, plugin, manifest, source="github-release",
                         tag=release.tag, urls=urls)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def stage_bundle(plugin: PrebuiltPlugin, destination: Path, *, tag: str = "",
                 offline: bool = False, timeout: float = 60, api_base: str = "",
                 vendored_root_path: Optional[Path] = None) -> Dict[str, Any]:
    """Put a runnable plugin folder at ``destination`` (download, or vendored copy)."""
    vendor = vendored_bundle(plugin.id, root=vendored_root_path)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if offline:
        if vendor is None:
            root = (Path(vendored_root_path) if vendored_root_path else vendored_root())
            raise PluginFetchError(
                f"offline mode: no vendored bundle for '{plugin.id}' in {root} — "
                "run scripts/fetch_prebuilt_plugins.py once on a machine with "
                "internet, or drop the bundle there by hand")
        return _stage_from_vendor(plugin, vendor, destination)
    try:
        return _stage_from_release(plugin, destination, tag=tag, timeout=timeout,
                                   api_base=api_base)
    except ObsidianError as exc:
        if vendor is None:
            raise
        log.warning(f"{plugin.id}: download failed ({exc}) — using the vendored bundle")
        staged = _stage_from_vendor(plugin, vendor, destination)
        staged["warning"] = f"used the vendored bundle ({exc})"
        return staged


def vendor_bundle(plugin: PrebuiltPlugin, *, root: Optional[Path] = None, tag: str = "",
                  timeout: float = 120, api_base: str = "") -> Dict[str, Any]:
    """Download a release bundle into ``obsidian-plugin/prebuilt/<id>/`` for offline use."""
    base = Path(root) if root else vendored_root()
    base.mkdir(parents=True, exist_ok=True)
    staged = _stage_from_release(plugin, base / plugin.id, tag=tag, timeout=timeout,
                                 api_base=api_base)
    return staged


# ── vault-level operations ──────────────────────────────────────────────────
def _study():
    """Late import — ``study`` imports this module's catalogue."""
    from . import study
    return study


def enabled_prebuilt_ids(vault: Path, *, vendored_root_path: Optional[Path] = None) -> List[str]:
    installed = {plugin.id for plugin in _study().installed_plugins(vault)}
    return [rule_id for rule_id in ALL_PREBUILT if rule_id in installed]


def install_prebuilt(vault: Path, plugins: Sequence[PrebuiltPlugin], *,
                     enable: bool = True, tag: str = "", offline: bool = False,
                     timeout: float = 60, api_base: str = "",
                     vendored_root_path: Optional[Path] = None) -> Dict[str, Any]:
    """Install prebuilt community plugins into ``<vault>/.obsidian/plugins/``."""
    vault = Path(vault)
    study = _study()
    installed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for plugin in plugins:
        destination = study.community_plugin_dir(vault) / plugin.id
        try:
            staged = stage_bundle(plugin, destination, tag=tag, offline=offline,
                                  timeout=timeout, api_base=api_base,
                                  vendored_root_path=vendored_root_path)
        except ObsidianError as exc:
            log.warning(f"{plugin.id}: {exc}")
            failed.append({"id": plugin.id, "name": plugin.name, "error": str(exc)})
            continue

        # the manifest's own id wins if a release renamed itself
        real_id = str(staged.get("plugin_id") or plugin.id)
        if real_id != plugin.id:
            renamed = study.community_plugin_dir(vault) / real_id
            if renamed.exists():
                shutil.rmtree(renamed)
            shutil.move(str(destination), str(renamed))
            staged["path"] = str(renamed)
        staged["enabled"] = False
        if enable:
            try:
                staged["enabled"] = _enable(vault, real_id, study)
            except OSError as exc:                     # pragma: no cover - disk issues
                staged["warning"] = f"installed but could not be enabled: {exc}"
        installed.append(staged)

    return {"ok": bool(installed) and not failed,
            "installed": installed, "failed": failed,
            "counts": {"requested": len(plugins), "installed": len(installed),
                       "failed": len(failed)},
            "restart_hint": "In Obsidian: Settings → Community plugins → turn off "
                            "Restricted mode, then enable the plugin (reload Obsidian "
                            "if it is already open)."}


def _enable(vault: Path, plugin_id: str, study: Any) -> bool:
    ids = study.enabled_plugin_ids(vault)
    if plugin_id in ids:
        return False
    ids.append(plugin_id)
    study.obsidian_config_dir(vault).mkdir(parents=True, exist_ok=True)
    (study.obsidian_config_dir(vault) / "community-plugins.json").write_text(
        json.dumps(ids, indent=2), encoding="utf-8")
    return True


def prebuilt_status(vault: Path, *, vendored_root_path: Optional[Path] = None,
                    check_releases: bool = False, timeout: float = 20,
                    api_base: str = "") -> List[Dict[str, Any]]:
    """Catalogue + install state for every prebuilt plugin (never raises)."""
    study = _study()
    vault = Path(vault)
    installed = {plugin.id: plugin for plugin in study.installed_plugins(vault)}
    vendored = vendored_plugins(root=vendored_root_path)
    rows: List[Dict[str, Any]] = []
    for plugin in PREBUILT_PLUGINS:
        info = installed.get(plugin.id)
        row: Dict[str, Any] = {
            "id": plugin.id, "name": plugin.name, "repo": plugin.repo, "why": plugin.why,
            "caveat": plugin.caveat, "installed": info is not None,
            "enabled": bool(info and info.enabled),
            "version": (info.version if info else ""),
            "vendored": plugin.id in vendored,
            "pinned_tag": plugin.pinned_tag,
        }
        if check_releases:
            try:
                release = resolve_release(plugin, tag=plugin.pinned_tag, timeout=timeout,
                                          api_base=api_base)
                row["latest"] = release.tag
                row["update_available"] = bool(info and info.version
                                               and release.tag != info.version)
            except ObsidianError as exc:
                row["latest"] = ""
                row["error"] = str(exc)
        rows.append(row)
    return rows


def describe_prebuilt(rows: Iterable[Dict[str, Any]]) -> str:
    """Render :func:`prebuilt_status` as markdown for the CLI and the tools."""
    lines = ["**Prebuilt community plugins** (free, installed ready-to-run):"]
    for row in rows:
        if row.get("enabled"):
            mark = "✓"
        elif row.get("installed"):
            mark = "◐"
        else:
            mark = "·"
        bits = []
        if row.get("version"):
            bits.append(f"v{row['version']}")
        if row.get("vendored"):
            bits.append("vendored")
        if row.get("update_available"):
            bits.append(f"update → {row['latest']}")
        if row.get("error"):
            bits.append(f"⚠ {row['error']}")
        state = "enabled" if row.get("enabled") else ("installed" if row.get("installed")
                                                      else "not installed")
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"  {mark} **{row['name']}** `{row['id']}` — {state}{suffix}")
        if row.get("why"):
            lines.append(f"      {row['why']}")
        if row.get("caveat"):
            lines.append(f"      ↳ {row['caveat']}")
    return "\n".join(lines)


__all__ = ["PREBUILT_PLUGINS", "ALL_PREBUILT", "PrebuiltPlugin", "PluginFetchError",
           "ReleaseInfo", "resolve_release", "resolve_selection", "find_prebuilt",
           "stage_bundle", "vendor_bundle", "install_prebuilt", "prebuilt_status",
           "describe_prebuilt", "vendored_bundle", "vendored_plugins", "vendored_root",
           "enabled_prebuilt_ids", "github_api_base", "ASSET_NAMES"]
