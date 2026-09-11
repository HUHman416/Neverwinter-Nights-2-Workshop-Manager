from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nwn2_workshop_manager.core import (
    ConfigStore,
    Settings,
    SyncSafetyError,
    build_sync_plan,
    clean_backup_path,
    detect_steam_candidates,
    initialize_current_folder,
    initialize_from_clean_backup,
    maybe_import_legacy_state,
    normalize_relative_path,
    set_mod_enabled,
    sync,
)


class ManagerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workshop = self.root / "steamapps/workshop/content/2738630"
        self.destination = (
            self.root
            / "steamapps/compatdata/2738630/pfx/drive_c/users/steamuser/Documents/Neverwinter Nights 2"
        )
        self.workshop.mkdir(parents=True)
        self.destination.mkdir(parents=True)
        self.store = ConfigStore(self.root / "config", self.root / "state")
        self.settings = Settings(
            workshop_dir=str(self.workshop),
            destination_dir=str(self.destination),
            priority_low_to_high=["100", "200"],
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_mod(self, mod_id: str, relative: str, content: str) -> Path:
        path = self.workshop / mod_id / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class PathTests(unittest.TestCase):
    def test_normalizes_ui_and_dialog_tlk(self):
        self.assertEqual(normalize_relative_path("UI/default.xml"), "ui/default.xml")
        self.assertEqual(normalize_relative_path("Dialog.TLK"), "dialog.tlk")

    def test_rejects_parent_traversal(self):
        with self.assertRaises(SyncSafetyError):
            normalize_relative_path("override/../../escape.txt")


class SyncTests(ManagerFixture):
    def test_conflict_priority_and_original_restore(self):
        original = self.destination / "override/shared.txt"
        original.parent.mkdir(parents=True)
        original.write_text("original", encoding="utf-8")
        self.write_mod("100", "override/shared.txt", "low")
        self.write_mod("200", "override/shared.txt", "high")

        initialize_current_folder(self.settings, self.store)
        plan = build_sync_plan(self.settings, self.store)
        self.assertEqual(plan.winners["override/shared.txt"][0], "200")
        self.assertEqual(len(plan.conflicts), 1)
        first = sync(self.settings, self.store)
        self.assertEqual(first.updated, 1)
        self.assertEqual(original.read_text(encoding="utf-8"), "high")

        set_mod_enabled(self.settings, "200", False)
        second = sync(self.settings, self.store)
        self.assertEqual(second.updated, 1)
        self.assertEqual(original.read_text(encoding="utf-8"), "low")

        set_mod_enabled(self.settings, "100", False)
        third = sync(self.settings, self.store)
        self.assertEqual(third.restored, 1)
        self.assertEqual(original.read_text(encoding="utf-8"), "original")
        self.assertFalse((self.store.originals_dir / "override/shared.txt").exists())

    def test_unsubscribed_new_file_is_removed(self):
        installed = self.destination / "override/new.txt"
        self.write_mod("100", "override/new.txt", "mod file")
        initialize_current_folder(self.settings, self.store)
        sync(self.settings, self.store)
        self.assertEqual(installed.read_text(encoding="utf-8"), "mod file")

        shutil.rmtree(self.workshop / "100")
        result = sync(self.settings, self.store)
        self.assertEqual(result.removed, 1)
        self.assertFalse(installed.exists())

    def test_changed_workshop_file_updates_destination(self):
        source = self.write_mod("100", "override/change.txt", "v1")
        initialize_current_folder(self.settings, self.store)
        sync(self.settings, self.store)
        source.write_text("v2", encoding="utf-8")
        plan = build_sync_plan(self.settings, self.store)
        self.assertEqual(plan.count("update"), 1)
        sync(self.settings, self.store)
        self.assertEqual((self.destination / "override/change.txt").read_text(), "v2")

    def test_missing_original_backup_blocks_cleanup(self):
        original = self.destination / "dialog.tlk"
        original.write_text("original", encoding="utf-8")
        self.write_mod("100", "dialog.TLK", "modded")
        initialize_current_folder(self.settings, self.store)
        sync(self.settings, self.store)
        (self.store.originals_dir / "dialog.tlk").unlink()
        set_mod_enabled(self.settings, "100", False)
        with self.assertRaises(SyncSafetyError):
            sync(self.settings, self.store)
        self.assertEqual(original.read_text(encoding="utf-8"), "modded")

    def test_clean_backup_initialization_preserves_manual_folder(self):
        (self.destination / "manual.txt").write_text("manual", encoding="utf-8")
        backup = clean_backup_path(self.settings)
        backup.mkdir(parents=True)
        (backup / "clean.txt").write_text("clean", encoding="utf-8")
        preserved = initialize_from_clean_backup(self.settings, self.store)
        self.assertTrue(self.settings.initialized)
        self.assertEqual((self.destination / "clean.txt").read_text(), "clean")
        self.assertEqual((preserved / "manual.txt").read_text(), "manual")

    def test_imports_legacy_script_state_and_restores_its_original(self):
        self.write_mod("100", "dialog.TLK", "modded")
        (self.destination / "dialog.tlk").write_text("modded", encoding="utf-8")
        legacy_root = self.root / "legacy-data"
        legacy = legacy_root / "nwn2-workshop-sync"
        (legacy / "originals").mkdir(parents=True)
        (legacy / "originals/dialog.tlk").write_text("original", encoding="utf-8")
        (legacy / "managed-files.tsv").write_text("dialog.tlk\t100\n", encoding="utf-8")

        with patch.dict(os.environ, {"XDG_DATA_HOME": str(legacy_root)}):
            self.assertTrue(maybe_import_legacy_state(self.settings, self.store))
        self.assertTrue(self.settings.initialized)

        set_mod_enabled(self.settings, "100", False)
        result = sync(self.settings, self.store)
        self.assertEqual(result.restored, 1)
        self.assertEqual((self.destination / "dialog.tlk").read_text(), "original")


class DetectionTests(unittest.TestCase):
    def test_detects_custom_library_from_vdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home/user"
            steam = home / ".local/share/Steam"
            custom = root / "mnt/games/SteamLibrary"
            (steam / "steamapps").mkdir(parents=True)
            workshop = custom / "steamapps/workshop/content/2738630"
            destination = (
                custom
                / "steamapps/compatdata/2738630/pfx/drive_c/users/steamuser/Documents/Neverwinter Nights 2"
            )
            workshop.mkdir(parents=True)
            destination.mkdir(parents=True)
            (steam / "steamapps/libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n  "1" {{ "path" "{custom}" }}\n}}\n', encoding="utf-8"
            )
            candidates = detect_steam_candidates(home)
            self.assertTrue(candidates)
            self.assertEqual(candidates[0].library, custom.resolve())
            self.assertTrue(candidates[0].has_workshop)
            self.assertTrue(candidates[0].has_destination)


if __name__ == "__main__":
    unittest.main()
