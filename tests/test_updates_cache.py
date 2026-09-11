from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import ManagerFixture

from nwn2_workshop_manager.core import (
    build_sync_plan,
    initialize_current_folder,
    scan_mods,
    sync,
)
from nwn2_workshop_manager.updater import download_release, version_tuple


class CacheTests(ManagerFixture):
    def manifest(self, revision):
        path = self.workshop.parent.parent / "appworkshop_2738630.acf"
        path.write_text(f'"100" {{ "manifest" "{revision}" }}')

    def test_cache_avoids_recursive_walk(self):
        self.write_mod("100", "override/a.txt", "one")
        self.manifest(1)
        scan_mods(self.settings, self.store)
        with patch.object(Path, "rglob", side_effect=AssertionError("unnecessary scan")):
            mods, _ = scan_mods(self.settings, self.store)
        self.assertEqual(len(mods[0].files), 1)

    def test_manifest_update_invalidates_cache(self):
        self.write_mod("100", "override/a.txt", "one")
        self.manifest(1)
        scan_mods(self.settings, self.store)
        self.write_mod("100", "override/b.txt", "two")
        self.manifest(2)
        mods, _ = scan_mods(self.settings, self.store)
        self.assertEqual(len(mods[0].files), 2)

    def test_full_scan_detects_manual_nested_addition(self):
        self.write_mod("100", "override/a.txt", "one")
        self.manifest(1)
        scan_mods(self.settings, self.store)
        self.write_mod("100", "override/b.txt", "two")
        mods, _ = scan_mods(self.settings, self.store, full=True)
        self.assertEqual(len(mods[0].files), 2)

    def test_unchanged_sync_does_not_compare_or_copy(self):
        self.write_mod("100", "override/a.txt", "one")
        initialize_current_folder(self.settings, self.store)
        sync(self.settings, self.store)
        with patch("nwn2_workshop_manager.core._files_equal", side_effect=AssertionError("read")), patch(
                "nwn2_workshop_manager.core._atomic_copy", side_effect=AssertionError("copy")):
            result = sync(self.settings, self.store)
        self.assertEqual(result.unchanged, 1)

    def test_custom_winner_and_disabled_fallback(self):
        self.write_mod("100", "override/a.txt", "one")
        self.write_mod("200", "override/a.txt", "two")
        self.settings.file_winners["override/a.txt"] = "100"
        self.assertEqual(build_sync_plan(self.settings, self.store).conflicts[0].winner, "100")
        self.settings.disabled_mods = ["100"]
        self.assertEqual(build_sync_plan(self.settings, self.store).winners["override/a.txt"][0], "200")


class UpdateTests(unittest.TestCase):
    def test_version_order(self):
        self.assertGreater(version_tuple("v0.10.0"), version_tuple("0.2.0"))
        with self.assertRaises(ValueError):
            version_tuple("../../bad")

    def test_verified_download_and_corrupt_rejection(self):
        payload = b"\x7fELF" + b"test" * 100
        release = {"version": "v0.2.0", "checksum": "checksum", "binary": "binary"}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("nwn2_workshop_manager.updater.request", side_effect=[
                    io.BytesIO(hashlib.sha256(payload).hexdigest().encode()), io.BytesIO(payload)]):
                target = download_release(release, root)
            self.assertEqual(target.read_bytes(), payload)
            self.assertTrue(target.stat().st_mode & 0o100)
            with patch("nwn2_workshop_manager.updater.request", side_effect=[
                    io.BytesIO(b"0" * 64), io.BytesIO(payload)]), self.assertRaises(ValueError):
                download_release(release, root)
            self.assertEqual(list(root.iterdir()), [target])
