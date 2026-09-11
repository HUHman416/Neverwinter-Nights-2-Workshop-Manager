"""Run with xvfb-run; use temporary configuration, never real mod data."""
import tempfile
from unittest.mock import patch

from nwn2_workshop_manager.ui import ManagerApp

with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
    "XDG_CONFIG_HOME": folder, "XDG_DATA_HOME": folder
}), patch.object(ManagerApp, "_startup", lambda self: None):
    app = ManagerApp()
    for page in app.pages:
        app.show_page(page)
        app.update_idletasks()
    app.destroy()
