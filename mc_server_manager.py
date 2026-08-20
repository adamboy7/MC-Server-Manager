#!/usr/bin/env python3
"""
Minecraft Server Manager
=========================
A desktop GUI for browsing a folder full of Minecraft servers: shows the world
preview (if any), the detected server platform and version, the player roster
(with face textures and operator highlighting), and the backups the server's
own tooling has made.

Requirements:
    pip install Pillow requests

Usage:
    python mc_server_manager.py

This file is the GUI. Everything it knows about Minecraft lives in the `mcsm`
package beside it, which has no GUI dependencies at all:

    mcsm.nbt         NBT reading, and byte-patching a single tag in place
    mcsm.model       ServerInfo / Platform / PlayerInfo
    mcsm.loaders     the loader vocabulary, shared with Fetch/
    mcsm.properties  server.properties
    mcsm.players     UUIDs and name lookup from local files
    mcsm.world       world layout, dimensions, export
    mcsm.backups     finding, reading, writing and restoring backups
    mcsm.detect      what platform a server folder is running
    mcsm.mojang      the session server (the only module that uses the network)
    mcsm.textedit    handing a file to the user's text editor
    mcsm.util        size/date helpers

Because none of those import tkinter, they can be exercised without a display
-- which is what the test suite does.
"""

import io
import json
import queue
import re
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    raise SystemExit("Missing dependency: Pillow. Install with: pip install Pillow")

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency: requests. Install with: pip install requests")


from mcsm.backups import (
    backup_search_dirs,
    create_world_backup,
    detect_backup_providers,
    get_backups_dir,
    get_world_backups_dir,
    list_world_backups,
    open_backup_archive,
    resolve_backup_convention,
    restore_backup_set,
)
from mcsm.detect import (
    detect_server,
    looks_like_server_folder,
)
from mcsm.model import (
    PlayerInfo,
    ServerInfo,
    format_mods_line,
    format_platform_line,
    format_version_line,
)
from mcsm.mojang import fetch_username_from_api
from mcsm.nbt import set_nbt_byte
from mcsm.players import (
    OP_LEVEL_DESCRIPTIONS,
    default_op_level,
    get_banned_players_path,
    get_ops_path,
    get_whitelist_path,
    load_name_map,
)
from mcsm.properties import (
    parse_bool_property,
    read_server_properties,
    update_server_properties,
)
from mcsm.textedit import open_in_text_editor
from mcsm.util import (
    CACHE_DIR,
    compute_folder_size,
    format_size,
)
from mcsm.world import (
    ALLOW_COMMANDS_PATH,
    ExportCancelled,
    export_world,
    get_level_dat_path,
    get_nether_dir,
    get_playerdata_dir,
    get_the_end_dir,
    get_world_dir,
    has_satellite_dimension_folders,
    plan_world_export,
    read_allow_commands,
    server_looks_live,
)

FACE_URL_TEMPLATES = [
    "https://crafatar.com/avatars/{uuid}?size={size}&overlay",
    "https://mc-heads.net/avatar/{uuid}/{size}",
]

PREVIEW_SIZE = 128  # world icon preview box, in pixels

# Used for anything destructive or irreversible -- the Ban menu entry, and the
# Allow Cheats toggle, which unlike the rest of the Settings dialog writes into
# the world's level.dat rather than a plain-text config file.
DANGER_COLOR = "#CC0000"


def fetch_face_image(u: str, size: int = 32) -> Optional[Image.Image]:
    cache_file = CACHE_DIR / f"{u}.png"
    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGBA").resize((size, size), Image.NEAREST)
        except Exception:
            cache_file.unlink(missing_ok=True)

    for template in FACE_URL_TEMPLATES:
        url = template.format(uuid=u, size=size)
        try:
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            img.save(cache_file)
            return img.resize((size, size), Image.NEAREST)
        except Exception:
            continue
    return None


def placeholder_face(size: int = 32) -> Image.Image:
    img = Image.new("RGBA", (size, size), (90, 90, 90, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, size - 3, size - 3], outline=(200, 200, 200, 255))
    draw.line([2, 2, size - 3, size - 3], fill=(140, 140, 140, 255))
    draw.line([2, size - 3, size - 3, 2], fill=(140, 140, 140, 255))
    return img


