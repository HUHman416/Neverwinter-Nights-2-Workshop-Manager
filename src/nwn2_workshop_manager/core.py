from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath

APP_ID = "2738630"
APP_NAME = "NWN2 Workshop Manager"
APP_SLUG = "nwn2-workshop-manager"
GAME_DOCUMENTS = "Neverwinter Nights 2"
VERSION = "0.3.1"


class ManagerError(RuntimeError):
    """Base error suitable for showing in the GUI."""


class ConfigurationError(ManagerError):
    pass


class InitializationRequired(ManagerError):
    pass


class SyncSafetyError(ManagerError):
    pass


@dataclass
class Settings:
    workshop_dir: str = ""
    destination_dir: str = ""
    initialized: bool = False
    disabled_mods: list[str] = field(default_factory=list)
    priority_low_to_high: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    auto_preview: bool = True
    check_updates: bool = True
    file_winners: dict[str, str] = field(default_factory=dict)

    @property
    def workshop(self) -> Path:
        return Path(self.workshop_dir).expanduser()

    @property
    def destination(self) -> Path:
        return Path(self.destination_dir).expanduser()


@dataclass(frozen=True)
class SteamCandidate:
    library: Path
    workshop: Path
    destination: Path
    has_workshop: bool
    has_destination: bool

    @property
    def score(self) -> int:
        return (4 if self.has_workshop else 0) + (4 if self.has_destination else 0)


@dataclass
class ModInfo:
    mod_id: str
    name: str
    directory: Path
    files: dict[str, Path]
    total_size: int
    enabled: bool
    priority: int
    conflict_count: int = 0
    skipped_symlinks: int = 0


@dataclass(frozen=True)
class Conflict:
    relative_path: str
    providers: tuple[str, ...]
    winner: str
    category: str = "Priority overlap"
    status: str = "Pending"


@dataclass(frozen=True)
class PlanAction:
    kind: str
    relative_path: str
    mod_id: str = ""
    detail: str = ""


@dataclass
class SyncPlan:
    mods: list[ModInfo]
    winners: dict[str, tuple[str, Path]]
    providers: dict[str, tuple[str, ...]]
    conflicts: list[Conflict]
    actions: list[PlanAction]
    unchanged: int
    warnings: list[str] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for action in self.actions if action.kind == kind)


@dataclass
class SyncResult:
    installed: int = 0
    updated: int = 0
    restored: int = 0
    removed: int = 0
    unchanged: int = 0
    managed: int = 0
    conflicts: int = 0
    messages: list[str] = field(default_factory=list)


def default_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_SLUG


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_SLUG


