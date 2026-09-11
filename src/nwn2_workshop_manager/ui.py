from __future__ import annotations

import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import (
    APP_ID,
    APP_NAME,
    APP_SLUG,
    VERSION,
    ConfigStore,
    ConfigurationError,
    ManagerError,
    ModInfo,
    Settings,
    SyncPlan,
    apply_best_detection,
    build_sync_plan,
    clean_backup_path,
    detect_steam_candidates,
    human_size,
    initialize_current_folder,
    initialize_from_clean_backup,
    maybe_import_legacy_state,
    move_priority,
    scan_mods,
    set_mod_enabled,
    sync,
)
from .launcher import launch
from .updater import check_release, download_release

BG = "#101319"
SIDEBAR = "#171b23"
PANEL = "#1d232d"
PANEL_2 = "#252d39"
GOLD = "#d0a84f"
GOLD_DARK = "#8e7135"
BLUE = "#5b91cc"
GREEN = "#58b987"
RED = "#d86868"
TEXT = "#f1eadb"
MUTED = "#9aa5b5"
BORDER = "#343e4d"


def _open_target(target: str | Path) -> None:
    app = tk._default_root
    if app:
        app.launch_target(str(target))


def _asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    candidates = [base / "assets" / name, base / name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def install_desktop_entry(appimage: str | None = None) -> Path:
    applications = Path.home() / ".local/share/applications"
    icons = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    icon_target = icons / f"{APP_SLUG}.svg"
    shutil.copy2(_asset_path("nwn2-workshop-manager.svg"), icon_target)

    executable = appimage or os.environ.get("APPIMAGE", sys.executable)
    if not appimage and not os.environ.get("APPIMAGE") and not getattr(sys, "frozen", False):
        exec_line = f"{shlex.quote(sys.executable)} -m nwn2_workshop_manager"
    else:
        escaped = executable.replace('"', '\\"')
        exec_line = f'"{escaped}"'
    desktop = applications / f"{APP_SLUG}.desktop"
    desktop.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                f"Name={APP_NAME}",
                "Comment=Safely install and manage NWN2 Enhanced Edition Workshop mods",
                f"Exec={exec_line}",
                f"Icon={APP_SLUG}",
                "Terminal=false",
                "Categories=Game;",
                "Keywords=NWN2;Neverwinter;Workshop;Mods;Steam;",
                "StartupNotify=true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    desktop.chmod(0o755)
    return desktop


def remove_desktop_entry() -> None:
    (Path.home() / ".local/share/applications" / f"{APP_SLUG}.desktop").unlink(missing_ok=True)
    (Path.home() / ".local/share/icons/hicolor/scalable/apps" / f"{APP_SLUG}.svg").unlink(
        missing_ok=True
    )


class PillButton(tk.Button):
    def __init__(self, parent, text: str, command=None, accent: bool = False, **kwargs):
        background = GOLD if accent else PANEL_2
        foreground = BG if accent else TEXT
        active_background = "#e0bd69" if accent else BORDER
        super().__init__(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=active_background,
            activeforeground=BG if accent else TEXT,
            relief="flat",
            bd=0,
            padx=15,
            pady=8,
            cursor="hand2",
            font=("Sans", 10, "bold"),
            **kwargs,
        )


class ManagerApp(tk.Tk):
    def launch_target(self, target):
        if self._busy and target.startswith("steam://rungameid/"):
            messagebox.showinfo("Please wait", "Wait for the current operation to finish before launching NWN2.", parent=self)
            return
        def worker():
            try:
                result = launch(target, self.store.state_dir / "launch.log")
                self._events.put(("launch-ok", None, result))
            except Exception as exc:  # noqa: BLE001 - surface external launch failures
                self._events.put(("launch-error", None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(bg=BG)

        self.store = ConfigStore()
        self.settings = self.store.load()
        self.mods: list[ModInfo] = []
        self.plan: SyncPlan | None = None
        self._busy = False
        self._update_busy = False
        self._events: queue.Queue = queue.Queue()

        self._configure_styles()
        self._build_shell()
        self.after(100, self._startup)
        self.after(100, self._poll_events)
        self.after(4000, lambda: self.check_for_updates(False) if self.settings.check_updates else None)

    def check_for_updates(self, manual=True):
        if self._update_busy:
            return
        self._update_busy = True

        def worker():
            try:
                result = check_release(VERSION)
                self._events.put(("update-check", manual, result))
            except Exception as exc:  # noqa: BLE001 - report network/metadata errors in Tk
                self._events.put(("update-error", manual, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def offer_update(self, release):
        if not release:
            return
        if self._busy:
            self.after(3000, lambda: self.offer_update(release))
            return
        if not messagebox.askyesno("Manager update available",
                f"Download {release['version']}? The checksum will be verified. "
                "Your current AppImage and mod settings will be kept.", parent=self):
            return

        def done(path):
            self.set_status(f"Update ready: {path.name}")
            shortcut = Path.home() / ".local/share/applications" / f"{APP_SLUG}.desktop"
            if shortcut.exists():
                install_desktop_entry(str(path))
            if messagebox.askyesno("Update ready", "Restart into the new version now?", parent=self):
                env = os.environ.copy()
                for key in list(env):
                    if key.startswith("_PYI") or key in ("APPIMAGE", "APPDIR", "LD_LIBRARY_PATH", "TCL_LIBRARY", "TK_LIBRARY"):
                        env.pop(key, None)
                env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                subprocess.Popen([str(path)], env=env, start_new_session=True)
                self.destroy()
        self.run_async(lambda: download_release(release, self.store.state_dir / "updates"),
                       done, "Downloading and verifying manager update…")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Sans", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Sans", 9))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            bordercolor=BORDER,
            rowheight=34,
            font=("Sans", 10),
        )
        style.configure(
            "Treeview.Heading",
            background=PANEL_2,
            foreground=TEXT,
            bordercolor=BORDER,
            relief="flat",
            font=("Sans", 9, "bold"),
        )
        style.map("Treeview", background=[("selected", "#35465a")], foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.configure(
            "TEntry",
            fieldbackground=PANEL_2,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            padding=8,
        )
        style.configure(
            "TCheckbutton",
            background=PANEL,
            foreground=TEXT,
            indicatorbackground=PANEL_2,
            indicatorforeground=GOLD,
        )
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("Gold.Horizontal.TProgressbar", troughcolor=PANEL_2, background=GOLD)

    def _build_shell(self) -> None:
        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg=SIDEBAR, width=215)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(22, 26))
        crest = tk.Canvas(brand, width=44, height=44, bg=SIDEBAR, highlightthickness=0)
        crest.pack(side="left")
        crest.create_polygon(22, 2, 40, 12, 36, 34, 22, 42, 8, 34, 4, 12, fill=GOLD)
        crest.create_polygon(22, 8, 34, 15, 31, 30, 22, 36, 13, 30, 10, 15, fill=PANEL)
        crest.create_text(22, 22, text="N2", fill=TEXT, font=("Sans", 10, "bold"))
        title_wrap = tk.Frame(brand, bg=SIDEBAR)
        title_wrap.pack(side="left", padx=(10, 0))
        tk.Label(title_wrap, text="NWN2", bg=SIDEBAR, fg=TEXT, font=("Sans", 15, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            title_wrap, text="WORKSHOP MANAGER", bg=SIDEBAR, fg=GOLD, font=("Sans", 7, "bold")
        ).pack(anchor="w")

        self.nav_buttons: dict[str, tk.Button] = {}
        for key, label in (
            ("workshop", "Workshop Mods"),
            ("conflicts", "File Conflicts"),
            ("backups", "Backups & Safety"),
            ("settings", "Settings"),
        ):
            button = tk.Button(
                sidebar,
                text=label,
                command=lambda page=key: self.show_page(page),
                anchor="w",
                bg=SIDEBAR,
                fg=MUTED,
                activebackground=PANEL,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                padx=22,
                pady=12,
                cursor="hand2",
                font=("Sans", 10, "bold"),
            )
            button.pack(fill="x", pady=1)
            self.nav_buttons[key] = button

        quick = tk.Frame(sidebar, bg=SIDEBAR)
        quick.pack(side="bottom", fill="x", padx=14, pady=(0, 48))
        PillButton(
            quick,
            "Open Steam Workshop",
            command=lambda: _open_target(
                f"steam://openurl/https://steamcommunity.com/app/{APP_ID}/workshop/"
            ),
        ).pack(fill="x", pady=(0, 7))
        PillButton(
            quick, "Launch NWN2 EE", command=lambda: _open_target(f"steam://rungameid/{APP_ID}")
        ).pack(fill="x")

        tk.Label(
            sidebar,
            text=f"v{VERSION}  •  NWN2 EE {APP_ID}",
            bg=SIDEBAR,
            fg="#697587",
            font=("Sans", 8),
        ).pack(side="bottom", pady=18)

        right = tk.Frame(shell, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        self.banner = tk.Frame(right, bg="#392f20", height=0)
        self.banner_label = tk.Label(
            self.banner, bg="#392f20", fg="#f0d59b", anchor="w", font=("Sans", 9, "bold")
        )
        self.banner_label.pack(side="left", fill="x", expand=True, padx=16, pady=9)
        self.banner_action = tk.Button(
            self.banner,
            text="Complete setup",
            command=self.show_initialization,
            bg=GOLD,
            fg=BG,
            activebackground="#e0bd69",
            relief="flat",
            bd=0,
            padx=12,
            pady=5,
            cursor="hand2",
            font=("Sans", 9, "bold"),
        )
        self.banner_action.pack(side="right", padx=12, pady=6)

        self.container = tk.Frame(right, bg=BG)
        self.container.pack(fill="both", expand=True)
        self.pages = {
            "workshop": WorkshopPage(self.container, self),
            "conflicts": ConflictsPage(self.container, self),
            "backups": BackupsPage(self.container, self),
            "settings": SettingsPage(self.container, self),
        }
        for page in self.pages.values():
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        status = tk.Frame(right, bg=SIDEBAR, height=30)
        status.pack(fill="x", side="bottom")
        self.status_dot = tk.Label(status, text="●", bg=SIDEBAR, fg=GREEN, font=("Sans", 9))
        self.status_dot.pack(side="left", padx=(14, 7))
        self.status_text = tk.Label(
            status, text="Ready", bg=SIDEBAR, fg=MUTED, font=("Sans", 8), anchor="w"
        )
        self.status_text.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(
            status, style="Gold.Horizontal.TProgressbar", mode="determinate", length=180
        )
        self.progress.pack(side="right", padx=12, pady=8)
        self.progress.pack_forget()
        self.show_page("workshop")

    def show_page(self, key: str) -> None:
        page = self.pages[key]
        page.tkraise()
        for name, button in self.nav_buttons.items():
            selected = name == key
            button.configure(bg=PANEL if selected else SIDEBAR, fg=TEXT if selected else MUTED)
        if hasattr(page, "refresh_view"):
            page.refresh_view()

    def _startup(self) -> None:
        if apply_best_detection(self.settings):
            self.store.save(self.settings)
        if self.settings.workshop_dir and self.settings.destination_dir:
            try:
                maybe_import_legacy_state(self.settings, self.store)
            except (ManagerError, OSError, ValueError) as exc:
                self.set_status(f"Legacy state import needs attention: {exc}", "error")
        self.pages["settings"].load_values()
        self.update_setup_banner()
        if not self.settings.workshop_dir or not self.settings.destination_dir:
            self.show_page("settings")
            messagebox.showinfo(
                "Choose your Steam folders",
                "The Steam library containing NWN2 EE was not detected. Choose the two folders in Settings.",
                parent=self,
            )
            return
        if not self.settings.initialized:
            self.show_initialization()
        else:
            self.refresh(preview=self.settings.auto_preview)

    def update_setup_banner(self) -> None:
        needs_setup = not self.settings.initialized
        if needs_setup:
            self.banner_label.configure(
                text="ONE-TIME SETUP NEEDED  •  Choose a clean baseline before the first sync."
            )
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", before=self.container)
        elif self.banner.winfo_ismapped():
            self.banner.pack_forget()

    def show_initialization(self) -> None:
        try:
            if not self.settings.workshop_dir or not self.settings.destination_dir:
                raise ConfigurationError("Choose the Workshop and destination folders in Settings first.")
            dialog = InitializationDialog(self, self.settings, self.store)
            self.wait_window(dialog)
            self.update_setup_banner()
            self.pages["backups"].refresh_view()
            if self.settings.initialized:
                self.refresh(preview=True)
        except ManagerError as exc:
            messagebox.showerror("Setup required", str(exc), parent=self)
            self.show_page("settings")

    def set_status(self, text: str, kind: str = "normal") -> None:
        color = {"normal": GREEN, "busy": GOLD, "error": RED}.get(kind, GREEN)
        self.status_dot.configure(fg=color)
        self.status_text.configure(text=text)

    def _set_busy(self, busy: bool, text: str = "Working…") -> None:
        self._busy = busy
        if busy:
            self._started_at = time.monotonic()
            self._operation_text = text
            self.after(1000, self._tick_elapsed)
            self.set_status(text, "busy")
            self.progress.configure(mode="indeterminate")
            self.progress.pack(side="right", padx=12, pady=8)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _tick_elapsed(self):
        if self._busy:
            elapsed = int(time.monotonic() - self._started_at)
            self.set_status(f"{self._operation_text} • {elapsed // 60}:{elapsed % 60:02d} elapsed", "busy")
            self.after(1000, self._tick_elapsed)

    def report_progress(self, current, total, text):
        self._events.put(("progress", None, (current, total, text)))

    def run_async(self, work, success, label: str) -> None:
        if self._busy:
            return
        self._set_busy(True, label)

        def runner():
            try:
                value = work()
                self._events.put(("success", success, value))
            except Exception as exc:  # noqa: BLE001 - marshal worker failures back to Tk
                self._events.put(("error", None, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                event, callback, value = self._events.get_nowait()
                if event.startswith("launch-"):
                    if event == "launch-error":
                        messagebox.showerror("Launch failed", value, parent=self)
                    elif not self._busy:
                        self.set_status(value)
                    continue
                if event in ("update-check", "update-error"):
                    self._update_busy = False
                    if event == "update-error":
                        if callback:
                            messagebox.showerror("Update check failed", value, parent=self)
                    elif value:
                        self.offer_update(value)
                    elif callback:
                        messagebox.showinfo("Up to date", f"You are running v{VERSION}.", parent=self)
                    continue
                if event == "progress":
                    current, total, path = value
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=total, value=current)
                    self._operation_text = f"{current}/{total} • {path}"
                    self.set_status(self._operation_text, "busy")
                    continue
                self._set_busy(False)
                if event == "success":
                    callback(value)
                else:
                    self.set_status(str(value), "error")
                    messagebox.showerror("NWN2 Workshop Manager", str(value), parent=self)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def refresh(self, preview: bool = False, full: bool = False) -> None:
        def progress(current, total, text):
            self._events.put(("progress", None, (current, total, text)))
        def work():
            if preview:
                return build_sync_plan(self.settings, self.store, full, progress)
            mods, warnings = scan_mods(self.settings, self.store, full, progress)
            return mods, warnings

        def done(value):
            if isinstance(value, SyncPlan):
                self.plan = value
                self.mods = value.mods
                summary = (
                    f"Preview ready: {len(value.actions)} change(s), "
                    f"{len(value.conflicts)} conflict(s)"
                )
            else:
                self.mods, warnings = value
                self.plan = None
                summary = f"Found {len(self.mods)} subscribed Workshop item(s)"
                if warnings:
                    summary += f" • {len(warnings)} warning(s)"
            self.store.save(self.settings)
            self.pages["workshop"].set_mods(self.mods, self.plan)
            self.pages["conflicts"].set_plan(self.plan)
            self.pages["backups"].refresh_view()
            self.set_status(summary)

        self.run_async(work, done, "Scanning the Workshop…")

    def preview_changes(self) -> None:
        def done(plan: SyncPlan):
            self.plan = plan
            self.mods = plan.mods
            self.pages["workshop"].set_mods(self.mods, plan)
            self.pages["conflicts"].set_plan(plan)
            self.set_status(f"Preview ready: {len(plan.actions)} change(s)")
            PreviewDialog(self, plan)

        self.run_async(
            lambda: build_sync_plan(self.settings, self.store, progress=self.report_progress), done, "Calculating changes…"
        )

    def sync_now(self) -> None:
        if not self.settings.initialized:
            self.show_initialization()
            return

        def after_plan(plan: SyncPlan):
            self.plan = plan
            if not plan.actions:
                messagebox.showinfo(
                    "Already synchronized",
                    f"All {plan.unchanged} managed files already match the current Workshop state.",
                    parent=self,
                )
                self.set_status("Workshop and NWN2 are already synchronized")
                return
            counts = ", ".join(
                f"{plan.count(kind)} {kind}"
                for kind in ("install", "update", "restore", "remove")
                if plan.count(kind)
            )
            if not messagebox.askyesno(
                "Apply Workshop changes?",
                f"The manager will apply {len(plan.actions)} file change(s):\n\n{counts}\n\n"
                "Original files are backed up before they are replaced. Continue?",
                parent=self,
            ):
                self.set_status("Sync cancelled")
                return

            def progress(current: int, total: int, path: str):
                self._events.put(("progress", None, (current, total, path)))

            def done(result):
                self.set_status(
                    f"Sync complete • {result.managed} managed files • {result.conflicts} conflict(s)"
                )
                messagebox.showinfo(
                    "Sync complete",
                    "NWN2 now matches your enabled Workshop mods.\n\n"
                    f"Installed: {result.installed}\nUpdated: {result.updated}\n"
                    f"Restored: {result.restored}\nRemoved: {result.removed}\n"
                    f"Unchanged: {result.unchanged}",
                    parent=self,
                )
                self.refresh(preview=True)

            self.run_async(
                lambda: sync(self.settings, self.store, progress), done, "Synchronizing files…"
            )

        self.run_async(
            lambda: build_sync_plan(self.settings, self.store, progress=self.report_progress), after_plan, "Preparing sync…"
        )


class Page(tk.Frame):
    def __init__(self, parent, app: ManagerApp):
        super().__init__(parent, bg=BG)
        self.app = app

    def heading(self, title: str, subtitle: str):
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="x", padx=28, pady=(24, 18))
        tk.Label(wrap, text=title, bg=BG, fg=TEXT, font=("Sans", 22, "bold")).pack(anchor="w")
        tk.Label(wrap, text=subtitle, bg=BG, fg=MUTED, font=("Sans", 10)).pack(
            anchor="w", pady=(4, 0)
        )


class WorkshopPage(Page):
    def __init__(self, parent, app: ManagerApp):
        super().__init__(parent, app)
        self.search_var = tk.StringVar()
        self.mod_by_id: dict[str, ModInfo] = {}
        self.heading(
            "Workshop Mods",
            "Enable, prioritize, preview, and safely sync your NWN2 Enhanced Edition subscriptions.",
        )

        cards = tk.Frame(self, bg=BG)
        cards.pack(fill="x", padx=28, pady=(0, 16))
        self.card_values = {}
        for key, title in (
            ("mods", "SUBSCRIBED MODS"),
            ("files", "MANAGED FILES"),
            ("conflicts", "DIFFERING OVERLAPS"),
        ):
            card = tk.Frame(cards, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10) if key != "conflicts" else 0)
            tk.Label(card, text=title, bg=PANEL, fg=MUTED, font=("Sans", 8, "bold")).pack(
                anchor="w", padx=15, pady=(12, 2)
            )
            value = tk.Label(card, text="—", bg=PANEL, fg=TEXT, font=("Sans", 20, "bold"))
            value.pack(anchor="w", padx=15, pady=(0, 11))
            self.card_values[key] = value

        toolbar = tk.Frame(self, bg=BG)
        toolbar.pack(fill="x", padx=28, pady=(0, 10))
        search = ttk.Entry(toolbar, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, padx=(0, 10))
        search.insert(0, "")
        self.search_var.trace_add("write", lambda *_: self._render_rows())
        PillButton(toolbar, "Refresh", command=lambda: app.refresh(False)).pack(side="left", padx=(0, 8))
        PillButton(toolbar, "Full Verify", command=lambda: app.refresh(True, True)).pack(side="left", padx=(0, 8))
        PillButton(toolbar, "Preview", command=app.preview_changes).pack(side="left", padx=(0, 8))
        PillButton(toolbar, "Sync Now", command=app.sync_now, accent=True).pack(side="left")

        table_wrap = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        table_wrap.pack(fill="both", expand=True, padx=28)
        columns = ("enabled", "name", "id", "files", "size", "priority", "conflicts")
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="browse")
        headings = {
            "enabled": "STATE",
            "name": "MOD",
            "id": "WORKSHOP ID",
            "files": "FILES",
            "size": "SIZE",
            "priority": "PRIORITY",
            "conflicts": "CONFLICTS",
        }
        widths = {"enabled": 78, "name": 260, "id": 120, "files": 70, "size": 85, "priority": 82, "conflicts": 85}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=55, anchor="w")
        scrollbar = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.tag_configure("disabled", foreground="#6f7988")
        self.tree.tag_configure("conflict", foreground="#e4bf70")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_selection())
        self.tree.bind("<Double-1>", lambda _event: self.toggle_selected())

        actions = tk.Frame(self, bg=BG)
        actions.pack(fill="x", padx=28, pady=12)
        self.selection_label = tk.Label(
            actions, text="Select a mod to manage it", bg=BG, fg=MUTED, font=("Sans", 9)
        )
        self.selection_label.pack(side="left", fill="x", expand=True)
        PillButton(actions, "Rename", command=self.rename_selected).pack(side="right", padx=(8, 0))
        PillButton(actions, "Workshop Page", command=self.open_selected).pack(
            side="right", padx=(8, 0)
        )
        PillButton(actions, "Lower", command=lambda: self.change_priority(-1)).pack(side="right", padx=(8, 0))
        PillButton(actions, "Raise", command=lambda: self.change_priority(1)).pack(side="right", padx=(8, 0))
        PillButton(actions, "Enable / Disable", command=self.toggle_selected).pack(side="right")

    def set_mods(self, mods: list[ModInfo], plan: SyncPlan | None) -> None:
        self.mod_by_id = {mod.mod_id: mod for mod in mods}
        self.card_values["mods"].configure(text=str(len(mods)))
        managed = len(plan.winners) if plan else len(self.app.store.load_managed().get("files", {}))
        self.card_values["files"].configure(text=f"{managed:,}")
        conflicts = sum(c.category != "Identical duplicate" for c in plan.conflicts) if plan else None
        self.card_values["conflicts"].configure(text=f"{conflicts:,}" if conflicts is not None else "Run Preview", fg=GOLD if conflicts else GREEN)
        self._render_rows()

    def _render_rows(self) -> None:
        selected = self.selected_id()
        for item in self.tree.get_children():
            self.tree.delete(item)
        query = self.search_var.get().strip().casefold()
        for mod in sorted(self.mod_by_id.values(), key=lambda item: item.priority, reverse=True):
            if query and query not in mod.name.casefold() and query not in mod.mod_id:
                continue
            tag = "disabled" if not mod.enabled else ("conflict" if mod.conflict_count else "")
            self.tree.insert(
                "",
                "end",
                iid=mod.mod_id,
                values=(
                    "Enabled" if mod.enabled else "Disabled",
                    mod.name,
                    mod.mod_id,
                    f"{len(mod.files):,}",
                    human_size(mod.total_size),
                    mod.priority + 1,
                    mod.conflict_count,
                ),
                tags=(tag,) if tag else (),
            )
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    def selected_id(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _update_selection(self) -> None:
        mod_id = self.selected_id()
        if not mod_id or mod_id not in self.mod_by_id:
            self.selection_label.configure(text="Select a mod to manage it")
            return
        mod = self.mod_by_id[mod_id]
        self.selection_label.configure(
            text=f"{mod.name}  •  {len(mod.files):,} files  •  priority {mod.priority + 1}"
        )

    def toggle_selected(self) -> None:
        mod_id = self.selected_id()
        if not mod_id:
            return
        mod = self.mod_by_id[mod_id]
        set_mod_enabled(self.app.settings, mod_id, not mod.enabled)
        self.app.store.save(self.app.settings)
        self.app.refresh(preview=True)

    def change_priority(self, direction: int) -> None:
        mod_id = self.selected_id()
        if not mod_id:
            return
        if move_priority(self.app.settings, mod_id, direction):
            self.app.store.save(self.app.settings)
            self.app.refresh(preview=True)

    def rename_selected(self) -> None:
        mod_id = self.selected_id()
        if not mod_id:
            return
        current = self.app.settings.aliases.get(mod_id, "")
        value = simpledialog.askstring(
            "Mod display name",
            f"Give Workshop item {mod_id} a friendly name:\n\n(Leave blank to use its Workshop ID.)",
            initialvalue=current,
            parent=self,
        )
        if value is None:
            return
        value = value.strip()
        if value:
            self.app.settings.aliases[mod_id] = value
        else:
            self.app.settings.aliases.pop(mod_id, None)
        self.app.store.save(self.app.settings)
        self.app.refresh(preview=False)

    def open_selected(self) -> None:
        mod_id = self.selected_id()
        if mod_id:
            _open_target(
                f"steam://openurl/https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}"
            )


class ConflictsPage(Page):
    def __init__(self, parent, app: ManagerApp):
        super().__init__(parent, app)
        self.heading(
            "File Conflicts",
            "Inspect overlaps, choose file winners, and track whether your choices have been applied.",
        )
        wrap = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=28, pady=(0, 18))
        self.tree = ttk.Treeview(wrap, columns=("path", "winner", "overridden", "category", "status"), show="headings")
        self.tree.heading("path", text="DESTINATION FILE")
        self.tree.heading("winner", text="WINNER")
        self.tree.heading("overridden", text="OTHER PROVIDERS")
        self.tree.column("path", width=430)
        self.tree.column("winner", width=155)
        self.tree.column("overridden", width=260)
        self.tree.heading("category", text="TYPE")
        self.tree.heading("status", text="STATE")
        self.tree.column("path", width=240, minwidth=160)
        self.tree.column("overridden", width=160, minwidth=90)
        self.tree.column("category", width=130, minwidth=110)
        self.tree.column("status", width=75, minwidth=65)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=28, pady=(0, 18))
        self.summary = tk.Label(footer, text="Run Preview to inspect conflicts.", bg=BG, fg=MUTED)
        self.summary.pack(side="left")
        PillButton(footer, "Preview Again", command=app.preview_changes).pack(side="right")
        controls = tk.Frame(self, bg=BG)
        controls.pack(fill="x", padx=28, pady=(0, 18))
        PillButton(controls, "Choose Winner…", command=self.choose_winner, accent=True).pack(side="left")
        PillButton(controls, "Apply Priority Winners", command=self.auto_resolve).pack(side="left", padx=8)
        PillButton(controls, "Inspect Versions", command=self.inspect_versions).pack(side="left", padx=8)
        PillButton(controls, "Apply / Sync", command=app.sync_now).pack(side="right")
        self.tree.bind("<Double-1>", lambda event: self.choose_winner())

    def auto_resolve(self):
        if self.app._busy:
            return
        if not messagebox.askyesno(
                "Apply priority winners?", "Clear custom choices and use mod priority for every file? "
                "A file-change preview will open next. Confirm Sync there to apply it.", parent=self):
            return
        self.app.settings.file_winners.clear()
        self.app.store.save(self.app.settings)
        self.app.preview_changes()

    def inspect_versions(self):
        if self.app._busy or not self.app.plan or not self.tree.selection():
            return
        relative = self.tree.item(self.tree.selection()[0], "values")[0]
        plan = self.app.plan
        conflict = next(c for c in plan.conflicts if c.relative_path == relative)
        dialog = tk.Toplevel(self)
        dialog.title("Inspect file versions")
        dialog.configure(bg=BG)
        dialog.geometry("760x520")
        text = tk.Text(dialog, bg=PANEL, fg=TEXT, wrap="word")
        text.pack(fill="both", expand=True, padx=16, pady=16)
        text.insert("end", f"{relative}\n{conflict.category} • {conflict.status}\n\n")
        for mod in plan.mods:
            if mod.mod_id not in conflict.providers:
                continue
            source = mod.files[relative]
            wins = sum(1 for c in plan.conflicts if c.winner == mod.mod_id)
            loses = sum(1 for c in plan.conflicts if mod.mod_id in c.providers and c.winner != mod.mod_id)
            text.insert("end", f"{mod.name} ({mod.mod_id}) — {'WINNER' if mod.mod_id == conflict.winner else 'OVERRIDDEN'}\n"
                        f"Across overlaps: {wins} wins / {loses} losses\n{source}\n")
            try:
                with source.open("rb") as handle:
                    data = handle.read(8192)
                preview = data.decode("utf-8") if b'\x00' not in data else "[Binary file; text preview unavailable]"
                text.insert("end", preview + "\n[Preview limited to first 8 KiB]\n\n")
            except (OSError, UnicodeError):
                text.insert("end", "[Binary or unreadable file]\n\n")
            PillButton(dialog, f"Open {mod.mod_id} folder", command=lambda path=source.parent: _open_target(path)).pack(side="left", padx=8, pady=10)
        text.configure(state="disabled")

    def choose_winner(self):
        if self.app._busy or not self.app.plan:
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Select a conflict", "Select a file first, then choose its winner.", parent=self)
            return
        path = self.tree.item(selection[0], "values")[0]
        conflict = next(c for c in self.app.plan.conflicts if c.relative_path == path)
        dialog = tk.Toplevel(self)
        dialog.title("Choose file winner")
        dialog.configure(bg=BG)
        dialog.transient(self.app)
        tk.Label(dialog, text=path, bg=BG, fg=TEXT, wraplength=550).pack(padx=20, pady=18)
        tk.Label(dialog, text="Select a version. Apply / Sync writes your choices to the game.", bg=BG, fg=MUTED).pack(padx=20)
        names = {mod.mod_id: mod.name for mod in self.app.plan.mods}

        def select(mod_id, all_files=False):
            paths = [c.relative_path for c in self.app.plan.conflicts if mod_id in c.providers] if all_files else [path]
            for relative in paths:
                self.app.settings.file_winners[relative] = mod_id
            self.app.store.save(self.app.settings)
            dialog.destroy()
            self.app.refresh(preview=True)

        for mod_id in conflict.providers:
            row = tk.Frame(dialog, bg=BG)
            row.pack(fill="x", padx=20, pady=8)
            PillButton(row, f"Use {names[mod_id]}", command=lambda mid=mod_id: select(mid)).pack(side="left")
            PillButton(row, "Prefer for all its conflicts", command=lambda mid=mod_id: select(mid, True)).pack(side="right", padx=8)

    def set_plan(self, plan: SyncPlan | None) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not plan:
            self.summary.configure(text="Run Preview to inspect conflicts.")
            return
        names = {mod.mod_id: mod.name for mod in plan.mods}
        for conflict in plan.conflicts:
            losers = [names.get(mod_id, mod_id) for mod_id in conflict.providers if mod_id != conflict.winner]
            self.tree.insert(
                "",
                "end",
                values=(conflict.relative_path, names.get(conflict.winner, conflict.winner), ", ".join(losers), conflict.category, conflict.status),
            )
        self.summary.configure(
            text=(
                f"{sum(c.status == 'Pending' for c in plan.conflicts)} pending • "
                f"{sum(c.status == 'Applied' for c in plan.conflicts)} applied • "
                f"{sum(c.category == 'Identical duplicate' for c in plan.conflicts)} identical duplicates"
                if plan.conflicts
                else "No file conflicts among enabled mods."
            ),
            fg=GOLD if plan.conflicts else GREEN,
        )


