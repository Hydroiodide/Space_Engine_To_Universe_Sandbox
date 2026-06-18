"""main.py — Tkinter GUI and entry point."""

import json, os, re, sys, shutil, threading, winsound
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import constants as _const
from constants import log_debug, set_log_callback, write_conversion_log
from scanner import parse_se_file, prescan_sc_directory, apply_limit_filter
from converter import (
    convert_to_ubox, convert_ubox_zip_to_se, convert_ubox_json_to_se,
    validate_ubox_file,
)

# ── Try winsound gracefully on non-Windows ────────────────────────────────────
try:
    import winsound as _ws
    def _ding(): _ws.MessageBeep(_ws.MB_OK)
except ImportError:
    def _ding(): pass


class ConversionGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SE → US2 Converter")

        # Maximise on Windows; fall back to a large centred geometry elsewhere
        try:
            self.root.state("zoomed")
        except Exception:
            self.root.geometry("1100x820")
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x  = (sw - 1100) // 2
            y  = (sh - 820)  // 2
            self.root.geometry(f"1100x820+{x}+{y}")
        self.root.minsize(860, 640)

        self.default_input_path  = self._find_se_export_folder()
        self.default_output_path = self._get_default_output_path()
        self._last_sc_dir        = self.default_input_path or os.path.expanduser("~")

        # ── Title ──────────────────────────────────────────────────────────────
        tk.Label(root, text="Space Engine → Universe Sandbox 2 Converter",
                 font=("Helvetica", 14, "bold")).pack(pady=(8, 4))

        # ── Input ──────────────────────────────────────────────────────────────
        file_frame = tk.LabelFrame(root, text="Input Files", padx=10, pady=6)
        file_frame.pack(fill="x", padx=12, pady=4)
        btn_row = tk.Frame(file_frame)
        btn_row.pack(fill="x", pady=4)
        tk.Button(btn_row, text="Select .sc File",       command=self.select_file,
                  width=16).pack(side="left", padx=4)
        tk.Button(btn_row, text="Select Folder (Batch)", command=self.select_folder,
                  width=18).pack(side="left", padx=4)
        self.file_var = tk.StringVar(value="No file/folder selected")
        self.is_batch = tk.BooleanVar(value=False)
        tk.Label(file_frame, textvariable=self.file_var, fg="gray",
                 wraplength=900, anchor="w").pack(fill="x", pady=2)
        if self.default_input_path:
            tk.Label(file_frame, text=f"SE export default: {self.default_input_path}",
                     fg="#0055aa", font=("Courier", 8)).pack(anchor="w")
        tk.Checkbutton(file_frame, text="Batch mode (process entire folder)",
                       variable=self.is_batch).pack(anchor="w")

        # ── Settings (two-column) ──────────────────────────────────────────────
        settings_outer = tk.LabelFrame(root, text="Export Settings", padx=10, pady=6)
        settings_outer.pack(fill="x", padx=12, pady=4)

        cols = tk.Frame(settings_outer)
        cols.pack(fill="x")
        left_col  = tk.Frame(cols)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right_col = tk.Frame(cols)
        right_col.pack(side="left", fill="both", expand=True)

        # Surface
        self.surface_var = tk.BooleanVar(value=True)
        self.surface_mode_var = tk.StringVar(value="Full US-like surface")

        # Retention controls (left)
        self.total_standalone = self.total_rings = self.total_comets = 0
        filter_frame = tk.LabelFrame(left_col,
            text="Object Keep Limits  (e.g. '25%' or exact '500')", padx=8, pady=4)
        filter_frame.pack(fill="x", pady=4)

        def _row(parent, label, default, live_text, attr_entry, attr_label):
            row = tk.Frame(parent); row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=32, anchor="w").pack(side="left")
            entry = tk.Entry(row, width=8); entry.insert(0, default)
            entry.pack(side="left", padx=3)
            lbl = tk.Label(row, text=live_text, fg="blue", width=20, anchor="w")
            lbl.pack(side="left")
            setattr(self, attr_entry, entry); setattr(self, attr_label, lbl)

        _row(filter_frame, "Asteroid Belt Members to Keep:",  "100%", "(scanning...)",
             "belt_entry",          "belt_live_label")
        _row(filter_frame, "Planetary Ring Particles to Keep:", "100%", "(max 2000)",
             "planetary_ring_entry", "planetary_ring_live_label")
        _row(filter_frame, "Comets to Keep:",                 "100%", "(scanning...)",
             "comet_entry",         "comet_live_label")

        self.belt_entry.bind("<KeyRelease>",
            lambda e: self._update_live_label(self.belt_entry, self.belt_live_label, "belt"))
        self.planetary_ring_entry.bind("<KeyRelease>",
            lambda e: self._update_planetary_ring_label())
        self.comet_entry.bind("<KeyRelease>",
            lambda e: self._update_live_label(self.comet_entry, self.comet_live_label, "comets"))

        # Basic and advanced export options.
        options_book = ttk.Notebook(right_col)
        options_book.pack(fill="x", pady=4)
        basic_frame = tk.Frame(options_book, padx=8, pady=6)
        advanced_frame = tk.Frame(options_book, padx=8, pady=6)
        options_book.add(basic_frame, text="Basic")
        options_book.add(advanced_frame, text="Advanced")

        self.moons_var         = tk.BooleanVar(value=True)
        self.dwarf_moons_var   = tk.BooleanVar(value=True)
        self.dwarfs_var        = tk.BooleanVar(value=True)
        self.rings_var         = tk.BooleanVar(value=True)
        self.export_comets_var = tk.BooleanVar(value=False)
        self.debug_var         = tk.BooleanVar(value=False)
        self.inherit_moon_tilt_var    = tk.BooleanVar(value=True)
        self.inherit_star_tilt_var    = tk.BooleanVar(value=False)
        self.align_orbits_to_star_var = tk.BooleanVar(value=False)
        self.normalize_atmosphere_var = tk.BooleanVar(value=False)
        self.atmosphere_mode_var = tk.StringVar(value="Off")
        self.start_paused_var = tk.BooleanVar(value=True)
        self.start_realtime_var = tk.BooleanVar(value=True)
        self.start_speed_value_var = tk.StringVar(value="1")
        self.start_speed_unit_var = tk.StringVar(value="hours per real second")
        self.disable_autospeed_var = tk.BooleanVar(value=False)
        self.static_atmospheres_var = tk.BooleanVar(value=False)
        self.lock_atm_depots_var = tk.BooleanVar(value=False)
        self.strict_atm_mass_var = tk.BooleanVar(value=False)
        self.strict_cloud_var = tk.BooleanVar(value=False)
        self.surface_gas_channel_var = tk.StringVar(value="Off")
        self.ocean_depot_mode_var = tk.StringVar(value="Legacy Oceans, Capped Lakes")
        self.atm_depot_mode_var = tk.StringVar(value="None / safest")
        self.surface_policy_var = tk.StringVar(value="No grid / safest")

        basic_pairs = [
            ("Export Moons",                        self.moons_var),
            ("Export Dwarf Moons",                  self.dwarf_moons_var),
            ("Export Dwarf Planets",                self.dwarfs_var),
            ("Export Rings",                        self.rings_var),
            ("Export Comets",                       self.export_comets_var),
            ("Enable Debug Logging",                self.debug_var),
            ("Generate Surface / Water Mask",       self.surface_var),
            ("Normalize Space Engine Atmospheres",  self.normalize_atmosphere_var),
        ]
        for i, (text, var) in enumerate(basic_pairs):
            tk.Checkbutton(basic_frame, text=text, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)

        basic_row = (len(basic_pairs) + 1) // 2
        tk.Label(basic_frame, text="Surface Mode:").grid(
            row=basic_row, column=0, sticky="w", padx=4, pady=2)
        self.surface_mode_combo = ttk.Combobox(
            basic_frame,
            textvariable=self.surface_mode_var,
            values=(
                "Liquid / Water Mask Only",
                "Preview only / safe",
                "Full US-like surface",
                "Active legacy / dangerous",
                "Off",
            ),
            state="readonly",
            width=29,
        )
        self.surface_mode_combo.grid(
            row=basic_row, column=1, sticky="w", padx=4, pady=2)

        tk.Label(basic_frame, text="Atmosphere Mode:").grid(
            row=basic_row + 1, column=0, sticky="w", padx=4, pady=2)
        self.atmosphere_mode_combo = ttk.Combobox(
            basic_frame,
            textvariable=self.atmosphere_mode_var,
            values=("Off", "Stability", "Habitability", "Aggressive"),
            state="readonly",
            width=18,
        )
        self.atmosphere_mode_combo.grid(
            row=basic_row + 1, column=1, sticky="w", padx=4, pady=2)

        advanced_pairs = [
            ("Align Moon Orbit to Parent Equator",  self.inherit_moon_tilt_var),
            ("Binary Star Tilt Inheritance",        self.inherit_star_tilt_var),
            ("Align Orbits to Star Equator",        self.align_orbits_to_star_var),
            ("Static Imported Atmospheres",         self.static_atmospheres_var),
            ("Lock Imported Atmospheric Depots",    self.lock_atm_depots_var),
            ("Strict Atmosphere Mass Validation",   self.strict_atm_mass_var),
            ("Strict Cloud Coverage Validation",    self.strict_cloud_var),
            ("Start Simulation Paused",             self.start_paused_var),
            ("Apply startup speed",                 self.start_realtime_var),
            ("Disable Auto-Speed on Export",        self.disable_autospeed_var),
        ]
        for i, (text, var) in enumerate(advanced_pairs):
            tk.Checkbutton(advanced_frame, text=text, variable=var).grid(
                row=i//2, column=i%2, sticky="w", padx=4, pady=1)

        speed_row = (len(advanced_pairs) + 1) // 2
        tk.Label(advanced_frame, text="Startup Speed:").grid(
            row=speed_row, column=0, sticky="w", padx=4, pady=2)
        speed_controls = tk.Frame(advanced_frame)
        speed_controls.grid(row=speed_row, column=1, sticky="w", padx=4, pady=2)
        tk.Entry(
            speed_controls, textvariable=self.start_speed_value_var, width=8
        ).pack(side="left", padx=(0, 4))
        ttk.Combobox(
            speed_controls,
            textvariable=self.start_speed_unit_var,
            values=(
                "seconds per real second",
                "minutes per real second",
                "hours per real second",
                "days per real second",
                "years per real second",
            ),
            state="readonly",
            width=24,
        ).pack(side="left")

        gas_row = speed_row + 1
        tk.Label(advanced_frame, text="Surface Gas Channel:").grid(
            row=gas_row, column=0, sticky="w", padx=4, pady=2)
        self.surface_gas_channel_combo = ttk.Combobox(
            advanced_frame,
            textvariable=self.surface_gas_channel_var,
            values=("Off", "Pressure-derived", "Raw legacy"),
            state="readonly",
            width=18,
        )
        self.surface_gas_channel_combo.grid(row=gas_row, column=1, sticky="w", padx=4, pady=2)

        ocean_row = gas_row + 1
        tk.Label(advanced_frame, text="Ocean Depot Export Mode:").grid(
            row=ocean_row, column=0, sticky="w", padx=4, pady=2)
        self.ocean_depot_mode_combo = ttk.Combobox(
            advanced_frame,
            textvariable=self.ocean_depot_mode_var,
            values=("Visual only", "Capped", "Legacy Oceans, Capped Lakes"),
            state="readonly",
            width=18,
        )
        self.ocean_depot_mode_combo.grid(row=ocean_row, column=1, sticky="w", padx=4, pady=2)

        atm_depot_row = ocean_row + 1
        tk.Label(advanced_frame, text="Static Atm Depot Mode:").grid(
            row=atm_depot_row, column=0, sticky="w", padx=4, pady=2)
        self.atm_depot_mode_combo = ttk.Combobox(
            advanced_frame,
            textvariable=self.atm_depot_mode_var,
            values=(
                "None / safest",
                "Carrier unlocked",
                "Carrier locked",
                "Chemical unlocked / test",
                "Chemical locked / test",
            ),
            state="readonly",
            width=24,
        )
        self.atm_depot_mode_combo.grid(row=atm_depot_row, column=1, sticky="w", padx=4, pady=2)

        surface_policy_row = atm_depot_row + 1
        tk.Label(advanced_frame, text="Atm Surface Policy:").grid(
            row=surface_policy_row, column=0, sticky="w", padx=4, pady=2)
        self.surface_policy_combo = ttk.Combobox(
            advanced_frame,
            textvariable=self.surface_policy_var,
            values=(
                "No grid / safest",
                "Passive grid / experimental",
                "Active legacy / dangerous",
            ),
            state="readonly",
            width=24,
        )
        self.surface_policy_combo.grid(row=surface_policy_row, column=1, sticky="w", padx=4, pady=2)

        tk.Label(
            advanced_frame,
            text=("Warning: active surface physics and legacy ocean depots can make "
                  "Universe Sandbox rewrite atmosphere pressure."),
            fg="#a33",
            wraplength=390,
            justify="left",
        ).grid(row=surface_policy_row + 1, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 2))
        _GAS_CHANNEL_TIP = (
            "Off is safest. Prevents material0.surface ch1 from fighting AtmosphereMass. "
            "Pressure-derived: safe visual map capped to atmosphere. Raw legacy: original behavior."
        )
        self.surface_gas_channel_combo.bind(
            "<Enter>", lambda e: self.status_var.set(_GAS_CHANNEL_TIP))
        self.surface_gas_channel_combo.bind(
            "<Leave>", lambda e: self.status_var.set("Ready."))
        _OCEAN_DEPOT_TIP = (
            "Visual only is safest: oceans use appearance and surface liquid maps without "
            "adding active volatile mass. Capped retains a tiny locked reservoir. Legacy "
            "exports full physical ocean mass and may change atmosphere pressure."
        )
        self.ocean_depot_mode_combo.bind(
            "<Enter>", lambda e: self.status_var.set(_OCEAN_DEPOT_TIP))
        self.ocean_depot_mode_combo.bind(
            "<Leave>", lambda e: self.status_var.set("Ready."))
        _SURFACE_MODE_TIP = (
            "Liquid / Water Mask Only attaches only material0 ch3, where water is white/high "
            "and land is black/low. Full US-like preserves canonical visual/material channels; "
            "Active legacy may rewrite imported atmosphere pressure."
        )
        self.surface_mode_combo.bind(
            "<Enter>", lambda e: self.status_var.set(_SURFACE_MODE_TIP))
        self.surface_mode_combo.bind(
            "<Leave>", lambda e: self.status_var.set("Ready."))

        # ── Output ─────────────────────────────────────────────────────────────
        out_frame = tk.LabelFrame(root, text="Output Destination", padx=10, pady=6)
        out_frame.pack(fill="x", padx=12, pady=4)
        out_row = tk.Frame(out_frame); out_row.pack(fill="x")
        tk.Button(out_row, text="Browse…", command=self.select_output_dir,
                  width=10).pack(side="left", padx=4)
        self.out_dir_var = tk.StringVar(value=self.default_output_path)
        self.auto_export_var = tk.BooleanVar(value=True)
        tk.Entry(out_row, textvariable=self.out_dir_var).pack(
            side="left", fill="x", expand=True, padx=4)
        tk.Checkbutton(out_frame, text="Auto-copy to Universe Sandbox Simulations folder",
                       variable=self.auto_export_var).pack(anchor="w")

        # ── Progress bar + status (visible before conversion starts) ───────────
        prog_frame = tk.LabelFrame(root, text="Progress", padx=10, pady=6)
        prog_frame.pack(fill="x", padx=12, pady=4)
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(prog_frame, textvariable=self.status_var,
                 anchor="w", font=("Helvetica", 9)).pack(fill="x")
        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate",
                                            length=400, maximum=100)
        self.progress_bar.pack(fill="x", pady=(2, 0))

        # ── Action buttons ─────────────────────────────────────────────────────
        btn_frame = tk.Frame(root, relief="ridge", bd=2)
        btn_frame.pack(fill="x", padx=12, pady=6)
        tk.Button(btn_frame, text="Convert", command=self.run_conversion,
                  bg="#4CAF50", fg="white", font=("Helvetica", 12, "bold"),
                  relief="raised", bd=3, width=14, height=2).pack(side="left", padx=8, pady=4)
        tk.Button(btn_frame, text="Clear Log", command=self.clear_log,
                  font=("Helvetica", 10), width=12, height=2).pack(side="left", padx=4, pady=4)
        tk.Button(btn_frame, text="Exit", command=root.quit,
                  font=("Helvetica", 10), width=10, height=2).pack(side="left", padx=4, pady=4)

        # ── Debug log (collapsed by default; shown when debug enabled) ─────────
        log_frame = tk.LabelFrame(root, text="Debug Log", padx=8, pady=4)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8,
                                                   font=("Courier", 8), state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.selected_path     = None
        self.conversion_thread = None
        self._failure_details  = []

        if self.default_input_path and os.path.isdir(self.default_input_path):
            self._trigger_scan(self.default_input_path)

    # ── Path detection ─────────────────────────────────────────────────────────

    def _find_se_export_folder(self):
        for p in [
            os.path.expanduser("~/SpaceEngine/export"),
            os.path.expanduser("~/Documents/SpaceEngine/export"),
        ]:
            if os.path.isdir(p): return p
        return None

    def _get_default_output_path(self):
        for p in [
            os.path.expanduser("~/Documents/Universe Sandbox/Simulations"),
            os.path.expanduser("~/Documents/Universe Sandbox 2/Simulations"),
        ]:
            if os.path.exists(p): return p
        primary = os.path.expanduser("~/Documents/Universe Sandbox/Simulations")
        os.makedirs(primary, exist_ok=True)
        return primary

    # ── Logging ────────────────────────────────────────────────────────────────

    def log_message(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        _const.CONVERSION_LOG = []

    def _set_status(self, text, progress=None):
        self.root.after(0, lambda: self.status_var.set(text))
        if progress is not None:
            self.root.after(0, lambda: self.progress_bar.config(value=progress))

    # ── File selection ─────────────────────────────────────────────────────────

    def select_file(self):
        f = filedialog.askopenfilename(
            initialdir=self._last_sc_dir if os.path.isdir(self._last_sc_dir) else ".",
            filetypes=[("Space Engine Scripts", "*.sc")])
        if f:
            self._last_sc_dir = os.path.dirname(f)
            self.selected_path = f
            self.file_var.set(f)
            self.is_batch.set(False)
            self._trigger_scan(os.path.dirname(f))

    def select_folder(self):
        d = filedialog.askdirectory(
            initialdir=self._last_sc_dir if os.path.isdir(self._last_sc_dir) else ".")
        if d:
            self._last_sc_dir = d
            self.selected_path = d
            self.file_var.set(f"{d} (Batch)")
            self.is_batch.set(True)
            self._trigger_scan(d)

    def select_output_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir_var.get())
        if d: self.out_dir_var.set(d)

    # ── Pre-scan ───────────────────────────────────────────────────────────────

    def _trigger_scan(self, directory):
        self.belt_live_label.config(text="(scanning...)")
        self.comet_live_label.config(text="(scanning...)")
        self.planetary_ring_live_label.config(text="(max 2000)")
        threading.Thread(target=self._scan_worker, args=(directory,), daemon=True).start()

    def _scan_worker(self, directory):
        try:
            sa, rp, cm = prescan_sc_directory(directory)
        except Exception:
            sa = rp = cm = 0
        self.total_standalone = sa
        self.total_rings      = rp
        self.total_comets     = cm
        self.root.after(0, self._refresh_all_live_labels)

    def _refresh_all_live_labels(self):
        self._update_live_label(self.belt_entry,  self.belt_live_label,  "belt")
        self._update_live_label(self.comet_entry, self.comet_live_label, "comets")
        self._update_planetary_ring_label()

    def _update_live_label(self, entry_widget, label_widget, category):
        raw   = entry_widget.get().strip()
        total = self.total_rings if category == "belt" else self.total_comets
        count = self._compute_live_count(raw, total)
        label_widget.config(text=f"({count} objects)")

    def _update_planetary_ring_label(self):
        raw   = self.planetary_ring_entry.get().strip()
        count = self._compute_live_count(raw, 2000)
        self.planetary_ring_live_label.config(text=f"(up to {count}/2000)")

    @staticmethod
    def _compute_live_count(raw_input, total):
        raw = (raw_input or "").strip()
        if raw in ("", "0") or total == 0: return 0
        if "%" in raw:
            try:   pct = float(raw.replace("%", "").strip())
            except (ValueError, TypeError): return 0
            return 0 if pct <= 0 else max(1, int(total * (pct / 100.0)))
        try:   return max(0, min(int(raw), total))
        except (ValueError, TypeError): return 0

    # ── Validation ─────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_input(raw: str) -> tuple:
        """Returns (is_valid, error_message)."""
        raw = (raw or "").strip()
        if raw == "": return True, ""
        if "%" in raw:
            try:
                v = float(raw.replace("%", "").strip())
                if v < 0 or v > 100: return False, "Percentage must be 0–100."
                return True, ""
            except ValueError:
                return False, "Invalid percentage."
        try:
            v = int(raw)
            if v < 0: return False, "Count must be ≥ 0."
            return True, ""
        except ValueError:
            return False, f"'{raw}' is not a valid percentage or count."

    # ── Conversion ─────────────────────────────────────────────────────────────

    def run_conversion(self):
        if not self.selected_path:
            messagebox.showerror("Error", "Please select a file or folder first.")
            return
        for field, label in [
            (self.belt_entry.get(),          "Asteroid Belt"),
            (self.planetary_ring_entry.get(), "Ring Particles"),
            (self.comet_entry.get(),          "Comets"),
        ]:
            ok, err = self._validate_input(field)
            if not ok:
                messagebox.showerror("Invalid Input", f"{label}: {err}")
                return
        _const.DEBUG_MODE = self.debug_var.get()
        if _const.DEBUG_MODE:
            set_log_callback(self.log_message)
        else:
            set_log_callback(None)
        self._failure_details = []
        self.progress_bar.config(value=0)
        self.status_var.set("Starting conversion…")
        self.conversion_thread = threading.Thread(
            target=self._conversion_worker, daemon=True)
        self.conversion_thread.start()

    def _conversion_worker(self):
        import traceback as tb
        try:
            _const.CONVERSION_LOG = []
            files_to_convert = []
            if self.is_batch.get():
                if not os.path.isdir(self.selected_path):
                    self._set_status("Error: selected path is not a folder.", 0); return
                files_to_convert = [
                    os.path.join(self.selected_path, f)
                    for f in sorted(os.listdir(self.selected_path))
                    if f.lower().endswith(".sc")
                ]
            else:
                if not os.path.isfile(self.selected_path):
                    self._set_status("Error: selected path is not a file.", 0); return
                files_to_convert = [self.selected_path]

            n          = len(files_to_convert)
            successful = 0
            failed     = 0
            failures   = []
            reports    = []

            for idx, sc_file in enumerate(files_to_convert):
                fname = os.path.basename(sc_file)
                base = os.path.splitext(fname)[0]
                safe = re.sub(r'[\\/*?:"<>|]', "", base).strip() or "SE_Import"
                out_ubox = os.path.join(self.out_dir_var.get(), safe + ".ubox")
                file_log_start = len(_const.CONVERSION_LOG)
                base_progress = int(idx / max(n, 1) * 100)
                self._set_status(f"[{idx+1}/{n}] Loading {fname}…", base_progress)

                try:
                    self._set_status(f"[{idx+1}/{n}] Parsing {fname}…", base_progress + 1)
                    se_data = parse_se_file(sc_file)
                    if not se_data:
                        failures.append((fname, "file", "Parsing", "No objects found"))
                        failed += 1; continue

                    def _status(msg):
                        self._set_status(f"[{idx+1}/{n}] {fname} — {msg}…",
                                         base_progress + 2)

                    import globals_compat as _gc
                    _gc.INHERIT_MOON_AXIAL_TILT      = self.inherit_moon_tilt_var.get()
                    _gc.INHERIT_STAR_AXIAL_TILT      = self.inherit_star_tilt_var.get()
                    _gc.ALIGN_ORBITS_TO_STAR_EQUATOR = self.align_orbits_to_star_var.get()
                    _gc_surface_map = {
                        "Off": (False, "none", False, False),
                        "Preview only / safe": (True, "preview_only", False, False),
                        "Liquid / Water Mask Only": (
                            True, "liquid_mask_only", True, False
                        ),
                        "Full US-like surface": (
                            True, "full_us_like", True, False
                        ),
                        "Active legacy / dangerous": (
                            True, "active_legacy", True, True
                        ),
                    }
                    _surface_settings = _gc_surface_map.get(
                        self.surface_mode_var.get(),
                        (True, "full_us_like", True, False),
                    )
                    if not self.surface_var.get():
                        _surface_settings = (False, "none", False, False)
                    (
                        _gc.GENERATE_SURFACE_DATA,
                        _gc.SURFACE_DATA_MODE,
                        _gc.ATTACH_SURFACE_GRID_COMPONENT,
                        _gc.ACTIVE_SURFACE_PHYSICS,
                    ) = _surface_settings
                    _gc.NORMALIZE_SE_ATMOSPHERE      = self.normalize_atmosphere_var.get()
                    _gc.NORMALIZE_SE_ATMOSPHERE_MODE = (
                        self.atmosphere_mode_var.get().strip().lower()
                        if self.normalize_atmosphere_var.get() else "off"
                    )
                    _gc.START_PAUSED = self.start_paused_var.get()
                    _gc.START_SIMULATION_SPEED_REALTIME = self.start_realtime_var.get()
                    speed_multipliers = {
                        "seconds per real second": 1.0,
                        "minutes per real second": 60.0,
                        "hours per real second": 3600.0,
                        "days per real second": 86400.0,
                        "years per real second": 31557600.0,
                    }
                    startup_speed = float(self.start_speed_value_var.get().strip())
                    if startup_speed <= 0.0:
                        raise ValueError("Startup speed must be greater than zero.")
                    _gc.DEFAULT_TIME_STEP_PER_REAL_SEC = (
                        startup_speed * speed_multipliers[self.start_speed_unit_var.get()]
                    )
                    _gc.DISABLE_AUTOSPEED_ON_EXPORT = self.disable_autospeed_var.get()
                    _gc.STATIC_IMPORTED_ATMOSPHERES = self.static_atmospheres_var.get()
                    _gc.PRESERVE_SE_ATMOSPHERE_PRESSURE = self.static_atmospheres_var.get()
                    _gc.LOCK_IMPORTED_ATMOSPHERIC_DEPOTS = self.lock_atm_depots_var.get()
                    _gc.LOCK_IMPORTED_LIQUID_DEPOTS = True
                    _gc.STRICT_ATMOSPHERE_MASS_CONSISTENCY = self.strict_atm_mass_var.get()
                    _gc.STRICT_CLOUD_COVERAGE_VALIDATION = self.strict_cloud_var.get()
                    _gc_gas_map = {
                        "Off": "off",
                        "Pressure-derived": "pressure",
                        "Raw legacy": "raw",
                    }
                    _gc.SURFACE_GAS_PRESSURE_MODE = _gc_gas_map.get(
                        self.surface_gas_channel_var.get(), "off")
                    _gc_ocean_map = {
                        "Visual only": "visual_only",
                        "Capped": "capped",
                        "Legacy Oceans, Capped Lakes": "legacy",
                        "Legacy full mass": "legacy",  # back-compat for any saved preference
                    }
                    _gc.OCEAN_DEPOT_EXPORT_MODE = _gc_ocean_map.get(
                        self.ocean_depot_mode_var.get(), "legacy")
                    _gc.EXPORT_FULL_OCEAN_MASS_AS_DEPOT = (
                        _gc.OCEAN_DEPOT_EXPORT_MODE == "legacy"
                    )
                    _gc.REMOVE_OCEAN_WATER_FROM_VOLATILE_DEPOTS_FOR_STATIC_ATMOSPHERES = (
                        _gc.OCEAN_DEPOT_EXPORT_MODE == "visual_only"
                    )
                    _gc.CAP_SURFACE_WATER_DEPOT_FOR_STATIC_ATMOSPHERES = (
                        _gc.OCEAN_DEPOT_EXPORT_MODE == "capped"
                    )
                    _gc_atm_depot_map = {
                        "None / safest":           "none",
                        "Carrier unlocked":        "carrier_unlocked",
                        "Carrier locked":          "carrier_locked",
                        "Chemical unlocked / test": "chemical_unlocked",
                        "Chemical locked / test":   "chemical_locked",
                    }
                    _gc.STATIC_ATMOSPHERE_DEPOT_MODE = _gc_atm_depot_map.get(
                        self.atm_depot_mode_var.get(), "none")
                    _gc_policy_map = {
                        "No grid / safest":          "no_grid",
                        "Passive grid / experimental": "passive_grid",
                        "Active legacy / dangerous":  "active_legacy",
                    }
                    _gc.STATIC_IMPORTED_ATMOSPHERE_SURFACE_POLICY = _gc_policy_map.get(
                        self.surface_policy_var.get(), "no_grid")

                    report = convert_to_ubox(
                        se_data, out_ubox,
                        belt_asteroid_input  = self.belt_entry.get().strip(),
                        planetary_ring_input = self.planetary_ring_entry.get().strip(),
                        comet_input          = self.comet_entry.get().strip(),
                        export_comets        = self.export_comets_var.get(),
                        export_moons         = self.moons_var.get(),
                        export_dwarf_moons   = self.dwarf_moons_var.get(),
                        export_dwarf_planets = self.dwarfs_var.get(),
                        export_rings         = self.rings_var.get(),
                        status_callback      = _status,
                        source_name          = fname,
                    )
                    reports.append(report)

                    if self.auto_export_var.get():
                        auto_path = os.path.expanduser(
                            "~/Documents/Universe Sandbox 2/Simulations")
                        if os.path.exists(auto_path) and os.path.exists(out_ubox):
                            shutil.copy2(out_ubox,
                                         os.path.join(auto_path, safe + ".ubox"))

                    successful += 1
                    self._set_status(f"[{idx+1}/{n}] {fname} — done.",
                                     int((idx + 1) / max(n, 1) * 100))

                except Exception as e:
                    exc_text = tb.format_exc()
                    stage = "Validation" if "validation failed" in str(e).lower() else "Conversion"
                    debug_log_path = None
                    log_debug(f"{fname}: {exc_text}", "ERROR")
                    if _const.DEBUG_MODE:
                        debug_log_path = os.path.splitext(out_ubox)[0] + "-conversion.log"
                        try:
                            write_conversion_log(debug_log_path, file_log_start)
                        except OSError as log_error:
                            log_debug(f"Could not save debug log: {log_error}", "ERROR")
                            debug_log_path = None
                        self.root.after(0, lambda m=exc_text: self.log_message(m))
                    reason = str(e)[:240]
                    if debug_log_path:
                        reason += f"\nDebug log: {os.path.abspath(debug_log_path)}"
                    failures.append((fname, "conversion", stage, reason))
                    failed += 1

            # ── Summary ────────────────────────────────────────────────────────
            self.progress_bar.config(value=100)
            if not failures:
                warning_count = sum(report.get("warning_count", 0) for report in reports)
                surface_slots = sum(report.get("surface_slots", 0) for report in reports)
                surface_bodies = sum(
                    report.get("surface_bodies_generated", 0) for report in reports
                )
                surface_enabled = any(report.get("surface_enabled") for report in reports)
                lines = [f"Done. {successful}/{n} converted successfully."]
                for report in reports:
                    lines.append(f"Output: {report['output_path']}")
                lines.append(f"Warnings: {warning_count}")
                lines.append(
                    f"Surface data: {'enabled' if surface_enabled else 'disabled'}, "
                    f"{surface_bodies} body map(s) generated, "
                    f"{surface_slots} active atlas slot(s) attached"
                )
                debug_paths = [report.get("debug_log_path") for report in reports
                               if report.get("debug_log_path")]
                if debug_paths:
                    lines.append("Debug log: " + debug_paths[0])
                summary = "\n".join(lines)
            else:
                lines = [f"Done. {successful} succeeded, {failed} failed.\n"]
                for fname, obj, stage, reason in failures:
                    lines.append(f"  Failed: {fname}")
                    lines.append(f"      Stage  : {stage}")
                    lines.append(f"      Reason : {reason}")
                warning_count = sum(report.get("warning_count", 0) for report in reports)
                if reports:
                    lines.append(f"\nWarnings in successful outputs: {warning_count}")
                summary = "\n".join(lines)

            self.root.after(0, lambda s=summary: self.status_var.set(s))
            if failures:
                self.root.after(0, lambda s=summary: messagebox.showwarning(
                    "Conversion Complete — Some Failures", s))

            _ding()

        except Exception as e:
            self.root.after(0, lambda m=str(e): self.status_var.set(f"Fatal: {m}"))
            self.root.after(0, lambda: self.progress_bar.config(value=0))
            if _const.DEBUG_MODE:
                self.root.after(0, lambda m=tb.format_exc(): self.log_message(m))