class ConfigStore:
    def __init__(self, config_dir: Path | None = None, state_dir: Path | None = None):
        self.config_dir = config_dir or default_config_dir()
        self.state_dir = state_dir or default_state_dir()
        self.config_file = self.config_dir / "config.json"
        self.managed_file = self.state_dir / "managed.json"
        self.baselines_file = self.state_dir / "baselines.json"
        self.originals_dir = self.state_dir / "originals"
        self.lock_file = self.state_dir / ".sync.lock"
        self.log_file = self.state_dir / "sync.log"

    def load(self) -> Settings:
        raw = _read_json(self.config_file, {})
        allowed = Settings.__dataclass_fields__.keys()
        values = {key: value for key, value in raw.items() if key in allowed}
        try:
            return Settings(**values)
        except TypeError:
            return Settings()

    def save(self, settings: Settings) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.config_file, asdict(settings))

    def load_managed(self) -> dict:
        return _read_json(self.managed_file, {"version": 2, "files": {}})

    def load_baselines(self) -> dict:
        return _read_json(self.baselines_file, {"version": 1, "files": {}})

    def append_log(self, lines: Iterable[str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}]\n")
            for line in lines:
                handle.write(f"{line}\n")


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise ManagerError("Another Workshop sync is already running.") from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _read_json(path: Path, fallback: dict) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else fallback
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _steam_roots(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    roots = [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
    ]

    user = home.name
    for pattern_root in (Path("/mnt"), Path("/var/mnt"), Path("/run/media") / user):
        if pattern_root.is_dir():
            try:
                roots.extend(path for path in pattern_root.glob("*/SteamLibrary") if path.is_dir())
                roots.extend(path for path in pattern_root.glob("*") if (path / "steamapps").is_dir())
            except OSError:
                continue

    discovered: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        key = str(root.resolve())
        if key not in seen:
            seen.add(key)
            discovered.append(root.resolve())

        library_file = root / "steamapps/libraryfolders.vdf"
        if library_file.is_file():
            try:
                text = library_file.read_text(encoding="utf-8", errors="replace")
                for raw in re.findall(r'"path"\s+"((?:\\.|[^"\\])*)"', text):
                    library = Path(raw.replace("\\\\", "\\"))
                    if library.exists():
                        resolved = library.resolve()
                        lib_key = str(resolved)
                        if lib_key not in seen:
                            seen.add(lib_key)
                            discovered.append(resolved)
            except OSError:
                pass
    return discovered


def detect_steam_candidates(home: Path | None = None) -> list[SteamCandidate]:
    candidates: list[SteamCandidate] = []
    for root in _steam_roots(home):
        steamapps = root / "steamapps"
        workshop = steamapps / "workshop/content" / APP_ID
        destination = (
            steamapps
            / "compatdata"
            / APP_ID
            / "pfx/drive_c/users/steamuser/Documents"
            / GAME_DOCUMENTS
        )
        has_workshop = workshop.is_dir()
        has_destination = destination.is_dir()
        if has_workshop or has_destination:
            candidates.append(
                SteamCandidate(root, workshop, destination, has_workshop, has_destination)
            )
    return sorted(candidates, key=lambda candidate: (-candidate.score, str(candidate.library)))


def apply_best_detection(settings: Settings, home: Path | None = None) -> bool:
    candidates = detect_steam_candidates(home)
    if not candidates:
        return False
    best = candidates[0]
    changed = False
    if not settings.workshop_dir:
        settings.workshop_dir = str(best.workshop)
        changed = True
    if not settings.destination_dir:
        settings.destination_dir = str(best.destination)
        changed = True
    return changed


def validate_settings(settings: Settings) -> None:
    if not settings.workshop_dir or not settings.destination_dir:
        raise ConfigurationError("Choose both the Workshop and NWN2 Documents folders.")
    if not settings.workshop.is_dir():
        raise ConfigurationError(f"Workshop folder was not found:\n{settings.workshop}")
    if not settings.destination.exists():
        raise ConfigurationError(f"NWN2 Documents folder was not found:\n{settings.destination}")
    if not settings.destination.is_dir():
        raise ConfigurationError(f"NWN2 destination is not a folder:\n{settings.destination}")


def normalize_relative_path(relative: str | Path) -> str:
    raw = str(relative).replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise SyncSafetyError(f"Unsafe Workshop path rejected: {relative}")

    parts = list(path.parts)
    if parts[0].casefold() == "ui":
        parts[0] = "ui"
    if len(parts) == 1 and parts[0].casefold() == "dialog.tlk":
        parts[0] = "dialog.tlk"
    return PurePosixPath(*parts).as_posix()


def _priority_order(settings: Settings, mod_ids: Iterable[str]) -> list[str]:
    ids = set(mod_ids)
    order = [mod_id for mod_id in settings.priority_low_to_high if mod_id in ids]
    order.extend(sorted(ids - set(order), key=lambda item: (int(item) if item.isdigit() else 0, item)))
    settings.priority_low_to_high = order
    return order


def _mod_display_name(mod_id: str, settings: Settings) -> str:
    alias = settings.aliases.get(mod_id, "").strip()
    return alias or f"Workshop Item {mod_id}"


def scan_mods(settings: Settings, store: ConfigStore | None = None, full: bool = False, progress=None) -> tuple[list[ModInfo], list[str]]:
    validate_settings(settings)
    warnings: list[str] = []
    mod_dirs = sorted(
        (path for path in settings.workshop.iterdir() if path.is_dir()),
        key=lambda path: (int(path.name) if path.name.isdigit() else 0, path.name),
    )
    order = _priority_order(settings, (path.name for path in mod_dirs))
    ranks = {mod_id: index for index, mod_id in enumerate(order)}
    disabled = set(settings.disabled_mods)
    mods: list[ModInfo] = []
    cache_path = (store or ConfigStore()).state_dir / "scan-cache.json"
    cache = _read_json(cache_path, {})
    if cache.get("root") != str(settings.workshop.resolve()):
        cache = {}
    cached = cache.get("items", {})
    manifest = settings.workshop.parent.parent / f"appworkshop_{APP_ID}.acf"
    try:
        manifest_text = manifest.read_text(encoding="utf-8")
    except OSError:
        manifest_text = ""
    fresh = {}

    for index, mod_dir in enumerate(mod_dirs):
        if mod_dir.is_symlink():
            continue
        if progress:
            progress(index + 1, len(mod_dirs), f"Checking mod {mod_dir.name}")
        # Each Steam item has a flat installed-metadata block containing its
        # content manifest and update time. Include every block for this ID.
        blocks = re.findall(r'"' + re.escape(mod_dir.name) + r'"\s*\{([^{}]*)\}', manifest_text)
        signature = [blocks, mod_dir.stat().st_mtime_ns]
        previous = cached.get(mod_dir.name, {})
        if blocks and not full and previous.get("signature") == signature:
            files = {rel: mod_dir / src for rel, src in previous["files"].items()}
            mods.append(ModInfo(mod_dir.name, _mod_display_name(mod_dir.name, settings), mod_dir,
                                files, previous["size"], mod_dir.name not in disabled, ranks[mod_dir.name]))
            fresh[mod_dir.name] = previous
            continue
        files: dict[str, Path] = {}
        total_size = 0
        skipped = 0
        try:
            paths = []
            for count, path in enumerate(mod_dir.rglob("*"), 1):
                paths.append(path)
                if progress and count % 200 == 0:
                    progress(index + 1, len(mod_dirs), f"Scanning mod {mod_dir.name}: {count:,} entries")
            paths.sort(key=lambda path: path.as_posix().casefold())
        except OSError as exc:
            raise SyncSafetyError(f"Could not read Workshop item {mod_dir.name}: {exc}") from exc
        for source in paths:
            if source.is_symlink():
                skipped += 1
                continue
            if not source.is_file():
                continue
            relative = normalize_relative_path(source.relative_to(mod_dir))
            files[relative] = source
            try:
                total_size += source.stat().st_size
            except OSError:
                pass
        if skipped:
            warnings.append(
                f"Workshop item {mod_dir.name}: skipped {skipped} symbolic link(s) for safety."
            )
        mods.append(
            ModInfo(
                mod_id=mod_dir.name,
                name=_mod_display_name(mod_dir.name, settings),
                directory=mod_dir,
                files=files,
                total_size=total_size,
                enabled=mod_dir.name not in disabled,
                priority=ranks[mod_dir.name],
                skipped_symlinks=skipped,
            )
        )
        fresh[mod_dir.name] = {"signature": signature, "size": total_size,
                              "files": {rel: str(src.relative_to(mod_dir)) for rel, src in files.items()}}
    _atomic_write_json(cache_path, {"root": str(settings.workshop.resolve()), "items": fresh})
    return mods, warnings


def file_stamp(path: Path) -> list[int] | None:
    try:
        st = path.stat()
        return [st.st_size, st.st_mtime_ns, st.st_ctime_ns]
    except OSError:
        return None


def _files_equal(first: Path, second: Path) -> bool:
    try:
        if not first.is_file() or not second.is_file():
            return False
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as first_handle, second.open("rb") as second_handle:
            while True:
                first_chunk = first_handle.read(1024 * 1024)
                second_chunk = second_handle.read(1024 * 1024)
                if first_chunk != second_chunk:
                    return False
                if not first_chunk:
                    return True
    except OSError:
        return False


def _safe_target(root: Path, relative: str) -> Path:
    normalized = normalize_relative_path(relative)
    root_resolved = root.resolve()
    target = root.joinpath(*PurePosixPath(normalized).parts)
    try:
        parent = target.parent.resolve(strict=False)
    except OSError as exc:
        raise SyncSafetyError(f"Could not resolve destination for {relative}: {exc}") from exc
    if not parent.is_relative_to(root_resolved):
        raise SyncSafetyError(f"Destination path escapes the NWN2 folder: {relative}")
    return target


def maybe_import_legacy_state(settings: Settings, store: ConfigStore) -> bool:
    if store.managed_file.exists():
        return False
    legacy = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "nwn2-workshop-sync"
    manifest = legacy / "managed-files.tsv"
    if not manifest.is_file():
        return False

    managed: dict[str, dict] = {}
    baselines: dict[str, dict] = {}
    store.originals_dir.mkdir(parents=True, exist_ok=True)
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or "\t" not in line:
            continue
        raw_relative, mod_id = line.split("\t", 1)
        relative = normalize_relative_path(raw_relative)
        managed[relative] = {"winner": mod_id.strip(), "providers": [mod_id.strip()]}
        legacy_original = _safe_target(legacy / "originals", relative)
        new_original = _safe_target(store.originals_dir, relative)
        if legacy_original.is_file():
            new_original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_original, new_original)
            baselines[relative] = {"present": True}
        else:
            baselines[relative] = {"present": False}

    store.state_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        store.managed_file,
        {
            "version": 2,
            "destination": str(settings.destination),
            "workshop": str(settings.workshop),
            "files": managed,
            "imported_from_legacy": True,
        },
    )
    _atomic_write_json(store.baselines_file, {"version": 1, "files": baselines})
    settings.initialized = True
    store.save(settings)
    return True