class BackupsPage(Page):
    def __init__(self, parent, app: ManagerApp):
        super().__init__(parent, app)
        self.heading(
            "Backups & Safety",
            "Original files are preserved automatically and restored when their final owning mod is disabled or removed.",
        )
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28)
        buttons = tk.Frame(self, bg=BG)
        buttons.pack(fill="x", padx=28, pady=20)
        PillButton(buttons, "Open NWN2 Folder", command=self.open_destination).pack(side="left")
        PillButton(buttons, "Open Manager Backups", command=self.open_state).pack(side="left", padx=8)
        PillButton(buttons, "View Sync Log", command=self.open_log).pack(side="left")
        self.initialize_button = PillButton(
            buttons, "Initialize From Clean Backup", command=app.show_initialization, accent=True
        )
        self.initialize_button.pack(side="right")

    def _row(self, title: str, value: str, color: str = TEXT) -> None:
        row = tk.Frame(self.body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=(0, 10))
        tk.Label(row, text=title, bg=PANEL, fg=MUTED, font=("Sans", 9, "bold")).pack(
            anchor="w", padx=16, pady=(12, 4)
        )
        tk.Label(
            row,
            text=value,
            bg=PANEL,
            fg=color,
            font=("Sans", 10),
            justify="left",
            anchor="w",
            wraplength=760,
        ).pack(fill="x", padx=16, pady=(0, 13))

    def refresh_view(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        settings = self.app.settings
        if not settings.destination_dir:
            self._row("DESTINATION", "Not configured", RED)
            return
        clean = clean_backup_path(settings)
        managed = self.app.store.load_managed().get("files", {})
        baseline = self.app.store.load_baselines().get("files", {})
        self._row(
            "MANAGER STATUS",
            "Initialized and ready" if settings.initialized else "One-time initialization still required",
            GREEN if settings.initialized else GOLD,
        )
        self._row(
            "ORIGINAL CLEAN BACKUP",
            f"Found\n{clean}" if clean.is_dir() else f"Not found\nExpected at: {clean}",
            GREEN if clean.is_dir() else MUTED,
        )
        self._row(
            "REVERSIBLE FILE OWNERSHIP",
            f"{len(managed):,} managed file(s) • {len(baseline):,} recorded original baseline(s)\n"
            f"Stored in: {self.app.store.state_dir}",
        )
        manual = sorted(settings.destination.parent.glob(f"{settings.destination.name}.manual-modded-backup*"))
        self._row(
            "PRESERVED MANUAL SETUPS",
            "\n".join(str(path) for path in manual) if manual else "None created by this app yet.",
        )
        if settings.initialized:
            self.initialize_button.configure(state="disabled")
        else:
            self.initialize_button.configure(state="normal")

    def open_destination(self) -> None:
        if self.app.settings.destination_dir:
            _open_target(self.app.settings.destination)

    def open_state(self) -> None:
        self.app.store.state_dir.mkdir(parents=True, exist_ok=True)
        _open_target(self.app.store.state_dir)

    def open_log(self) -> None:
        self.app.store.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.app.store.log_file.exists():
            self.app.store.log_file.write_text("No synchronization has been run yet.\n", encoding="utf-8")
        _open_target(self.app.store.log_file)


class SettingsPage(Page):
    def __init__(self, parent, app: ManagerApp):
        super().__init__(parent, app)
        self.heading(
            "Settings",
            "Bazzite, native Steam, Flatpak Steam, and custom Steam libraries are detected automatically.",
        )
        form = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        form.pack(fill="x", padx=28)
        self.workshop_var = tk.StringVar()
        self.destination_var = tk.StringVar()
        self.auto_preview_var = tk.BooleanVar(value=True)
        self.update_var = tk.BooleanVar(value=True)
        self._path_row(form, "WORKSHOP CONTENT FOLDER", self.workshop_var, self.choose_workshop)
        self._path_row(form, "NWN2 PROTON DOCUMENTS FOLDER", self.destination_var, self.choose_destination)
        options = tk.Frame(form, bg=PANEL)
        options.pack(fill="x", padx=16, pady=(4, 15))
        ttk.Checkbutton(
            options, text="Calculate a change preview on launch", variable=self.auto_preview_var
        ).pack(side="left")
        ttk.Checkbutton(options, text="Check for manager updates on launch", variable=self.update_var).pack(side="left")

        actions = tk.Frame(self, bg=BG)
        actions.pack(fill="x", padx=28, pady=16)
        PillButton(actions, "Detect Steam Library", command=self.detect).pack(side="left")
        PillButton(actions, "Save Settings", command=self.save, accent=True).pack(side="left", padx=8)
        PillButton(actions, "Check for Updates", command=app.check_for_updates).pack(side="left", padx=8)

        integration = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        integration.pack(fill="x", padx=28, pady=(10, 0))
        tk.Label(
            integration, text="DESKTOP INTEGRATION", bg=PANEL, fg=MUTED, font=("Sans", 9, "bold")
        ).pack(anchor="w", padx=16, pady=(14, 5))
        tk.Label(
            integration,
            text="Add the AppImage to your desktop environment's Applications menu, or remove that shortcut later.",
            bg=PANEL,
            fg=TEXT,
            font=("Sans", 10),
        ).pack(anchor="w", padx=16)
        integration_buttons = tk.Frame(integration, bg=PANEL)
        integration_buttons.pack(fill="x", padx=16, pady=14)
        PillButton(integration_buttons, "Add to Applications", command=self.install_shortcut).pack(
            side="left"
        )
        PillButton(integration_buttons, "Remove Shortcut", command=self.remove_shortcut).pack(
            side="left", padx=8
        )

    def _path_row(self, parent, label: str, variable: tk.StringVar, command) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=16, pady=(14, 2))
        tk.Label(row, text=label, bg=PANEL, fg=MUTED, font=("Sans", 8, "bold")).pack(anchor="w")
        controls = tk.Frame(row, bg=PANEL)
        controls.pack(fill="x", pady=(5, 0))
        ttk.Entry(controls, textvariable=variable).pack(side="left", fill="x", expand=True)
        PillButton(controls, "Browse…", command=command).pack(side="left", padx=(8, 0))

    def load_values(self) -> None:
        self.workshop_var.set(self.app.settings.workshop_dir)
        self.destination_var.set(self.app.settings.destination_dir)
        self.auto_preview_var.set(self.app.settings.auto_preview)
        self.update_var.set(self.app.settings.check_updates)

    def choose_workshop(self) -> None:
        selected = filedialog.askdirectory(
            title="Choose workshop/content/2738630",
            initialdir=self.workshop_var.get() or str(Path.home()),
            parent=self,
        )
        if selected:
            self.workshop_var.set(selected)

    def choose_destination(self) -> None:
        selected = filedialog.askdirectory(
            title="Choose Documents/Neverwinter Nights 2",
            initialdir=self.destination_var.get() or str(Path.home()),
            parent=self,
        )
        if selected:
            self.destination_var.set(selected)

    def detect(self) -> None:
        candidates = detect_steam_candidates()
        if not candidates:
            messagebox.showinfo(
                "Steam library not found",
                "No NWN2 EE Workshop or Proton folder was detected. You can still choose both folders manually.",
                parent=self,
            )
            return
        best = candidates[0]
        self.workshop_var.set(str(best.workshop))
        self.destination_var.set(str(best.destination))
        messagebox.showinfo(
            "Steam library detected", f"Using Steam library:\n{best.library}", parent=self
        )

    def save(self) -> None:
        new_workshop = self.workshop_var.get().strip()
        new_destination = self.destination_var.get().strip()
        old_paths = (self.app.settings.workshop_dir, self.app.settings.destination_dir)
        new_paths = (new_workshop, new_destination)
        if old_paths != new_paths and self.app.store.load_managed().get("files"):
            messagebox.showerror(
                "Managed destination already active",
                "This manager state already belongs to another NWN2 folder. Return to that folder before syncing. "
                "A future profile can be used for a second installation.",
                parent=self,
            )
            self.load_values()
            return
        self.app.settings.workshop_dir = new_workshop
        self.app.settings.destination_dir = new_destination
        self.app.settings.auto_preview = self.auto_preview_var.get()
        self.app.settings.check_updates = self.update_var.get()
        if old_paths != new_paths:
            self.app.settings.initialized = False
        try:
            # The destination may be waiting to be created from an existing clean
            # backup, so only require a real Workshop directory at this stage.
            if not self.app.settings.workshop.is_dir():
                raise ConfigurationError("The selected Workshop folder does not exist.")
            if not self.app.settings.destination.is_dir():
                raise ConfigurationError("The selected NWN2 destination folder does not exist.")
            self.app.store.save(self.app.settings)
            self.app.update_setup_banner()
            self.app.set_status("Settings saved")
            messagebox.showinfo("Settings saved", "Steam and NWN2 folders are configured.", parent=self)
            if not self.app.settings.initialized:
                self.app.show_initialization()
        except ManagerError as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self)

    def install_shortcut(self) -> None:
        try:
            path = install_desktop_entry()
            messagebox.showinfo("Shortcut installed", f"Applications entry created:\n{path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Could not install shortcut", str(exc), parent=self)

    def remove_shortcut(self) -> None:
        try:
            remove_desktop_entry()
            messagebox.showinfo("Shortcut removed", "The Applications entry was removed.", parent=self)
        except OSError as exc:
            messagebox.showerror("Could not remove shortcut", str(exc), parent=self)