def launch_gui():
    root = tk.Tk()
    ConversionGUI(root)
    root.mainloop()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--validate", "--validate-output"):
        if len(argv) != 2:
            print("Usage: python main.py --validate path/to/file.ubox")
            return 2
        try:
            result = validate_ubox_file(argv[1])
        except Exception as exc:
            print(f"Validation failed: {exc}")
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if argv and argv[0] in ("-h", "--help"):
        print("Usage:\n  python main.py\n  python main.py --validate path/to/file.ubox")
        return 0

    sc_files   = [f for f in os.listdir(".") if f.endswith(".sc")]
    ubox_files = [f for f in os.listdir(".")
                  if f.endswith(".ubox") or
                     (f.startswith("simulation") and f.endswith(".json")
                      and "info" not in f and "ui" not in f)]
    if ubox_files:
        for f in ubox_files:
            (convert_ubox_zip_to_se if f.endswith(".ubox") else convert_ubox_json_to_se)(f)
    if sc_files:
        data = []
        for f in sc_files:
            data.extend(parse_se_file(f))
        if data:
            base = re.sub(r'[\\/*?:"<>|]', "",
                          os.path.splitext(sc_files[0])[0]).strip() or "SE_Import"
            convert_to_ubox(data, base + ".ubox")
        else:
            print("Error  No data parsed")
    if not sc_files and not ubox_files:
        print("Error  No .sc or .ubox files found")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main())
    launch_gui()