def build_sync_plan(settings: Settings, store: ConfigStore, full: bool = False, progress=None) -> SyncPlan:
    validate_settings(settings)
    maybe_import_legacy_state(settings, store)
    mods, warnings = scan_mods(settings, store, full, progress)

    enabled = sorted((mod for mod in mods if mod.enabled), key=lambda mod: mod.priority)
    provider_map: dict[str, list[str]] = {}
    winner_map: dict[str, tuple[str, Path]] = {}
    for mod in enabled:
        for relative, source in mod.files.items():
            provider_map.setdefault(relative, []).append(mod.mod_id)
            winner_map[relative] = (mod.mod_id, source)

    providers = {relative: tuple(values) for relative, values in provider_map.items()}
    by_id = {mod.mod_id: mod for mod in enabled}
    for relative, preferred in settings.file_winners.items():
        if preferred in provider_map.get(relative, []):
            winner_map[relative] = (preferred, by_id[preferred].files[relative])
    comparisons_path = store.state_dir / "conflict-cache.json"
    comparisons = _read_json(comparisons_path, {})
    fresh_comparisons = {}
    conflicts = []
    overlaps = [(rel, ids) for rel, ids in providers.items() if len(ids) > 1]
    for index, (relative, values) in enumerate(overlaps, 1):
        if progress:
            progress(index, len(overlaps), f"Comparing overlap {relative}")
        sources = [by_id[mod_id].files[relative] for mod_id in values]
        signature = [[str(src), file_stamp(src)] for src in sources]
        cached = comparisons.get(relative, {})
        if not full and cached.get("signature") == signature and all(stamp for _, stamp in signature):
            identical = cached.get("identical", False)
        else:
            identical = all(_files_equal(sources[0], src) for src in sources[1:])
        fresh_comparisons[relative] = {"signature": signature, "identical": identical}
        category = "Identical duplicate" if identical else (
            "Custom winner" if settings.file_winners.get(relative) == winner_map[relative][0] else "Priority overlap")
        conflicts.append(Conflict(relative, values, winner_map[relative][0], category))
    _atomic_write_json(comparisons_path, fresh_comparisons)
    conflict_paths_by_mod: dict[str, int] = {}
    for conflict in conflicts:
        for mod_id in conflict.providers:
            conflict_paths_by_mod[mod_id] = conflict_paths_by_mod.get(mod_id, 0) + 1
    for mod in mods:
        mod.conflict_count = conflict_paths_by_mod.get(mod.mod_id, 0)

    managed_state = store.load_managed()
    old_files = managed_state.get("files", {})
    recorded_destination = managed_state.get("destination", "")
    if old_files and recorded_destination:
        current = os.path.abspath(os.path.expanduser(settings.destination_dir))
        recorded = os.path.abspath(os.path.expanduser(recorded_destination))
        if current != recorded:
            raise ConfigurationError(
                "The existing manager state belongs to a different NWN2 destination:\n"
                f"{recorded}\n\nReturn to that destination before synchronizing."
            )
    baselines = store.load_baselines().get("files", {})
    actions: list[PlanAction] = []
    unchanged = 0

    for index, (relative, (mod_id, source)) in enumerate(sorted(winner_map.items()), 1):
        if progress and (index % 100 == 0 or index == len(winner_map)):
            progress(index, len(winner_map), f"Checking file {index:,}/{len(winner_map):,}: {relative}")
        destination = _safe_target(settings.destination, relative)
        old_winner = old_files.get(relative, {}).get("winner", "")
        old = old_files.get(relative, {})
        if (not full and old_winner == mod_id and old.get("source_stamp") == file_stamp(source)
                and old.get("destination_stamp") == file_stamp(destination)
                and old.get("source_stamp") is not None):
            unchanged += 1
            continue
        if not destination.exists():
            actions.append(PlanAction("install", relative, mod_id, "File is not installed"))
        elif old_winner != mod_id or not _files_equal(source, destination):
            detail = "Conflict winner changed" if old_winner and old_winner != mod_id else "Workshop file changed"
            actions.append(PlanAction("update", relative, mod_id, detail))
        else:
            unchanged += 1

    for relative, old in sorted(old_files.items()):
        if relative in winner_map:
            continue
        destination = _safe_target(settings.destination, relative)
        baseline = baselines.get(relative, {})
        if baseline.get("present"):
            actions.append(PlanAction("restore", relative, old.get("winner", ""), "Restore original file"))
        elif destination.exists() or destination.is_symlink():
            actions.append(PlanAction("remove", relative, old.get("winner", ""), "No subscribed mod owns this file"))

    pending = {action.relative_path for action in actions}
    conflicts = [replace(conflict, status="Pending" if conflict.relative_path in pending else "Applied")
                 for conflict in conflicts]
    return SyncPlan(
        mods=mods,
        winners=winner_map,
        providers=providers,
        conflicts=conflicts,
        actions=actions,
        unchanged=unchanged,
        warnings=warnings,
    )