class InitializationDialog(tk.Toplevel):
    def __init__(self, parent: ManagerApp, settings: Settings, store: ConfigStore):
        super().__init__(parent)
        self.parent = parent
        self.settings = settings
        self.store = store
        self.title("One-time safe setup")
        self.configure(bg=BG)
        self.geometry("700x500")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        tk.Label(self, text="Prepare a safe baseline", bg=BG, fg=TEXT, font=("Sans", 20, "bold")).pack(
            anchor="w", padx=26, pady=(24, 6)
        )
        tk.Label(
            self,
            text="Do this once before the manager installs Workshop files.",
            bg=BG,
            fg=MUTED,
            font=("Sans", 10),
        ).pack(anchor="w", padx=26)

        backup = clean_backup_path(settings)
        card = tk.Frame(self, bg=PANEL, highlightbackground=GOLD if backup.is_dir() else BORDER, highlightthickness=1)
        card.pack(fill="x", padx=26, pady=(22, 12))
        tk.Label(
            card,
            text="RECOMMENDED • RESTORE THE CLEAN PRE-WORKSHOP BACKUP",
            bg=PANEL,
            fg=GOLD,
            font=("Sans", 9, "bold"),
        ).pack(anchor="w", padx=16, pady=(14, 5))
        tk.Label(
            card,
            text=(
                f"Found: {backup}\n\nYour current manually modded folder will be preserved, then this clean copy becomes the "
                "working folder. Nothing is deleted."
                if backup.is_dir()
                else f"No clean backup was found at:\n{backup}"
            ),
            bg=PANEL,
            fg=TEXT if backup.is_dir() else MUTED,
            justify="left",
            wraplength=620,
        ).pack(anchor="w", padx=16)
        self.clean_button = PillButton(
            card, "Use Clean Backup", command=self.use_clean, accent=True, state="normal" if backup.is_dir() else "disabled"
        )
        self.clean_button.pack(anchor="w", padx=16, pady=14)

        current = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        current.pack(fill="x", padx=26)
        tk.Label(
            current,
            text="KEEP THE CURRENT FOLDER AS THE BASELINE",
            bg=PANEL,
            fg=MUTED,
            font=("Sans", 9, "bold"),
        ).pack(anchor="w", padx=16, pady=(13, 5))
        tk.Label(
            current,
            text="Use this only if the current folder is already clean. Files currently present are treated as originals.",
            bg=PANEL,
            fg=TEXT,
            justify="left",
            wraplength=620,
        ).pack(anchor="w", padx=16)
        PillButton(current, "Use Current Folder", command=self.use_current).pack(
            anchor="w", padx=16, pady=13
        )
        tk.Label(
            self,
            text="Close NWN2 and Steam before using the clean-backup option.",
            bg=BG,
            fg=MUTED,
            font=("Sans", 9, "italic"),
        ).pack(side="bottom", pady=18)

    def use_clean(self) -> None:
        if not messagebox.askyesno(
            "Restore clean baseline?",
            "Confirm that NWN2 and Steam are closed. The current folder will be moved to a preserved "
            "manual-modded backup, then the clean backup will be copied into place.",
            parent=self,
        ):
            return
        try:
            preserved = initialize_from_clean_backup(self.settings, self.store)
            messagebox.showinfo(
                "Safe baseline ready",
                "The clean NWN2 folder is ready.\n\n"
                + (f"Your previous manual setup was preserved at:\n{preserved}" if preserved else ""),
                parent=self,
            )
            self.destroy()
        except (ManagerError, OSError, shutil.Error) as exc:
            messagebox.showerror("Initialization failed", str(exc), parent=self)

    def use_current(self) -> None:
        if not messagebox.askyesno(
            "Use current folder?",
            "Only continue if this NWN2 folder is already clean. Existing files that overlap Workshop mods will be "
            "preserved and treated as originals.",
            parent=self,
        ):
            return
        try:
            initialize_current_folder(self.settings, self.store)
            self.destroy()
        except (ManagerError, OSError) as exc:
            messagebox.showerror("Initialization failed", str(exc), parent=self)