class Tooltip:
    """Small hover tooltip attached to a single Tk widget."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _show(self, event=None):
        if self.tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#FFFFE0",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            wraplength=300,
        ).pack(ipadx=4, ipady=2)

    def _hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Minecraft Server Manager")
        self.geometry("1050x680")
        self.minsize(820, 520)

        self.root_folder: Optional[Path] = None
        self.servers: list = []
        self.tree_index: dict = {}
        self.image_refs: list = []  # keep PhotoImage refs alive, Tk drops GC'd images
        self.task_queue: "queue.Queue" = queue.Queue()
        self.current_server: Optional[ServerInfo] = None
        self._pending_world_size = ""
        self._pending_total_size = ""

        self._build_ui()
        self.after(100, self._poll_queue)

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=6)
        ttk.Button(toolbar, text="Select Servers Folder...", command=self.choose_folder).pack(side="left")
        self.folder_label = ttk.Label(toolbar, text="No folder selected")
        self.folder_label.pack(side="left", padx=10)
        ttk.Button(toolbar, text="Rescan", command=self.rescan).pack(side="right")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # Left: server list
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        columns = ("tags", "date", "difficulty")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Server")
        self.tree.heading("tags", text="Type")
        self.tree.heading("date", text="Date")
        self.tree.heading("difficulty", text="Difficulty")
        self.tree.column("#0", width=220)
        self.tree.column("tags", width=120)
        self.tree.column("date", width=100)
        self.tree.column("difficulty", width=80)
        self.tree.pack(fill="both", expand=True, side="left")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_server)
        self.tree.bind("<Button-3>", self._show_server_menu)

        # Right: details
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        details = ttk.Frame(right)
        details.pack(fill="x", pady=(0, 8))

        self.name_label = ttk.Label(details, text="Select a server", font=("Segoe UI", 14, "bold"))
        self.name_label.pack(anchor="w")

        self.motd_label = ttk.Label(
            details, text="", foreground="#555555", font=("Segoe UI", 9, "italic"),
            wraplength=360, justify="left",
        )
        # Not packed here -- only shown when the selected server has a motd set;
        # on_select_server() toggles it with .pack()/.pack_forget().

        self.preview_frame = preview_frame = tk.Frame(
            details, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
            bg="#222222", relief="groove", bd=1,
        )
        preview_frame.pack_propagate(False)
        preview_frame.pack(anchor="w", pady=(6, 6))
        self.preview_label = tk.Label(preview_frame, bg="#222222",
                                       text="No\nPreview", fg="#888888", justify="center")
        self.preview_label.pack(fill="both", expand=True)
        preview_frame.bind("<Button-3>", self._show_preview_menu)
        self.preview_label.bind("<Button-3>", self._show_preview_menu)

        self.tags_label = ttk.Label(details, text="")
        self.tags_label.pack(anchor="w")
        self.version_label = ttk.Label(details, text="")
        self.version_label.pack(anchor="w")
        self.mods_label = ttk.Label(details, text="")
        # Not packed here -- only shown for servers that have a mods folder
        # (platform.mods_dir); on_select_server() toggles it with
        # .pack()/.pack_forget().
        self.path_label = ttk.Label(details, text="", foreground="#666666")
        self.path_label.pack(anchor="w")

        self.world_size_label = ttk.Label(details, text="", foreground="#666666")
        self.world_size_label.pack(anchor="w")

        self.backup_size_label = ttk.Label(details, text="", foreground="#666666")
        # Not packed here -- only shown when the selected server has a backups
        # folder; on_select_server() toggles it with .pack()/.pack_forget().

        players_header = ttk.Frame(right)
        players_header.pack(fill="x")
        ttk.Label(players_header, text="Players", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.player_count_label = ttk.Label(players_header, text="")
        self.player_count_label.pack(side="left", padx=8)

        player_container = ttk.Frame(right)
        player_container.pack(fill="both", expand=True, pady=(4, 0))

        self.player_canvas = tk.Canvas(player_container, highlightthickness=0)
        player_vsb = ttk.Scrollbar(player_container, orient="vertical", command=self.player_canvas.yview)
        self.player_frame = ttk.Frame(self.player_canvas)
        self.player_frame.bind(
            "<Configure>",
            lambda e: self.player_canvas.configure(scrollregion=self.player_canvas.bbox("all")),
        )
        self.player_canvas.create_window((0, 0), window=self.player_frame, anchor="nw")
        self.player_canvas.configure(yscrollcommand=player_vsb.set)
        self.player_canvas.pack(side="left", fill="both", expand=True)
        player_vsb.pack(side="right", fill="y")
        self._bind_mousewheel(self.player_canvas)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

        # Styling for controls that write somewhere riskier than a config
        # file. Themes vary in how much of a widget's colour they'll cede to
        # a style (the Windows "vista" theme in particular draws its own
        # checkbox indicator), so anything using this pairs it with a plain
        # red ttk.Label, whose foreground every theme honours.
        style = ttk.Style(self)
        style.configure("Danger.TCheckbutton", foreground=DANGER_COLOR)
        style.map(
            "Danger.TCheckbutton",
            foreground=[("active", DANGER_COLOR), ("selected", DANGER_COLOR),
                        ("disabled", "#AA8888")],
        )

    def _bind_mousewheel(self, widget):
        """Scroll `widget` (a Canvas) on the mouse wheel, but only while the
        pointer is actually over it -- not globally. MouseWheel events go
        straight to whatever child widget (a player row's label, say) is
        under the cursor rather than bubbling up to the canvas, so a plain
        widget.bind() won't see them; the traditional workaround is
        bind_all(). But bind_all() is application-wide, so once bound it
        also hijacks wheel scrolling in *other* windows -- e.g. it would
        keep scrolling the player list while the Roll Back dialog is
        focused and being scrolled. To avoid that, only bind_all() while
        hovering `widget` or one of its descendants (toggled via Enter/
        Leave), and undo it the moment the pointer leaves. Call
        _hook_scroll_children() after adding new descendants (e.g. player
        rows) so they're covered too.
        """
        self._scroll_widget = widget

        def _is_scrollable():
            bbox = widget.bbox("all")
            return bool(bbox) and (bbox[3] - bbox[1]) > widget.winfo_height()

        def _scroll(units):
            if _is_scrollable():
                widget.yview_scroll(units, "units")

        def _bind_wheel(_event=None):
            widget.bind_all("<MouseWheel>", lambda e: _scroll(-1 if e.delta > 0 else 1))  # Windows/macOS
            widget.bind_all("<Button-4>", lambda e: _scroll(-1))  # Linux
            widget.bind_all("<Button-5>", lambda e: _scroll(1))

        def _unbind_wheel(_event=None):
            widget.unbind_all("<MouseWheel>")
            widget.unbind_all("<Button-4>")
            widget.unbind_all("<Button-5>")

        self._scroll_bind_wheel = _bind_wheel
        self._scroll_unbind_wheel = _unbind_wheel
        self._hook_scroll_children(widget)

    def _hook_scroll_children(self, widget):
        """Recursively wire up Enter/Leave hover tracking (see
        _bind_mousewheel) on `widget` and all of its current descendants.
        Call this again after adding new child widgets -- e.g. after
        rebuilding the player list -- so the wheel keeps working while
        hovering the new rows."""
        widget.bind("<Enter>", self._scroll_bind_wheel, add="+")
        widget.bind("<Leave>", self._scroll_unbind_wheel, add="+")
        for child in widget.winfo_children():
            self._hook_scroll_children(child)

    def _center_over_main_window(self, dialog: tk.Toplevel):
        """Position a Toplevel centered over the main window instead of
        wherever the OS/WM decides to place it (on Windows this is often a
        fixed cascade position with no relation to the main window or the
        mouse). Requires the dialog's widgets to already be packed so its
        requested size is known."""
        dialog.update_idletasks()
        w = dialog.winfo_width()
        h = dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # -- Folder scanning -----------------------------------------------------

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Select folder containing Minecraft servers")
        if not folder:
            return
        self.root_folder = Path(folder)
        self.folder_label.config(text=str(self.root_folder))
        self.rescan()

    def rescan(self):
        if not self.root_folder:
            return
        self.tree.delete(*self.tree.get_children())
        self.status_var.set("Scanning...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        servers = []
        try:
            subdirs = [p for p in sorted(self.root_folder.iterdir()) if p.is_dir()]
        except OSError as e:
            self.task_queue.put(("error", f"Could not read folder: {e}"))
            self.task_queue.put(("scan_done", []))
            return

        for sub in subdirs:
            if not looks_like_server_folder(sub):
                continue
            try:
                info = detect_server(sub)
                servers.append(info)
            except Exception as e:
                self.task_queue.put(("error", f"Failed to scan {sub.name}: {e}"))
        self.task_queue.put(("scan_done", servers))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.task_queue.get_nowait()
                if kind == "scan_done":
                    self.servers = payload
                    self._populate_tree()
                    self.status_var.set(f"Found {len(self.servers)} server(s)")
                elif kind == "error":
                    self.status_var.set(payload)
                elif kind == "face_ready":
                    # PhotoImage MUST be constructed on the main thread -- Tk is not
                    # thread-safe, so the background worker only fetched a PIL image.
                    pil_img, target_label = payload
                    if target_label.winfo_exists():
                        tkimg = ImageTk.PhotoImage(pil_img)
                        self.image_refs.append(tkimg)
                        target_label.configure(image=tkimg)
                elif kind == "name_ready":
                    target_label, resolved_name, suffix_bits = payload
                    if target_label.winfo_exists():
                        name_text = resolved_name + ("  [" + ", ".join(suffix_bits) + "]" if suffix_bits else "")
                        target_label.configure(text=name_text)
                elif kind == "preview_ready":
                    pil_img = payload
                    tkimg = ImageTk.PhotoImage(pil_img)
                    self.image_refs.append(tkimg)
                    self.preview_label.configure(image=tkimg, text="")
                elif kind == "world_size_ready":
                    info, size_bytes = payload
                    if info is self.current_server:
                        self._pending_world_size = format_size(size_bytes) if size_bytes is not None else "n/a"
                        self._update_size_label()
                elif kind == "total_size_ready":
                    info, size_bytes = payload
                    if info is self.current_server:
                        self._pending_total_size = format_size(size_bytes)
                        self._update_size_label()
                elif kind == "backup_size_ready":
                    info, size_bytes = payload
                    if info is self.current_server:
                        self.backup_size_label.config(text=f"Backup size: {format_size(size_bytes)}")
                elif kind == "backup_progress":
                    info, n, name = payload
                    progress = getattr(self, "_backup_progress", None)
                    if progress:
                        try:
                            progress["detail"].config(text=f"{n} files -- {name[:44]}")
                        except tk.TclError:
                            pass
                elif kind == "backup_done":
                    info, paths, error = payload
                    self._close_progress_dialog(getattr(self, "_backup_progress", None))
                    self._backup_progress = None
                    if error is not None:
                        messagebox.showerror("Backup Failed", str(error))
                    elif paths is None:
                        self.status_var.set("Backup cancelled")
                    else:
                        # The backup folder just grew; drop the cached sizes
                        # so the detail pane recomputes on next selection.
                        info.backup_size_bytes = None
                        info.sizes_computed = False
                        names = ", ".join(p.name for p in paths)
                        self.status_var.set(
                            f"Backed up {info.level_name}: {len(paths)} archive"
                            f"{'s' if len(paths) != 1 else ''} ({names})")
                elif kind == "restore_progress":
                    n, name = payload
                    progress = getattr(self, "_restore_progress", None)
                    if progress:
                        try:
                            progress["detail"].config(text=f"{n} files -- {name[:44]}")
                        except tk.TclError:
                            pass
                elif kind == "restore_done":
                    info, result, error = payload
                    self._close_progress_dialog(getattr(self, "_restore_progress", None))
                    self._restore_progress = None
                    self._finish_world_restore(info, result, error)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_index = {}
        for s in self.servers:
            iid = self.tree.insert("", "end", text=s.name, values=(", ".join(s.tags), s.last_log_date, s.difficulty))
            self.tree_index[iid] = s

    # -- Server list context menu --------------------------------------------

    def _show_server_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        info = self.tree_index.get(iid)
        if not info:
            return
        has_backups = get_backups_dir(info).is_dir()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open Folder...", command=lambda: self._open_server_folder(info))
        menu.add_command(
            label="Create Backup...",
            command=lambda: self._create_backup(info),
            state="normal" if get_world_dir(info).is_dir() else "disabled",
        )
        menu.add_command(
            label="Roll Back World...",
            command=lambda: self._open_world_rollback_dialog(info),
            state="normal" if get_world_dir(info).is_dir() else "disabled",
        )
        menu.add_command(
            label="Browse Backups...",
            command=lambda: self._open_backups_folder(info),
            state="normal" if has_backups else "disabled",
        )
        menu.add_command(
            label="Copy Seed",
            command=lambda: self._copy_seed(info),
            state="normal" if info.seed is not None else "disabled",
        )
        menu.add_command(
            label="Export World...",
            command=lambda: self._export_world(info),
            state="normal" if get_world_dir(info).is_dir() else "disabled",
        )
        menu.add_separator()
        menu.add_command(label="Set MOTD...", command=lambda: self._set_motd(info))
        menu.add_command(label="Settings...", command=lambda: self._open_settings(info))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_server_folder(self, info: ServerInfo):
        if not info.path.exists():
            messagebox.showwarning("Folder Not Found", f"{info.path} no longer exists.")
            return
        try:
            subprocess.run(["explorer", str(info.path)])
        except OSError as e:
            messagebox.showerror("Error", f"Could not open Explorer: {e}")

    def _copy_seed(self, info: ServerInfo):
        self.clipboard_clear()
        self.clipboard_append(info.seed)
        self.update()
        self.status_var.set(f"Copied seed for {info.name}")

    def _open_backups_folder(self, info: ServerInfo):
        backups_dir = get_backups_dir(info)
        if not backups_dir.is_dir():
            messagebox.showwarning("Folder Not Found", f"{backups_dir} no longer exists.")
            return
        target_dir = get_world_backups_dir(info)
        try:
            subprocess.run(["explorer", str(target_dir)])
        except OSError as e:
            messagebox.showerror("Error", f"Could not open Explorer: {e}")

    # -- Creating a backup ----------------------------------------------------

    def _create_backup(self, info: ServerInfo):
        """Back the world up now, following whatever convention the server's
        own backup tool uses."""
        world_dir = get_world_dir(info)
        if not world_dir.is_dir():
            messagebox.showwarning("World Not Found", f"{world_dir} no longer exists.")
            return

        convention = resolve_backup_convention(info)
        providers = detect_backup_providers(info)
        folders = [info.level_name]
        if has_satellite_dimension_folders(info):
            folders += [f"{info.level_name}_nether", f"{info.level_name}_the_end"]

        size_note = (format_size(info.world_size_bytes)
                     if info.world_size_bytes is not None else "not yet calculated")
        lines = [
            f"Back up {info.level_name} from {info.name}?",
            "",
            f"Archives:     {len(folders)} ({', '.join(folders)})",
            f"Destination:  {convention.target_dir(info.level_name, datetime.now())}",
            f"World size:   {size_note}",
        ]
        matching = next((p for p in providers if p.key == convention.provider), None)
        if matching is not None:
            # Mimicry means their retention counts ours. Say so before the
            # user relies on this archive being there next month.
            note = matching.retention or "its own retention policy"
            lines += ["",
                      f"This follows {matching.name}'s naming, so it will sit "
                      f"alongside its archives -- and {note} applies to this one too."]
        live = server_looks_live(info)
        if live:
            lines += ["", f"WARNING: {live}",
                      "A backup taken while the server is writing may be inconsistent."]
        if not messagebox.askyesno("Create Backup", "\n".join(lines)):
            return

        cancel_event = threading.Event()
        progress = self._open_progress_dialog(
            "Creating Backup", f"Backing up {info.level_name}...", cancel_event)

        def worker():
            try:
                paths = create_world_backup(
                    info, progress_cb=lambda n, name: self.task_queue.put(
                        ("backup_progress", (info, n, name))),
                    cancel_event=cancel_event)
                self.task_queue.put(("backup_done", (info, paths, None)))
            except ExportCancelled:
                self.task_queue.put(("backup_done", (info, None, None)))
            except Exception as e:
                self.task_queue.put(("backup_done", (info, None, e)))

        self._backup_progress = progress
        threading.Thread(target=worker, daemon=True).start()

    # -- Rolling the world back -----------------------------------------------

    def _open_world_rollback_dialog(self, info: ServerInfo):
        """Pick a backup set and restore the whole world from it.

        The list is of *sets*, not files: a split world is archived as one
        file per dimension sharing a timestamp, and restoring one third of a
        world is never what anyone means."""
        sets = list_world_backups(info)
        if not sets:
            dirs = "\n".join(f"  {d}" for d in backup_search_dirs(info)) or "  (none found)"
            messagebox.showinfo(
                "No Backups Found",
                f"No backup archives were found for {info.level_name}.\n\n"
                f"Searched:\n{dirs}")
            return

        dialog = tk.Toplevel(self)
        dialog.title(f"Roll Back World - {info.name}")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text=f"Backups of {info.level_name}:",
                  font=("Segoe UI", 10, "bold")).pack(padx=12, pady=(12, 6), anchor="w")

        cols = ("provider", "dims", "size", "status")
        tree = ttk.Treeview(dialog, columns=cols, show="tree headings", height=11)
        tree.heading("#0", text="Date")
        tree.heading("provider", text="Made by")
        tree.heading("dims", text="Dimensions")
        tree.heading("size", text="Size")
        tree.heading("status", text="Status")
        tree.column("#0", width=140)
        tree.column("provider", width=130)
        tree.column("dims", width=90, anchor="center")
        tree.column("size", width=80, anchor="e")
        tree.column("status", width=230)
        tree.tag_configure("unavailable", foreground="#999999")
        tree.tag_configure("suspect", foreground=DANGER_COLOR)
        tree.pack(padx=12, fill="both", expand=True)

        set_by_iid = {}
        for bset in sets:
            iid = tree.insert("", "end", text=bset.display_date(), values=(
                bset.provider_name, len(bset.members),
                format_size(bset.total_bytes), "Checking..."))
            set_by_iid[iid] = bset
            if not bset.restorable:
                tree.item(iid, values=(
                    bset.provider_name, len(bset.members),
                    format_size(bset.total_bytes),
                    "Read-only -- use the mod's own restore"), tags=("unavailable",))

        result_queue: "queue.Queue" = queue.Queue()

        def scan_worker():
            for iid, bset in set_by_iid.items():
                if not bset.restorable:
                    continue
                problems = []
                for folder, member in sorted(bset.members.items()):
                    archive = open_backup_archive(member.path)
                    if archive is None:
                        problems.append(f"{member.path.name} is unreadable")
                        continue
                    with archive:
                        reason = archive.incremental_reason()
                        if reason:
                            problems.append(f"{folder}: {reason}")
                missing = bset.missing_folders(info)
                result_queue.put((iid, problems, missing))

        threading.Thread(target=scan_worker, daemon=True).start()
        ok_iids = set()

        def poll_results():
            if not dialog.winfo_exists():
                return
            try:
                while True:
                    iid, problems, missing = result_queue.get_nowait()
                    bset = set_by_iid[iid]
                    base = (bset.provider_name, len(bset.members),
                            format_size(bset.total_bytes))
                    if problems:
                        tree.item(iid, values=base + (problems[0][:60],),
                                  tags=("suspect",))
                    elif missing:
                        ok_iids.add(iid)
                        tree.item(iid, values=base + (
                            "Incomplete -- missing " + ", ".join(missing),),
                            tags=("suspect",))
                    else:
                        ok_iids.add(iid)
                        tree.item(iid, values=base + ("Ready",))
            except queue.Empty:
                pass
            dialog.after(100, poll_results)

        dialog.after(100, poll_results)

        scope_var = tk.StringVar(value="all")
        scope_frame = ttk.LabelFrame(dialog, text="Restore")
        scope_frame.pack(padx=12, pady=(8, 0), fill="x")
        for value, label in (
            ("all", "Everything in the backup"),
            ("world", "World only -- keeps current player progress"),
            ("players", "Players only -- keeps current terrain"),
        ):
            ttk.Radiobutton(scope_frame, text=label, value=value,
                            variable=scope_var).pack(anchor="w", padx=8, pady=1)
        scope_note = ttk.Label(dialog, text="", foreground="#666666", wraplength=660,
                               justify="left")
        scope_note.pack(padx=12, pady=(4, 0), anchor="w")

        def describe_scope(*_a):
            bset = set_by_iid.get(tree.selection()[0]) if tree.selection() else None
            folders = ", ".join(sorted(bset.members)) if bset else "the world folder(s)"
            scope = scope_var.get()
            if scope == "all":
                text = (f"Replaces {folders} entirely. The current copy is kept as "
                        f"<folder>.old-<stamp> until every archive has landed, then "
                        f"deleted.")
            elif scope == "world":
                text = (f"Overwrites everything in {folders} except playerdata/, "
                        f"players/, stats/ and advancements/. Files not in the backup "
                        f"are left alone.")
            else:
                text = (f"Overwrites only playerdata/, players/, stats/ and "
                        f"advancements/ inside {folders}. Files not in the backup are "
                        f"left alone.")
            scope_note.config(text=text)

        scope_var.trace_add("write", describe_scope)
        describe_scope()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=12, fill="x")
        ttk.Button(btn_frame, text="Cancel",
                   command=dialog.destroy).pack(side="right", padx=(6, 0))
        restore_btn = ttk.Button(btn_frame, text="Roll Back", state="disabled")
        restore_btn.pack(side="right")

        def on_select(_event=None):
            sel = tree.selection()
            restore_btn.configure(
                state="normal" if sel and sel[0] in ok_iids else "disabled")
            describe_scope()

        def do_restore():
            sel = tree.selection()
            if not sel or sel[0] not in ok_iids:
                return
            bset = set_by_iid[sel[0]]
            if self._confirm_and_start_world_restore(info, bset, scope_var.get()):
                dialog.destroy()

        restore_btn.configure(command=do_restore)
        tree.bind("<<TreeviewSelect>>", on_select)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        self._center_over_main_window(dialog)

    def _confirm_and_start_world_restore(self, info: ServerInfo, bset, scope: str) -> bool:
        """Confirm, optionally take a safety backup, then run the restore on
        a worker thread. Returns True if the restore was started."""
        live = server_looks_live(info)
        if live and not messagebox.askyesno(
            "Server May Be Running",
            f"{live}\n\n"
            "This check is a guess -- a recently-touched session.lock does not "
            "prove the server is up, and a stopped server can still look busy. "
            "But if it IS running, it holds the world open and will write over "
            "whatever is restored, usually within seconds.\n\n"
            "Continue anyway?",
            default="no",
        ):
            return False

        missing = bset.missing_folders(info)
        scope_text = {"all": "everything", "world": "world files only",
                      "players": "player files only"}[scope]
        lines = [
            f"Roll {info.level_name} back to the backup dated {bset.display_date()}?",
            "",
            f"Made by:   {bset.provider_name}",
            f"Restoring: {scope_text}",
            f"Archives:  {', '.join(sorted(bset.members))}",
        ]
        if missing:
            lines += ["",
                      "WARNING: this backup has no archive for " + ", ".join(missing) +
                      ". Those dimensions will be left as they are, which may leave "
                      "the world inconsistent."]
        lines += ["", "This overwrites the live world."]
        if not messagebox.askyesno("Roll Back World", "\n".join(lines)):
            return False

        safety_dir = resolve_backup_convention(info, "safety").directory
        safety = messagebox.askyesnocancel(
            "Safety Backup",
            "Back up the current world first?\n\n"
            "Strongly recommended -- without it the current world cannot be "
            f"recovered after this restore.\n\n"
            f"It goes to {safety_dir}, which is ours alone: unlike a normal "
            "backup it does not follow the installed tool's naming, so that "
            "tool's retention can never prune it.")
        if safety is None:
            return False
        if safety:
            try:
                create_world_backup(info, purpose="safety")
            except Exception as e:
                messagebox.showerror(
                    "Safety Backup Failed",
                    f"Could not back up the current world:\n\n{e}\n\n"
                    "The rollback has been cancelled.")
                return False
        elif not messagebox.askyesno(
            "No Safety Backup",
            "Continue without a safety backup?\n\n"
            "The current state of this world will be gone for good."
        ):
            return False

        cancel_event = threading.Event()
        self._restore_progress = self._open_progress_dialog(
            "Rolling Back", f"Restoring {info.level_name}...", cancel_event)

        def worker():
            try:
                result = restore_backup_set(
                    info, bset, scope,
                    progress_cb=lambda n, name: self.task_queue.put(
                        ("restore_progress", (n, name))),
                    cancel_event=cancel_event)
                self.task_queue.put(("restore_done", (info, result, None)))
            except ExportCancelled:
                self.task_queue.put(("restore_done", (info, None, None)))
            except Exception as e:
                self.task_queue.put(("restore_done", (info, None, e)))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _finish_world_restore(self, info: ServerInfo, result, error):
        if error is not None:
            messagebox.showerror(
                "Rollback Failed",
                f"{error}\n\nAny folders already swapped have been put back.")
            return
        if result is None:
            self.status_var.set("Rollback cancelled")
            return
        # Version, seed, players and even the detected platform can all change
        # after a restore -- a backup taken on Spigot restored onto Paper now
        # reads as a conflict, which is correct and worth showing.
        self._rescan_single_server(info)
        self.status_var.set(
            f"Restored {', '.join(result['restored'])} -- {result['files']} files")

    def _rescan_single_server(self, info: ServerInfo):
        """Re-detect one server in place and refresh the UI for it."""
        try:
            fresh = detect_server(info.path)
        except Exception:
            return
        for iid, existing in self.tree_index.items():
            if existing is info:
                self.tree_index[iid] = fresh
                self.tree.item(iid, text=fresh.name, values=(
                    ", ".join(fresh.tags), fresh.last_log_date, fresh.difficulty))
                for i, s in enumerate(self.servers):
                    if s is info:
                        self.servers[i] = fresh
                if self.current_server is info:
                    self.tree.selection_set(iid)
                    self.on_select_server(None)
                break

    def _open_progress_dialog(self, title: str, message: str, cancel_event):
        """Small modal progress window with a Cancel button. Returns a dict
        holding the widgets so the queue handler can update and close it."""
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()
        label = ttk.Label(dialog, text=message, width=52)
        label.pack(padx=14, pady=(14, 6), anchor="w")
        detail = ttk.Label(dialog, text="", foreground="#666666", width=52)
        detail.pack(padx=14, pady=(0, 8), anchor="w")
        bar = ttk.Progressbar(dialog, mode="indeterminate", length=340)
        bar.pack(padx=14, pady=(0, 8))
        bar.start(12)
        ttk.Button(dialog, text="Cancel",
                   command=cancel_event.set).pack(padx=14, pady=(0, 12), anchor="e")
        # Closing the window means the same thing as pressing Cancel; the
        # worker thread is what actually stops.
        dialog.protocol("WM_DELETE_WINDOW", cancel_event.set)
        self._center_over_main_window(dialog)
        return {"dialog": dialog, "label": label, "detail": detail, "bar": bar}

    def _close_progress_dialog(self, progress):
        if not progress:
            return
        try:
            progress["bar"].stop()
            progress["dialog"].grab_release()
            progress["dialog"].destroy()
        except tk.TclError:
            pass

    # -- World export ---------------------------------------------------------

    def _export_world(self, info: ServerInfo):
        """Copy this server's world out to a standalone folder in the vanilla
        single-folder layout, ready to drop into .minecraft/saves or a vanilla
        server. For a Bukkit-style split world the three dimension folders are
        merged back into one; for a single-folder world it's a plain copy, so
        the menu entry behaves the same way everywhere."""
        world_dir = get_world_dir(info)
        if not world_dir.is_dir():
            messagebox.showwarning("World Not Found", f"{world_dir} no longer exists.")
            return

        split = has_satellite_dimension_folders(info)
        parent = filedialog.askdirectory(
            title=f"Export {info.level_name} to folder...",
            mustexist=True,
        )
        if not parent:
            return
        parent_path = Path(parent)

        # The exported folder is named after the level, since that's the name
        # the world will show up under in the singleplayer save list.
        safe_level = re.sub(r'[\\/:*?"<>|]', "_", info.level_name) or "world"
        dest = parent_path / safe_level
        if dest.exists():
            alt = self._next_available_name(parent_path, safe_level)
            if not messagebox.askyesno(
                "Folder Exists",
                f"'{safe_level}' already exists in that folder.\n\n"
                f"Export as '{alt.name}' instead?",
            ):
                return
            dest = alt

        # Guard against writing the export inside one of the folders being
        # read. The plan is built before any file is written so this wouldn't
        # actually recurse forever, but it would leave a copy of the world
        # nested inside the live world -- never what anyone meant. Exporting
        # elsewhere under the server folder is fine and stays allowed.
        try:
            dest_resolved = dest.resolve()
            sources = [world_dir]
            if split:
                sources += [get_nether_dir(info), get_the_end_dir(info)]
            for source in sources:
                source_resolved = source.resolve()
                if dest_resolved == source_resolved or source_resolved in dest_resolved.parents:
                    messagebox.showerror(
                        "Invalid Destination",
                        f"Choose a destination outside {source.name} -- exporting a "
                        "world into itself would nest the copy inside the original.",
                    )
                    return
        except OSError:
            pass

        self._run_export(info, dest, split)

    @staticmethod
    def _next_available_name(parent: Path, base: str) -> Path:
        """First unused '{base} (n)' inside parent. Bounded rather than a bare
        while True so a pathological folder can't spin forever; after the cap
        we fall back to a timestamp, which is effectively guaranteed free."""
        for n in range(2, 1000):
            candidate = parent / f"{base} ({n})"
            if not candidate.exists():
                return candidate
        return parent / f"{base} {datetime.now().strftime('%Y-%m-%d--%H-%M-%S')}"

    def _run_export(self, info: ServerInfo, dest: Path, split: bool):
        """Modal progress dialog driving the export on a worker thread. The
        worker owns everything slow (walking the world to build the plan, then
        the copy itself) and reports through a shared state dict that the UI
        polls -- pushing one queue message per file would swamp the event loop
        on a world with tens of thousands of region/chunk files."""
        dialog = tk.Toplevel(self)
        dialog.title("Export World")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        summary = (
            f"Merging {info.level_name}, _nether and _the_end into one world folder"
            if split else
            f"Copying {info.level_name}"
        )
        ttk.Label(dialog, text=summary, font=("Segoe UI", 10, "bold")).pack(
            padx=14, pady=(14, 2), anchor="w"
        )
        ttk.Label(dialog, text=f"To: {dest}", foreground="#666666", wraplength=420).pack(
            padx=14, pady=(0, 10), anchor="w"
        )

        bar = ttk.Progressbar(dialog, mode="indeterminate", length=420)
        bar.pack(padx=14, pady=(0, 6))
        bar.start(15)

        detail_var = tk.StringVar(value="Scanning world...")
        ttk.Label(dialog, textvariable=detail_var, foreground="#555555").pack(
            padx=14, pady=(0, 10), anchor="w"
        )

        cancel_event = threading.Event()
        result_queue: "queue.Queue" = queue.Queue()
        # Written by the worker, read by the poller. Plain dict assignment is
        # atomic enough under the GIL for a progress readout; nothing here
        # needs a consistent multi-field snapshot.
        state = {"phase": "scan", "files": 0, "bytes": 0, "total": 0}

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=14, pady=(0, 14), anchor="e")
        cancel_btn = ttk.Button(btn_frame, text="Cancel")
        cancel_btn.pack(side="right")

        def request_cancel():
            cancel_event.set()
            cancel_btn.configure(state="disabled")
            detail_var.set("Cancelling...")

        cancel_btn.configure(command=request_cancel)
        dialog.protocol("WM_DELETE_WINDOW", request_cancel)
        dialog.bind("<Escape>", lambda e: request_cancel())

        def worker():
            try:
                plan = plan_world_export(info)
                if cancel_event.is_set():
                    raise ExportCancelled()
                if not plan:
                    result_queue.put(("empty", None))
                    return
                state["total"] = len(plan)
                state["phase"] = "copy"

                def progress_cb(files_done, bytes_done):
                    state["files"] = files_done
                    state["bytes"] = bytes_done

                total_bytes = export_world(plan, dest, progress_cb, cancel_event)
                result_queue.put(("done", (len(plan), total_bytes)))
            except ExportCancelled:
                self._remove_partial_export(dest)
                result_queue.put(("cancelled", None))
            except OSError as e:
                self._remove_partial_export(dest)
                result_queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            if not dialog.winfo_exists():
                return
            try:
                status, payload = result_queue.get_nowait()
            except queue.Empty:
                if state["phase"] == "copy" and state["total"]:
                    if str(bar.cget("mode")) != "determinate":
                        bar.stop()
                        bar.configure(mode="determinate", maximum=state["total"])
                    bar["value"] = state["files"]
                    detail_var.set(
                        f"Copying {state['files']} of {state['total']} files "
                        f"({format_size(state['bytes'])})"
                    )
                dialog.after(100, poll)
                return

            bar.stop()
            dialog.grab_release()
            dialog.destroy()
            self._finish_export(info, dest, status, payload)

        dialog.after(100, poll)
        self._center_over_main_window(dialog)

    @staticmethod
    def _remove_partial_export(dest: Path):
        """Delete a half-written export. Safe because export_world creates dest
        itself and refuses to run if it already exists, so whatever is in there
        was written by this export and nothing else."""
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass

    def _finish_export(self, info: ServerInfo, dest: Path, status: str, payload):
        if status == "done":
            file_count, total_bytes = payload
            self.status_var.set(
                f"Exported {info.level_name} ({file_count} files, "
                f"{format_size(total_bytes)}) to {dest}"
            )
            if messagebox.askyesno(
                "Export Complete",
                f"Exported {info.level_name} to:\n{dest}\n\n"
                f"{file_count} files, {format_size(total_bytes)}.\n\n"
                "Open the folder now?",
            ):
                try:
                    subprocess.run(["explorer", str(dest)])
                except OSError as e:
                    messagebox.showerror("Error", f"Could not open Explorer: {e}")
        elif status == "cancelled":
            self.status_var.set(f"Export of {info.level_name} cancelled")
        elif status == "empty":
            messagebox.showwarning(
                "Nothing to Export", f"No world files found under {get_world_dir(info)}."
            )
        else:
            messagebox.showerror("Export Failed", f"Could not export the world: {payload}")
            self.status_var.set(f"Export of {info.level_name} failed")

    def _set_motd(self, info: ServerInfo):
        """Modal dialog to set/overwrite this server's motd, pre-filled with
        the current value. Writes straight to server.properties."""
        dialog = tk.Toplevel(self)
        dialog.title("Set MOTD")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(dialog, text=f"MOTD for {info.name}:").pack(padx=12, pady=(12, 4), anchor="w")
        motd_var = tk.StringVar(value=info.motd)
        entry = ttk.Entry(dialog, textvariable=motd_var, width=50)
        entry.pack(padx=12, pady=(0, 12), fill="x")
        entry.focus_set()
        entry.select_range(0, "end")

        def on_ok(event=None):
            new_motd = motd_var.get()
            try:
                update_server_properties(info.path, {"motd": new_motd})
            except OSError as e:
                messagebox.showerror("Error", f"Could not write server.properties: {e}")
                return
            info.motd = new_motd
            dialog.destroy()
            if self.current_server is info:
                self.on_select_server(None)
            self.status_var.set(f"Updated MOTD for {info.name}")

        def on_cancel(event=None):
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=(0, 12), anchor="e")
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Save", command=on_ok).pack(side="right")

        dialog.bind("<Return>", on_ok)
        dialog.bind("<Escape>", on_cancel)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self._center_over_main_window(dialog)

    def _open_settings(self, info: ServerInfo):
        """Modal dialog with quick checkboxes for the handful of boolean
        server.properties toggles that get flipped often enough to warrant a
        shortcut. Re-reads properties fresh each time so it reflects any
        manual edits made outside this app.

        "Allow Cheats" is the odd one out and is styled red to say so: it
        lives in the world's level.dat, not server.properties, so saving it
        rewrites a binary world file rather than a line of text."""
        props = read_server_properties(info.path)
        props_path = info.path / "server.properties"
        allow_cheats_initial = read_allow_commands(info)
        level_dat = get_level_dat_path(info)
        level_dat_present = level_dat.is_file()

        dialog = tk.Toplevel(self)
        dialog.title(f"Settings - {info.name}")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog, text=f"Settings for {info.name}", font=("Segoe UI", 11, "bold")
        ).pack(padx=12, pady=(12, 8), anchor="w")

        toggles = (
            ("allow-flight", "Allow Flight"),
            ("enable-command-block", "Enable Command Block"),
            ("pvp", "PvP"),
            ("white-list", "Whitelist"),
        )
        vars_by_key = {}
        for key, label in toggles:
            var = tk.BooleanVar(value=parse_bool_property(props, key))
            vars_by_key[key] = var
            ttk.Checkbutton(dialog, text=label, variable=var).pack(padx=12, pady=2, anchor="w")

        # A world with no allowCommands tag reads as None; the tag gets
        # inserted on save, so the checkbox still works -- it just starts
        # from the same "off" state Minecraft assumes when the tag is absent.
        cheats_var = tk.BooleanVar(value=bool(allow_cheats_initial))
        cheats_box = ttk.Checkbutton(
            dialog, text="Allow Cheats", variable=cheats_var, style="Danger.TCheckbutton"
        )
        cheats_box.pack(padx=12, pady=2, anchor="w")

        # The red styling is the at-a-glance signal that this one isn't a
        # server.properties toggle; the tooltip carries the detail.
        tip = "Edits level.dat, stop the server first"
        if not level_dat_present:
            tip = f"level.dat not found in {info.level_name} -- cannot edit"
            cheats_box.configure(state="disabled")
        elif allow_cheats_initial is None:
            tip += " (this world has no allowCommands tag yet -- one will be added)"
        Tooltip(cheats_box, tip)

        def pending_changes():
            """Toggles that differ from what was on disk when this dialog
            opened. Drives the prompt in on_edit_properties -- without it,
            launching an external editor over unsaved toggles is a lost
            update in whichever direction the user saves second."""
            changed = [
                label for key, label in toggles
                if vars_by_key[key].get() != parse_bool_property(props, key)
            ]
            if level_dat_present and cheats_var.get() != bool(allow_cheats_initial):
                changed.append("Allow Cheats (level.dat)")
            return changed

        def on_save(event=None):
            """Returns True if everything asked for was written. The return
            value matters to on_edit_properties, which must not launch the
            editor after a save the user cancelled."""
            cheats_changed = (
                level_dat_present and cheats_var.get() != bool(allow_cheats_initial)
            )
            if cheats_changed and not self._confirm_cheats_change(
                info, level_dat, cheats_var.get(), allow_cheats_initial
            ):
                return False

            updates = {key: ("true" if var.get() else "false") for key, var in vars_by_key.items()}
            try:
                update_server_properties(info.path, updates)
            except OSError as e:
                messagebox.showerror("Error", f"Could not write server.properties: {e}")
                return False

            # server.properties is already saved at this point, so a level.dat
            # failure below reports itself but doesn't roll anything back --
            # the two files are independent and the properties write succeeded
            # on its own terms.
            cheats_note = ""
            if cheats_changed:
                try:
                    outcome = set_nbt_byte(
                        level_dat, ALLOW_COMMANDS_PATH, 1 if cheats_var.get() else 0
                    )
                except Exception as e:
                    # Broad on purpose: a corrupt or unexpected level.dat can
                    # surface as OSError, ValueError, EOFError, gzip.BadGzipFile,
                    # zlib.error or struct.error. In a GUI a dialog beats a
                    # traceback to a console nobody is watching.
                    messagebox.showerror(
                        "Error",
                        f"Saved server.properties, but could not write level.dat: {e}",
                    )
                    dialog.destroy()
                    return False
                state = "enabled" if cheats_var.get() else "disabled"
                cheats_note = f", cheats {state}"
                if outcome == "inserted":
                    cheats_note += " (allowCommands tag added)"

            dialog.destroy()
            self.status_var.set(f"Updated settings for {info.name}{cheats_note}")
            return True

        def on_edit_properties(event=None):
            """Hand server.properties to a real text editor for the settings
            this dialog's four checkboxes don't cover.

            The dialog always closes on the way out, whether or not anything
            was saved. Leaving it open would let a later Save write the four
            toggles back over whatever the user just edited externally, and
            the dialog re-reads properties on every open anyway -- so
            reopening it after the edit shows the new values."""
            pending = pending_changes()
            if pending:
                answer = messagebox.askyesnocancel(
                    "Unsaved Changes",
                    "These toggles haven't been saved yet:\n\n  "
                    + "\n  ".join(pending)
                    + "\n\nSave them before opening server.properties?\n\n"
                    "Choose No to discard them and edit the file as it is "
                    "on disk.",
                    parent=dialog,
                )
                if answer is None:
                    return
                if answer:
                    if not on_save():
                        # Save failed, or the cheats confirmation was
                        # declined. Leave the dialog up so the user can see
                        # what state things are in.
                        return
                else:
                    dialog.destroy()
            else:
                dialog.destroy()

            try:
                opened_with = open_in_text_editor(props_path)
            except OSError as e:
                if messagebox.askyesno(
                    "Could Not Open Editor",
                    f"Couldn't open server.properties in a text editor:\n\n{e}\n\n"
                    "Show the file in Explorer instead?",
                ):
                    self._reveal_in_explorer(props_path)
                return
            self.status_var.set(
                f"Opened server.properties for {info.name} in {opened_with}"
            )

        def on_cancel(event=None):
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        # fill="x" rather than anchor="e" so the frame spans the dialog and
        # Edit Properties can sit genuinely apart from the Cancel/Save pair
        # instead of just being packed first next to them.
        btn_frame.pack(padx=12, pady=(8, 12), fill="x")
        edit_btn = ttk.Button(btn_frame, text="Edit Properties...", command=on_edit_properties)
        edit_btn.pack(side="left")
        if not props_path.is_file():
            edit_btn.configure(state="disabled")
            Tooltip(edit_btn, f"No server.properties in {info.path.name}")
        else:
            Tooltip(edit_btn, "Open server.properties in your text editor")
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Save", command=on_save).pack(side="right")

        dialog.bind("<Escape>", on_cancel)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self._center_over_main_window(dialog)

    def _confirm_cheats_change(
        self, info: ServerInfo, level_dat: Path, enabling: bool, previous: Optional[bool]
    ) -> bool:
        """Confirm a level.dat write before it happens. Returns True to
        proceed. Called before any file is touched, so declining leaves both
        server.properties and level.dat untouched."""
        action = "Enable" if enabling else "Disable"
        lines = [
            f"{action} cheats for {info.level_name}?",
            "",
            "This writes to the world's level.dat, not server.properties:",
            f"  {level_dat}",
            "",
        ]
        if previous is None:
            lines.append(
                "This world has no allowCommands tag yet, so one will be added to "
                "the Data compound."
            )
            lines.append("")
        lines.append(
            f"The current file will be copied to {level_dat.name}.mcsm-bak first."
        )
        lines.append("")
        lines.append(
            "Stop the server before saving -- a running server holds the world in "
            "memory and will overwrite level.dat on its next save, discarding this "
            "change."
        )
        return messagebox.askyesno(f"{action} Cheats", "\n".join(lines))

    # -- Server selection / details ------------------------------------------

    def on_select_server(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        info = self.tree_index.get(sel[0])
        if not info:
            return
        self.current_server = info

        self.name_label.config(text=info.name)
        if info.motd:
            self.motd_label.config(text=info.motd)
            self.motd_label.pack(anchor="w", pady=(2, 4), before=self.preview_frame)
        else:
            self.motd_label.pack_forget()
        self.tags_label.config(text=format_platform_line(info))
        self.version_label.config(text=format_version_line(info))

        mods_text = format_mods_line(info)
        if mods_text is None:
            self.mods_label.pack_forget()
        else:
            self.mods_label.config(text=mods_text)
            self.mods_label.pack(anchor="w", after=self.version_label)
        self.path_label.config(text=str(info.path))

        world_dir = get_world_dir(info)
        backups_dir = get_backups_dir(info)
        has_backups = backups_dir.is_dir()  # cheap stat, fine synchronously

        if info.sizes_computed:
            # Already scanned this ServerInfo instance -- reuse the cached
            # sizes instead of re-walking the folders. A manual Rescan
            # replaces the instance entirely, which is what invalidates this.
            self._pending_world_size = (
                format_size(info.world_size_bytes) if info.world_size_bytes is not None else "n/a"
            )
            self._pending_total_size = format_size(info.total_size_bytes)
            self._update_size_label()
            if has_backups:
                self.backup_size_label.config(text=f"Backup size: {format_size(info.backup_size_bytes)}")
                self.backup_size_label.pack(anchor="w")
            else:
                self.backup_size_label.pack_forget()
        else:
            self._pending_world_size = "Calculating..." if world_dir.is_dir() else "n/a"
            self._pending_total_size = "Calculating..."
            self._update_size_label()

            if has_backups:
                self.backup_size_label.config(text="Backup size: Calculating...")
                self.backup_size_label.pack(anchor="w")
            else:
                self.backup_size_label.pack_forget()

            # Total is always computed (the server folder always exists here);
            # world may not (e.g. a "No level.dat" server).
            threading.Thread(
                target=self._compute_sizes,
                args=(info, world_dir if world_dir.is_dir() else None, backups_dir if has_backups else None),
                daemon=True,
            ).start()

        self.preview_label.configure(image="", text="No\nPreview", bg="#222222")
        if info.icon_path:
            threading.Thread(target=self._load_preview, args=(info.icon_path,), daemon=True).start()

        for w in self.player_frame.winfo_children():
            w.destroy()

        op_count = sum(1 for p in info.players if p.is_op)
        self.player_count_label.config(
            text=f"({len(info.players)} known, {op_count} op{'s' if op_count != 1 else ''})"
        )

        if not info.players:
            ttk.Label(self.player_frame, text="No playerdata found").pack(anchor="w", padx=4, pady=4)
            self._hook_scroll_children(self.player_frame)
            return

        # Ops first, then alphabetical, for quick scanning.
        for p in sorted(info.players, key=lambda pl: (not pl.is_op, pl.name.lower())):
            self._add_player_row(p)
        self._hook_scroll_children(self.player_frame)

    def _load_preview(self, path: Path):
        try:
            img = Image.open(path).convert("RGBA")
            w, h = img.size
            scale = PREVIEW_SIZE / max(w, h)
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            self.task_queue.put(("preview_ready", img))
        except Exception:
            pass

    def _update_size_label(self):
        self.world_size_label.config(text=f"World: {self._pending_world_size} | Total: {self._pending_total_size}")

    def _compute_sizes(self, info: ServerInfo, world_dir: Optional[Path], backups_dir: Optional[Path]):
        """Background thread: sums folder sizes and posts results back through
        the task queue, tagged with `info` so a stale result (user already
        selected a different server) can be dropped in _poll_queue. Posted as
        separate messages -- world, then total (the whole server folder, which
        can be much bigger and slower, e.g. a plugins/web-map tile cache) --
        then backups, so each label fills in as soon as its own scan is done
        rather than waiting on the slowest one. Results are also cached on
        `info` itself so re-selecting this server later reuses them instead
        of re-walking the folders -- only a manual Rescan (which replaces the
        ServerInfo instance) triggers a fresh walk."""
        if world_dir is not None:
            info.world_size_bytes = compute_folder_size(world_dir)
            self.task_queue.put(("world_size_ready", (info, info.world_size_bytes)))
        else:
            info.world_size_bytes = None
        info.total_size_bytes = compute_folder_size(info.path)
        self.task_queue.put(("total_size_ready", (info, info.total_size_bytes)))
        if backups_dir is not None:
            info.backup_size_bytes = compute_folder_size(backups_dir)
            self.task_queue.put(("backup_size_ready", (info, info.backup_size_bytes)))
        else:
            info.backup_size_bytes = None
        info.sizes_computed = True

    # -- World image preview context menu ------------------------------------

    def _show_preview_menu(self, event):
        info = self.current_server
        if not info:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="Save Image...",
            command=lambda: self._save_preview_image(info),
            state="normal" if info.icon_path else "disabled",
        )
        menu.add_command(label="Replace Image...", command=lambda: self._replace_preview_image(info))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _save_preview_image(self, info: ServerInfo):
        if not info.icon_path or not info.icon_path.exists():
            messagebox.showwarning("No Image", "This server has no icon image to save.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save Image",
            initialfile=f"{info.name}_icon.png",
            defaultextension=".png",
            filetypes=[("PNG files", "*.png"), ("All files", "*.*")],
        )
        if not dest:
            return
        shutil.copy2(info.icon_path, dest)
        self.status_var.set(f"Saved image to {dest}")

    def _replace_preview_image(self, info: ServerInfo):
        src = filedialog.askopenfilename(
            title="Replace Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.gif *.bmp"), ("All files", "*.*")],
        )
        if not src:
            return
        # No existing icon anywhere for this server -- create the standard
        # vanilla dedicated-server icon at the server root.
        target = info.icon_path or (info.path / "server-icon.png")
        try:
            img = Image.open(src).convert("RGBA")
            img = img.resize((64, 64), Image.LANCZOS)  # required size for a working server icon
            img.save(target, format="PNG")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save image: {e}")
            return
        info.icon_path = target
        self.status_var.set(f"Replaced image for {info.name}")
        self.on_select_server(None)

    def _add_player_row(self, p: PlayerInfo):
        row = ttk.Frame(self.player_frame)
        row.pack(fill="x", padx=4, pady=2, anchor="w")

        face_label = tk.Label(row, width=32, height=32, bg="#333333")
        face_label.pack(side="left", padx=(0, 8))

        suffix_bits = []
        if p.is_banned:
            suffix_bits.append("BANNED")
        if p.is_op:
            suffix_bits.append("OP")
        if p.is_bedrock:
            suffix_bits.append("Bedrock?")
        name_text = p.name + ("  [" + ", ".join(suffix_bits) + "]" if suffix_bits else "")

        # Banned red takes priority over operator amber.
        fg = "#CC0000" if p.is_banned else ("#CC7A00" if p.is_op else "#000000")
        name_label = tk.Label(
            row,
            text=name_text,
            anchor="w",
            fg=fg,
            font=("Segoe UI", 10, "bold" if (p.is_op or p.is_banned) else "normal"),
        )
        name_label.pack(side="left")

        uuid_label = tk.Label(row, text=p.uuid, fg="#999999", font=("Consolas", 8))
        uuid_label.pack(side="left", padx=8)

        tooltip_text = (p.ban_reason or "No reason recorded.") if p.is_banned else None
        for widget in (row, face_label, name_label, uuid_label):
            widget.bind("<Button-3>", lambda e, pl=p: self._show_player_menu(e, pl))
            if tooltip_text:
                Tooltip(widget, tooltip_text)

        threading.Thread(target=self._load_face, args=(p, face_label), daemon=True).start()

        # Name fell back to the raw UUID -- not present in usercache.json /
        # ops.json / whitelist.json / banned-players.json. Try resolving it
        # against Mojang's API in the background rather than leaving the
        # UUID displayed.
        if p.name == p.uuid and not p.is_bedrock:
            threading.Thread(target=self._load_name, args=(p, name_label, suffix_bits), daemon=True).start()

    def _load_face(self, p: PlayerInfo, label: tk.Label):
        if p.is_bedrock:
            img = placeholder_face(32)
        else:
            img = fetch_face_image(p.uuid, 32) or placeholder_face(32)
        self.task_queue.put(("face_ready", (img, label)))

    def _load_name(self, p: PlayerInfo, label: tk.Label, suffix_bits: list):
        name = fetch_username_from_api(p.uuid)
        if name:
            p.name = name
            self.task_queue.put(("name_ready", (label, name, suffix_bits)))

    # -- Player context menu ------------------------------------------------

    def _show_player_menu(self, event, p: PlayerInfo):
        info = self.current_server
        if not info:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="View File", command=lambda: self._view_playerdata_file(info, p))
        menu.add_command(label="Copy UUID", command=lambda: self._copy_uuid(p))
        menu.add_command(label="Export Playerdata...", command=lambda: self._export_playerdata(info, p))
        has_backups = bool(list_world_backups(info))
        menu.add_command(
            label="Roll Back...",
            command=lambda: self._open_rollback_dialog(info, p),
            state="normal" if has_backups else "disabled",
        )
        menu.add_separator()
        if p.is_whitelisted:
            menu.add_command(label="Remove from Whitelist", command=lambda: self._remove_from_whitelist(info, p))
        else:
            menu.add_command(label="Add to Whitelist", command=lambda: self._add_to_whitelist(info, p))
        menu.add_separator()
        if p.is_op:
            menu.add_command(label="Remove OP...", command=lambda: self._deop_player(info, p))
        else:
            menu.add_command(label="Make OP...", command=lambda: self._op_player(info, p))
        menu.add_separator()
        if p.is_banned:
            menu.add_command(label="Pardon", command=lambda: self._pardon_player(info, p))
        else:
            menu.add_command(label="Ban...", foreground="#CC0000", command=lambda: self._ban_player(info, p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _view_playerdata_file(self, info: ServerInfo, p: PlayerInfo):
        dat_file = get_playerdata_dir(info) / f"{p.uuid}.dat"
        if not dat_file.exists():
            messagebox.showwarning("File Not Found", f"{dat_file.name} no longer exists.")
            return
        self._reveal_in_explorer(dat_file)

    def _reveal_in_explorer(self, target: Path):
        """Open Explorer with `target` selected. Reports its own failure --
        every caller reaches this as a "show me where it is" convenience, not
        as the operation they actually asked for."""
        try:
            # explorer.exe parses its command line itself rather than via
            # normal argv, and expects the path immediately after the comma,
            # quoted on its own (`/select,"C:\path with spaces\file"`). Passed
            # as a list, subprocess's list2cmdline would instead quote the
            # whole "/select,<path>" token together whenever the path has a
            # space, which explorer fails to parse -- it silently falls back
            # to its default window instead of erroring. Passing a single
            # string sidesteps list2cmdline and gives explorer exactly the
            # command line it expects.
            subprocess.run(f'explorer /select,"{target}"')
        except OSError as e:
            messagebox.showerror("Error", f"Could not open Explorer: {e}")

    def _copy_uuid(self, p: PlayerInfo):
        self.clipboard_clear()
        self.clipboard_append(p.uuid)
        self.update()
        self.status_var.set(f"Copied UUID for {p.name}")

    def _export_playerdata(self, info: ServerInfo, p: PlayerInfo):
        # Prefix match (not "{uuid}.*") so stray/duplicate files that share
        # the player's UUID but aren't exact "<uuid>.<ext>" -- e.g. a manual
        # backup copy named "<uuid> - Copy.dat" -- are still swept into the
        # export even though they're filtered out of the player list itself.
        matches = sorted(get_playerdata_dir(info).glob(f"{p.uuid}*"))
        if not matches:
            messagebox.showwarning("Nothing to Export", f"No playerdata files found for {p.name}.")
            return
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", p.name)

        if len(matches) > 1:
            choice = messagebox.askyesnocancel(
                "Multiple Files Found",
                f"Found {len(matches)} files for {p.name} (backups/mod-created files "
                "alongside the main playerdata file).\n\n"
                "Export ALL of them as a zip? Choose \"No\" to export just the main "
                f"{p.uuid}.dat file instead.",
            )
            if choice is None:
                return
            if not choice:
                main_file = get_playerdata_dir(info) / f"{p.uuid}.dat"
                matches = [main_file] if main_file in matches else matches[:1]

        if len(matches) == 1:
            src = matches[0]
            dest = filedialog.asksaveasfilename(
                title="Export Playerdata",
                initialfile=f"{safe_name}_{p.uuid}{src.suffix}",
                defaultextension=src.suffix,
                filetypes=[("All files", "*.*")],
            )
            if not dest:
                return
            shutil.copy2(src, dest)
        else:
            dest = filedialog.asksaveasfilename(
                title="Export Playerdata (zip)",
                initialfile=f"{safe_name}_{p.uuid}_playerdata.zip",
                defaultextension=".zip",
                filetypes=[("Zip files", "*.zip")],
            )
            if not dest:
                return
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in matches:
                    zf.write(f, arcname=f.name)
        self.status_var.set(f"Exported playerdata for {p.name} to {dest}")

    def _open_rollback_dialog(self, info: ServerInfo, p: PlayerInfo):
        """Modal dialog listing this world's dated backups, greyed out for
        any backup that doesn't contain the selected player. Presence is
        checked in a background thread (each backup zip's central directory
        has to be opened and scanned) and streamed back into the list as
        results come in, so the dialog isn't blocked on however many backups
        there are."""
        backups = list_world_backups(info)

        dialog = tk.Toplevel(self)
        dialog.title(f"Roll Back - {p.name}")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog, text=f"Backups for {p.name} ({p.uuid}):", font=("Segoe UI", 10, "bold")
        ).pack(padx=12, pady=(12, 6), anchor="w")

        tree = ttk.Treeview(dialog, columns=("status",), show="tree headings", height=10)
        tree.heading("#0", text="Date")
        tree.heading("status", text="Status")
        tree.column("#0", width=180)
        tree.column("status", width=160)
        tree.tag_configure("unavailable", foreground="#999999")
        tree.pack(padx=12, fill="both", expand=True)

        matches_by_iid = {}
        set_by_iid = {}
        for bset in backups:
            iid = tree.insert("", "end", text=bset.display_date(), values=("Checking...",))
            set_by_iid[iid] = bset
            if not bset.restorable:
                tree.item(iid, values=("Read-only backup",), tags=("unavailable",))

        result_queue: "queue.Queue" = queue.Queue()

        def scan_worker():
            for iid, bset in set_by_iid.items():
                if not bset.restorable:
                    continue
                # Search every archive in the set, not just the first: on a
                # split world the player files live in the overworld archive,
                # and which one that is depends on the provider's naming.
                hits = []
                for member in bset.members.values():
                    archive = open_backup_archive(member.path)
                    if archive is None:
                        continue
                    with archive:
                        for name in archive.player_file_entries(p.uuid):
                            hits.append((member.path, name, archive.rel(name)))
                result_queue.put((iid, hits))

        threading.Thread(target=scan_worker, daemon=True).start()

        def poll_results():
            if not dialog.winfo_exists():
                return
            try:
                while True:
                    iid, hits = result_queue.get_nowait()
                    if hits:
                        matches_by_iid[iid] = hits
                        tree.item(iid, values=(f"Available ({len(hits)} file"
                                               f"{'s' if len(hits) != 1 else ''})",),
                                  tags=("available",))
                    else:
                        tree.item(iid, values=("Player not found",), tags=("unavailable",))
            except queue.Empty:
                pass
            dialog.after(100, poll_results)

        dialog.after(100, poll_results)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=12, anchor="e")
        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=dialog.destroy)
        cancel_btn.pack(side="right", padx=(6, 0))
        rollback_btn = ttk.Button(btn_frame, text="Roll Back", state="disabled")
        rollback_btn.pack(side="right")

        def do_rollback():
            sel = tree.selection()
            if not sel:
                return
            iid = sel[0]
            matches = matches_by_iid.get(iid)
            if not matches:
                return
            bset = set_by_iid[iid]
            if self._perform_rollback(info, p, matches, bset.display_date()):
                dialog.destroy()

        rollback_btn.configure(command=do_rollback)

        def on_select(_event=None):
            sel = tree.selection()
            if sel and "available" in tree.item(sel[0], "tags"):
                rollback_btn.configure(state="normal")
            else:
                rollback_btn.configure(state="disabled")

        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: do_rollback())

        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._center_over_main_window(dialog)

    def _perform_rollback(
        self, info: ServerInfo, p: PlayerInfo, matches: list, display_date: str
    ) -> bool:
        """Restore one player's files from a backup, overwriting the live
        copies. `matches` is a list of (archive path, entry name, path within
        the world) tuples, which may span several archives in a set.

        Returns True on success so the caller can close the dialog, False if
        the user cancelled or it failed."""
        if len(matches) > 1:
            choice = messagebox.askyesnocancel(
                "Multiple Files Found",
                f"Found {len(matches)} files for {p.name} in this backup "
                "(backups/mod-created files alongside the main playerdata file).\n\n"
                "Restore ALL of them? Choose \"No\" to restore just the main "
                f"{p.uuid}.dat file instead.",
            )
            if choice is None:
                return False
            if not choice:
                main = next((m for m in matches
                             if m[2].rsplit("/", 1)[-1] == f"{p.uuid}.dat"), None)
                matches = [main] if main else matches[:1]

        file_list = "\n".join(sorted(m[2] for m in matches))
        if not messagebox.askyesno(
            "Roll Back Playerdata",
            f"Restore playerdata for {p.name} from the backup dated {display_date}?\n\n"
            f"This will overwrite the following live file(s):\n{file_list}\n\n"
            "This cannot be undone.",
        ):
            return False

        # The destination follows the *live* world's layout, not the
        # archive's: a backup taken before 26.x carries playerdata/, and this
        # server may now keep the same files under players/data/.
        dest_dir = get_playerdata_dir(info)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            by_archive = {}
            for archive_path, name, rel in matches:
                by_archive.setdefault(archive_path, []).append((name, rel))
            for archive_path, items in by_archive.items():
                archive = open_backup_archive(archive_path)
                if archive is None:
                    raise OSError(f"{archive_path.name} is not a readable archive")
                with archive:
                    for name, rel in items:
                        out = dest_dir / rel.rsplit("/", 1)[-1]
                        with archive.open(name) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        except (OSError, zipfile.BadZipFile) as e:
            messagebox.showerror("Error", f"Could not restore playerdata: {e}")
            return False

        self.status_var.set(f"Restored playerdata for {p.name} from backup dated {display_date}")
        return True

    def _prompt_ban_reason(self, p: PlayerInfo) -> Optional[str]:
        """Modal dialog asking for a ban reason, pre-filled with the default.
        Returns the entered reason, or None if the user cancelled."""
        dialog = tk.Toplevel(self)
        dialog.title("Ban Player")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text=f"Ban {p.name} ({p.uuid})?\n\n"
            "This edits banned-players.json directly. If the server is currently "
            "running, it may overwrite this file -- restart the server for the ban "
            "to take effect reliably.",
            wraplength=360,
            justify="left",
        ).pack(padx=12, pady=(12, 8), anchor="w")

        ttk.Label(dialog, text="Reason:").pack(padx=12, anchor="w")
        reason_var = tk.StringVar(value="Banned by an operator.")
        reason_entry = ttk.Entry(dialog, textvariable=reason_var, width=50)
        reason_entry.pack(padx=12, pady=(0, 12), fill="x")
        reason_entry.focus_set()
        reason_entry.select_range(0, "end")

        result = {"reason": None}

        def on_ok(event=None):
            result["reason"] = reason_var.get().strip() or "Banned by an operator."
            dialog.destroy()

        def on_cancel(event=None):
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(padx=12, pady=(0, 12), anchor="e")
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Ban", command=on_ok).pack(side="right")

        dialog.bind("<Return>", on_ok)
        dialog.bind("<Escape>", on_cancel)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        self._center_over_main_window(dialog)
        dialog.wait_window()
        return result["reason"]

    def _ban_player(self, info: ServerInfo, p: PlayerInfo):
        reason = self._prompt_ban_reason(p)
        if reason is None:
            return
        banned_path = get_banned_players_path(info)
        entries = []
        if banned_path.exists():
            try:
                loaded = json.loads(banned_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        entries.append({
            "uuid": p.uuid,
            "name": p.name,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000"),
            "source": "Server",
            "expires": "forever",
            "reason": reason,
        })
        try:
            banned_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write banned-players.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Banned {p.name}")

    def _pardon_player(self, info: ServerInfo, p: PlayerInfo):
        reason_text = p.ban_reason or "No reason recorded."
        if not messagebox.askyesno(
            "Pardon Player",
            f"Pardon {p.name} ({p.uuid})?\n\n"
            f"Ban reason: {reason_text}",
        ):
            return
        banned_path = get_banned_players_path(info)
        entries = []
        if banned_path.exists():
            try:
                loaded = json.loads(banned_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        if len(new_entries) == len(entries):
            return
        try:
            banned_path.write_text(json.dumps(new_entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write banned-players.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Pardoned {p.name}")

    def _add_to_whitelist(self, info: ServerInfo, p: PlayerInfo):
        whitelist_path = get_whitelist_path(info)
        entries = []
        if whitelist_path.exists():
            try:
                loaded = json.loads(whitelist_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        entries.append({"uuid": p.uuid, "name": p.name})
        try:
            whitelist_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write whitelist.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Added {p.name} to whitelist")

    def _remove_from_whitelist(self, info: ServerInfo, p: PlayerInfo):
        whitelist_path = get_whitelist_path(info)
        entries = []
        if whitelist_path.exists():
            try:
                loaded = json.loads(whitelist_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        if len(new_entries) == len(entries):
            return
        try:
            whitelist_path.write_text(json.dumps(new_entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write whitelist.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Removed {p.name} from whitelist")

    def _op_player(self, info: ServerInfo, p: PlayerInfo):
        level = default_op_level(info)
        lines = [
            f"Give operator status to {p.name} ({p.uuid})?",
            "",
            f"Permission level {level} — {OP_LEVEL_DESCRIPTIONS.get(level, 'custom permissions')}.",
        ]
        if p.name == p.uuid:
            lines.append("")
            lines.append("This player's name hasn't been resolved -- the raw UUID will be written as the name.")
        if p.is_bedrock:
            lines.append("")
            lines.append("This is a Bedrock/Geyser player -- the UUID is a Floodgate identifier, not a Mojang account.")
        live_reason = server_looks_live(info)
        if live_reason:
            lines.append("")
            lines.append(f"Warning: {live_reason} ops.json may be overwritten by the running server.")
        if not messagebox.askyesno("Op Player", "\n".join(lines)):
            return
        ops_path = get_ops_path(info)
        entries = []
        if ops_path.exists():
            try:
                loaded = json.loads(ops_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        existing = next(
            (e for e in entries if isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower()),
            None,
        )
        entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        entries.append({
            "uuid": p.uuid,
            "name": p.name,
            "level": (existing or {}).get("level", level),
            "bypassesPlayerLimit": (existing or {}).get("bypassesPlayerLimit", False),
        })
        try:
            ops_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write ops.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Opped {p.name}")

    def _deop_player(self, info: ServerInfo, p: PlayerInfo):
        lines = [f"Remove operator status from {p.name} ({p.uuid})?"]
        live_reason = server_looks_live(info)
        if live_reason:
            lines.append("")
            lines.append(f"Warning: {live_reason} ops.json may be overwritten by the running server.")
        if not messagebox.askyesno("Deop Player", "\n".join(lines)):
            return
        ops_path = get_ops_path(info)
        entries = []
        if ops_path.exists():
            try:
                loaded = json.loads(ops_path.read_text(encoding="utf-8"))
                entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                entries = []
        new_entries = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == p.uuid.lower())
        ]
        if len(new_entries) == len(entries):
            return
        try:
            ops_path.write_text(json.dumps(new_entries, indent=4), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not write ops.json: {e}")
            return
        self._reload_current_server_player_state()
        self.status_var.set(f"Deopped {p.name}")

    def _reload_current_server_player_state(self):
        info = self.current_server
        if not info:
            return
        _, ops, banned, whitelisted = load_name_map(info.path)
        for pl in info.players:
            key = pl.uuid.lower()
            pl.is_op = key in ops
            pl.is_banned = key in banned
            pl.ban_reason = banned.get(key)
            pl.is_whitelisted = key in whitelisted
        self.on_select_server(None)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