def clean_backup_path(settings: Settings) -> Path:
    return Path(f"{settings.destination}.backup")


def initialize_from_clean_backup(settings: Settings, store: ConfigStore) -> Path | None:
    validate_settings(settings)
    backup = clean_backup_path(settings)
    if not backup.is_dir():
        raise InitializationRequired(f"The clean backup was not found:\n{backup}")
    if store.managed_file.exists():
        raise ManagerError("This destination is already managed. Initialization was cancelled.")

    destination = settings.destination
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent)
    )
    staged_destination = staging_root / destination.name
    preserved: Path | None = None
    try:
        # Finish the potentially long copy before moving the live folder.
        shutil.copytree(backup, staged_destination, copy_function=shutil.copy2)
        if destination.exists():
            preferred = Path(f"{destination}.manual-modded-backup")
            if preferred.exists():
                stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
                preferred = Path(f"{preferred}-{stamp}")
            shutil.move(str(destination), str(preferred))
            preserved = preferred
        os.replace(staged_destination, destination)
    except Exception:
        if preserved and not destination.exists():
            shutil.move(str(preserved), str(destination))
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    settings.initialized = True
    store.save(settings)
    return preserved


def initialize_current_folder(settings: Settings, store: ConfigStore) -> None:
    validate_settings(settings)
    if store.managed_file.exists():
        raise ManagerError("This destination is already managed.")
    settings.initialized = True
    store.save(settings)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _prune_empty_parents(path: Path, root: Path) -> None:
    current = path.parent
    root = root.resolve()
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def sync(
    settings: Settings,
    store: ConfigStore,
    progress: Callable[[int, int, str], None] | None = None,
) -> SyncResult:
    validate_settings(settings)
    maybe_import_legacy_state(settings, store)
    if not settings.initialized:
        raise InitializationRequired(
            "Complete the one-time setup first so the manager knows which files are original."
        )

    with FileLock(store.lock_file):
        plan = build_sync_plan(settings, store, progress=progress)
        managed = store.load_managed()
        old_files: dict[str, dict] = managed.get("files", {})
        baselines = store.load_baselines()
        baseline_files: dict[str, dict] = baselines.setdefault("files", {})
        store.originals_dir.mkdir(parents=True, exist_ok=True)

        # Establish every new baseline before changing any game file. This makes a
        # partially interrupted run safe to retry.
        baseline_changed = False
        for relative in sorted(plan.winners):
            if relative in baseline_files:
                continue
            destination = _safe_target(settings.destination, relative)
            original = _safe_target(store.originals_dir, relative)
            if destination.is_file():
                original.parent.mkdir(parents=True, exist_ok=True)
                _atomic_copy(destination, original)
                baseline_files[relative] = {"present": True}
            else:
                baseline_files[relative] = {"present": False}
            baseline_changed = True
        if baseline_changed:
            _atomic_write_json(store.baselines_file, baselines)

        # Refuse a cleanup if an expected original is missing. Keeping a stale mod
        # file is safer than destroying the user's only remaining copy.
        for relative in set(old_files) - set(plan.winners):
            baseline = baseline_files.get(relative, {})
            original = _safe_target(store.originals_dir, relative)
            if baseline.get("present") and not original.is_file():
                raise SyncSafetyError(
                    f"Cannot restore {relative}: its original backup is missing. No files were removed."
                )

        result = SyncResult(unchanged=plan.unchanged, conflicts=len(plan.conflicts))
        total = max(1, len(plan.winners) + len(set(old_files) - set(plan.winners)))
        completed = 0
        new_files: dict[str, dict] = {}
        changed = {action.relative_path for action in plan.actions}

        for relative, (mod_id, source) in sorted(plan.winners.items()):
            destination = _safe_target(settings.destination, relative)
            was_present = destination.exists()
            was_same = relative not in changed
            if not was_same:
                _atomic_copy(source, destination)
            if not was_present:
                result.installed += 1
                result.messages.append(f"Installed {relative}")
            elif not was_same:
                result.updated += 1
                result.messages.append(f"Updated {relative}")
            new_files[relative] = {
                "winner": mod_id,
                "providers": list(plan.providers.get(relative, (mod_id,))),
                "source_relative": normalize_relative_path(source.relative_to(settings.workshop / mod_id)),
                "source_stamp": file_stamp(source),
                "destination_stamp": file_stamp(destination),
            }
            completed += 1
            if progress:
                progress(completed, total, relative)

        removed_paths = sorted(set(old_files) - set(plan.winners))
        for relative in removed_paths:
            destination = _safe_target(settings.destination, relative)
            baseline = baseline_files.get(relative, {})
            original = _safe_target(store.originals_dir, relative)
            if baseline.get("present"):
                _atomic_copy(original, destination)
                result.restored += 1
                result.messages.append(f"Restored original {relative}")
            elif destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    raise SyncSafetyError(f"Refusing to remove directory where a managed file was expected: {relative}")
                destination.unlink()
                _prune_empty_parents(destination, settings.destination)
                result.removed += 1
                result.messages.append(f"Removed {relative}")
            completed += 1
            if progress:
                progress(completed, total, relative)

        new_managed = {
            "version": 2,
            "destination": str(settings.destination),
            "workshop": str(settings.workshop),
            "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": new_files,
        }
        _atomic_write_json(store.managed_file, new_managed)

        # Cleanup retired baselines only after the new manifest is durable.
        for relative in removed_paths:
            original = _safe_target(store.originals_dir, relative)
            original.unlink(missing_ok=True)
            baseline_files.pop(relative, None)
            _prune_empty_parents(original, store.originals_dir)
        _atomic_write_json(store.baselines_file, baselines)

        result.managed = len(new_files)
        store.save(settings)
        try:
            store.append_log(
                [
                    f"Sync complete: {result.managed} managed, {result.conflicts} conflicts",
                    (
                        f"Installed {result.installed}, updated {result.updated}, "
                        f"restored {result.restored}, removed {result.removed}, "
                        f"unchanged {result.unchanged}"
                    ),
                    *result.messages,
                ]
            )
        except OSError:
            # A read-only log location must not turn a completed sync into a
            # reported failure after the managed manifest is already durable.
            pass
        return result


def set_mod_enabled(settings: Settings, mod_id: str, enabled: bool) -> None:
    disabled = set(settings.disabled_mods)
    if enabled:
        disabled.discard(mod_id)
    else:
        disabled.add(mod_id)
    settings.disabled_mods = sorted(disabled)


def move_priority(settings: Settings, mod_id: str, direction: int) -> bool:
    order = list(settings.priority_low_to_high)
    if mod_id not in order:
        return False
    index = order.index(mod_id)
    new_index = index + direction
    if new_index < 0 or new_index >= len(order):
        return False
    order[index], order[new_index] = order[new_index], order[index]
    settings.priority_low_to_high = order
    return True


def human_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"
