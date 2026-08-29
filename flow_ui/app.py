"""Pulse flow feedback control UI: MP5Y-25 PV + NI 9264 AO.

Run:
  python -m flow_ui.app
  or
  python flow_ui/app.py
  or
  python pump.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

# Allow `python flow_ui/app.py` without installing as a package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow_ui.control import LowPassFilter, PIController
from flow_ui.daq_service import ChannelConfig, DaqError, DaqService
from flow_ui.flow_math import DEFAULT_PULSE_ML
from flow_ui.level_control import SensorType, ValveRole, decide_valve
from flow_ui.mp5y_service import Mp5yConfig, Mp5yError, Mp5yService


# Professional instrumentation palette: white, navy, teal, and amber.
COLORS = {
    "bg": "#F4F7FA",
    "panel": "#FFFFFF",
    "panel_alt": "#F7F9FC",
    "border": "#DCE3EA",
    "title": "#17324D",
    "text": "#334E68",
    "muted": "#829AB1",
    "accent": "#1F7A8C",
    "accent_deep": "#176270",
    "accent_soft": "#D9EEF1",
    "value": "#0B7285",
    "ok": "#2E9D74",
    "warn": "#E69F3A",
    "bad": "#D64545",
    "graph_bg": "#FFFFFF",
    "grid": "#E7EDF3",
    "pv": "#0B8A9A",
    "sv": "#3E6FB0",
    "ao": "#2E9D74",
    "hz": "#D28A24",
}


class TrendGraph(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        lines: list[tuple[str, str]],
        low: float = 0.0,
        high: float = 5.0,
        unit: str = "",
    ) -> None:
        super().__init__(parent, bg=COLORS["graph_bg"], highlightthickness=0, height=260)
        self.plot_title = title
        self.lines = lines
        self.series = [deque(maxlen=160) for _ in lines]
        self.low = low
        self.high = high
        self.unit = unit
        self.configure(highlightbackground=COLORS["border"], highlightthickness=1)
        self.bind("<Configure>", lambda _e: self.draw())

    def add(self, *values: float, redraw: bool = True) -> None:
        for series, value in zip(self.series, values):
            series.append(float(value))
        if redraw:
            self.draw()

    def clear(self) -> None:
        for series in self.series:
            series.clear()
        self.draw()

    def set_scale(self, low: float, high: float, unit: str = "") -> None:
        if high > low:
            self.low, self.high = low, high
            if unit:
                self.unit = unit

    def draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 80)
        height = max(self.winfo_height(), 48)
        compact = width < 260 or height < 100
        left = 28 if compact else 42
        top = 18 if compact else 28
        right = width - 8
        bottom = height - (10 if compact else 18)
        title_font = ("Segoe UI", 9 if compact else 12, "bold")
        tick_font = ("Segoe UI", 7 if compact else 9)

        self.create_rectangle(1, 1, width - 2, height - 2, outline=COLORS["border"], width=1)
        self.create_text(
            left,
            10 if compact else 16,
            text=self.plot_title,
            fill=COLORS["title"],
            anchor="w",
            font=title_font,
        )

        for i in range(5):
            y = top + (bottom - top) * i / 4
            value = self.high - (self.high - self.low) * i / 4
            self.create_line(left, y, right, y, fill=COLORS["grid"])
            label = f"{value:.0f}" if abs(self.high - self.low) >= 20 else f"{value:.1f}"
            self.create_text(
                left - 4,
                y,
                text=label,
                fill=COLORS["muted"],
                anchor="e",
                font=tick_font,
            )

        self.create_line(left, top, left, bottom, fill=COLORS["border"])
        self.create_line(left, bottom, right, bottom, fill=COLORS["border"])
        if self.unit and not compact:
            self.create_text(
                left,
                bottom + 14,
                text=self.unit,
                fill=COLORS["muted"],
                anchor="w",
                font=tick_font,
            )

        for index, ((name, color), points) in enumerate(zip(self.lines, self.series)):
            lx = right - (70 if compact else 118)
            ly = (8 if compact else 12) + index * (14 if compact else 18)
            self.create_oval(lx, ly, lx + 8, ly + 8, fill=color, outline="")
            self.create_text(
                lx + 12,
                ly + 4,
                text=name,
                fill=COLORS["text"],
                anchor="w",
                font=tick_font,
            )
            if len(points) < 2:
                continue
            coords: list[float] = []
            span = self.high - self.low
            for point_index, value in enumerate(points):
                x = left + (right - left) * point_index / (len(points) - 1)
                fraction = (value - self.low) / span if span else 0.0
                y = bottom - max(0.0, min(1.0, fraction)) * (bottom - top)
                coords.extend((x, y))
            self.create_line(*coords, fill=color, width=2, smooth=True)


class FlowControlApp(tk.Tk):
    POLL_MS = 200  # main control cycle 0.2 s
    GRAPH_MS = 500  # full graph redraw interval
    REDISCOVER_MS = 5000  # NI device rediscovery
    TC_POLL_S = 5.0  # thermocouple read interval

    @staticmethod
    def _bundle_dir() -> Path:
        # PyInstaller one-file extracts assets under sys._MEIPASS.
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS) / "flow_ui"
        return Path(__file__).resolve().parent

    @classmethod
    def _runtime_dir(cls) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def __init__(self) -> None:
        super().__init__()
        self.title("Pulse Flow · Feedback Control")
        self.SETTINGS_PATH = self._runtime_dir() / "settings.json"
        self.ICON_DIR = self._bundle_dir() / "assets"
        self.ICON_ICO = self.ICON_DIR / "app_icon.ico"
        self.ICON_PNG = self.ICON_DIR / "app_icon.png"
        screen_w = max(self.winfo_screenwidth(), 800)
        screen_h = max(self.winfo_screenheight(), 480)
        # 24″ desktop by default; set PULSEFLOW_TOUCH=1 only for legacy 7″ touch UI.
        self.touch_mode = os.environ.get("PULSEFLOW_TOUCH", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if self.touch_mode:
            width, height = min(screen_w, 1024), min(screen_h, 600)
            self.geometry(f"{width}x{height}+0+0")
            self.minsize(min(800, width), min(480, height))
        else:
            width = min(1600, int(screen_w * 0.92))
            height = min(920, int(screen_h * 0.88))
            self.geometry(f"{width}x{height}")
            self.minsize(1280, 720)
        self.configure(bg=COLORS["bg"])
        self._apply_window_icon()

        self.channels = ChannelConfig()
        self.mp5y_config = Mp5yConfig(port="COM3")
        self.mp5y2_config = Mp5yConfig(port="COM4")
        self.daq = DaqService(self.channels)
        self.mp5y = Mp5yService(self.mp5y_config)
        self.mp5y2 = Mp5yService(self.mp5y2_config)
        self.lpf = LowPassFilter()
        self.lpf2 = LowPassFilter()
        self.pi = PIController(0.0, 5.0)
        self.pi2 = PIController(0.0, 5.0)
        self.pulse_ml = tk.StringVar(value="0.46")
        self.decimal_places = tk.StringVar(value="auto")

        self.active_page = "feedback"
        self.feedback_running = False
        self.level_running = False
        self._level_was_available = False
        self._ao_popup: tk.Toplevel | None = None
        self._tc_popup: tk.Toplevel | None = None
        self._keypad_popup: tk.Toplevel | None = None
        self.valve_commands = [False, False, False]
        self.io_test_active = False
        self.igniter_on = False
        self.status_on = False
        self.current_ao = 0.0
        self.current_ao2 = 0.0
        self.current_ng_ao = 0.0
        self.current_spare_ao = 0.0
        self.spare_do4_on = False
        self.ng_ao_voltage = tk.StringVar(value="0.0")
        self.sv = tk.StringVar(value="0.0")
        self.sv2 = tk.StringVar(value="0.0")
        self.sv_input = tk.StringVar(value="0.0")
        self.filtered_flow = 0.0
        self.filtered_flow2 = 0.0
        self._last_loop_at: float | None = None
        self._last_graph_draw_at = 0.0
        # Wall-clock time when pump feedback control was started.
        self._feedback_started_wall: datetime | None = None
        self._feedback_timer_started_at: float | None = None
        self._feedback_elapsed_s = 0.0
        self.tc_running = False
        self.tc_names = [f"TC {index}" for index in range(10)]
        self.tc_name_vars: list[tk.StringVar] = []
        self.tc_value_labels: list[ttk.Label] = []
        self.valve_names = ["급수", "배수", "배수"]
        self.valve_name_vars = [
            tk.StringVar(value=name) for name in self.valve_names
        ]
        self.valve_do_labels: list[ttk.Label] = []
        self.valve_sensor_types = [SensorType.FOUR_POINT, SensorType.TWO_POINT, SensorType.TWO_POINT]
        self._last_tc_read_at = 0.0
        self._tc_worker_busy = False
        self._tc_pending_values: list[float] | None = None
        self._tc_pending_error: str | None = None
        self._tc_last_ui_values: list[str] = [""] * 10
        self._tc_last_status_text = ""

        self._build_style()
        self._build_layout()
        self.load_settings()
        self.show_page("feedback")
        self._link_ok = False
        self._mp5y_ok = False
        self._mp5y_port_present = False
        self._mp5y2_ok = False
        self._mp5y2_port_present = False
        self._status_base_color = COLORS["bad"]
        self._refresh_ng_ao_label()
        self.refresh_connection()
        self.after(400, self._blink_status_lamp)
        self.after(250, self._tick_process_clock)
        self.after(self.POLL_MS, self.update_loop)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_window_icon(self) -> None:
        """Use flow_ui/assets/app_icon.* for the window/taskbar icon."""
        try:
            if self.ICON_ICO.exists():
                # Windows title-bar / taskbar prefers .ico
                self.iconbitmap(default=str(self.ICON_ICO))
            if self.ICON_PNG.exists():
                self._icon_image = tk.PhotoImage(file=str(self.ICON_PNG))
                self.iconphoto(True, self._icon_image)
        except tk.TclError:
            # Missing/invalid icon should never block startup.
            pass

    # ------------------------------------------------------------------ UI
    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("App.TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure(
            "Title.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["title"],
            font=("Segoe UI", 16 if self.touch_mode else 18, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "Panel.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=("Segoe UI", 11),
        )
        style.configure(
            "PanelTitle.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["title"],
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "Hint.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Value.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["value"],
            font=("Segoe UI", 24, "bold"),
        )
        style.configure(
            "ValueSmall.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["accent_deep"],
            font=("Segoe UI", 13, "bold"),
        )
        style.configure(
            "ValueCompact.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["value"],
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "InjectionCaption.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["title"],
            font=("Segoe UI", 14, "bold"),
        )
        style.configure(
            "InjectionTime.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["value"],
            font=("Segoe UI", 28, "bold"),
        )
        style.configure(
            "FlameStatus.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "Compact.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(8, 5),
        )
        style.configure(
            "TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(10, 7),
            background=COLORS["accent_soft"],
            foreground=COLORS["title"],
            borderwidth=0,
        )
        style.map("TButton", background=[("active", "#BFDDE2")])
        style.configure("Nav.TButton", background="#E8EEF4")
        style.map("Nav.TButton", background=[("active", "#D6E2EC")])
        style.configure("NavActive.TButton", background=COLORS["accent"], foreground="#FFFFFF")
        style.map("NavActive.TButton", background=[("active", COLORS["accent_deep"])])
        style.configure("Start.TButton", background="#BFE4D5", foreground="#174B3A", padding=(10, 8))
        style.map("Start.TButton", background=[("active", "#9FD4C1")])
        style.configure("Stop.TButton", background="#F1D5D8", foreground="#722F37", padding=(10, 8))
        style.map("Stop.TButton", background=[("active", "#E7BDC2")])
        style.configure(
            "TEntry",
            fieldbackground=COLORS["panel_alt"],
            foreground=COLORS["title"],
            insertcolor=COLORS["title"],
            padding=5,
            borderwidth=1,
        )
        style.configure(
            "Highlight.TEntry",
            fieldbackground=COLORS["accent_soft"],
            foreground=COLORS["accent_deep"],
            insertcolor=COLORS["accent_deep"],
            font=("Segoe UI", 14, "bold"),
            padding=5,
            borderwidth=2,
        )
        style.map(
            "Highlight.TEntry",
            fieldbackground=[("focus", "#C7E7EC")],
            bordercolor=[("focus", COLORS["accent"])],
        )
        style.configure(
            "TouchTitle.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["title"],
            font=("Segoe UI", 17, "bold"),
        )
        style.configure(
            "TouchValue.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["value"],
            font=("Segoe UI", 32, "bold"),
        )
        style.configure(
            "TouchSV.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["sv"],
            font=("Segoe UI", 24, "bold"),
        )
        style.configure(
            "Touch.TButton",
            font=("Segoe UI", 12, "bold"),
            padding=(12, 11),
            background=COLORS["accent_soft"],
            foreground=COLORS["title"],
        )
        style.map("Touch.TButton", background=[("active", "#BFDDE2")])
        style.configure(
            "TouchNav.TButton",
            font=("Segoe UI", 11, "bold"),
            padding=(12, 10),
            background="#E8EEF4",
            foreground=COLORS["title"],
        )
        style.configure(
            "TouchNavActive.TButton",
            font=("Segoe UI", 11, "bold"),
            padding=(12, 10),
            background=COLORS["accent"],
            foreground="#FFFFFF",
        )
        style.map(
            "TouchNavActive.TButton",
            background=[("active", COLORS["accent_deep"])],
        )
        style.configure(
            "TouchStart.TButton",
            font=("Segoe UI", 13, "bold"),
            padding=(14, 13),
            background="#BFE4D5",
            foreground="#174B3A",
        )
        style.configure(
            "TouchStop.TButton",
            font=("Segoe UI", 13, "bold"),
            padding=(14, 13),
            background="#F1D5D8",
            foreground="#722F37",
        )
        style.configure(
            "Keypad.TButton",
            font=("Segoe UI", 18, "bold"),
            padding=(12, 10),
            background="#E8EEF4",
            foreground=COLORS["title"],
        )

    def _build_layout(self) -> None:
        header_padding = (8, 4, 8, 2) if self.touch_mode else (10, 6, 10, 4)
        header = ttk.Frame(self, style="App.TFrame", padding=header_padding)
        header.pack(fill="x")

        titles = ttk.Frame(header, style="App.TFrame")
        titles.pack(side="left")
        title_style = "TouchTitle.TLabel" if self.touch_mode else "Title.TLabel"
        title_text = "PROCESS CONTROL" if self.touch_mode else "PROCESS CONTROL DASHBOARD"
        ttk.Label(titles, text=title_text, style=title_style).pack(anchor="w")
        self.clock_label = ttk.Label(
            titles,
            text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"),
            style="Sub.TLabel",
        )
        self.clock_label.pack(anchor="w", pady=(1, 0))

        status = ttk.Frame(header, style="App.TFrame")
        status.pack(side="right")
        if not self.touch_mode:
            ttk.Button(
                status,
                text="TC MON",
                style="Nav.TButton",
                command=self.open_tc_popup,
            ).pack(side="right", padx=(0, 6))
            ttk.Button(
                status,
                text="I/O TEST",
                style="Nav.TButton",
                command=self.open_ao_popup,
            ).pack(side="right", padx=(0, 12))
        # Large blinking DAQ / MP5Y link lamp — kept bigger than the old 12px dot.
        self.status_canvas = tk.Canvas(
            status, width=34, height=34, bg=COLORS["bg"], highlightthickness=0
        )
        self.status_canvas.pack(side="right", padx=(10, 0))
        self.status_dot = self.status_canvas.create_oval(
            3, 3, 31, 31, fill=COLORS["bad"], outline="#9B2C2C", width=2
        )
        self.status_label = ttk.Label(status, text="통신 확인 중", style="Sub.TLabel")
        self.status_label.pack(side="right")

        self.nav_buttons: dict[str, ttk.Button] = {}

        content_padding = (6, 2, 6, 4) if self.touch_mode else (8, 2, 8, 4)
        self.content = ttk.Frame(self, style="App.TFrame", padding=content_padding)
        self.content.pack(fill="both", expand=True)
        self.pages = {
            "ao": self._create_ao_page(),
            "feedback": self._create_feedback_page(),
        }

    def panel(self, parent: tk.Misc) -> ttk.Frame:
        wrapper = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        frame = ttk.Frame(wrapper, style="Panel.TFrame", padding=8 if self.touch_mode else 10)
        frame.pack(fill="both", expand=True)
        frame._card = wrapper  # type: ignore[attr-defined]
        return frame

    def place_panel(self, panel: ttk.Frame, **grid_kwargs) -> None:
        panel._card.grid(**grid_kwargs)  # type: ignore[attr-defined]

    def field(self, parent: tk.Misc, row: int, label: str, default: str, hint: str = "") -> tk.StringVar:
        value = tk.StringVar(value=default)
        row_pad = 1 if not self.touch_mode else 2
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(
            row=row, column=0, sticky="w", pady=row_pad
        )
        entry = ttk.Entry(parent, textvariable=value, width=10)
        entry.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=row_pad)
        if self.touch_mode:
            keypad_ranges = {
                "펄스정수 (ml/P)": (0.001, 999.0, 3),
                "SV 목표 유량 (cc/min)": (0.0, 9999.0, 1),
                "P Gain (V·min/cc)": (0.0, 100.0, 3),
                "I Gain (V·min/cc·s)": (0.0, 100.0, 3),
                "LPF 차단주파수 (Hz)": (0.0, 100.0, 2),
                "출력 전압 (V)": (0.0, 5.0, 3),
            }
            if label in keypad_ranges:
                minimum, maximum, decimals = keypad_ranges[label]
                entry.bind(
                    "<Button-1>",
                    lambda _event, var=value, title=label, low=minimum, high=maximum, digits=decimals: (
                        self.open_numeric_keypad(var, title, low, high, digits),
                        "break",
                    )[1],
                )
        if hint:
            ttk.Label(parent, text=hint, style="Hint.TLabel").grid(
                row=row + 1, column=0, columnspan=2, sticky="w", pady=(0, 2)
            )
        return value

    def _create_ao_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        controls = self.panel(page)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 14))
        ttk.Label(controls, text="AO 수동 출력", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )
        self.ao_voltage = self.field(controls, 1, "출력 전압 (V)", "0.0", "범위 0.0 ~ 5.0 V")
        ttk.Button(controls, text="전압 출력", style="Start.TButton", command=self.output_ao).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0)
        )
        ttk.Button(controls, text="0 V (정지)", style="Stop.TButton", command=self.zero_ao).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        self.ao_value = ttk.Label(controls, text="현재 출력: 0.000 V", style="Value.TLabel")
        self.ao_value.grid(row=5, column=0, columnspan=2, sticky="w", pady=(22, 0))
        ttk.Label(
            controls,
            text="9264 ao0 (REF.W) → 물펌프/인버터\n9264 ao1 (NG PUMP) → NG 펌프\nAO GND는 각 장치 COM과 공통",
            style="Hint.TLabel",
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.ao_graph = TrendGraph(page, "AO 출력 추이", [("AO", COLORS["ao"])], 0, 5, "V")
        self.ao_graph.grid(row=0, column=1, sticky="nsew")
        return page

    def _create_feedback_page(self) -> ttk.Frame:
        if self.touch_mode:
            return self._create_touch_feedback_page()
        return self._create_desktop_feedback_page()

    def _create_touch_feedback_page(self) -> ttk.Frame:
        """Build the 1024×600 operator interface for a 7-inch touchscreen."""
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        nav = ttk.Frame(page, style="App.TFrame")
        nav.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        nav_items = (
            ("pump", "펌프"),
            ("level", "레벨 / 밸브"),
            ("flame", "점화"),
        )
        self.touch_nav_buttons: dict[str, ttk.Button] = {}
        for column, (name, text) in enumerate(nav_items):
            nav.columnconfigure(column, weight=1)
            button = ttk.Button(
                nav,
                text=text,
                style="TouchNav.TButton",
                command=lambda section=name: self._show_touch_section(section),
            )
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))
            self.touch_nav_buttons[name] = button

        actions = (
            ("I/O TEST", self.open_ao_popup),
            ("TC MON", self.open_tc_popup),
            ("설정", self.open_settings),
        )
        for offset, (text, command) in enumerate(actions, start=3):
            nav.columnconfigure(offset, weight=1)
            ttk.Button(
                nav, text=text, style="TouchNav.TButton", command=command
            ).grid(row=0, column=offset, sticky="ew", padx=(4, 0))

        body = ttk.Frame(page, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.touch_sections = {
            "pump": self._create_touch_pump_section(body),
            "level": self._create_touch_level_section(body),
            "flame": self._create_touch_flame_section(body),
        }
        for section in self.touch_sections.values():
            section.grid(row=0, column=0, sticky="nsew")
        self._show_touch_section("pump")
        return page

    def _create_touch_pump_section(self, parent: tk.Misc) -> ttk.Frame:
        section = ttk.Frame(parent, style="App.TFrame")
        section.columnconfigure(0, weight=4)
        section.columnconfigure(1, weight=3)
        section.columnconfigure(2, weight=3)
        section.rowconfigure(0, weight=1)

        current = self.panel(section)
        self.place_panel(current, row=0, column=0, sticky="nsew", padx=(0, 5))
        ttk.Label(current, text="현재 유량", style="PanelTitle.TLabel").pack(anchor="w")
        self.focus_pv = ttk.Label(current, text="PV  0.0", style="TouchValue.TLabel")
        self.focus_pv.pack(anchor="w", pady=(3, 0))
        self.pv_value = self.focus_pv
        self.focus_sv = ttk.Label(current, text="SV  0.0", style="TouchSV.TLabel")
        self.focus_sv.pack(anchor="w")
        self.focus_err = ttk.Label(current, text="오차: 0.0 cc/min", style="ValueSmall.TLabel")
        self.focus_err.pack(anchor="w", pady=(2, 0))
        self.fb_hz = ttk.Label(current, text="MP5Y: 대기", style="Hint.TLabel")
        self.fb_hz.pack(anchor="w", pady=(2, 0))
        self.output_value = ttk.Label(current, text="AO0: 0.000 V", style="ValueSmall.TLabel")
        self.output_value.pack(anchor="w", pady=(2, 0))
        self.focus_pv2 = ttk.Label(current, text="PV  0.0", style="TouchValue.TLabel")
        self.focus_pv2.pack(anchor="w", pady=(6, 0))
        self.focus_sv2 = ttk.Label(current, text="SV  0.0", style="TouchSV.TLabel")
        self.focus_sv2.pack(anchor="w")
        self.focus_err2 = ttk.Label(current, text="오차: 0.0 cc/min", style="ValueSmall.TLabel")
        self.focus_err2.pack(anchor="w", pady=(2, 0))
        self.fb_hz2 = ttk.Label(current, text="MP5Y: —", style="Hint.TLabel")
        self.fb_hz2.pack(anchor="w", pady=(2, 0))
        self.output_value2 = ttk.Label(
            current, text="AO2: 0.000 V", style="ValueSmall.TLabel"
        )
        self.output_value2.pack(anchor="w", pady=(2, 3))
        self.control_start_label = ttk.Label(
            current, text="제어 시작  --:--:--", style="ValueSmall.TLabel"
        )
        self.control_start_label.pack(anchor="w", pady=(0, 3))
        injection_row = ttk.Frame(current, style="Panel.TFrame")
        injection_row.pack(fill="x", pady=(0, 3))
        self.injection_time_label = ttk.Label(
            injection_row, text="물 주입시간  00:00:00", style="ValueSmall.TLabel"
        )
        self.injection_time_label.pack(side="left")
        ttk.Button(
            injection_row,
            text="초기화",
            style="Touch.TButton",
            command=self.reset_injection_time,
        ).pack(side="right")

        self.feedback_button = ttk.Button(
            current,
            text="제어 시작",
            style="TouchStart.TButton",
            command=self.toggle_feedback,
        )
        self.feedback_button.pack(fill="x", pady=(3, 4))

        self.feedback_graph = TrendGraph(
            current,
            "PV / SV",
            [("PV", COLORS["pv"]), ("SV", COLORS["sv"])],
            0,
            200,
            "cc/min",
        )
        self.feedback_graph.configure(height=105)
        self.feedback_graph.pack(fill="both", expand=True, pady=(2, 5))
        self.feedback_ao_graph = TrendGraph(
            current, "REF.W", [("REF.W", COLORS["ao"])], 0, 5, "V"
        )

        target = self.panel(section)
        self.place_panel(target, row=0, column=1, sticky="nsew", padx=(0, 5))
        ttk.Label(target, text="펌프1 SV (cc/min)", style="PanelTitle.TLabel").pack(anchor="w")
        self.touch_sv_display = ttk.Button(
            target,
            textvariable=self.sv,
            style="Touch.TButton",
            command=lambda: self.open_numeric_keypad(
                self.sv, "펌프1 목표 유량 SV (cc/min)", 0.0, 9999.0, 1
            ),
        )
        self.touch_sv_display.pack(fill="x", pady=(6, 5))
        ttk.Label(target, text="cc/min · 숫자를 누르면 키패드", style="Hint.TLabel").pack(
            anchor="w", pady=(0, 6)
        )

        adjust = ttk.Frame(target, style="Panel.TFrame")
        adjust.pack(fill="x")
        for index, (text, delta) in enumerate((("-10", -10), ("-1", -1), ("+1", 1), ("+10", 10))):
            row, column = divmod(index, 2)
            adjust.columnconfigure(column, weight=1)
            ttk.Button(
                adjust,
                text=text,
                style="Touch.TButton",
                command=lambda amount=delta: self._adjust_sv(amount),
            ).grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 3, 0),
                pady=(0 if row == 0 else 3, 0),
            )

        ttk.Separator(target, orient="horizontal").pack(fill="x", pady=(10, 8))
        ttk.Label(target, text="펌프2 SV (cc/min)", style="PanelTitle.TLabel").pack(anchor="w")
        self.touch_sv2_display = ttk.Button(
            target,
            textvariable=self.sv2,
            style="Touch.TButton",
            command=lambda: self.open_numeric_keypad(
                self.sv2, "펌프2 목표 유량 SV (cc/min)", 0.0, 9999.0, 1
            ),
        )
        self.touch_sv2_display.pack(fill="x", pady=(6, 5))

        adjust2 = ttk.Frame(target, style="Panel.TFrame")
        adjust2.pack(fill="x")
        for index, (text, delta) in enumerate((("-10", -10), ("-1", -1), ("+1", 1), ("+10", 10))):
            row, column = divmod(index, 2)
            adjust2.columnconfigure(column, weight=1)
            ttk.Button(
                adjust2,
                text=text,
                style="Touch.TButton",
                command=lambda amount=delta: self._adjust_sv2(amount),
            ).grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 3, 0),
                pady=(0 if row == 0 else 3, 0),
            )

        ttk.Label(target, text="빠른 설정", style="Panel.TLabel").pack(anchor="w", pady=(10, 4))
        presets = ttk.Frame(target, style="Panel.TFrame")
        presets.pack(fill="x")
        for index, value in enumerate((50, 100, 120, 150)):
            row, column = divmod(index, 2)
            presets.columnconfigure(column, weight=1)
            ttk.Button(
                presets,
                text=f"{value}",
                style="Touch.TButton",
                command=lambda preset=value: self._set_sv(preset),
            ).grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 4, 0),
                pady=(0 if row == 0 else 4, 0),
            )

        ttk.Button(
            target,
            text="전체 숫자 키패드",
            style="Touch.TButton",
            command=lambda: self.open_numeric_keypad(
                self.sv, "목표 유량 SV (cc/min)", 0.0, 9999.0, 1
            ),
        ).pack(fill="x", pady=(9, 0))

        ttk.Separator(target, orient="horizontal").pack(fill="x", pady=(12, 8))
        ttk.Label(target, text="NG PUMP (AO1)", style="PanelTitle.TLabel").pack(anchor="w")
        self.ng_ao_value = ttk.Label(
            target, text="NG PUMP: 0.000 V", style="ValueSmall.TLabel"
        )
        self.ng_ao_value.pack(anchor="w", pady=(4, 4))
        self.touch_ng_ao_display = ttk.Button(
            target,
            textvariable=self.ng_ao_voltage,
            style="Touch.TButton",
            command=lambda: self.open_numeric_keypad(
                self.ng_ao_voltage, "NG PUMP (AO1) 출력 (V)", 0.0, 5.0, 3
            ),
        )
        self.touch_ng_ao_display.pack(fill="x", pady=(0, 4))
        ttk.Label(target, text="0.000 ~ 5.000 V", style="Hint.TLabel").pack(anchor="w")
        ng_buttons = ttk.Frame(target, style="Panel.TFrame")
        ng_buttons.pack(fill="x", pady=(8, 0))
        ng_buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(
            ng_buttons,
            text="출력",
            style="TouchStart.TButton",
            command=self.apply_ng_ao,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(
            ng_buttons,
            text="0 V",
            style="TouchStop.TButton",
            command=self.zero_ng_ao,
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        tuning = self.panel(section)
        self.place_panel(tuning, row=0, column=2, sticky="nsew")
        ttk.Label(tuning, text="제어 설정", style="PanelTitle.TLabel").pack(anchor="w")
        self.fb_pulse_ml = self._touch_numeric_field(
            tuning, "펄스정수 (ml/P)", "0.46", 0.001, 999.0, 3
        )
        self.p_gain = self._touch_numeric_field(
            tuning, "P Gain", "0.020", 0.0, 100.0, 3
        )
        self.i_gain = self._touch_numeric_field(
            tuning, "I Gain", "0.005", 0.0, 100.0, 3
        )
        self.lpf_cutoff = self._touch_numeric_field(
            tuning, "LPF (Hz) · 0=OFF", "0.8", 0.0, 100.0, 2
        )
        ttk.Button(
            tuning, text="설정 저장", style="Touch.TButton", command=self.save_settings
        ).pack(fill="x", pady=(7, 0))
        self.sv.trace_add("write", lambda *_args: self._refresh_touch_sv())
        self.sv2.trace_add("write", lambda *_args: self._refresh_touch_sv())
        self._refresh_touch_sv()
        return section

    def _create_touch_level_section(self, parent: tk.Misc) -> ttk.Frame:
        section = ttk.Frame(parent, style="App.TFrame")
        section.columnconfigure((0, 1, 2), weight=1)
        section.rowconfigure(1, weight=1)

        heading = self.panel(section)
        self.place_panel(heading, row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(heading, text="레벨 / 밸브 자동 제어", style="PanelTitle.TLabel").pack(
            side="left"
        )
        self.level_master_status = ttk.Label(
            heading, text="대기 · 밸브 닫힘", style="ValueSmall.TLabel"
        )
        self.level_master_status.pack(side="right")

        self.level_high_labels = []
        self.level_low_labels = []
        self.valve_status_labels = []
        self.level_logic_labels = []
        self.valve_do_labels = []
        rules = (
            "4접점 · L열림 / H닫힘 (H+L 동시ON=고수위)",
            "LOW → 닫힘  /  HIGH → 열림",
            "LOW → 닫힘  /  HIGH → 열림",
        )
        for index, rule in enumerate(rules):
            card = self.panel(section)
            self.place_panel(
                card,
                row=1,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 5, 0),
            )
            head = ttk.Frame(card, style="Panel.TFrame")
            head.pack(fill="x")
            do_label = ttk.Label(
                head, text=f"DO{index}", style="PanelTitle.TLabel"
            )
            do_label.pack(side="left")
            self.valve_do_labels.append(do_label)
            name_entry = ttk.Entry(
                card, textvariable=self.valve_name_vars[index], width=12
            )
            name_entry.pack(fill="x", pady=(6, 0))
            name_entry.bind(
                "<Return>",
                lambda _event, i=index: self._commit_valve_name(i),
            )
            ttk.Label(card, text=rule, style="Hint.TLabel").pack(
                anchor="w", pady=(2, 14)
            )
            high = ttk.Label(card, text="●  HIGH   OFF", style="ValueSmall.TLabel")
            high.pack(anchor="w", pady=5)
            low = ttk.Label(card, text="●  LOW    OFF", style="ValueSmall.TLabel")
            low.pack(anchor="w", pady=5)
            ttk.Separator(card, orient="horizontal").pack(fill="x", pady=12)
            valve = ttk.Label(card, text="닫힘", style="TouchSV.TLabel")
            valve.pack(anchor="w")
            logic = ttk.Label(card, text="대기", style="Panel.TLabel")
            logic.pack(anchor="w", pady=(8, 0))
            self.level_high_labels.append(high)
            self.level_low_labels.append(low)
            self.valve_status_labels.append(valve)
            self.level_logic_labels.append(logic)
        ttk.Button(
            section,
            text="밸브 이름 저장",
            style="Touch.TButton",
            command=self.save_valve_names,
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        return section

    def _create_touch_flame_section(self, parent: tk.Misc) -> ttk.Frame:
        section = ttk.Frame(parent, style="App.TFrame")
        section.columnconfigure(0, weight=1)
        section.rowconfigure(0, weight=1)
        card = self.panel(section)
        self.place_panel(card, row=0, column=0, sticky="nsew")

        ttk.Label(card, text="점화 / Coolint P", style="PanelTitle.TLabel").pack(anchor="w")
        center = ttk.Frame(card, style="Panel.TFrame")
        center.pack(expand=True)
        controls = ttk.Frame(center, style="Panel.TFrame")
        controls.pack()
        self.igniter_button = ttk.Button(
            controls,
            text="IGN OFF",
            style="TouchStop.TButton",
            command=self.toggle_igniter,
            width=10,
        )
        self.igniter_button.pack(anchor="w")
        self.igniter_caption = self.igniter_button
        self.coolint_p_button = ttk.Button(
            controls,
            text="Coolint P OFF",
            style="TouchStop.TButton",
            command=self.toggle_coolint_p,
            width=12,
        )
        self.coolint_p_button.pack(anchor="w", pady=(14, 0))
        return section

    def _create_desktop_feedback_page(self) -> ttk.Frame:
        """Desktop dashboard: settings | pump1 | pump2 | trends/status, valves below."""
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(0, weight=18, uniform="dash")
        page.columnconfigure(1, weight=27, uniform="dash")
        page.columnconfigure(2, weight=27, uniform="dash")
        page.columnconfigure(3, weight=28, uniform="dash")
        page.rowconfigure(0, weight=3)
        page.rowconfigure(1, weight=2)

        # --- Col 0: PI tuning + run control + NG pump ---
        controls = self.panel(page)
        controls.configure(padding=8)
        self.place_panel(controls, row=0, column=0, sticky="nsew", padx=(0, 4))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="펌프 설정", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.fb_pulse_ml = self.field(controls, 1, "펄스정수", "0.46")
        self.p_gain = self.field(controls, 3, "P Gain", "0.020")
        self.i_gain = self.field(controls, 5, "I Gain", "0.005")
        self.lpf_cutoff = self.field(controls, 7, "LPF (Hz)", "0.8", "0=OFF")
        ttk.Button(controls, text="저장", command=self.save_settings).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )

        ttk.Separator(controls, orient="horizontal").grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=(10, 8)
        )
        self.feedback_button = ttk.Button(
            controls, text="제어 시작", style="Start.TButton", command=self.toggle_feedback
        )
        self.feedback_button.grid(row=11, column=0, columnspan=2, sticky="ew")

        ttk.Separator(controls, orient="horizontal").grid(
            row=12, column=0, columnspan=2, sticky="ew", pady=(10, 6)
        )
        ttk.Label(controls, text="NG PUMP (AO1)", style="PanelTitle.TLabel").grid(
            row=13, column=0, columnspan=2, sticky="w"
        )
        ng_row = ttk.Frame(controls, style="Panel.TFrame")
        ng_row.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ng_row.columnconfigure(0, weight=1)
        ttk.Entry(
            ng_row,
            textvariable=self.ng_ao_voltage,
            style="Highlight.TEntry",
            justify="center",
            width=8,
        ).grid(row=0, column=0, sticky="ew")
        ng_btns = ttk.Frame(ng_row, style="Panel.TFrame")
        ng_btns.grid(row=0, column=1, padx=(6, 0))
        ttk.Button(ng_btns, text="출력", style="Start.TButton", command=self.apply_ng_ao).pack(
            side="left", padx=(0, 3)
        )
        ttk.Button(ng_btns, text="0V", style="Stop.TButton", command=self.zero_ng_ao).pack(
            side="left"
        )
        self.ng_ao_value = ttk.Label(
            controls, text="NG: 0.000 V", style="ValueSmall.TLabel"
        )
        self.ng_ao_value.grid(row=15, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- Col 1: Pump 1 + graphs + injection timer ---
        pump1 = self.panel(page)
        pump1.configure(padding=8)
        self.place_panel(pump1, row=0, column=1, sticky="nsew", padx=(0, 4))
        ttk.Label(pump1, text="펌프1 · AO0", style="PanelTitle.TLabel").pack(anchor="w")
        self.focus_pv = ttk.Label(pump1, text="0.0", style="Value.TLabel")
        self.focus_pv.pack(anchor="w", pady=(2, 0))
        self.pv_value = self.focus_pv
        ttk.Label(pump1, text="cc/min", style="Hint.TLabel").pack(anchor="w")
        sv_row1 = ttk.Frame(pump1, style="Panel.TFrame")
        sv_row1.pack(fill="x", pady=(8, 0))
        ttk.Label(sv_row1, text="SV", style="Panel.TLabel").pack(side="left")
        self.sv_entry = ttk.Entry(
            sv_row1,
            textvariable=self.sv_input,
            style="Highlight.TEntry",
            justify="center",
            width=9,
        )
        self.sv_entry.pack(side="right")
        self.sv_entry.bind("<Return>", self._apply_sv_from_entry)
        self.sv_entry.bind("<KP_Enter>", self._apply_sv_from_entry)
        self.sv_apply_hint = ttk.Label(
            pump1, text="Enter로 적용", style="Hint.TLabel"
        )
        self.sv_apply_hint.pack(anchor="e", pady=(2, 0))
        stat1 = ttk.Frame(pump1, style="Panel.TFrame")
        stat1.pack(fill="x", pady=(6, 0))
        self.focus_err = ttk.Label(stat1, text="Δ 0.0", style="ValueSmall.TLabel")
        self.focus_err.pack(side="left")
        self.fb_hz = ttk.Label(stat1, text="MP5Y 대기", style="Hint.TLabel")
        self.fb_hz.pack(side="right")
        self.output_value = ttk.Label(pump1, text="AO0  0.000 V", style="ValueSmall.TLabel")
        self.output_value.pack(anchor="w", pady=(6, 0))

        ttk.Separator(pump1, orient="horizontal").pack(fill="x", pady=(8, 6))
        self.feedback_graph = TrendGraph(
            pump1,
            "펌프1 PV / SV",
            [("PV", COLORS["pv"]), ("SV", COLORS["sv"])],
            0,
            200,
            "cc/min",
        )
        self.feedback_graph.configure(height=118)
        self.feedback_graph.pack(fill="both", expand=True, pady=(0, 4))
        self.feedback_ao_graph = TrendGraph(
            pump1, "펌프1 AO0", [("AO0", COLORS["ao"])], 0, 5, "V"
        )
        self.feedback_ao_graph.configure(height=78)
        self.feedback_ao_graph.pack(fill="x", pady=(0, 6))

        timing_block = ttk.Frame(pump1, style="Panel.TFrame")
        timing_block.pack(fill="x", pady=(8, 0))
        self.control_start_label = ttk.Label(
            timing_block, text="제어 시작  --:--:--", style="ValueCompact.TLabel"
        )
        self.control_start_label.pack(anchor="w", pady=(0, 6))
        injection_block = ttk.Frame(timing_block, style="Panel.TFrame")
        injection_block.pack(fill="x")
        injection_header = ttk.Frame(injection_block, style="Panel.TFrame")
        injection_header.pack(fill="x")
        ttk.Label(
            injection_header, text="물 주입시간", style="InjectionCaption.TLabel"
        ).pack(side="left")
        ttk.Button(
            injection_header, text="초기화", command=self.reset_injection_time
        ).pack(side="right")
        self.injection_time_label = ttk.Label(
            injection_block, text="00:00:00", style="InjectionTime.TLabel"
        )
        self.injection_time_label.pack(anchor="w", pady=(4, 0))

        # --- Col 2: Pump 2 + graphs ---
        pump2 = self.panel(page)
        pump2.configure(padding=8)
        self.place_panel(pump2, row=0, column=2, sticky="nsew", padx=(0, 4))
        ttk.Label(pump2, text="펌프2 · AO2", style="PanelTitle.TLabel").pack(anchor="w")
        self.focus_pv2 = ttk.Label(pump2, text="0.0", style="Value.TLabel")
        self.focus_pv2.pack(anchor="w", pady=(2, 0))
        sv_row2 = ttk.Frame(pump2, style="Panel.TFrame")
        sv_row2.pack(fill="x", pady=(8, 0))
        ttk.Label(sv_row2, text="SV", style="Panel.TLabel").pack(side="left")
        self.sv2_entry = ttk.Entry(
            sv_row2,
            textvariable=self.sv2,
            style="Highlight.TEntry",
            justify="center",
            width=9,
        )
        self.sv2_entry.pack(side="right")
        ttk.Label(pump2, text="cc/min", style="Hint.TLabel").pack(anchor="e")
        stat2 = ttk.Frame(pump2, style="Panel.TFrame")
        stat2.pack(fill="x", pady=(6, 0))
        self.focus_err2 = ttk.Label(stat2, text="Δ 0.0", style="ValueSmall.TLabel")
        self.focus_err2.pack(side="left")
        self.fb_hz2 = ttk.Label(stat2, text="MP5Y2 —", style="Hint.TLabel")
        self.fb_hz2.pack(side="right")
        self.output_value2 = ttk.Label(
            pump2, text="AO2  0.000 V", style="ValueSmall.TLabel"
        )
        self.output_value2.pack(anchor="w", pady=(6, 0))

        ttk.Separator(pump2, orient="horizontal").pack(fill="x", pady=(8, 6))
        self.feedback_graph2 = TrendGraph(
            pump2,
            "펌프2 PV / SV",
            [("PV", COLORS["pv"]), ("SV", COLORS["sv"])],
            0,
            200,
            "cc/min",
        )
        self.feedback_graph2.configure(height=118)
        self.feedback_graph2.pack(fill="both", expand=True, pady=(0, 4))
        self.feedback_ao_graph2 = TrendGraph(
            pump2, "펌프2 AO2", [("AO2", COLORS["ao"])], 0, 5, "V"
        )
        self.feedback_ao_graph2.configure(height=78)
        self.feedback_ao_graph2.pack(fill="x")

        # --- Col 3: comm + IGN/Coolint ---
        monitor = self.panel(page)
        monitor.configure(padding=6)
        self.place_panel(monitor, row=0, column=3, sticky="nsew")
        ttk.Label(monitor, text="통신 / 보조", style="PanelTitle.TLabel").pack(anchor="w")
        comm_row = ttk.Frame(monitor, style="Panel.TFrame")
        comm_row.pack(fill="x")
        comm_row.columnconfigure((0, 1), weight=1)

        mp1_comm = ttk.Frame(comm_row, style="Panel.TFrame")
        mp1_comm.grid(row=0, column=0, sticky="w")
        self.mp5y_canvas = tk.Canvas(
            mp1_comm, width=22, height=22, bg=COLORS["panel"], highlightthickness=0
        )
        self.mp5y_canvas.pack(side="left")
        self.mp5y_lamp = self.mp5y_canvas.create_oval(
            2, 2, 20, 20, fill=COLORS["bad"], outline="#9B2C2C", width=2
        )
        mp5y_text = ttk.Frame(mp1_comm, style="Panel.TFrame")
        mp5y_text.pack(side="left", padx=(4, 0))
        ttk.Label(mp5y_text, text="MP5Y", style="Panel.TLabel").pack(anchor="w")
        self.mp5y_status_label = ttk.Label(
            mp5y_text, text="COM3", style="Hint.TLabel"
        )
        self.mp5y_status_label.pack(anchor="w")

        mp2_comm = ttk.Frame(comm_row, style="Panel.TFrame")
        mp2_comm.grid(row=0, column=1, sticky="e")
        self.mp5y2_canvas = tk.Canvas(
            mp2_comm, width=22, height=22, bg=COLORS["panel"], highlightthickness=0
        )
        self.mp5y2_canvas.pack(side="left")
        self.mp5y2_lamp = self.mp5y2_canvas.create_oval(
            2, 2, 20, 20, fill=COLORS["bad"], outline="#9B2C2C", width=2
        )
        mp5y2_text = ttk.Frame(mp2_comm, style="Panel.TFrame")
        mp5y2_text.pack(side="left", padx=(4, 0))
        ttk.Label(mp5y2_text, text="MP5Y2", style="Panel.TLabel").pack(anchor="w")
        self.mp5y2_status_label = ttk.Label(
            mp5y2_text, text="COM4", style="Hint.TLabel"
        )
        self.mp5y2_status_label.pack(anchor="w")

        aux_row = ttk.Frame(monitor, style="Panel.TFrame")
        aux_row.pack(fill="x", pady=(8, 0))
        aux_row.columnconfigure((0, 1), weight=1)
        self.igniter_button = ttk.Button(
            aux_row,
            text="IGN OFF",
            style="Start.TButton",
            command=self.toggle_igniter,
        )
        self.igniter_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.igniter_caption = self.igniter_button
        self.coolint_p_button = ttk.Button(
            aux_row,
            text="Coolint P OFF",
            style="Stop.TButton",
            command=self.toggle_coolint_p,
        )
        self.coolint_p_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # --- Bottom: level / valves (compact) ---
        level_section = self.panel(page)
        level_section.configure(padding=6)
        self.place_panel(
            level_section, row=1, column=0, columnspan=4, sticky="nsew", pady=(4, 0)
        )
        header = ttk.Frame(level_section, style="Panel.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="레벨 / 밸브", style="PanelTitle.TLabel").pack(side="left")
        self.level_master_status = ttk.Label(
            header, text="대기 · 밸브 닫힘", style="Hint.TLabel"
        )
        self.level_master_status.pack(side="right")

        cards = ttk.Frame(level_section, style="Panel.TFrame")
        cards.pack(fill="both", expand=True, pady=(4, 0))
        for i in range(3):
            cards.columnconfigure(i, weight=1, uniform="valve")
            cards.rowconfigure(0, weight=1)

        self.level_high_labels = []
        self.level_low_labels = []
        self.valve_status_labels = []
        self.level_logic_labels = []
        self.valve_do_labels = []
        rules = (
            "4접점 · L열림 / H닫힘",
            "L닫힘 / H열림",
            "L닫힘 / H열림",
        )
        for index, rule in enumerate(rules):
            wrap = tk.Frame(cards, bg=COLORS["border"], padx=1, pady=1)
            wrap.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 4, 0),
            )
            card = ttk.Frame(wrap, style="Panel.TFrame", padding=6)
            card.pack(fill="both", expand=True)
            head = ttk.Frame(card, style="Panel.TFrame")
            head.pack(fill="x")
            do_label = ttk.Label(head, text=f"DO{index}", style="PanelTitle.TLabel")
            do_label.pack(side="left")
            self.valve_do_labels.append(do_label)
            valve = ttk.Label(head, text="닫힘", style="ValueSmall.TLabel")
            valve.pack(side="right")
            name_entry = ttk.Entry(
                card, textvariable=self.valve_name_vars[index], width=12
            )
            name_entry.pack(fill="x", pady=(4, 0))
            name_entry.bind(
                "<Return>",
                lambda _event, i=index: self._commit_valve_name(i),
            )
            ttk.Label(card, text=rule, style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
            sensor_row = ttk.Frame(card, style="Panel.TFrame")
            sensor_row.pack(fill="x", pady=(6, 0))
            high = ttk.Label(sensor_row, text="H OFF", style="Hint.TLabel")
            high.pack(side="left")
            low = ttk.Label(sensor_row, text="L OFF", style="Hint.TLabel")
            low.pack(side="right")
            logic = ttk.Label(card, text="대기", style="Hint.TLabel")
            logic.pack(anchor="w", pady=(4, 0))
            self.level_high_labels.append(high)
            self.level_low_labels.append(low)
            self.valve_status_labels.append(valve)
            self.level_logic_labels.append(logic)
        return page

    # ------------------------------------------------------------- helpers
    def _show_touch_section(self, name: str) -> None:
        if not hasattr(self, "touch_sections"):
            return
        self.touch_sections[name].tkraise()
        for section_name, button in self.touch_nav_buttons.items():
            button.configure(
                style="TouchNavActive.TButton" if section_name == name else "TouchNav.TButton"
            )

    def _touch_numeric_field(
        self,
        parent: tk.Misc,
        label: str,
        default: str,
        minimum: float,
        maximum: float,
        decimals: int,
    ) -> tk.StringVar:
        value = tk.StringVar(value=default)
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text=label, style="Panel.TLabel").pack(side="left")
        ttk.Button(
            row,
            textvariable=value,
            style="Touch.TButton",
            width=8,
            command=lambda: self.open_numeric_keypad(
                value, label, minimum, maximum, decimals
            ),
        ).pack(side="right")
        return value

    def _set_sv(self, value: float) -> None:
        self.sv.set(f"{max(0.0, min(9999.0, float(value))):.1f}")

    def _apply_sv_from_entry(self, _event: tk.Event | None = None) -> str:
        """Apply desktop SV only when the operator presses Enter."""
        text = self.sv_input.get().strip()
        try:
            value = float(text)
        except ValueError:
            messagebox.showerror("입력 오류", "SV에 숫자를 입력하세요.")
            self.sv_input.set(self.sv.get())
            return "break"
        if not 0.0 <= value <= 9999.0:
            messagebox.showerror("범위 오류", "SV는 0.0 ~ 9999.0 cc/min이어야 합니다.")
            self.sv_input.set(self.sv.get())
            return "break"
        applied = f"{value:.1f}"
        self.sv.set(applied)
        self.sv_input.set(applied)
        self.sv_apply_hint.configure(
            text=f"적용됨: {applied} cc/min", foreground=COLORS["ok"]
        )
        self.sv_entry.selection_clear()
        return "break"

    def _adjust_sv(self, delta: float) -> None:
        current = self.number_silent(self.sv, 0.0)
        self._set_sv(current + delta)

    def _set_sv2(self, value: float) -> None:
        self.sv2.set(f"{max(0.0, min(9999.0, float(value))):.1f}")

    def _adjust_sv2(self, delta: float) -> None:
        current = self.number_silent(self.sv2, 0.0)
        self._set_sv2(current + delta)

    def _refresh_touch_sv(self) -> None:
        if not self.touch_mode or not hasattr(self, "focus_sv"):
            return
        value = self.number_silent(self.sv, 0.0)
        self.focus_sv.configure(text=f"SV  {value:.1f}")
        if hasattr(self, "focus_sv2"):
            value2 = self.number_silent(self.sv2, 0.0)
            self.focus_sv2.configure(text=f"SV  {value2:.1f}")

    def open_numeric_keypad(
        self,
        variable: tk.StringVar,
        title: str,
        minimum: float,
        maximum: float,
        decimals: int,
    ) -> None:
        """Open a finger-friendly modal keypad for numeric process settings."""
        if self._keypad_popup is not None and self._keypad_popup.winfo_exists():
            self._keypad_popup.destroy()

        popup = tk.Toplevel(self)
        self._keypad_popup = popup
        popup.title(title)
        popup.configure(bg=COLORS["bg"])
        popup.transient(self)
        popup.grab_set()

        width = min(430, max(360, self.winfo_screenwidth() - 30))
        height = min(500, max(420, self.winfo_screenheight() - 30))
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.resizable(False, False)

        buffer = tk.StringVar(value=variable.get().strip() or "0")
        frame = ttk.Frame(popup, style="App.TFrame", padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, style="TouchTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=f"허용 범위: {minimum:g} ~ {maximum:g}",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(1, 6))
        display = ttk.Label(frame, textvariable=buffer, style="TouchValue.TLabel", anchor="e")
        display.pack(fill="x", pady=(0, 8), ipady=4)

        keys = ttk.Frame(frame, style="App.TFrame")
        keys.pack(fill="both", expand=True)
        for column in range(3):
            keys.columnconfigure(column, weight=1)
        for row in range(4):
            keys.rowconfigure(row, weight=1)

        def append(character: str) -> None:
            text = buffer.get()
            if character == ".":
                if "." not in text:
                    buffer.set((text or "0") + ".")
                return
            if len(text) >= 12:
                return
            buffer.set(character if text == "0" else text + character)

        for index, character in enumerate(("7", "8", "9", "4", "5", "6", "1", "2", "3")):
            row, column = divmod(index, 3)
            ttk.Button(
                keys,
                text=character,
                style="Keypad.TButton",
                command=lambda key=character: append(key),
            ).grid(row=row, column=column, sticky="nsew", padx=3, pady=3)
        ttk.Button(
            keys, text="C", style="Keypad.TButton", command=lambda: buffer.set("0")
        ).grid(row=3, column=0, sticky="nsew", padx=3, pady=3)
        ttk.Button(
            keys, text="0", style="Keypad.TButton", command=lambda: append("0")
        ).grid(row=3, column=1, sticky="nsew", padx=3, pady=3)
        ttk.Button(
            keys, text=".", style="Keypad.TButton", command=lambda: append(".")
        ).grid(row=3, column=2, sticky="nsew", padx=3, pady=3)

        actions = ttk.Frame(frame, style="App.TFrame")
        actions.pack(fill="x", pady=(7, 0))
        actions.columnconfigure((0, 1, 2), weight=1)

        def backspace() -> None:
            text = buffer.get()[:-1]
            buffer.set(text or "0")

        def apply_value() -> None:
            try:
                value = float(buffer.get())
            except ValueError:
                messagebox.showerror("입력 오류", "숫자를 입력하세요.", parent=popup)
                return
            if not minimum <= value <= maximum:
                messagebox.showerror(
                    "입력 오류",
                    f"{minimum:g} ~ {maximum:g} 범위로 입력하세요.",
                    parent=popup,
                )
                return
            variable.set(f"{value:.{decimals}f}")
            popup.destroy()

        ttk.Button(actions, text="⌫", style="Touch.TButton", command=backspace).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(actions, text="취소", style="Touch.TButton", command=popup.destroy).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        ttk.Button(actions, text="적용", style="TouchStart.TButton", command=apply_value).grid(
            row=0, column=2, sticky="ew", padx=(4, 0)
        )

    def show_page(self, page: str) -> None:
        self.active_page = page
        for name, frame in self.pages.items():
            frame.pack_forget()
            if name in self.nav_buttons:
                self.nav_buttons[name].configure(
                    style="NavActive.TButton" if name == page else "Nav.TButton"
                )
        self.pages[page].pack(fill="both", expand=True)

    # ---------------------------------------------------------- popup UI
    def open_tc_popup(self) -> None:
        """Open optional NI 9214 thermocouple monitoring."""
        if self._tc_popup is not None and self._tc_popup.winfo_exists():
            self._tc_popup.lift()
            self._tc_popup.focus_force()
            return

        popup = tk.Toplevel(self)
        self._tc_popup = popup
        popup.title("TC MONITORING · NI 9214")
        popup.configure(bg=COLORS["bg"])
        popup.transient(self)

        frame = ttk.Frame(popup, style="App.TFrame", padding=10)
        frame.pack(fill="both", expand=True)

        heading = ttk.Frame(frame, style="App.TFrame")
        heading.pack(fill="x", pady=(0, 4))
        ttk.Label(
            heading, text="TC MONITORING · NI 9214", style="PanelTitle.TLabel"
        ).pack(side="left")
        self.tc_status_label = ttk.Label(
            heading, text="NI 9214 연결 확인 중", style="ValueSmall.TLabel"
        )
        self.tc_status_label.pack(side="right")

        ttk.Label(
            frame,
            text="왼쪽 K TYPE CH0~4  |  오른쪽 T TYPE CH5~9  |  NI 9214 내장 CJC 자동 보상",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 4))

        channel_row = ttk.Frame(frame, style="App.TFrame")
        channel_row.pack(fill="x", pady=(0, 6))
        self.tc_k_channel_var = tk.StringVar(value=self.channels.tc_k_inputs)
        self.tc_t_channel_var = tk.StringVar(value=self.channels.tc_t_inputs)
        ttk.Label(channel_row, text="K 채널", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            channel_row, textvariable=self.tc_k_channel_var, width=18
        ).pack(side="left", padx=(5, 12))
        ttk.Label(channel_row, text="T 채널", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            channel_row, textvariable=self.tc_t_channel_var, width=18
        ).pack(side="left", padx=(5, 0))

        columns = ttk.Frame(frame, style="App.TFrame")
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="tc")
        columns.columnconfigure(1, weight=1, uniform="tc")
        columns.rowconfigure(0, weight=1)

        self.tc_name_vars = [tk.StringVar(value=name) for name in self.tc_names]
        self.tc_value_labels = [None] * 10  # type: ignore[list-item]
        row_pad = 2 if self.touch_mode else 3

        def build_group(
            parent: tk.Misc,
            title: str,
            channel_indexes: range,
            column: int,
        ) -> None:
            panel = self.panel(parent)
            panel.configure(padding=6 if self.touch_mode else 8)
            self.place_panel(
                panel,
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 6, 0),
            )
            ttk.Label(panel, text=title, style="PanelTitle.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
            )
            for col, text in enumerate(("채널", "이름", "현재 온도")):
                ttk.Label(panel, text=text, style="Hint.TLabel").grid(
                    row=1, column=col, sticky="w" if col < 2 else "ew", padx=3
                )
            panel.columnconfigure(1, weight=0)
            panel.columnconfigure(2, weight=1)
            for row, index in enumerate(channel_indexes, start=2):
                ttk.Label(
                    panel, text=f"CH{index}", style="ValueSmall.TLabel"
                ).grid(row=row, column=0, sticky="w", padx=3, pady=row_pad)
                ttk.Entry(
                    panel, textvariable=self.tc_name_vars[index], width=10
                ).grid(row=row, column=1, sticky="w", padx=3, pady=row_pad)
                value_label = ttk.Label(
                    panel, text="— °C", style="Value.TLabel"
                )
                value_label.grid(
                    row=row, column=2, sticky="ew", padx=6, pady=row_pad
                )
                self.tc_value_labels[index] = value_label

        build_group(columns, "K TYPE · CH0~4", range(0, 5), 0)
        build_group(columns, "T TYPE · CH5~9", range(5, 10), 1)

        actions = ttk.Frame(frame, style="App.TFrame")
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(
            actions,
            text="이름 저장",
            style="TouchStart.TButton" if self.touch_mode else "Start.TButton",
            command=self.save_tc_settings,
        ).pack(side="left")
        ttk.Button(actions, text="닫기", command=self._close_tc_popup).pack(
            side="right"
        )

        # Size to full content so CH0~4 / CH5~9 and action buttons stay visible.
        popup.update_idletasks()
        screen_w = max(self.winfo_screenwidth(), 800)
        screen_h = max(self.winfo_screenheight(), 600)
        need_w = max(frame.winfo_reqwidth() + 20, 780)
        need_h = max(frame.winfo_reqheight() + 28, 560)
        width = min(need_w, screen_w - 24)
        height = min(need_h, screen_h - 48)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.minsize(min(width, 720), min(height, 520))

        self.tc_running = True
        self._last_tc_read_at = 0.0
        self._refresh_tc_status()
        popup.protocol("WM_DELETE_WINDOW", self._close_tc_popup)

    def _refresh_tc_status(self, error: str = "") -> None:
        if getattr(self, "tc_status_label", None) is None:
            return
        if error:
            text = f"TC 오류 · {error}"
            color = COLORS["bad"]
        elif self.daq.tc_available:
            text = "NI 9214 연결됨 · 모니터링 중"
            color = COLORS["ok"]
        else:
            text = "NI 9214 미연결 · 연결 대기"
            color = COLORS["warn"]
        if text == self._tc_last_status_text:
            return
        self._tc_last_status_text = text
        self.tc_status_label.configure(text=text, foreground=color)

    def _apply_tc_ui_values(
        self, values: list[float] | None = None, *, error: bool = False
    ) -> None:
        if not self.tc_value_labels:
            return
        for index, label in enumerate(self.tc_value_labels):
            if label is None:
                continue
            if error:
                text, color = "ERR", COLORS["bad"]
            elif values is None:
                text, color = "— °C", COLORS["muted"]
            else:
                text = f"{values[index]:.1f} °C"
                color = COLORS["title"]
            if self._tc_last_ui_values[index] == text:
                continue
            self._tc_last_ui_values[index] = text
            label.configure(text=text, foreground=color)

    def _tc_worker_read(self) -> None:
        try:
            values = self.daq.read_thermocouples()
            self._tc_pending_values = values
            self._tc_pending_error = None
        except Exception as exc:  # noqa: BLE001 - keep UI loop alive
            self._tc_pending_values = None
            self._tc_pending_error = str(exc)
        finally:
            self._tc_worker_busy = False

    def _update_tc_monitor(self) -> None:
        if not (
            self.tc_running
            and self._tc_popup is not None
            and self._tc_popup.winfo_exists()
        ):
            return

        # Apply background read results on the UI thread only.
        if self._tc_pending_error is not None:
            error = self._tc_pending_error
            self._tc_pending_error = None
            self._refresh_tc_status(error)
            self._apply_tc_ui_values(error=True)
        elif self._tc_pending_values is not None:
            values = self._tc_pending_values
            self._tc_pending_values = None
            self._apply_tc_ui_values(values)
            self._refresh_tc_status()

        now = time.monotonic()
        if now - self._last_tc_read_at < self.TC_POLL_S:
            return
        if self._tc_worker_busy:
            return
        self._last_tc_read_at = now
        if not self.daq.tc_available:
            self._refresh_tc_status()
            self._apply_tc_ui_values(None)
            return

        self._tc_worker_busy = True
        threading.Thread(
            target=self._tc_worker_read, name="tc-reader", daemon=True
        ).start()

    def _sync_tc_inputs(self, show_error: bool = True) -> bool:
        del show_error
        if not self.tc_name_vars:
            return True
        self.tc_names = [
            self.tc_name_vars[index].get().strip() or f"TC {index}"
            for index in range(10)
        ]
        if hasattr(self, "tc_k_channel_var"):
            self.channels.tc_k_inputs = (
                self.tc_k_channel_var.get().strip() or self.channels.tc_k_inputs
            )
            self.channels.tc_t_inputs = (
                self.tc_t_channel_var.get().strip() or self.channels.tc_t_inputs
            )
        return True

    def save_tc_settings(self) -> None:
        if self._sync_tc_inputs():
            self.save_settings()

    def _close_tc_popup(self) -> None:
        self._sync_tc_inputs(show_error=False)
        self.tc_running = False
        # Give the background reader a moment to finish before closing the task.
        wait_until = time.monotonic() + 0.4
        while self._tc_worker_busy and time.monotonic() < wait_until:
            time.sleep(0.02)
        self.daq.close_thermocouples()
        self._tc_pending_values = None
        self._tc_pending_error = None
        self._tc_last_ui_values = [""] * 10
        self._tc_last_status_text = ""
        if self._tc_popup is not None and self._tc_popup.winfo_exists():
            self._tc_popup.destroy()
        self._tc_popup = None
        self.tc_name_vars = []
        self.tc_value_labels = []

    def open_ao_popup(self) -> None:
        """Open manual AO2 + valve DO0..2 test window."""
        if self._ao_popup is not None and self._ao_popup.winfo_exists():
            self._ao_popup.lift()
            self._ao_popup.focus_force()
            return
        if not (self.daq.available and self.daq.level_available):
            messagebox.showerror(
                "NI-DAQ 오류",
                "I/O TEST는 NI 9264와 NI 9477이 모두 연결되어야 사용할 수 있습니다.",
            )
            return
        if self.feedback_running:
            self.toggle_feedback()

        popup = tk.Toplevel(self)
        self._ao_popup = popup
        popup.title("I/O 수동 TEST (AO2 + DO0~2)")
        popup.configure(bg=COLORS["bg"])
        self.io_test_active = True
        try:
            self.valve_commands = self.daq.write_valves([False, False, False])
            self.current_spare_ao = self.daq.write_voltage(
                0.0, self.channels.spare_ao
            )
        except DaqError as exc:
            self.io_test_active = False
            popup.destroy()
            self._ao_popup = None
            self._show_hardware_error(exc)
            return

        screen_w = max(self.winfo_screenwidth(), 800)
        screen_h = max(self.winfo_screenheight(), 480)
        w = min(860, max(720, int(screen_w * 0.80)))
        h = min(560, max(460, int(screen_h * 0.72)))
        popup.geometry(f"{w}x{h}")

        frame = ttk.Frame(popup, style="App.TFrame", padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="I/O 수동 TEST", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 10)
        )
        ttk.Label(
            frame,
            text=(
                "TEST 중 레벨 자동제어는 일시 정지됩니다. "
                "V1~V3(DO0~2)는 레벨센서와 무관하게 수동 개폐합니다. "
                "창을 닫으면 AO2=0 V, DO0~2=OFF 후 자동제어로 복귀합니다. "
                "Coolint P(DO4)는 메인 화면에서 계속 제어됩니다."
            ),
            style="Hint.TLabel",
            wraplength=w - 60,
        ).pack(anchor="w", pady=(0, 10))

        body = ttk.Frame(frame, style="App.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure((0, 1), weight=1, uniform="test")
        body.rowconfigure(0, weight=1)

        controls = self.panel(body)
        self.place_panel(controls, row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(controls, text="AO 2 · 0~5 V", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(controls, text=self.channels.spare_ao, style="Hint.TLabel").pack(
            anchor="w", pady=(2, 8)
        )
        self.spare_ao_voltage = tk.StringVar(value="0.0")
        spare_ao_entry = ttk.Entry(
            controls,
            textvariable=self.spare_ao_voltage,
            style="Highlight.TEntry",
            justify="center",
        )
        spare_ao_entry.pack(fill="x")
        if self.touch_mode:
            spare_ao_entry.bind(
                "<Button-1>",
                lambda _event: (
                    self.open_numeric_keypad(
                        self.spare_ao_voltage, "AO 2 출력 (V)", 0.0, 5.0, 3
                    ),
                    "break",
                )[1],
            )
        spare_ao_buttons = ttk.Frame(controls, style="Panel.TFrame")
        spare_ao_buttons.pack(fill="x", pady=(8, 0))
        spare_ao_buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(
            spare_ao_buttons,
            text="출력",
            style="Start.TButton",
            command=self.output_spare_ao,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(
            spare_ao_buttons,
            text="0 V",
            style="Stop.TButton",
            command=self.zero_spare_ao,
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.spare_ao_value_label = ttk.Label(
            controls, text="AO 2: 0.000 V", style="ValueSmall.TLabel"
        )
        self.spare_ao_value_label.pack(anchor="w", pady=(7, 0))

        valves = self.panel(body)
        self.place_panel(valves, row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(valves, text="전동볼밸브 DO0~2", style="PanelTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            valves, text="레벨센서 무시 · 수동 개폐 · 기능명 편집 가능", style="Hint.TLabel"
        ).pack(anchor="w", pady=(2, 10))
        self.test_valve_labels = []
        for index in range(3):
            row = ttk.Frame(valves, style="Panel.TFrame")
            row.pack(fill="x", pady=(0, 2))
            ttk.Label(
                row, text=f"DO{index}", style="PanelTitle.TLabel"
            ).pack(side="left")
            status = ttk.Label(row, text="닫힘", style="ValueSmall.TLabel")
            status.pack(side="right")
            self.test_valve_labels.append(status)
            name_entry = ttk.Entry(
                valves, textvariable=self.valve_name_vars[index], width=16
            )
            name_entry.pack(fill="x", pady=(0, 4))
            name_entry.bind(
                "<Return>",
                lambda _event, i=index: self._commit_valve_name(i),
            )
            buttons = ttk.Frame(valves, style="Panel.TFrame")
            buttons.pack(fill="x", pady=(0, 10))
            buttons.columnconfigure((0, 1), weight=1)
            ttk.Button(
                buttons,
                text="열기",
                style="Start.TButton",
                command=lambda valve=index: self.set_test_valve(valve, True),
            ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
            ttk.Button(
                buttons,
                text="닫기",
                style="Stop.TButton",
                command=lambda valve=index: self.set_test_valve(valve, False),
            ).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        ttk.Button(
            valves,
            text="밸브 이름 저장",
            style="Start.TButton",
            command=self.save_valve_names,
        ).pack(fill="x", pady=(4, 0))

        popup.protocol("WM_DELETE_WINDOW", self._close_ao_popup)

    def _close_ao_popup(self) -> None:
        try:
            # Clear TEST outputs. Water pump AO0, NG PUMP AO1, Coolint P stay as-is.
            try:
                self.daq.write_voltage(0.0, self.channels.spare_ao)
            except DaqError:
                pass
            try:
                self.valve_commands = self.daq.write_valves([False, False, False])
            except DaqError:
                pass
        finally:
            self.current_spare_ao = 0.0
            self.io_test_active = False
            if self._ao_popup is not None and self._ao_popup.winfo_exists():
                self._ao_popup.destroy()
            self._ao_popup = None

    def _sync_valve_names(self) -> None:
        names: list[str] = []
        for index, var in enumerate(self.valve_name_vars):
            text = var.get().strip() or self.valve_names[index]
            names.append(text)
            var.set(text)
        self.valve_names = names

    def _commit_valve_name(self, index: int) -> None:
        text = self.valve_name_vars[index].get().strip() or self.valve_names[index]
        self.valve_name_vars[index].set(text)
        self.valve_names[index] = text

    def save_valve_names(self) -> None:
        self._sync_valve_names()
        try:
            data: dict[str, object] = {}
            if self.SETTINGS_PATH.exists():
                try:
                    loaded = json.loads(self.SETTINGS_PATH.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    messagebox.showerror(
                        "저장 실패",
                        f"기존 settings.json을 읽지 못해 덮어쓰지 않았습니다: {exc}",
                    )
                    return
                if isinstance(loaded, dict):
                    data = loaded
            data["valve_names"] = self.valve_names
            self.SETTINGS_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            messagebox.showinfo("저장", "밸브 기능 이름을 저장했습니다.")
        except OSError as exc:
            messagebox.showerror("저장 실패", str(exc))

    def set_test_valve(self, index: int, opened: bool) -> None:
        """Directly command one valve while the I/O test popup owns DO0..DO2."""
        commands = list(self.valve_commands)
        commands[index] = bool(opened)
        try:
            self.valve_commands = self.daq.write_valves(commands)
        except DaqError as exc:
            self._show_hardware_error(exc)
            return
        if hasattr(self, "test_valve_labels") and index < len(self.test_valve_labels):
            self.test_valve_labels[index].configure(
                text="열림" if opened else "닫힘",
                foreground=COLORS["ok"] if opened else COLORS["accent_deep"],
            )
        if hasattr(self, "valve_status_labels") and index < len(self.valve_status_labels):
            self.valve_status_labels[index].configure(
                text="열림" if opened else "닫힘",
                foreground=COLORS["ok"] if opened else COLORS["accent_deep"],
            )

    def number(self, variable: tk.StringVar, label: str) -> float | None:
        try:
            return float(variable.get().strip())
        except ValueError:
            messagebox.showerror("입력 오류", f"{label}에 숫자를 입력하세요.")
            return None

    def number_silent(self, variable: tk.StringVar, default: float) -> float:
        try:
            return float(variable.get().strip())
        except ValueError:
            return default

    def open_wiring_guide(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("시스템 배선 가이드")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("700x720")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=22)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(frame, text="유량 · 펌프 · 레벨 · 밸브 배선", style="PanelTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            frame,
            text="NI 9422 DI0~5 / NI 9477 DO0~2 / NI 9264 ao0(REF.W)·ao1(NG PUMP)",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        guide = (
            "【유량계 OF05ZAT-AR → MP5Y-25】\n"
            "  AR = 전압 펄스 출력 (High/Low 전압 직접 출력)\n"
            "  펄스 Out / GND  →  MP5Y 입력 단자\n"
            "  MP5Y 입력방식 in-A: PnP  ★ 필수\n"
            "  펄스정수 0.46 ml/P\n"
            "  MP5Y 모드: F1 주파수\n"
            "  프리스케일: 2.76 × 10^1 = 27.6  → 화면 = cc/min\n"
            "\n"
            "【MP5Y-25 → USB-RS485】\n"
            "  MP5Y A(+)  →  컨버터 A\n"
            "  MP5Y B(−)  →  컨버터 B\n"
            "  PC COM 포트: COM9\n"
            "  통신: 9600 / 8 / None / Stop2 / Addr 1\n"
            "\n"
            "【REF.W → NI 9264 ao0】\n"
            "  REF.W / 인버터 V1  →  NI 9264 ao0\n"
            "  COM               →  AO COM\n"
            "\n"
            "【NG PUMP → NI 9264 ao1】\n"
            "  NG PUMP 신호 입력 →  NI 9264 ao1\n"
            "  NG PUMP COM       →  AO COM\n"
            "\n"
            "【레벨센서 → NI 9422】\n"
            "  급수(4접점 DFR): COM(백) 공통, H(황)→DI0+, L(청)→DI1+\n"
            "    ※ HH/LL 미사용. H ON이면 L도 ON → 고수위(충돌 아님)\n"
            "  배수2(2접점): HIGH→DI2+ / LOW→DI3+\n"
            "  배수3(2접점): HIGH→DI4+ / LOW→DI5+\n"
            "  DI0−~DI5− → PSU 0V\n"
            "  접점 ON 시 DI+–DI−에 24V → 입력 ON\n"
            "\n"
            "【전동볼밸브 3개 → NI 9477 (싱킹 출력)】\n"
            "  밸브 Red(+24V) → PSU +24V\n"
            "  밸브 White(SIG) → DO0 / DO1 / DO2\n"
            "  밸브 Black(0V) + NI 9477 COM → PSU 0V\n"
            "  DO ON = White를 0V로 당김 = 열림 명령\n"
            "  ※ 0V 여부는 명령 확인이며 실제 기계 위치 피드백은 아님\n"
            "\n"
            "【Coolint P DO4 → NI 9477】\n"
            "  빨간선 → SMPS +24V / 검은선 → DO4 / COM → 0V\n"
            "  메인 화면 Coolint P ON/OFF (≤1 A)\n"
            "\n"
            "UI는 MP5Y 표시값(cc/min)을 그대로 PV로 사용합니다.\n"
            "(소프트웨어에서 ×0.46×60 를 다시 하지 않음)"
        )
        box = tk.Text(
            frame,
            height=20,
            wrap="word",
            font=("Consolas", 11),
            bg=COLORS["panel_alt"],
            fg=COLORS["title"],
            relief="flat",
            padx=12,
            pady=12,
        )
        box.insert("1.0", guide)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)
        ttk.Button(frame, text="닫기", command=dialog.destroy).pack(pady=(14, 0))

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("채널 / 통신 설정")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("620x620" if self.touch_mode else "590x650")
        dialog.transient(self)
        dialog.grab_set()

        frame_padding = 10 if self.touch_mode else 20
        outer_padding = 6 if self.touch_mode else 16
        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=frame_padding)
        frame.pack(fill="both", expand=True, padx=outer_padding, pady=outer_padding)
        ttk.Label(frame, text="MP5Y / NI-DAQ", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        ao_var = tk.StringVar(value=self.channels.ao_pump)
        ng_ao_var = tk.StringVar(value=self.channels.ao_ng_pump)
        di_var = tk.StringVar(value=self.channels.level_inputs)
        do_var = tk.StringVar(value=self.channels.valve_outputs)
        igniter_do_var = tk.StringVar(value=self.channels.igniter_output)
        ao2_var = tk.StringVar(value=self.channels.ao_pump2)
        spare_do4_var = tk.StringVar(value=self.channels.spare_do4)
        port_var = tk.StringVar(value=self.mp5y_config.port)
        port2_var = tk.StringVar(value=self.mp5y2_config.port)
        slave_var = tk.StringVar(value=str(self.mp5y_config.slave_id))
        slave2_var = tk.StringVar(value=str(self.mp5y2_config.slave_id))
        baud_var = tk.StringVar(value=str(self.mp5y_config.baudrate))
        mode_var = tk.StringVar(value=self.mp5y_config.value_mode)
        fmt_var = tk.StringVar(value=self.mp5y_config.pv_format)
        for row, label, var in (
            (1, "펌프1 AO0 (0~5V)", ao_var),
            (2, "펌프2 AO2 (0~5V)", ao2_var),
            (3, "NG PUMP AO1 (0~5V)", ng_ao_var),
            (4, "레벨 입력 DI0:5", di_var),
            (5, "밸브 출력 DO0:2", do_var),
            (6, "점화기 SSR DO3", igniter_do_var),
            (7, "Coolint P DO4", spare_do4_var),
            (8, "MP5Y COM 포트", port_var),
            (9, "MP5Y2 COM (선택)", port2_var),
            (10, "MP5Y 주소", slave_var),
            (11, "MP5Y2 주소", slave2_var),
            (12, "MP5Y Baud", baud_var),
            (13, "표시모드 (frequency_hz/flow_ccpm)", mode_var),
            (14, "PV포맷 (int16/int32/dec32)", fmt_var),
        ):
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(
                row=row, column=0, sticky="w", pady=3 if self.touch_mode else 8
            )
            ttk.Entry(frame, textvariable=var, width=28).grid(row=row, column=1, padx=(12, 0))

        def apply() -> None:
            self.channels.ao_pump = ao_var.get().strip() or self.channels.ao_pump
            self.channels.ao_pump2 = ao2_var.get().strip() or self.channels.ao_pump2
            self.channels.ao_ng_pump = (
                ng_ao_var.get().strip() or self.channels.ao_ng_pump
            )
            self.channels.level_inputs = di_var.get().strip() or self.channels.level_inputs
            self.channels.valve_outputs = do_var.get().strip() or self.channels.valve_outputs
            self.channels.igniter_output = igniter_do_var.get().strip() or self.channels.igniter_output
            self.channels.spare_do4 = spare_do4_var.get().strip() or self.channels.spare_do4
            # DO4 is reserved for Coolint P; do not keep a legacy FX mapping on line4.
            if (self.channels.inverter_run or "").endswith("/port0/line4"):
                self.channels.inverter_run = ""
            self.daq.channels = self.channels
            self.mp5y_config.port = port_var.get().strip() or "COM3"
            self.mp5y2_config.port = port2_var.get().strip() or "COM4"
            try:
                self.mp5y_config.slave_id = int(slave_var.get().strip())
                self.mp5y2_config.slave_id = int(slave2_var.get().strip())
            except ValueError:
                messagebox.showerror("입력 오류", "MP5Y 주소는 숫자여야 합니다.")
                return
            try:
                self.mp5y_config.baudrate = int(baud_var.get().strip())
                self.mp5y2_config.baudrate = self.mp5y_config.baudrate
            except ValueError:
                messagebox.showerror("입력 오류", "Baud는 숫자여야 합니다.")
                return
            mode = mode_var.get().strip()
            if mode not in {"frequency_hz", "flow_ccpm"}:
                messagebox.showerror("입력 오류", "표시모드는 frequency_hz 또는 flow_ccpm")
                return
            fmt = fmt_var.get().strip()
            if fmt not in {"int16", "int32", "dec32"}:
                messagebox.showerror("입력 오류", "PV포맷은 int16 / int32 / dec32")
                return
            self.mp5y_config.value_mode = mode
            self.mp5y_config.pv_format = fmt
            self.mp5y2_config.value_mode = mode
            self.mp5y2_config.pv_format = fmt
            self.mp5y.close()
            self.mp5y2.close()
            self.mp5y = Mp5yService(self.mp5y_config)
            self.mp5y2 = Mp5yService(self.mp5y2_config)
            self.refresh_connection()
            dialog.destroy()

        ttk.Button(
            frame,
            text="적용",
            style="TouchStart.TButton" if self.touch_mode else "Start.TButton",
            command=apply,
        ).grid(
            row=15, column=0, columnspan=2, sticky="ew", pady=(18, 0)
        )

    # -------------------------------------------------------------- actions
    def output_ao(self) -> None:
        voltage = self.number(self.ao_voltage, "출력 전압")
        if voltage is None:
            return
        if not 0.0 <= voltage <= 5.0:
            messagebox.showerror("범위 오류", "AO 출력은 0.0 ~ 5.0 V여야 합니다.")
            return
        try:
            self.current_ao = self.daq.write_voltage(voltage)
        except DaqError as exc:
            self._show_hardware_error(exc)
            return
        self.ao_value.configure(text=f"현재 출력: {self.current_ao:.3f} V")
        self.ao_graph.add(self.current_ao)

    def zero_ao(self) -> None:
        self.ao_voltage.set("0.0")
        try:
            self.current_ao = self.daq.write_voltage(0.0)
        except DaqError as exc:
            self._show_hardware_error(exc)
            return
        self.ao_value.configure(text=f"현재 출력: {self.current_ao:.3f} V")
        self.ao_graph.add(self.current_ao)

    def output_spare_ao(self) -> None:
        voltage = self.number(self.spare_ao_voltage, "AO 2 출력 전압")
        if voltage is None:
            return
        if not 0.0 <= voltage <= 5.0:
            messagebox.showerror("범위 오류", "AO 2 출력은 0.0 ~ 5.0 V여야 합니다.")
            return
        try:
            self.current_spare_ao = self.daq.write_voltage(
                voltage, self.channels.spare_ao
            )
        except DaqError as exc:
            self._show_hardware_error(exc)
            return
        self.spare_ao_value_label.configure(
            text=f"AO 2: {self.current_spare_ao:.3f} V"
        )

    def zero_spare_ao(self) -> None:
        self.spare_ao_voltage.set("0.0")
        self.output_spare_ao()

    def set_spare_do4(self, on: bool) -> None:
        try:
            self.spare_do4_on = self.daq.write_spare_do4(on)
        except DaqError as exc:
            self._show_hardware_error(exc)
            return
        if hasattr(self, "spare_do4_status"):
            self.spare_do4_status.configure(
                text="ON" if self.spare_do4_on else "OFF",
                foreground=COLORS["ok"] if self.spare_do4_on else COLORS["accent_deep"],
            )
        if hasattr(self, "coolint_p_button"):
            self._set_coolint_p_button(self.spare_do4_on)

    def apply_ng_ao(self) -> None:
        """Apply NG PUMP AO1 voltage from the main UI."""
        if not self.daq.available:
            messagebox.showerror("NI-DAQ 오류", "NI 9264 연결을 확인하세요.")
            return
        voltage = self.number(self.ng_ao_voltage, "NG PUMP 출력 전압")
        if voltage is None:
            return
        if not 0.0 <= voltage <= 5.0:
            messagebox.showerror("범위 오류", "NG PUMP 출력은 0.0 ~ 5.0 V여야 합니다.")
            return
        try:
            self.current_ng_ao = self.daq.write_voltage(
                voltage, self.channels.ao_ng_pump
            )
        except DaqError as exc:
            self._show_hardware_error(exc)
            return
        self._refresh_ng_ao_label()

    def zero_ng_ao(self) -> None:
        self.ng_ao_voltage.set("0.0")
        self.apply_ng_ao()

    def _refresh_ng_ao_label(self) -> None:
        if getattr(self, "ng_ao_value", None) is not None:
            if self.touch_mode:
                text = f"NG PUMP: {self.current_ng_ao:.3f} V"
            else:
                text = f"NG: {self.current_ng_ao:.3f} V"
            self.ng_ao_value.configure(text=text)

    def validate_feedback(self) -> bool:
        for var, label in (
            (self.fb_pulse_ml, "펄스정수"),
            (self.sv, "펌프1 SV"),
            (self.sv2, "펌프2 SV"),
            (self.p_gain, "P Gain"),
            (self.i_gain, "I Gain"),
            (self.lpf_cutoff, "LPF"),
        ):
            if self.number(var, label) is None:
                return False
        pulse = self.number_silent(self.fb_pulse_ml, 0)
        if pulse <= 0:
            messagebox.showerror("입력 오류", "펄스정수는 0보다 커야 합니다.")
            return False
        return True

    def toggle_feedback(self) -> None:
        if not self.feedback_running and not self.validate_feedback():
            return
        if not self.feedback_running and not self.daq.available:
            messagebox.showerror(
                "NI-DAQ 오류",
                "피드백 제어는 NI 9264(AO0/AO2) 연결이 필요합니다.",
            )
            return
        if not self.feedback_running and not self._mp5y_ok:
            messagebox.showerror(
                "MP5Y 통신 오류",
                "펌프1 유량 피드백에는 MP5Y 응답이 필요합니다.\n"
                "펌프2는 펄스미터 미연결 시에도 AO2 제어가 가능합니다.",
            )
            return
        if not self.feedback_running:
            self.feedback_running = True
            self.pi.reset()
            self.pi2.reset()
            self.lpf.reset()
            self.lpf2.reset()
            self._last_loop_at = time.monotonic()
            self._feedback_started_wall = datetime.now()
            self._feedback_timer_started_at = time.monotonic()
            self._refresh_control_start_label()
            self.feedback_graph.clear()
            self.feedback_ao_graph.clear()
            if hasattr(self, "feedback_graph2"):
                self.feedback_graph2.clear()
                self.feedback_ao_graph2.clear()
            sv = self.number_silent(self.sv, 0.0)
            sv2 = self.number_silent(self.sv2, 0.0)
            scale_max = max(200.0, max(sv, sv2) * 1.5)
            self.feedback_graph.set_scale(0, scale_max, "cc/min")
            if hasattr(self, "feedback_graph2"):
                self.feedback_graph2.set_scale(0, scale_max, "cc/min")
        else:
            self._stop_feedback_timer()
            self.feedback_running = False
            try:
                self.current_ao = self.daq.write_voltage(0.0)
                self.current_ao2 = self.daq.write_voltage(
                    0.0, self.channels.ao_pump2
                )
            except DaqError as exc:
                self._safe_stop(exc)
                return
            self.output_value.configure(text=f"AO0: {self.current_ao:.3f} V")
            if hasattr(self, "output_value2"):
                self.output_value2.configure(
                    text=f"AO2: {self.current_ao2:.3f} V"
                )

        self.feedback_button.configure(
            text="제어 중지" if self.feedback_running else "제어 시작",
            style=(
                "TouchStop.TButton"
                if self.touch_mode and self.feedback_running
                else "TouchStart.TButton"
                if self.touch_mode
                else "Stop.TButton"
                if self.feedback_running
                else "Start.TButton"
            ),
        )

    def _start_level_control_automatically(self) -> bool:
        """Start level control on a new NI 9422/9477 connection."""
        try:
            # Always establish a fail-safe closed output before the first scan.
            self.valve_commands = self.daq.write_valves([False, False, False])
        except DaqError as exc:
            self.level_running = False
            self.level_master_status.configure(text=f"자동 기동 실패 · {exc}")
            return False
        self.level_running = True
        self.level_master_status.configure(text="자동 제어 중")
        return True

    # --------------------------------------------------------------- loops
    def refresh_connection(self) -> None:
        self.daq.check_connection()
        self._refresh_tc_status()
        ao_ok = self.daq.available
        level_ok = self.daq.level_available
        if level_ok and not self._level_was_available:
            self._level_was_available = self._start_level_control_automatically()
        elif not level_ok:
            self.level_running = False
            self._level_was_available = False
            self.valve_commands = [False, False, False]
            self.level_master_status.configure(text="대기 · 밸브 닫힘")
            self._render_level_states([False] * 6, ["대기"] * 3)
        # Keep the Modbus session open once it works. Repeated open/close every
        # 2s is unreliable on many USB-RS485 adapters and looked like "응답 없음".
        self._mp5y_port_present = self.mp5y.port_present()
        mp_ok = self.mp5y.check_connection()
        if not mp_ok:
            self.mp5y.close()
        self._mp5y_ok = mp_ok
        self._paint_mp5y_lamp()
        self._mp5y2_port_present = self.mp5y2.port_present()
        mp2_ok = self.mp5y2.check_connection()
        self.mp5y2.close()
        self._mp5y2_ok = mp2_ok
        self._paint_mp5y2_lamp()

        connected = ao_ok or level_ok or mp_ok or mp2_ok
        self._link_ok = connected
        if ao_ok and level_ok and mp_ok and mp2_ok:
            self._status_base_color = COLORS["ok"]
            text = (
                f"NI DAQ + {self.mp5y_config.port}/{self.mp5y2_config.port} MP5Y 연결됨"
            )
        elif ao_ok and level_ok and mp_ok:
            self._status_base_color = COLORS["warn"]
            if self._mp5y2_port_present:
                text = (
                    f"NI DAQ 연결됨 · {self.mp5y2_config.port} 연결 / "
                    "MP5Y2 응답 없음"
                )
            else:
                text = f"NI DAQ 연결됨 · {self.mp5y2_config.port} 미연결"
        elif ao_ok and level_ok:
            self._status_base_color = COLORS["warn"]
            if self._mp5y_port_present:
                detail = self.mp5y.error or "MP5Y 응답 없음"
                text = f"NI DAQ 연결됨 · {self.mp5y_config.port} / {detail}"
            else:
                text = f"NI DAQ 연결됨 · {self.mp5y_config.port} 미연결"
        elif mp_ok and ao_ok:
            self._status_base_color = COLORS["warn"]
            text = f"유량/AO OK · {self.daq.error}"
        elif ao_ok or level_ok:
            self._status_base_color = COLORS["warn"]
            text = f"NI 부분연결 · {self.daq.error}"
        elif mp_ok:
            self._status_base_color = COLORS["warn"]
            text = f"MP5Y OK / NI: {self.daq.error}"
        elif self._mp5y_port_present:
            self._status_base_color = COLORS["warn"]
            detail = self.mp5y.error or "MP5Y 응답 없음"
            text = f"{self.mp5y_config.port} / {detail} · NI: {self.daq.error}"
        else:
            self._status_base_color = COLORS["bad"]
            text = self.daq.error if self.daq.error else "연결 확인 필요 (COM / NI)"

        if not connected:
            self.status_on = False
            self._paint_status_lamp(COLORS["bad"], outline="#9B2C2C")
        if getattr(self.daq, "simulated", False):
            text = f"시뮬레이션 모드 · {text}"
        self.status_label.configure(text=text)
        job = getattr(self, "_refresh_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_job = self.after(self.REDISCOVER_MS, self.refresh_connection)

    def _blink_status_lamp(self) -> None:
        """Blink the large DAQ/link lamp so connection state is obvious."""
        if self._link_ok:
            self.status_on = not self.status_on
            if self._status_base_color == COLORS["ok"]:
                color = COLORS["ok"] if self.status_on else "#A8D8C0"
                outline = "#1F7A4D" if self.status_on else "#7FB89A"
            elif self._status_base_color == COLORS["warn"]:
                color = COLORS["warn"] if self.status_on else "#F2D19A"
                outline = "#B7791F" if self.status_on else "#D6B27A"
            else:
                color = self._status_base_color
                outline = "#9B2C2C"
            self._paint_status_lamp(color, outline=outline)
        if getattr(self, "mp5y_canvas", None) is not None:
            self._paint_mp5y_lamp(blink=self.status_on if self._mp5y_ok else False)
        if getattr(self, "mp5y2_canvas", None) is not None:
            self._paint_mp5y2_lamp(blink=self.status_on if self._mp5y2_ok else False)
        self.after(400, self._blink_status_lamp)

    def _paint_status_lamp(self, color: str, outline: str = "") -> None:
        self.status_canvas.itemconfigure(
            self.status_dot,
            fill=color,
            outline=outline or color,
        )

    def _paint_mp5y_lamp(self, blink: bool = True) -> None:
        """Update dedicated pulse-meter (MP5Y) communication lamp."""
        if getattr(self, "mp5y_canvas", None) is None:
            return
        if self._mp5y_ok:
            color = COLORS["ok"] if blink else "#A8D8C0"
            outline = "#1F7A4D" if blink else "#7FB89A"
            text = "통신 ON"
        elif self._mp5y_port_present:
            color = COLORS["warn"]
            outline = "#B7791F"
            detail = self.mp5y.error or "응답 없음"
            text = f"{self.mp5y_config.port} · {detail}"
        else:
            color = COLORS["bad"]
            outline = "#9B2C2C"
            text = f"{self.mp5y_config.port} 미연결"
        self.mp5y_canvas.itemconfigure(self.mp5y_lamp, fill=color, outline=outline)
        if getattr(self, "mp5y_status_label", None) is not None:
            self.mp5y_status_label.configure(text=text)

    def _paint_mp5y2_lamp(self, blink: bool = True) -> None:
        if getattr(self, "mp5y2_canvas", None) is None:
            return
        if self._mp5y2_ok:
            color = COLORS["ok"] if blink else "#A8D8C0"
            outline = "#1F7A4D" if blink else "#7FB89A"
            text = "통신 ON"
        elif self._mp5y2_port_present:
            color = COLORS["warn"]
            outline = "#B7791F"
            text = f"{self.mp5y2_config.port} 연결 · 응답 없음"
        else:
            color = COLORS["bad"]
            outline = "#9B2C2C"
            text = f"{self.mp5y2_config.port} 미연결"
        self.mp5y2_canvas.itemconfigure(self.mp5y2_lamp, fill=color, outline=outline)
        if getattr(self, "mp5y2_status_label", None) is not None:
            self.mp5y2_status_label.configure(text=text)

    def update_loop(self) -> None:
        daq_failed = False
        try:
            if self.feedback_running:
                self._update_feedback()
        except Mp5yError as exc:
            self._stop_mp5y_control(exc)
        except DaqError as exc:
            daq_failed = True
            self._safe_stop(exc)
        except Exception as exc:  # noqa: BLE001
            daq_failed = True
            self._safe_stop(RuntimeError(f"제어 루프 오류: {exc}"))

        try:
            if daq_failed:
                return
            if self.level_running:
                self._update_levels()
            self._update_tc_monitor()
        except DaqError as exc:
            self._safe_stop(exc)
        except Exception as exc:  # noqa: BLE001
            self._safe_stop(RuntimeError(f"DAQ 루프 오류: {exc}"))
        finally:
            self.after(self.POLL_MS, self.update_loop)

    def _stop_mp5y_control(self, error: Exception) -> None:
        """Stop only flow-dependent control; keep independent DAQ I/O alive."""
        self._stop_feedback_timer()
        self.feedback_running = False
        stop_error = ""
        try:
            self.current_ao = self.daq.write_voltage(0.0)
        except DaqError as exc:
            stop_error = f" / AO0 정지 실패: {exc}"
        try:
            self.current_ao2 = self.daq.write_voltage(0.0, self.channels.ao_pump2)
        except DaqError as exc:
            stop_error += f" / AO2 정지 실패: {exc}"
        self.mp5y.close()
        self.mp5y2.close()
        self._mp5y_ok = False
        self._mp5y_port_present = self.mp5y.port_present()
        self._mp5y2_ok = False
        self._mp5y2_port_present = self.mp5y2.port_present()
        self._paint_mp5y_lamp(blink=False)
        self._paint_mp5y2_lamp(blink=False)
        self.feedback_button.configure(
            text="제어 시작",
            style="TouchStart.TButton" if self.touch_mode else "Start.TButton",
        )
        self.output_value.configure(text=f"AO0: {self.current_ao:.3f} V")
        if hasattr(self, "output_value2"):
            self.output_value2.configure(text=f"AO2: {self.current_ao2:.3f} V")
        port_text = (
            f"{self.mp5y_config.port} 연결 / MP5Y 응답 없음"
            if self._mp5y_port_present
            else f"{self.mp5y_config.port} 미연결"
        )
        port2_text = (
            f"{self.mp5y2_config.port} 연결 / MP5Y2 응답 없음"
            if self._mp5y2_port_present
            else f"{self.mp5y2_config.port} 미연결"
        )
        self.status_label.configure(
            text=f"{port_text} · {port2_text} · NI DAQ 제어는 계속 사용 가능{stop_error}"
        )

    def _show_hardware_error(self, error: Exception) -> None:
        self._link_ok = False
        self._mp5y_ok = False
        self._mp5y2_ok = False
        self._paint_status_lamp(COLORS["bad"], outline="#9B2C2C")
        self._paint_mp5y_lamp(blink=False)
        self._paint_mp5y2_lamp(blink=False)
        self.status_label.configure(text=str(error))
        messagebox.showerror("하드웨어 오류", str(error))

    def _safe_stop(self, error: Exception) -> None:
        self._stop_feedback_timer()
        self.feedback_running = False
        self.level_running = False
        self.igniter_on = False
        stop_error = ""
        try:
            self.current_ao = self.daq.write_voltage(0.0)
        except DaqError as exc:
            stop_error = f" / 0 V 출력 실패: {exc}"
        try:
            self.current_ng_ao = self.daq.write_voltage(
                0.0, self.channels.ao_ng_pump
            )
        except DaqError as exc:
            stop_error += f" / NG PUMP 0 V 실패: {exc}"
        try:
            self.current_ao2 = self.daq.write_voltage(0.0, self.channels.ao_pump2)
        except DaqError as exc:
            stop_error += f" / AO2 0 V 실패: {exc}"
        try:
            self.valve_commands = self.daq.write_valves([False, False, False])
        except DaqError as exc:
            stop_error += f" / 밸브 닫힘 실패: {exc}"
        try:
            self.daq.write_igniter(False)
        except DaqError as exc:
            stop_error += f" / 점화기 SSR OFF 실패: {exc}"
        try:
            self.spare_do4_on = self.daq.write_spare_do4(False)
        except DaqError as exc:
            stop_error += f" / DO 4 OFF 실패: {exc}"
        self.mp5y.close()
        self.mp5y2.close()
        self.feedback_button.configure(
            text="제어 시작",
            style="TouchStart.TButton" if self.touch_mode else "Start.TButton",
        )
        if hasattr(self, "igniter_button"):
            self._set_igniter_button(False)
        if hasattr(self, "coolint_p_button"):
            self._set_coolint_p_button(False)
        self.level_master_status.configure(text="안전 정지 · 모든 밸브 닫힘")
        self._render_level_states([False] * 6, ["오류 · 안전 닫힘"] * 3)
        self.output_value.configure(text=f"AO0: {self.current_ao:.3f} V")
        if hasattr(self, "output_value2"):
            self.output_value2.configure(text=f"AO2: {self.current_ao2:.3f} V")
        self._refresh_ng_ao_label()
        self._link_ok = False
        self._mp5y_ok = False
        self._mp5y2_ok = False
        self._paint_status_lamp(COLORS["bad"], outline="#9B2C2C")
        self._paint_mp5y_lamp(blink=False)
        self._paint_mp5y2_lamp(blink=False)
        self.status_label.configure(text=f"안전 정지: {error}{stop_error}")

    def toggle_igniter(self) -> None:
        """Manual ignition SSR toggle (DO3)."""
        if not self.daq.level_available:
            # Level availability ~= NI 9422/9477 present; keeps UX consistent.
            messagebox.showerror("NI-DAQ 오류", "NI 9422/9477 연결을 확인하세요.")
            return
        requested = not self.igniter_on
        self.igniter_on = requested
        try:
            self.daq.write_igniter(self.igniter_on)
        except DaqError as exc:
            self.igniter_on = False
            self._show_hardware_error(exc)
            self._set_igniter_button(False)
            return
        self._set_igniter_button(self.igniter_on)

    def _set_igniter_button(self, on: bool) -> None:
        if self.touch_mode:
            style = "TouchStart.TButton" if on else "TouchStop.TButton"
        else:
            style = "Start.TButton" if on else "Stop.TButton"
        self.igniter_button.configure(text=("IGN ON" if on else "IGN OFF"), style=style)

    def toggle_coolint_p(self) -> None:
        """Manual Coolint P pump toggle (DO4)."""
        if not self.daq.level_available:
            messagebox.showerror("NI-DAQ 오류", "NI 9422/9477 연결을 확인하세요.")
            return
        self.set_spare_do4(not self.spare_do4_on)

    def _set_coolint_p_button(self, on: bool) -> None:
        if self.touch_mode:
            style = "TouchStart.TButton" if on else "TouchStop.TButton"
        else:
            style = "Start.TButton" if on else "Stop.TButton"
        self.coolint_p_button.configure(
            text=("Coolint P ON" if on else "Coolint P OFF"), style=style
        )

    def _update_levels(self) -> None:
        if self.io_test_active:
            self.level_master_status.configure(text="I/O TEST · 레벨 자동제어 일시정지")
            return
        try:
            values = self.daq.read_levels()
            roles = (ValveRole.SUPPLY, ValveRole.DRAIN, ValveRole.DRAIN)
            decisions = []
            commands = []
            for index, role in enumerate(roles):
                high = values[index * 2]
                low = values[index * 2 + 1]
                decision = decide_valve(
                    role, high, low, self.valve_commands[index],
                    sensor_type=self.valve_sensor_types[index],
                )
                decisions.append(decision)
                commands.append(decision.opened)
            self.valve_commands = self.daq.write_valves(commands)
            self._render_level_states(values, [decision.state for decision in decisions])
            if any(decision.fault for decision in decisions):
                self.level_master_status.configure(text="센서 충돌 · 안전 닫힘")
            else:
                self.level_master_status.configure(text="자동 제어 중")
        except DaqError as exc:
            self.level_master_status.configure(
                text=f"레벨 I/O 오류 · {exc} · 재시도"
            )
        except Exception as exc:  # noqa: BLE001
            self.level_master_status.configure(
                text=f"레벨 루프 오류 · {exc} · 재시도"
            )

    def _refresh_control_start_label(self) -> None:
        if getattr(self, "control_start_label", None) is None:
            return
        if self._feedback_started_wall is not None:
            text = self._feedback_started_wall.strftime("%H:%M:%S")
        else:
            text = "--:--:--"
        self.control_start_label.configure(text=f"제어 시작  {text}")

    def _stop_feedback_timer(self) -> None:
        if self._feedback_timer_started_at is not None:
            self._feedback_elapsed_s += max(
                0.0, time.monotonic() - self._feedback_timer_started_at
            )
            self._feedback_timer_started_at = None

    def reset_injection_time(self) -> None:
        """Clear accumulated water injection time, including while running."""
        self._feedback_elapsed_s = 0.0
        self._feedback_timer_started_at = (
            time.monotonic() if self.feedback_running else None
        )
        self._refresh_injection_time()

    def _refresh_injection_time(self) -> None:
        elapsed = self._feedback_elapsed_s
        if self._feedback_timer_started_at is not None:
            elapsed += max(0.0, time.monotonic() - self._feedback_timer_started_at)
        total_seconds = int(elapsed)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if getattr(self, "injection_time_label", None) is not None:
            clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            if self.touch_mode:
                self.injection_time_label.configure(
                    text=f"물 주입시간  {clock}", style="ValueSmall.TLabel"
                )
            else:
                self.injection_time_label.configure(
                    text=clock, style="InjectionTime.TLabel"
                )

    def _tick_process_clock(self) -> None:
        """Refresh wall clock display."""
        if getattr(self, "clock_label", None) is not None:
            self.clock_label.configure(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self._refresh_injection_time()
        self.after(250, self._tick_process_clock)

    def _render_level_states(self, values: list[bool], states: list[str]) -> None:
        for index in range(3):
            high = values[index * 2]
            low = values[index * 2 + 1]
            opened = self.valve_commands[index]
            self.level_high_labels[index].configure(
                text=(
                    f"● HIGH  {'ON' if high else 'OFF'}"
                    if self.touch_mode
                    else f"H {'ON' if high else 'OFF'}"
                ),
                foreground=COLORS["bad"] if high else COLORS["muted"],
            )
            self.level_low_labels[index].configure(
                text=(
                    f"● LOW   {'ON' if low else 'OFF'}"
                    if self.touch_mode
                    else f"L {'ON' if low else 'OFF'}"
                ),
                foreground=COLORS["warn"] if low else COLORS["muted"],
            )
            self.valve_status_labels[index].configure(
                text="열림" if opened else "닫힘",
                foreground=COLORS["ok"] if opened else COLORS["accent_deep"],
            )
            self.level_logic_labels[index].configure(text=states[index])

    def _apply_mp5y_options(self, pulse_ml: float) -> None:
        self.mp5y_config.pulse_ml = pulse_ml
        text = self.decimal_places.get().strip().lower()
        if text in {"", "auto", "dot"}:
            self.mp5y_config.decimal_places = None
        else:
            try:
                places = int(text)
            except ValueError:
                places = -1
            self.mp5y_config.decimal_places = places if 0 <= places <= 4 else None
        self.mp5y.config = self.mp5y_config

    def _apply_mp5y2_options(self, pulse_ml: float) -> None:
        self.mp5y2_config.pulse_ml = pulse_ml
        text = self.decimal_places.get().strip().lower()
        if text in {"", "auto", "dot"}:
            self.mp5y2_config.decimal_places = None
        else:
            try:
                places = int(text)
            except ValueError:
                places = -1
            self.mp5y2_config.decimal_places = places if 0 <= places <= 4 else None
        self.mp5y2.config = self.mp5y2_config

    def _read_mp5y(self, pulse_ml: float) -> tuple[float, float, int, int]:
        self._apply_mp5y_options(pulse_ml)
        try:
            return self.mp5y.read_flow(pulse_ml=pulse_ml)
        except Mp5yError:
            # Offline / missing converter: keep the UI usable in simulation.
            # If AO hardware is live, fail closed so the pump is not driven
            # from a fake PV.
            if self.daq.available:
                raise
            return self.mp5y.simulate_flow(pulse_ml=pulse_ml)

    def _read_mp5y2(self, pulse_ml: float) -> tuple[float, float, int, int]:
        """Read pump2 flow; optional meter returns zero PV when unplugged."""
        if not self.mp5y2.port_present():
            return (0.0, float("nan"), 0, 0)
        self._apply_mp5y2_options(pulse_ml)
        try:
            return self.mp5y2.read_flow(pulse_ml=pulse_ml)
        except Mp5yError:
            if self.daq.available:
                return (0.0, float("nan"), 0, 0)
            return self.mp5y2.simulate_flow(pulse_ml=pulse_ml)

    def _update_feedback(self) -> None:
        pulse_ml = self.number_silent(self.fb_pulse_ml, DEFAULT_PULSE_ML)
        raw_flow, hz, _raw, _dot = self._read_mp5y(pulse_ml)
        raw_flow2, hz2, _raw2, _dot2 = self._read_mp5y2(pulse_ml)
        now = time.monotonic()
        dt = max(now - (self._last_loop_at or now), 0.001)
        self._last_loop_at = now

        cutoff = self.number_silent(self.lpf_cutoff, 0.8)
        pv = self.lpf.update(raw_flow, dt, cutoff)
        pv2 = self.lpf2.update(raw_flow2, dt, cutoff)
        self.filtered_flow = pv
        self.filtered_flow2 = pv2
        sv = self.number_silent(self.sv, 0.0)
        sv2 = self.number_silent(self.sv2, 0.0)
        p_gain = self.number_silent(self.p_gain, 0.02)
        i_gain = self.number_silent(self.i_gain, 0.005)

        ao = self.pi.update(sv, pv, p_gain, i_gain, dt)
        ao2 = self.pi2.update(sv2, pv2, p_gain, i_gain, dt)
        self.current_ao = self.daq.write_voltage(ao)
        self.current_ao2 = self.daq.write_voltage(ao2, self.channels.ao_pump2)

        hz_text = "—" if math.isnan(hz) else f"{hz:.2f}"
        hz2_text = "—" if math.isnan(hz2) else f"{hz2:.2f}"
        if self.touch_mode:
            self.focus_pv.configure(text=f"PV  {pv:.1f}")
            self.focus_pv2.configure(text=f"PV  {pv2:.1f}")
            self.focus_sv.configure(text=f"SV  {sv:.1f}")
            if hasattr(self, "focus_sv2"):
                self.focus_sv2.configure(text=f"SV  {sv2:.1f}")
            self.focus_err.configure(text=f"오차: {sv - pv:+.1f} cc/min")
            if hasattr(self, "focus_err2"):
                self.focus_err2.configure(text=f"오차: {sv2 - pv2:+.1f} cc/min")
            if self.mp5y_config.value_mode == "flow_ccpm":
                self.fb_hz.configure(text=f"MP5Y 표시: {raw_flow:.2f} cc/min")
            else:
                self.fb_hz.configure(text=f"MP5Y 표시: {hz_text} Hz")
            if not self._mp5y2_port_present:
                self.fb_hz2.configure(text="MP5Y2: 미연결")
            elif self.mp5y2_config.value_mode == "flow_ccpm":
                self.fb_hz2.configure(text=f"MP5Y2 표시: {raw_flow2:.2f} cc/min")
            else:
                self.fb_hz2.configure(text=f"MP5Y2 표시: {hz2_text} Hz")
            self.output_value.configure(text=f"AO0: {self.current_ao:.3f} V")
            if hasattr(self, "output_value2"):
                self.output_value2.configure(text=f"AO2: {self.current_ao2:.3f} V")
        else:
            self.focus_pv.configure(text=f"{pv:.1f}")
            self.focus_pv2.configure(text=f"{pv2:.1f}")
            self.focus_err.configure(text=f"Δ {sv - pv:+.1f}")
            if hasattr(self, "focus_err2"):
                self.focus_err2.configure(text=f"Δ {sv2 - pv2:+.1f}")
            if self.mp5y_config.value_mode == "flow_ccpm":
                self.fb_hz.configure(text=f"{raw_flow:.1f} cc/min")
            else:
                self.fb_hz.configure(text=f"{hz_text} Hz")
            if not self._mp5y2_port_present:
                self.fb_hz2.configure(text="미연결")
            elif self.mp5y2_config.value_mode == "flow_ccpm":
                self.fb_hz2.configure(text=f"{raw_flow2:.1f} cc/min")
            else:
                self.fb_hz2.configure(text=f"{hz2_text} Hz")
            self.output_value.configure(text=f"AO0  {self.current_ao:.3f} V")
            if hasattr(self, "output_value2"):
                self.output_value2.configure(text=f"AO2  {self.current_ao2:.3f} V")
        scale_max = max(200.0, max(sv, sv2) * 1.5)
        self.feedback_graph.set_scale(0, scale_max, "cc/min")
        if hasattr(self, "feedback_graph2"):
            self.feedback_graph2.set_scale(0, scale_max, "cc/min")
        now_mono = time.monotonic()
        redraw = (now_mono - self._last_graph_draw_at) * 1000.0 >= self.GRAPH_MS
        self.feedback_graph.add(pv, sv, redraw=redraw)
        self.feedback_ao_graph.add(self.current_ao, redraw=redraw)
        if hasattr(self, "feedback_graph2"):
            self.feedback_graph2.add(pv2, sv2, redraw=redraw)
            self.feedback_ao_graph2.add(self.current_ao2, redraw=redraw)
        if redraw:
            self._last_graph_draw_at = now_mono

    # ------------------------------------------------------------- settings
    def save_settings(self) -> None:
        self._sync_tc_inputs(show_error=False)
        self._sync_valve_names()
        data = {
            "pulse_ml": self.fb_pulse_ml.get(),
            "sv": self.sv.get(),
            "sv2": self.sv2.get(),
            "p_gain": self.p_gain.get(),
            "i_gain": self.i_gain.get(),
            "lpf_cutoff": self.lpf_cutoff.get(),
            "ao_pump": self.channels.ao_pump,
            "ao_pump2": self.channels.ao_pump2,
            "ao_ng_pump": self.channels.ao_ng_pump,
            "spare_ao": self.channels.spare_ao,
            "ng_ao_voltage": self.ng_ao_voltage.get(),
            "level_inputs": self.channels.level_inputs,
            "valve_outputs": self.channels.valve_outputs,
            "igniter_output": self.channels.igniter_output,
            "spare_do4": self.channels.spare_do4,
            "inverter_run": self.channels.inverter_run,
            "tc_k_inputs": self.channels.tc_k_inputs,
            "tc_t_inputs": self.channels.tc_t_inputs,
            "tc_names": self.tc_names,
            "valve_names": self.valve_names,
            "valve_sensor_types": [st.value for st in self.valve_sensor_types],
            "mp5y_port": self.mp5y_config.port,
            "mp5y2_port": self.mp5y2_config.port,
            "mp5y_slave_id": self.mp5y_config.slave_id,
            "mp5y2_slave_id": self.mp5y2_config.slave_id,
            "mp5y_baudrate": self.mp5y_config.baudrate,
            "mp5y_value_mode": self.mp5y_config.value_mode,
            "mp5y_pv_format": self.mp5y_config.pv_format,
            "decimal_places": self.decimal_places.get(),
        }
        try:
            self.SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
            messagebox.showinfo("저장", "설정을 저장했습니다.")
        except OSError as exc:
            messagebox.showerror("저장 실패", str(exc))

    def load_settings(self) -> None:
        if not self.SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(self.SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.pulse_ml.set(str(data.get("pulse_ml", "0.46")))
        self.fb_pulse_ml.set(str(data.get("pulse_ml", "0.46")))
        self.sv.set(str(data.get("sv", "0.0")))
        self.sv2.set(str(data.get("sv2", "0.0")))
        if hasattr(self, "sv_input"):
            self.sv_input.set(self.sv.get())
        self.p_gain.set(str(data.get("p_gain", "0.020")))
        self.i_gain.set(str(data.get("i_gain", "0.005")))
        self.lpf_cutoff.set(str(data.get("lpf_cutoff", "0.8")))
        self.channels.ao_pump = str(data.get("ao_pump", self.channels.ao_pump))
        self.channels.ao_pump2 = str(data.get("ao_pump2", self.channels.ao_pump2))
        self.channels.ao_ng_pump = str(
            data.get("ao_ng_pump", self.channels.ao_ng_pump)
        )
        self.channels.spare_ao = str(data.get("spare_ao", self.channels.spare_ao))
        self.ng_ao_voltage.set(str(data.get("ng_ao_voltage", self.ng_ao_voltage.get())))
        self.channels.level_inputs = str(data.get("level_inputs", self.channels.level_inputs))
        self.channels.valve_outputs = str(data.get("valve_outputs", self.channels.valve_outputs))
        self.channels.igniter_output = str(data.get("igniter_output", self.channels.igniter_output))
        self.channels.spare_do4 = str(data.get("spare_do4", self.channels.spare_do4))
        legacy_inv = str(data.get("inverter_run", self.channels.inverter_run))
        # Old builds mapped DO4 to iG5A FX. Free line4 for Coolint P.
        if legacy_inv.endswith("/port0/line4"):
            if "spare_do4" not in data:
                self.channels.spare_do4 = legacy_inv
            self.channels.inverter_run = ""
        else:
            self.channels.inverter_run = legacy_inv
        self.channels.tc_k_inputs = str(
            data.get("tc_k_inputs", self.channels.tc_k_inputs)
        )
        self.channels.tc_t_inputs = str(
            data.get("tc_t_inputs", self.channels.tc_t_inputs)
        )
        tc_names = data.get("tc_names", self.tc_names)
        if isinstance(tc_names, list) and len(tc_names) == 10:
            self.tc_names = [str(name) for name in tc_names]
        valve_names = data.get("valve_names", self.valve_names)
        if isinstance(valve_names, list) and len(valve_names) == 3:
            self.valve_names = [str(name) for name in valve_names]
            for index, name in enumerate(self.valve_names):
                if index < len(self.valve_name_vars):
                    self.valve_name_vars[index].set(name)
        vst = data.get("valve_sensor_types")
        if isinstance(vst, list) and len(vst) == 3:
            mapping = {t.value: t for t in SensorType}
            self.valve_sensor_types = [
                mapping.get(str(v), SensorType.TWO_POINT) for v in vst
            ]
        self.daq.channels = self.channels
        self._refresh_ng_ao_label()
        self.mp5y_config.port = str(data.get("mp5y_port", "COM3"))
        self.mp5y2_config.port = str(data.get("mp5y2_port", "COM4"))
        self.mp5y_config.slave_id = int(data.get("mp5y_slave_id", 1))
        self.mp5y2_config.slave_id = int(data.get("mp5y2_slave_id", 1))
        self.mp5y_config.baudrate = int(data.get("mp5y_baudrate", 9600))
        self.mp5y2_config.baudrate = self.mp5y_config.baudrate
        self.mp5y_config.value_mode = str(data.get("mp5y_value_mode", "flow_ccpm"))
        self.mp5y2_config.value_mode = self.mp5y_config.value_mode
        self.mp5y_config.pv_format = str(data.get("mp5y_pv_format", "int16"))
        self.mp5y2_config.pv_format = self.mp5y_config.pv_format
        self.decimal_places.set(str(data.get("decimal_places", "auto")))
        self.mp5y = Mp5yService(self.mp5y_config)
        self.mp5y2 = Mp5yService(self.mp5y2_config)

    def on_close(self) -> None:
        try:
            self.feedback_running = False
            self.level_running = False
            for attr in ("_refresh_job",):
                job = getattr(self, attr, None)
                if job is not None:
                    try:
                        self.after_cancel(job)
                    except Exception:  # noqa: BLE001
                        pass
            self.daq.close()
            self.mp5y.close()
            self.mp5y2.close()
        finally:
            self.destroy()


def main() -> None:
    app = FlowControlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
