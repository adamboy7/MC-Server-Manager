#!/usr/bin/env python3
"""Minecraft server setup GUI -- proof of concept.

Vanilla only for now; the provider dropdown is wired up so Paper/Fabric/Forge
appear automatically once their modules land in mcserver/providers/.

Threading model: all network work happens on a daemon worker thread, which
never touches a widget. It pushes ('event', payload) tuples onto a queue that
the Tk thread drains from an after() poll. That's the only safe way to do this
-- Tk is not thread-safe, and calling widget methods from the worker will
eventually deadlock or segfault rather than fail loudly.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcserver.providers import PROVIDERS, get_provider  # noqa: E402
from mcserver.providers.base import Cancelled, ProviderError, Version  # noqa: E402

POLL_MS = 60


class ServerSetupApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.provider = None
        self.versions: list[Version] = []
        self.busy = False
        self.cancel_event: "threading.Event | None" = None

        self._build_widgets()
        self.after(POLL_MS, self._drain_events)
        self._reload_versions()

    # ---------------- layout ----------------

    def _build_widgets(self) -> None:
        r = 0
        ttk.Label(self, text="Server type:").grid(row=r, column=0, sticky="w", pady=4)
        self.provider_var = tk.StringVar(value=next(iter(PROVIDERS)))
        self.provider_box = ttk.Combobox(
            self, textvariable=self.provider_var, state="readonly",
            values=list(PROVIDERS), width=18,
        )
        self.provider_box.grid(row=r, column=1, sticky="w", pady=4)
        self.provider_box.bind("<<ComboboxSelected>>", lambda _e: self._reload_versions())

        r += 1
        ttk.Label(self, text="Version:").grid(row=r, column=0, sticky="w", pady=4)
        self.version_var = tk.StringVar()
        # Wide enough for the longest entry the channel split can produce,
        # e.g. "1.21.11  (snapshot)  Recommended".
        self.version_box = ttk.Combobox(
            self, textvariable=self.version_var, state="disabled", width=34,
        )
        self.version_box.grid(row=r, column=1, sticky="ew", pady=4)

        r += 1
        self.snapshots_var = tk.BooleanVar(value=False)
        # Off, the list is one entry per Minecraft release, already resolved to
        # the loader's recommended build (or its newest, marked "(latest)", when
        # nothing is promoted yet). On, snapshots join the list and each version
        # splits into Recommended / Latest so a specific build can be chosen.
        ttk.Checkbutton(
            self, text="Include snapshots, pre-releases and Latest builds",
            variable=self.snapshots_var, command=self._reload_versions,
        ).grid(row=r, column=1, sticky="w")

        r += 1
        ttk.Label(self, text="Install to:").grid(row=r, column=0, sticky="w", pady=4)
        pathrow = ttk.Frame(self)
        pathrow.grid(row=r, column=1, sticky="ew", pady=4)
        pathrow.columnconfigure(0, weight=1)
        self.dir_var = tk.StringVar()
        ttk.Entry(pathrow, textvariable=self.dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(pathrow, text="Browse...", command=self._pick_dir).grid(
            row=0, column=1, padx=(6, 0)
        )

        r += 1
        self.eula_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text="I accept the Minecraft EULA (minecraft.net/eula)",
            variable=self.eula_var,
        ).grid(row=r, column=1, sticky="w", pady=(2, 8))

        r += 1
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 2))

        r += 1
        self.status = ttk.Label(self, text="Loading versions...", foreground="#555")
        self.status.grid(row=r, column=0, columnspan=2, sticky="w")

        # Zero-height spacer that soaks up any vertical slack. Without it the
        # slack lands *below* the last row, which reads as a dead gap under the
        # buttons; with it, the form stays top-aligned and the buttons hug the
        # bottom edge the way a dialog's buttons should.
        r += 1
        self.rowconfigure(r, weight=1)

        r += 1
        btns = ttk.Frame(self)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.cancel_btn = ttk.Button(
            btns, text="Cancel", command=self._request_cancel, state="disabled"
        )
        self.cancel_btn.grid(row=0, column=0, padx=(0, 6))
        self.install_btn = ttk.Button(
            btns, text="Create Server", command=self._start_install, state="disabled"
        )
        self.install_btn.grid(row=0, column=1)

    # ---------------- helpers ----------------

    def _pick_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Choose an empty folder for the server")
        if chosen:
            self.dir_var.set(os.path.normpath(chosen))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "readonly"
        self.provider_box.configure(state=state)
        self.version_box.configure(state=state if self.versions else "disabled")
        self.install_btn.configure(
            state="disabled" if (busy or not self.versions) else "normal"
        )
        # Cancel is only meaningful while an install is actually running.
        self.cancel_btn.configure(
            state="normal" if (busy and self.cancel_event is not None) else "disabled"
        )

    def _request_cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.cancel_btn.configure(state="disabled")
            self.status.configure(text="Cancelling...", foreground="#a60")

    def _emit(self, kind: str, *payload) -> None:
        """Called from worker threads only."""
        self.events.put((kind, payload))

    # ---------------- version loading ----------------

    def _reload_versions(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.versions = []
        self.version_box.configure(values=[], state="disabled")
        self.version_var.set("")
        self.status.configure(text="Loading versions...", foreground="#555")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        name = self.provider_var.get()
        unstable = self.snapshots_var.get()

        def work() -> None:
            try:
                prov = get_provider(name)
                versions = prov.list_versions(include_unstable=unstable)
                self._emit("versions", prov, versions)
            except ProviderError as exc:
                self._emit("error", "Could not load versions", str(exc))
            except Exception as exc:  # PoC: surface anything unexpected
                self._emit("error", "Unexpected error loading versions", repr(exc))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- install ----------------

    def _start_install(self) -> None:
        if self.busy:
            return
        dest = self.dir_var.get().strip()
        idx = self.version_box.current()
        if idx < 0 or not self.versions:
            messagebox.showwarning("Pick a version", "Select a Minecraft version first.")
            return
        if not dest:
            messagebox.showwarning("Pick a folder", "Choose where to install the server.")
            return

        version = self.versions[idx]

        # Providers that compile (Spigot) can tell us up front that the build
        # cannot succeed. Far better than failing 15 minutes in.
        check = getattr(self.provider, "check_prerequisites", None)
        if check is not None:
            try:
                problems = check(version)
            except ProviderError as exc:
                messagebox.showerror("Could not check requirements", str(exc))
                return
            if problems:
                messagebox.showerror(
                    f"{self.provider.name} {version.id} cannot be built here",
                    "\n\n".join(problems),
                )
                return

        if os.path.isdir(dest) and os.listdir(dest):
            if not messagebox.askyesno(
                "Folder is not empty",
                f"{dest}\n\nis not empty. Existing files with the same names "
                "(server.jar, start.bat, start.sh) will be overwritten.\n\nContinue?",
            ):
                return

        self.cancel_event = threading.Event()
        self._set_busy(True)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.status.configure(text="Starting...", foreground="#555")

        prov = self.provider
        accept = self.eula_var.get()
        cancel = self.cancel_event

        def work() -> None:
            def progress(stage, done, total, msg):
                self._emit("progress", stage, done, total, msg)

            try:
                result = prov.install(
                    version, dest, progress=progress, accept_eula=accept, cancel=cancel
                )
                self._emit("done", version, result)
            except Cancelled:
                self._emit("cancelled")
            except ProviderError as exc:
                self._emit("error", "Install failed", str(exc))
            except Exception as exc:
                self._emit("error", "Unexpected error", repr(exc))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- event pump (Tk thread) ----------------

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                handler = getattr(self, f"_on_{kind}", None)
                if handler:
                    handler(*payload)
        except queue.Empty:
            pass
        finally:
            self.after(POLL_MS, self._drain_events)

    def _on_versions(self, prov, versions: list[Version]) -> None:
        self.cancel_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.provider = prov
        self.versions = versions

        if not versions:
            self.status.configure(text="No versions found.", foreground="#b00")
            self._set_busy(False)
            return

        self.version_box.configure(values=[str(v) for v in versions], state="readonly")
        self.version_box.current(0)  # newest
        note = ""
        if getattr(prov, "compiles_from_source", False):
            note = "  -- builds from source, expect 10-30 min"
        self.status.configure(
            text=f"{len(versions)} versions available. Newest: {versions[0]}{note}",
            foreground="#555",
        )
        self._set_busy(False)

    def _on_progress(self, stage, done, total, msg) -> None:
        if total:
            pct = min(100, done * 100 // total)
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=pct)
        else:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        self.status.configure(text=msg, foreground="#555")

    def _on_cancelled(self) -> None:
        self.cancel_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.status.configure(text="Cancelled.", foreground="#a60")
        self._set_busy(False)
        messagebox.showinfo(
            "Cancelled", "The install was cancelled. No server was created."
        )

    def _on_done(self, version: Version, result) -> None:
        self.cancel_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100)
        self.status.configure(text="Done.", foreground="#070")
        self._set_busy(False)

        lines = [
            f"{self.provider.name} {version.id} server installed to:",
            result.server_dir,
            "",
            "Start it with start.bat (Windows) or ./start.sh (Linux/macOS).",
        ]
        if result.notes:
            lines += ["", *result.notes]
        messagebox.showinfo("Server ready", "\n".join(lines))

    def _on_error(self, title: str, detail: str) -> None:
        self.cancel_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.status.configure(text=title, foreground="#b00")
        self._set_busy(False)
        messagebox.showerror(title, detail)


MIN_WIDTH = 520  # enough for the install-path entry to be usable


def main() -> None:
    root = tk.Tk()
    root.title("Minecraft Server Setup")
    try:
        root.call("tk", "scaling", 1.3)
    except tk.TclError:
        pass
    ServerSetupApp(root)

    # Size to the content instead of a hardcoded height. Fonts and DPI scaling
    # differ enough between Windows, macOS and Linux that any fixed number is
    # either clipping something or leaving a gap on two of the three.
    root.update_idletasks()
    width = max(MIN_WIDTH, root.winfo_reqwidth())
    height = root.winfo_reqheight()
    root.geometry(f"{width}x{height}")
    root.minsize(width, height)  # never shrink below the form
    root.mainloop()


if __name__ == "__main__":
    main()
