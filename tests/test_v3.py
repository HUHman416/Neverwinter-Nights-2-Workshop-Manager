from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import MagicMock, patch

from test_core import ManagerFixture

from nwn2_workshop_manager.core import build_sync_plan, initialize_current_folder, sync
from nwn2_workshop_manager.launcher import commands_for, host_environment, launch


class ConflictStateTests(ManagerFixture):
    def test_duplicate_applied_then_changed(self):
        self.write_mod("100", "override/a", "same")
        source = self.write_mod("200", "override/a", "same")
        initialize_current_folder(self.settings, self.store)
        first = build_sync_plan(self.settings, self.store).conflicts[0]
        self.assertEqual((first.category, first.status), ("Identical duplicate", "Pending"))
        sync(self.settings, self.store)
        self.assertEqual(build_sync_plan(self.settings, self.store).conflicts[0].status, "Applied")
        source.write_text("changed")
        changed = build_sync_plan(self.settings, self.store).conflicts[0]
        self.assertEqual((changed.category, changed.status), ("Priority overlap", "Pending"))

    def test_custom_choice_then_reset_and_apply(self):
        self.write_mod("100", "override/a", "one")
        self.write_mod("200", "override/a", "two")
        initialize_current_folder(self.settings, self.store)
        self.settings.file_winners["override/a"] = "100"
        self.assertEqual(build_sync_plan(self.settings, self.store).conflicts[0].category, "Custom winner")
        sync(self.settings, self.store)
        self.settings.file_winners.clear()
        self.assertEqual(build_sync_plan(self.settings, self.store).conflicts[0].status, "Pending")
        sync(self.settings, self.store)
        self.assertEqual((self.destination / "override/a").read_text(), "two")
        self.assertEqual(build_sync_plan(self.settings, self.store).conflicts[0].status, "Applied")

    def test_conflict_comparison_cache_skips_content_reads(self):
        self.write_mod("100", "override/a", "same")
        self.write_mod("200", "override/a", "same")
        initialize_current_folder(self.settings, self.store)
        sync(self.settings, self.store)
        with patch("nwn2_workshop_manager.core._files_equal", side_effect=AssertionError("read")):
            self.assertEqual(build_sync_plan(self.settings, self.store).conflicts[0].category, "Identical duplicate")

    def test_failed_native_falls_back_and_logs(self):
        process1, process2 = MagicMock(), MagicMock()
        process1.wait.return_value = 1
        process2.wait.return_value = 0
        with patch("nwn2_workshop_manager.launcher.commands_for", return_value=[["steam"], ["flatpak"]]), patch(
                "nwn2_workshop_manager.launcher.subprocess.Popen", side_effect=[process1, process2]):
            result = launch("steam://rungameid/2738630", self.root / "launch.log")
        self.assertIn("flatpak", result)
        self.assertIn("steam", (self.root / "launch.log").read_text())

    def test_all_launchers_fail_reports_log(self):
        process = MagicMock()
        process.wait.return_value = 1
        with patch("nwn2_workshop_manager.launcher.commands_for", return_value=[["steam"]]), patch(
                "nwn2_workshop_manager.launcher.subprocess.Popen", return_value=process), self.assertRaises(RuntimeError):
            launch("steam://rungameid/2738630", self.root / "launch.log")


class LaunchTests(TestCase):
    def test_host_env_restores_original_and_preserves_desktop_session(self):
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/host",
                                     "APPDIR": "/bundle", "DISPLAY": ":1", "TCL_LIBRARY": "/bundle/tcl"}):
            env = host_environment()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/host")
        self.assertEqual(env["DISPLAY"], ":1")
        self.assertNotIn("APPDIR", env)
        self.assertNotIn("TCL_LIBRARY", env)

    def test_game_command_uses_applaunch_and_flatpak_fallback(self):
        with patch("nwn2_workshop_manager.launcher.shutil.which", side_effect=lambda cmd, **kw: "/usr/bin/" + cmd):
            commands = commands_for("steam://rungameid/2738630", {})
        self.assertEqual(commands[0], ["/usr/bin/steam", "-applaunch", "2738630"])
        self.assertEqual(commands[1], ["/usr/bin/flatpak", "run", "com.valvesoftware.Steam", "-applaunch", "2738630"])

    def test_workshop_has_browser_fallback(self):
        with patch("nwn2_workshop_manager.launcher.shutil.which", side_effect=lambda cmd, **kw: "/usr/bin/" + cmd):
            commands = commands_for("steam://openurl/https://steamcommunity.com/app/2738630/workshop/", {})
        self.assertEqual(commands[-1][1], "https://steamcommunity.com/app/2738630/workshop/")