class PreviewDialog(tk.Toplevel):
    def __init__(self, parent: ManagerApp, plan: SyncPlan):
        super().__init__(parent)
        self.title("Workshop change preview")
        self.configure(bg=BG)
        self.geometry("850x580")
        self.transient(parent)

        tk.Label(self, text="Change Preview", bg=BG, fg=TEXT, font=("Sans", 20, "bold")).pack(
            anchor="w", padx=24, pady=(22, 4)
        )
        summary = (
            f"{plan.count('install')} install • {plan.count('update')} update • "
            f"{plan.count('restore')} restore • {plan.count('remove')} remove • "
            f"{plan.unchanged} unchanged"
        )
        tk.Label(self, text=summary, bg=BG, fg=MUTED).pack(anchor="w", padx=24, pady=(0, 15))

        wrap = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=24)
        tree = ttk.Treeview(wrap, columns=("action", "path", "owner"), show="headings")
        tree.heading("action", text="ACTION")
        tree.heading("path", text="DESTINATION FILE")
        tree.heading("owner", text="WORKSHOP ITEM")
        tree.column("action", width=95)
        tree.column("path", width=530)
        tree.column("owner", width=130)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for action in plan.actions:
            tree.insert("", "end", values=(action.kind.title(), action.relative_path, action.mod_id))

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=24, pady=18)
        PillButton(bottom, "Close", command=self.destroy).pack(side="right")
        PillButton(bottom, "Sync These Changes", command=self._sync, accent=True).pack(
            side="right", padx=(0, 8)
        )

    def _sync(self) -> None:
        self.destroy()
        self.master.sync_now()


def run_gui() -> None:
    app = ManagerApp()
    app.mainloop()
