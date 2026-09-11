"""Run with xvfb-run; use temporary configuration, never real mod data."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from nwn2_workshop_manager.core import build_sync_plan
from nwn2_workshop_manager.ui import ManagerApp

with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
    "XDG_CONFIG_HOME": folder, "XDG_DATA_HOME": folder
}), patch.object(ManagerApp, "_startup", lambda self: None):
    app = ManagerApp()
    for page in app.pages:
        app.show_page(page)
        app.update_idletasks()
    workshop = Path(folder) / "workshop"
    destination = Path(folder) / "destination"
    destination.mkdir()
    for mod in ("100", "200"):
        source = workshop / mod / "override"
        source.mkdir(parents=True)
        (source / "example.txt").write_text(mod)
    app.settings.workshop_dir = str(workshop)
    app.settings.destination_dir = str(destination)
    app.plan = build_sync_plan(app.settings, app.store)
    page = app.pages["conflicts"]
    page.set_plan(app.plan)
    page.tree.selection_set(page.tree.get_children()[0])
    page.inspect_versions()
    app.update_idletasks()
    assert page.tree.item(page.tree.get_children()[0], "values")[-1] == "Pending"
    with patch("nwn2_workshop_manager.ui.messagebox.askyesno", return_value=True), patch.object(app, "preview_changes") as preview:
        app.settings.file_winners["override/example.txt"] = "100"
        page.auto_resolve()
        assert not app.settings.file_winners
        preview.assert_called_once()
    app.destroy()
