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
import sys
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

# Allow `python flow_ui/app.py` without installing as a package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow_ui.control import LowPassFilter, PIController
from flow_ui.daq_service import ChannelConfig, DaqError, DaqService
from flow_ui.flow_math import DEFAULT_PULSE_ML
from flow_ui.level_control import ValveRole, decide_valve
from flow_ui.mp5y_service import MODE_NAMES, Mp5yConfig, Mp5yError, Mp5yService


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

    def add(self, *values: float) -> None:
        for series, value in zip(self.series, values):
            series.append(float(value))
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
        width = max(self.winfo_width(), 320)
        height = max(self.winfo_height(), 110)
        left, top, right, bottom = 58, 40, width - 18, height - 28

        self.create_rectangle(1, 1, width - 2, height - 2, outline=COLORS["border"], width=1)
        self.create_text(
            left,
            16,
            text=self.plot_title,
            fill=COLORS["title"],
            anchor="w",
            font=("Segoe UI", 12, "bold"),
        )

        for i in range(5):
            y = top + (bottom - top) * i / 4
            value = self.high - (self.high - self.low) * i / 4
            self.create_line(left, y, right, y, fill=COLORS["grid"])
            label = f"{value:.0f}" if abs(self.high - self.low) >= 20 else f"{value:.1f}"
            self.create_text(
                left - 8,
                y,
                text=label,
                fill=COLORS["muted"],
                anchor="e",
                font=("Segoe UI", 9),
            )

        self.create_line(left, top, left, bottom, fill=COLORS["border"])
        self.create_line(left, bottom, right, bottom, fill=COLORS["border"])
        if self.unit:
            self.create_text(
                left,
                bottom + 14,
                text=self.unit,
                fill=COLORS["muted"],
                anchor="w",
                font=("Segoe UI", 9),
            )

        for index, ((name, color), points) in enumerate(zip(self.lines, self.series)):
            lx = right - 118
            ly = 12 + index * 18
            self.create_oval(lx, ly, lx + 10, ly + 10, fill=color, outline="")
            self.create_text(
                lx + 16,
                ly + 5,
                text=name,
                fill=COLORS["text"],
                anchor="w",
                font=("Segoe UI", 9),
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
    POLL_MS = 120

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
        self.touch_mode = screen_w <= 1100 and screen_h <= 700
        if self.touch_mode:
            width, height = screen_w, screen_h
            self.geometry(f"{width}x{height}+0+0")
            self.minsize(min(800, width), min(480, height))
        else:
            width = min(1120, int(screen_w * 0.92))
            height = min(640, int(screen_h * 0.82))
            self.geometry(f"{width}x{height}")
            self.minsize(min(900, width), min(520, height))
        self.configure(bg=COLORS["bg"])
        self._apply_window_icon()

        self.channels = ChannelConfig()
        self.mp5y_config = Mp5yConfig(port="COM3")
        self.daq = DaqService(self.channels)
        self.mp5y = Mp5yService(self.mp5y_config)
        self.lpf = LowPassFilter()
        self.pi = PIController(0.0, 5.0)

        self.active_page = "feedback"
        self.monitor_running = False
        self.feedback_running = False
        self.level_running = False
        self._level_was_available = False
        self._monitor_popup: tk.Toplevel | None = None
        self._ao_popup: tk.Toplevel | None = None
        self._keypad_popup: tk.Toplevel | None = None
        self.valve_commands = [False, False, False]
        self.igniter_on = False
        self.status_on = False
        self.current_ao = 0.0
        self.filtered_flow = 0.0
        self._last_loop_at: float | None = None

        self._build_style()
        self._build_layout()
        self.load_settings()
        self.show_page("feedback")
        self._link_ok = False
        self._status_base_color = COLORS["bad"]
        self.refresh_connection()
        self.after(400, self._blink_status_lamp)
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
            font=("Segoe UI", 22, "bold"),
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
            font=("Segoe UI", 14, "bold"),
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
            font=("Segoe UI", 18, "bold"),
        )
        style.configure(
            "FlameStatus.TLabel",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "Compact.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 6),
        )
        style.configure(
            "TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
            background=COLORS["accent_soft"],
            foreground=COLORS["title"],
            borderwidth=0,
        )
        style.map("TButton", background=[("active", "#BFDDE2")])
        style.configure("Nav.TButton", background="#E8EEF4")
        style.map("Nav.TButton", background=[("active", "#D6E2EC")])
        style.configure("NavActive.TButton", background=COLORS["accent"], foreground="#FFFFFF")
        style.map("NavActive.TButton", background=[("active", COLORS["accent_deep"])])
        style.configure("Start.TButton", background="#BFE4D5", foreground="#174B3A", padding=(12, 11))
        style.map("Start.TButton", background=[("active", "#9FD4C1")])
        style.configure("Stop.TButton", background="#F1D5D8", foreground="#722F37", padding=(12, 11))
        style.map("Stop.TButton", background=[("active", "#E7BDC2")])
        style.configure(
            "TEntry",
            fieldbackground=COLORS["panel_alt"],
            foreground=COLORS["title"],
            insertcolor=COLORS["title"],
            padding=7,
            borderwidth=1,
        )
        style.configure(
            "Highlight.TEntry",
            fieldbackground=COLORS["accent_soft"],
            foreground=COLORS["accent_deep"],
            insertcolor=COLORS["accent_deep"],
            font=("Segoe UI", 17, "bold"),
            padding=9,
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
        header_padding = (10, 6, 10, 4) if self.touch_mode else (14, 10, 14, 6)
        header = ttk.Frame(self, style="App.TFrame", padding=header_padding)
        header.pack(fill="x")

        titles = ttk.Frame(header, style="App.TFrame")
        titles.pack(side="left")
        title_style = "TouchTitle.TLabel" if self.touch_mode else "Title.TLabel"
        title_text = "PROCESS CONTROL" if self.touch_mode else "PROCESS CONTROL DASHBOARD"
        ttk.Label(titles, text=title_text, style=title_style).pack(anchor="w")

        status = ttk.Frame(header, style="App.TFrame")
        status.pack(side="right")
        if not self.touch_mode:
            ttk.Button(
                status,
                text="유량계 TEST",
                style="Nav.TButton",
                command=self.open_monitor_popup,
            ).pack(side="right", padx=(0, 6))
            ttk.Button(
                status,
                text="펌프 TEST",
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

        content_padding = (8, 2, 8, 4) if self.touch_mode else (14, 4, 14, 8)
        self.content = ttk.Frame(self, style="App.TFrame", padding=content_padding)
        self.content.pack(fill="both", expand=True)
        self.pages = {
            "monitor": self._create_monitor_page(),
            "ao": self._create_ao_page(),
            "feedback": self._create_feedback_page(),
        }

    def panel(self, parent: tk.Misc) -> ttk.Frame:
        wrapper = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        frame = ttk.Frame(wrapper, style="Panel.TFrame", padding=14)
        frame.pack(fill="both", expand=True)
        frame._card = wrapper  # type: ignore[attr-defined]
        return frame

    def place_panel(self, panel: ttk.Frame, **grid_kwargs) -> None:
        panel._card.grid(**grid_kwargs)  # type: ignore[attr-defined]

    def field(self, parent: tk.Misc, row: int, label: str, default: str, hint: str = "") -> tk.StringVar:
        value = tk.StringVar(value=default)
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=value, width=14)
        entry.grid(row=row, column=1, sticky="e", padx=(12, 0))
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

    def _create_monitor_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        controls = self.panel(page)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 14))
        ttk.Label(controls, text="유량 모니터", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )
        self.pulse_ml = self.field(controls, 1, "펄스정수 (ml/P)", "0.46")
        self.decimal_places = self.field(
            controls, 3, "소수점 자리", "auto", "auto 또는 0~4 (MP5Y 화면과 값이 다를 때)"
        )
        self.mon_flow = ttk.Label(controls, text="0.0 cc/min", style="Value.TLabel")
        self.mon_flow.grid(row=5, column=0, columnspan=2, sticky="w", pady=(18, 4))
        self.mon_hz = ttk.Label(controls, text="MP5Y 표시: 0.00 cc/min", style="ValueSmall.TLabel")
        self.mon_hz.grid(row=6, column=0, columnspan=2, sticky="w")
        self.mon_conv = ttk.Label(
            controls, text="환산: MP5Y 프리스케일 27.6 사용 (이중환산 없음)", style="Panel.TLabel"
        )
        self.mon_conv.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.mon_raw = ttk.Label(controls, text="MP5Y raw: 0", style="Panel.TLabel")
        self.mon_raw.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.mon_mode = ttk.Label(controls, text="모드: -", style="Panel.TLabel")
        self.mon_mode.grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.monitor_button = ttk.Button(
            controls, text="측정 시작", style="Start.TButton", command=self.toggle_monitor
        )
        self.monitor_button.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        ttk.Label(
            controls,
            text="MP5Y 화면(cc/min)을 그대로 PV로 사용\nin-A=PnP, 프리스케일 27.6",
            style="Hint.TLabel",
            wraplength=250,
            justify="left",
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(12, 0))

        graphs = ttk.Frame(page, style="App.TFrame")
        graphs.grid(row=0, column=1, sticky="nsew")
        graphs.rowconfigure((0, 1), weight=1)
        graphs.columnconfigure(0, weight=1)
        self.mon_flow_graph = TrendGraph(
            graphs, "유량 추이", [("Flow", COLORS["pv"])], 0, 200, "cc/min"
        )
        self.mon_flow_graph.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.mon_hz_graph = TrendGraph(
            graphs, "추정 주파수", [("Hz", COLORS["hz"])], 0, 20, "Hz"
        )
        self.mon_hz_graph.grid(row=1, column=0, sticky="nsew")
        return page

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
            text="9264 ao0 → 펌프 전압\nAO GND는 펌프 COM과 공통",
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
            ("flame", "화염 / 점화"),
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
            ("유량 TEST", self.open_monitor_popup),
            ("펌프 TEST", self.open_ao_popup),
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
        self.focus_sv = ttk.Label(current, text="SV  120.0", style="TouchSV.TLabel")
        self.focus_sv.pack(anchor="w")
        self.focus_err = ttk.Label(current, text="오차: 0.0 cc/min", style="ValueSmall.TLabel")
        self.focus_err.pack(anchor="w", pady=(2, 0))
        self.fb_hz = ttk.Label(current, text="MP5Y: 대기", style="Hint.TLabel")
        self.fb_hz.pack(anchor="w", pady=(2, 0))
        self.output_value = ttk.Label(current, text="AO: 0.000 V", style="ValueSmall.TLabel")
        self.output_value.pack(anchor="w", pady=(2, 3))

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
            current, "AO", [("AO", COLORS["ao"])], 0, 5, "V"
        )

        self.feedback_button = ttk.Button(
            current,
            text="피드백 제어 시작",
            style="TouchStart.TButton",
            command=self.toggle_feedback,
        )
        self.feedback_button.pack(fill="x")

        target = self.panel(section)
        self.place_panel(target, row=0, column=1, sticky="nsew", padx=(0, 5))
        ttk.Label(target, text="목표 유량 SV", style="PanelTitle.TLabel").pack(anchor="w")
        self.sv = tk.StringVar(value="120.0")
        self.touch_sv_display = ttk.Button(
            target,
            textvariable=self.sv,
            style="Touch.TButton",
            command=lambda: self.open_numeric_keypad(
                self.sv, "목표 유량 SV (cc/min)", 0.0, 9999.0, 1
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
            heading, text="DAQ 연결 대기 · 밸브 안전 닫힘", style="ValueSmall.TLabel"
        )
        self.level_master_status.pack(side="right")

        self.level_high_labels = []
        self.level_low_labels = []
        self.valve_status_labels = []
        self.level_logic_labels = []
        definitions = (
            ("밸브 1 · 급수", "LOW → 열림  /  HIGH → 닫힘"),
            ("밸브 2 · 배수", "LOW → 닫힘  /  HIGH → 열림"),
            ("밸브 3 · 배수", "LOW → 닫힘  /  HIGH → 열림"),
        )
        for index, (name, rule) in enumerate(definitions):
            card = self.panel(section)
            self.place_panel(
                card,
                row=1,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 5, 0),
            )
            ttk.Label(card, text=name, style="PanelTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=rule, style="Hint.TLabel").pack(anchor="w", pady=(2, 14))
            high = ttk.Label(card, text="●  HIGH   OFF", style="ValueSmall.TLabel")
            high.pack(anchor="w", pady=5)
            low = ttk.Label(card, text="●  LOW    OFF", style="ValueSmall.TLabel")
            low.pack(anchor="w", pady=5)
            ttk.Separator(card, orient="horizontal").pack(fill="x", pady=12)
            valve = ttk.Label(card, text="닫힘", style="TouchSV.TLabel")
            valve.pack(anchor="w")
            logic = ttk.Label(card, text="DAQ 연결 대기", style="Panel.TLabel")
            logic.pack(anchor="w", pady=(8, 0))
            self.level_high_labels.append(high)
            self.level_low_labels.append(low)
            self.valve_status_labels.append(valve)
            self.level_logic_labels.append(logic)
        return section

    def _create_touch_flame_section(self, parent: tk.Misc) -> ttk.Frame:
        section = ttk.Frame(parent, style="App.TFrame")
        section.columnconfigure(0, weight=1)
        section.rowconfigure(0, weight=1)
        card = self.panel(section)
        self.place_panel(card, row=0, column=0, sticky="nsew")

        ttk.Label(card, text="FD / IGN", style="PanelTitle.TLabel").pack(anchor="w")
        center = ttk.Frame(card, style="Panel.TFrame")
        center.pack(expand=True)
        self.flame_canvas = tk.Canvas(
            center, width=120, height=120, bg=COLORS["panel"], highlightthickness=0
        )
        self.flame_canvas.pack(side="left", padx=(0, 18))
        self.flame_ring = self.flame_canvas.create_oval(
            5, 5, 115, 115, fill="#F1F5F9", outline="#CBD5E1", width=4
        )
        self.flame_lamp = self.flame_canvas.create_oval(
            28, 28, 92, 92, fill="#CBD5E1", outline="#94A3B8", width=3
        )
        controls = ttk.Frame(center, style="Panel.TFrame")
        controls.pack(side="left")
        self.flame_label = ttk.Label(controls, text="FD", style="TouchSV.TLabel")
        self.flame_label.pack(anchor="w", pady=(0, 14))
        ign_row = ttk.Frame(controls, style="Panel.TFrame")
        ign_row.pack(anchor="w")
        self.igniter_button = ttk.Button(
            ign_row,
            text="",
            style="TouchStop.TButton",
            command=self.toggle_igniter,
            width=4,
        )
        self.igniter_button.pack(side="left")
        self.igniter_caption = ttk.Label(ign_row, text="IGN", style="TouchSV.TLabel")
        self.igniter_caption.pack(side="left", padx=(8, 0))
        return section

    def _create_desktop_feedback_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(0, weight=0)
        page.columnconfigure(1, weight=1)
        page.columnconfigure(2, weight=0)
        page.rowconfigure(0, weight=1)
        page.rowconfigure(1, weight=0)

        controls = self.panel(page)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 10))
        ttk.Label(controls, text="펌프 피드백 제어", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )

        self.fb_pulse_ml = self.field(controls, 1, "펄스정수 (ml/P)", "0.46")
        self.p_gain = self.field(controls, 3, "P Gain (V·min/cc)", "0.020")
        self.i_gain = self.field(controls, 5, "I Gain (V·min/cc·s)", "0.005")
        self.lpf_cutoff = self.field(controls, 7, "LPF 차단주파수 (Hz)", "0.8", "0 = 필터 미사용")

        self.feedback_button = ttk.Button(
            controls, text="피드백 제어 시작", style="Start.TButton", command=self.toggle_feedback
        )
        self.feedback_button.grid(row=9, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(controls, text="설정 저장", command=self.save_settings).grid(
            row=9, column=1, sticky="ew", padx=(8, 0), pady=(4, 0)
        )

        focus = self.panel(page)
        self.place_panel(focus, row=0, column=1, sticky="nsew", padx=(0, 10))
        ttk.Label(focus, text="CURRENT FLOW", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 5)
        )
        self.focus_pv = ttk.Label(focus, text="PV  0.0 cc/min", style="ValueCompact.TLabel")
        self.focus_pv.pack(anchor="w", pady=(0, 6))
        self.pv_value = self.focus_pv

        ttk.Label(focus, text="SV 목표 유량 (cc/min)", style="Panel.TLabel").pack(anchor="w")
        self.sv = tk.StringVar(value="120.0")
        sv_row = ttk.Frame(focus, style="Panel.TFrame")
        sv_row.pack(anchor="w", pady=(3, 6))
        self.sv_entry = ttk.Entry(
            sv_row,
            textvariable=self.sv,
            style="Highlight.TEntry",
            justify="center",
            width=8,
        )
        self.sv_entry.pack(side="left")
        # Kept as hidden update targets; the compact desktop card intentionally
        # shows only PV, editable SV, flame state, and ignition control.
        self.focus_err = ttk.Label(focus, text="오차: 0.0 cc/min", style="ValueSmall.TLabel")
        self.fb_hz = ttk.Label(focus, text="MP5Y: 대기", style="Hint.TLabel")
        self.output_value = ttk.Label(focus, text="AO: 0.000 V", style="ValueSmall.TLabel")

        ttk.Separator(focus, orient="horizontal").pack(fill="x", pady=(7, 7))
        flame_row = ttk.Frame(focus, style="Panel.TFrame")
        flame_row.pack(anchor="w")
        self.flame_canvas = tk.Canvas(
            flame_row, width=34, height=34, bg=COLORS["panel"], highlightthickness=0
        )
        self.flame_canvas.pack(side="left")
        self.flame_ring = self.flame_canvas.create_oval(
            2, 2, 32, 32, fill="#F1F5F9", outline="#CBD5E1", width=2
        )
        self.flame_lamp = self.flame_canvas.create_oval(
            8, 8, 26, 26, fill="#CBD5E1", outline="#94A3B8", width=2
        )
        self.flame_label = ttk.Label(flame_row, text="FD", style="FlameStatus.TLabel")
        self.flame_label.pack(side="left", padx=(6, 14))
        self.igniter_button = ttk.Button(
            flame_row,
            text="",
            style="Compact.TButton",
            command=self.toggle_igniter,
            width=3,
        )
        self.igniter_button.pack(side="left")
        self.igniter_caption = ttk.Label(flame_row, text="IGN", style="FlameStatus.TLabel")
        self.igniter_caption.pack(side="left", padx=(6, 0))

        graphs = ttk.Frame(page, style="App.TFrame", width=292)
        graphs.grid(row=0, column=2, sticky="ns")
        graphs.grid_propagate(False)
        graphs.rowconfigure((0, 1), weight=1)
        graphs.columnconfigure(0, weight=1)
        self.feedback_graph = TrendGraph(
            graphs,
            "PV / SV 유량",
            [("PV", COLORS["pv"]), ("SV", COLORS["sv"])],
            0,
            200,
            "cc/min",
        )
        self.feedback_graph.configure(height=96)
        self.feedback_graph.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.feedback_ao_graph = TrendGraph(
            graphs, "AO 펌프 전압", [("AO", COLORS["ao"])], 0, 5, "V"
        )
        self.feedback_ao_graph.configure(height=96)
        self.feedback_ao_graph.grid(row=1, column=0, sticky="nsew")

        level_section = self.panel(page)
        self.place_panel(level_section, row=1, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        top = ttk.Frame(level_section, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="레벨 / 밸브 자동 제어", style="PanelTitle.TLabel").pack(side="left")
        self.level_master_status = ttk.Label(
            top, text="DAQ 연결 대기 · 밸브 안전 닫힘", style="Panel.TLabel"
        )
        self.level_master_status.pack(side="right")

        cards = ttk.Frame(level_section, style="Panel.TFrame")
        cards.pack(fill="x", pady=(6, 0))
        for i in range(3):
            cards.columnconfigure(i, weight=1)

        self.level_high_labels = []
        self.level_low_labels = []
        self.valve_status_labels = []
        self.level_logic_labels = []
        definitions = (
            ("밸브 1 · 급수", "LOW→열림 / HIGH→닫힘"),
            ("밸브 2 · 배수", "LOW→닫힘 / HIGH→열림"),
            ("밸브 3 · 배수", "LOW→닫힘 / HIGH→열림"),
        )
        for index, (name, rule) in enumerate(definitions):
            card = ttk.Frame(cards, style="Panel.TFrame", padding=(8, 4))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 4, 0))
            ttk.Label(card, text=name, style="PanelTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=rule, style="Hint.TLabel").pack(anchor="w", pady=(1, 4))
            high = ttk.Label(card, text="● HIGH OFF", style="Panel.TLabel")
            high.pack(anchor="w")
            low = ttk.Label(card, text="● LOW  OFF", style="Panel.TLabel")
            low.pack(anchor="w", pady=(1, 4))
            valve = ttk.Label(card, text="닫힘", style="ValueSmall.TLabel")
            valve.pack(anchor="w")
            logic = ttk.Label(card, text="대기", style="Panel.TLabel")
            logic.pack(anchor="w", pady=(2, 0))
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

    def _adjust_sv(self, delta: float) -> None:
        current = self.number_silent(self.sv, 0.0)
        self._set_sv(current + delta)

    def _refresh_touch_sv(self) -> None:
        if not self.touch_mode or not hasattr(self, "focus_sv"):
            return
        value = self.number_silent(self.sv, 0.0)
        self.focus_sv.configure(text=f"SV  {value:.1f}")

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
    def open_monitor_popup(self) -> None:
        """Open MP5Y -> 유량 테스트 window."""
        # If already open, bring to front.
        if self._monitor_popup is not None and self._monitor_popup.winfo_exists():
            self._monitor_popup.lift()
            self._monitor_popup.focus_force()
            return

        popup = tk.Toplevel(self)
        self._monitor_popup = popup
        popup.title("유량계 TEST (MP5Y)")
        popup.configure(bg=COLORS["bg"])
        # Small-screen safe sizing
        screen_w = max(self.winfo_screenwidth(), 1024)
        screen_h = max(self.winfo_screenheight(), 700)
        w = min(980, int(screen_w * 0.7))
        h = min(680, int(screen_h * 0.7))
        popup.geometry(f"{w}x{h}")

        frame = ttk.Frame(popup, style="App.TFrame", padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="유량계 TEST", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 10))

        body = ttk.Frame(frame, style="App.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        controls = self.panel(body)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 14))

        # Controls (pulse constant only affects UI if you later switch modes)
        self.pulse_ml = self.field(controls, 1, "펄스정수 (ml/P)", "0.46")
        self.decimal_places = self.field(
            controls, 3, "소수점 자리", "auto", "auto 또는 0~4 (MP5Y 값과 다르면)"
        )

        self.mon_flow = ttk.Label(controls, text="0.0 cc/min", style="Value.TLabel")
        self.mon_flow.grid(row=5, column=0, columnspan=2, sticky="w", pady=(18, 4))

        self.mon_hz = ttk.Label(controls, text="MP5Y 표시: 0.00 cc/min", style="ValueSmall.TLabel")
        self.mon_hz.grid(row=6, column=0, columnspan=2, sticky="w")

        self.mon_conv = ttk.Label(
            controls,
            text="환산: MP5Y 프리스케일 27.6 사용 (이중환산 없음)",
            style="Panel.TLabel",
        )
        self.mon_conv.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.mon_raw = ttk.Label(controls, text="MP5Y raw: 0", style="Panel.TLabel")
        self.mon_raw.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.mon_mode = ttk.Label(controls, text="모드: -", style="Panel.TLabel")
        self.mon_mode.grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.monitor_button = ttk.Button(
            controls, text="측정 시작", style="Start.TButton", command=self.toggle_monitor
        )
        self.monitor_button.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(22, 0))

        ttk.Label(
            controls,
            text="유량계 → MP5Y(통신) → PV(cc/min) 표시\n"
            "※ 자동 제어와 별개입니다.",
            style="Hint.TLabel",
            wraplength=250,
            justify="left",
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(12, 0))

        graphs = ttk.Frame(body, style="App.TFrame")
        graphs.grid(row=0, column=1, sticky="nsew")
        graphs.rowconfigure(0, weight=1)
        graphs.columnconfigure(0, weight=1)

        self.mon_flow_graph = TrendGraph(graphs, "유량 추이", [("Flow", COLORS["pv"])], 0, 200, "cc/min")
        self.mon_flow_graph.grid(row=0, column=0, sticky="nsew")

        popup.protocol("WM_DELETE_WINDOW", self._close_monitor_popup)

    def _close_monitor_popup(self) -> None:
        try:
            if self.monitor_running:
                self.toggle_monitor()
        finally:
            if self._monitor_popup is not None and self._monitor_popup.winfo_exists():
                self._monitor_popup.destroy()
            self._monitor_popup = None

    def open_ao_popup(self) -> None:
        """Open NI 9264 AO manual test window."""
        if self._ao_popup is not None and self._ao_popup.winfo_exists():
            self._ao_popup.lift()
            self._ao_popup.focus_force()
            return

        popup = tk.Toplevel(self)
        self._ao_popup = popup
        popup.title("펌프 수동 전압 TEST (NI 9264 AO)")
        popup.configure(bg=COLORS["bg"])

        screen_w = max(self.winfo_screenwidth(), 800)
        screen_h = max(self.winfo_screenheight(), 480)
        w = min(900, max(760, int(screen_w * 0.75)))
        h = min(540, max(460, int(screen_h * 0.70)))
        popup.geometry(f"{w}x{h}")

        frame = ttk.Frame(popup, style="App.TFrame", padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="펌프 수동 전압 TEST", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 10)
        )

        body = ttk.Frame(frame, style="App.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        controls = self.panel(body)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 12))
        ttk.Label(controls, text="수동 AO 설정", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        ttk.Label(controls, text="출력 전압 (V)", style="Panel.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w"
        )
        self.ao_voltage = tk.StringVar(value="0.0")
        ao_entry = ttk.Entry(
            controls,
            textvariable=self.ao_voltage,
            style="Highlight.TEntry",
            justify="center",
            width=15,
        )
        ao_entry.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 3))
        if self.touch_mode:
            ao_entry.bind(
                "<Button-1>",
                lambda _event: (
                    self.open_numeric_keypad(
                        self.ao_voltage, "펌프 수동 출력 전압 (V)", 0.0, 5.0, 3
                    ),
                    "break",
                )[1],
            )
        ttk.Label(controls, text="허용 범위 0.000 ~ 5.000 V", style="Hint.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w"
        )
        ttk.Button(
            controls, text="전압 출력", style="Start.TButton", command=self.output_ao
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(
            controls, text="0 V (정지)", style="Stop.TButton", command=self.zero_ao
        ).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.ao_value = ttk.Label(controls, text="현재 출력: 0.000 V", style="Value.TLabel")
        self.ao_value.grid(row=6, column=0, columnspan=2, sticky="w", pady=(18, 0))

        self.ao_graph = TrendGraph(body, "AO 출력 추이", [("AO", COLORS["ao"])], 0, 5, "V")
        self.ao_graph.grid(row=0, column=1, sticky="nsew")

        popup.protocol("WM_DELETE_WINDOW", self._close_ao_popup)

    def _close_ao_popup(self) -> None:
        try:
            # Always stop pump command when closing the manual window.
            try:
                self.daq.write_voltage(0.0)
            except DaqError:
                pass
        finally:
            if self._ao_popup is not None and self._ao_popup.winfo_exists():
                self._ao_popup.destroy()
            self._ao_popup = None

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
            text="NI 9422 DI0~5 / NI 9477 DO0~2 / NI 9264 ao0",
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
            "  PC COM 포트: COM3\n"
            "  통신: 9600 / 8 / None / Stop2 / Addr 1\n"
            "\n"
            "【펌프 → NI 9264 ao0】\n"
            "  펌프 + 입력    →  NI 9264  ao0\n"
            "  펌프 GND/COM   →  AO GND\n"
            "\n"
            "【레벨센서 3개 → NI 9422】\n"
            "  센서 Black(24V COM) → PSU +24V\n"
            "  센서1 Red(HIGH) → DI0+ / White(LOW) → DI1+\n"
            "  센서2 Red(HIGH) → DI2+ / White(LOW) → DI3+\n"
            "  센서3 Red(HIGH) → DI4+ / White(LOW) → DI5+\n"
            "  DI0−~DI5− → PSU 0V\n"
            "  접점 ON 시 DI+–DI−에 24V → 입력 ON\n"
            "  ※ 접점 N/O·N/C 방향은 현장에서 ON 표시로 확인\n"
            "\n"
            "【전동볼밸브 3개 → NI 9477 (싱킹 출력)】\n"
            "  밸브 Red(+24V) → PSU +24V\n"
            "  밸브 White(SIG) → DO0 / DO1 / DO2\n"
            "  밸브 Black(0V) + NI 9477 COM → PSU 0V\n"
            "  DO ON = White를 0V로 당김 = 열림 명령\n"
            "  ※ 0V 여부는 명령 확인이며 실제 기계 위치 피드백은 아님\n"
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
        dialog.geometry("620x550" if self.touch_mode else "590x570")
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
        di_var = tk.StringVar(value=self.channels.level_inputs)
        do_var = tk.StringVar(value=self.channels.valve_outputs)
        flame_di_var = tk.StringVar(value=self.channels.flame_input)
        igniter_do_var = tk.StringVar(value=self.channels.igniter_output)
        port_var = tk.StringVar(value=self.mp5y_config.port)
        slave_var = tk.StringVar(value=str(self.mp5y_config.slave_id))
        baud_var = tk.StringVar(value=str(self.mp5y_config.baudrate))
        mode_var = tk.StringVar(value=self.mp5y_config.value_mode)
        fmt_var = tk.StringVar(value=self.mp5y_config.pv_format)
        for row, label, var in (
            (1, "AO 펌프 채널", ao_var),
            (2, "레벨 입력 DI0:5", di_var),
            (3, "밸브 출력 DO0:2", do_var),
            (4, "화염 감지 DI6", flame_di_var),
            (5, "점화기 SSR DO3", igniter_do_var),
            (6, "MP5Y COM 포트", port_var),
            (7, "MP5Y 주소", slave_var),
            (8, "MP5Y Baud", baud_var),
            (9, "표시모드 (frequency_hz/flow_ccpm)", mode_var),
            (10, "PV포맷 (int16/int32/dec32)", fmt_var),
        ):
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(
                row=row, column=0, sticky="w", pady=3 if self.touch_mode else 8
            )
            ttk.Entry(frame, textvariable=var, width=28).grid(row=row, column=1, padx=(12, 0))

        def apply() -> None:
            self.channels.ao_pump = ao_var.get().strip() or self.channels.ao_pump
            self.channels.level_inputs = di_var.get().strip() or self.channels.level_inputs
            self.channels.valve_outputs = do_var.get().strip() or self.channels.valve_outputs
            self.channels.flame_input = flame_di_var.get().strip() or self.channels.flame_input
            self.channels.igniter_output = igniter_do_var.get().strip() or self.channels.igniter_output
            self.daq.channels = self.channels
            self.mp5y_config.port = port_var.get().strip() or "COM3"
            try:
                self.mp5y_config.slave_id = int(slave_var.get().strip())
            except ValueError:
                messagebox.showerror("입력 오류", "MP5Y 주소는 숫자여야 합니다.")
                return
            try:
                self.mp5y_config.baudrate = int(baud_var.get().strip())
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
            self.mp5y.close()
            self.mp5y = Mp5yService(self.mp5y_config)
            self.refresh_connection()
            dialog.destroy()

        ttk.Button(
            frame,
            text="적용",
            style="TouchStart.TButton" if self.touch_mode else "Start.TButton",
            command=apply,
        ).grid(
            row=11, column=0, columnspan=2, sticky="ew", pady=(18, 0)
        )

    # -------------------------------------------------------------- actions
    def toggle_monitor(self) -> None:
        if self.feedback_running:
            messagebox.showinfo("안내", "피드백 제어 중에는 모니터를 따로 시작할 수 없습니다.")
            return
        if not self.monitor_running:
            self.monitor_running = True
            self.lpf.reset()
            self._last_loop_at = time.monotonic()
            self.mon_flow_graph.clear()
            self.mon_hz_graph.clear()
        else:
            self.monitor_running = False
        self.monitor_button.configure(
            text="측정 중지" if self.monitor_running else "측정 시작",
            style="Stop.TButton" if self.monitor_running else "Start.TButton",
        )

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

    def validate_feedback(self) -> bool:
        for var, label in (
            (self.fb_pulse_ml, "펄스정수"),
            (self.sv, "SV"),
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
        if self.monitor_running:
            self.toggle_monitor()

        if not self.feedback_running:
            self.feedback_running = True
            self.pi.reset()
            self.lpf.reset()
            self._last_loop_at = time.monotonic()
            self.feedback_graph.clear()
            self.feedback_ao_graph.clear()
            sv = self.number_silent(self.sv, 120)
            self.feedback_graph.set_scale(0, max(200.0, sv * 1.5), "cc/min")
        else:
            self.feedback_running = False
            try:
                self.current_ao = self.daq.write_voltage(0.0)
            except DaqError as exc:
                self._safe_stop(exc)
                return
            self.output_value.configure(text=f"AO: {self.current_ao:.3f} V")

        self.feedback_button.configure(
            text="피드백 제어 중지" if self.feedback_running else "피드백 제어 시작",
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
        self.level_master_status.configure(text="DAQ 연결 · 자동 제어 동작 중")
        return True

    # --------------------------------------------------------------- loops
    def refresh_connection(self) -> None:
        self.daq.check_connection()
        ao_ok = self.daq.available
        level_ok = self.daq.level_available
        if level_ok and not self._level_was_available:
            self._level_was_available = self._start_level_control_automatically()
        elif not level_ok:
            self.level_running = False
            self._level_was_available = False
            self.valve_commands = [False, False, False]
            self.level_master_status.configure(text="DAQ 연결 대기 · 밸브 안전 닫힘")
            self._render_level_states([False] * 6, ["DAQ 연결 대기"] * 3)
        # Avoid holding the serial port locked during idle; probe then release.
        mp_ok = self.mp5y.check_connection()
        self.mp5y.close()

        connected = ao_ok or level_ok or mp_ok
        self._link_ok = connected
        if ao_ok and level_ok and mp_ok:
            self._status_base_color = COLORS["ok"]
            text = "MP5Y + NI 연결됨"
        elif mp_ok and ao_ok:
            self._status_base_color = COLORS["warn"]
            text = "유량/AO OK · 레벨 I/O 확인"
        elif mp_ok:
            self._status_base_color = COLORS["warn"]
            text = f"MP5Y OK / NI: {self.daq.error}"
        else:
            self._status_base_color = COLORS["bad"]
            text = "연결 확인 필요 (COM3 / NI)"

        if not connected:
            self.status_on = False
            self._paint_status_lamp(COLORS["bad"], outline="#9B2C2C")
        self.status_label.configure(text=text)
        self.after(2000, self.refresh_connection)

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
        self.after(400, self._blink_status_lamp)

    def _paint_status_lamp(self, color: str, outline: str = "") -> None:
        self.status_canvas.itemconfigure(
            self.status_dot,
            fill=color,
            outline=outline or color,
        )

    def update_loop(self) -> None:
        try:
            if self.monitor_running:
                self._update_monitor()
            if self.feedback_running:
                self._update_feedback()
            if self.level_running:
                self._update_levels()
            self._update_flame_status()
        except (Mp5yError, DaqError) as exc:
            self._safe_stop(exc)
        except Exception as exc:  # noqa: BLE001
            self._safe_stop(RuntimeError(f"루프 오류: {exc}"))
        self.after(self.POLL_MS, self.update_loop)

    def _show_hardware_error(self, error: Exception) -> None:
        self._link_ok = False
        self._paint_status_lamp(COLORS["bad"], outline="#9B2C2C")
        self.status_label.configure(text=str(error))
        messagebox.showerror("하드웨어 오류", str(error))

    def _safe_stop(self, error: Exception) -> None:
        self.monitor_running = False
        self.feedback_running = False
        self.level_running = False
        self.igniter_on = False
        stop_error = ""
        try:
            self.current_ao = self.daq.write_voltage(0.0)
        except DaqError as exc:
            stop_error = f" / 0 V 출력 실패: {exc}"
        try:
            self.valve_commands = self.daq.write_valves([False, False, False])
        except DaqError as exc:
            stop_error += f" / 밸브 닫힘 실패: {exc}"
        try:
            self.daq.write_igniter(False)
        except DaqError as exc:
            stop_error += f" / 점화기 SSR OFF 실패: {exc}"
        self.mp5y.close()
        self.monitor_button.configure(text="측정 시작", style="Start.TButton")
        self.feedback_button.configure(
            text="피드백 제어 시작",
            style="TouchStart.TButton" if self.touch_mode else "Start.TButton",
        )
        if hasattr(self, "igniter_button"):
            self._set_igniter_button(False)
        self.level_master_status.configure(text="안전 정지 · 모든 밸브 닫힘")
        self._render_level_states([False] * 6, ["오류 · 안전 닫힘"] * 3)
        self.output_value.configure(text=f"AO: {self.current_ao:.3f} V")
        self._link_ok = False
        self._paint_status_lamp(COLORS["bad"], outline="#9B2C2C")
        self.status_label.configure(text=f"안전 정지: {error}{stop_error}")

    def toggle_igniter(self) -> None:
        """Manual ignition SSR toggle (DO3)."""
        if not self.daq.level_available:
            # Level availability ~= NI 9422/9477 present; keeps UX consistent.
            messagebox.showerror("NI-DAQ 오류", "NI 9422/9477 연결을 확인하세요.")
            return
        requested = not self.igniter_on
        if requested and not messagebox.askyesno(
            "점화 확인",
            "점화기 SSR을 ON 하시겠습니까?\n주변 안전과 연료 상태를 확인하세요.",
        ):
            return
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
            style = "Start.TButton" if on else "Compact.TButton"
        self.igniter_button.configure(text="", style=style)

    def _update_flame_status(self) -> None:
        self.flame_label.configure(text="FD")
        if not self.daq.level_available:
            self.flame_label.configure(foreground=COLORS["muted"])
            self.flame_canvas.itemconfigure(
                self.flame_ring, fill="#F1F5F9", outline="#CBD5E1"
            )
            self.flame_canvas.itemconfigure(
                self.flame_lamp, fill="#CBD5E1", outline="#94A3B8"
            )
            return
        try:
            flame = self.daq.read_flame()
        except DaqError:
            # Don't hard-fail the loop for a single DI read.
            self.flame_label.configure(foreground=COLORS["bad"])
            self.flame_canvas.itemconfigure(
                self.flame_ring, fill="#FCE8E8", outline=COLORS["bad"]
            )
            self.flame_canvas.itemconfigure(
                self.flame_lamp, fill=COLORS["bad"], outline="#9B2C2C"
            )
            return
        self.flame_label.configure(
            foreground=COLORS["warn"] if flame else COLORS["muted"],
        )
        self.flame_canvas.itemconfigure(
            self.flame_ring,
            fill="#FFF4D6" if flame else "#F1F5F9",
            outline="#F3B13F" if flame else "#CBD5E1",
        )
        self.flame_canvas.itemconfigure(
            self.flame_lamp,
            fill="#FFB020" if flame else "#CBD5E1",
            outline="#D97706" if flame else "#94A3B8",
        )

    def _update_levels(self) -> None:
        values = self.daq.read_levels()
        roles = (ValveRole.SUPPLY, ValveRole.DRAIN, ValveRole.DRAIN)
        decisions = []
        commands = []
        for index, role in enumerate(roles):
            high = values[index * 2]
            low = values[index * 2 + 1]
            decision = decide_valve(role, high, low, self.valve_commands[index])
            decisions.append(decision)
            commands.append(decision.opened)
        self.valve_commands = self.daq.write_valves(commands)
        self._render_level_states(values, [decision.state for decision in decisions])
        if any(decision.fault for decision in decisions):
            self.level_master_status.configure(text="센서 충돌 감지 · 해당 밸브 안전 닫힘")
        else:
            self.level_master_status.configure(text="자동 제어 동작 중")

    def _render_level_states(self, values: list[bool], states: list[str]) -> None:
        for index in range(3):
            high = values[index * 2]
            low = values[index * 2 + 1]
            opened = self.valve_commands[index]
            self.level_high_labels[index].configure(
                text=f"● HIGH  {'ON' if high else 'OFF'}",
                foreground=COLORS["bad"] if high else COLORS["muted"],
            )
            self.level_low_labels[index].configure(
                text=f"● LOW   {'ON' if low else 'OFF'}",
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

    def _update_monitor(self) -> None:
        pulse_ml = self.number_silent(self.pulse_ml, DEFAULT_PULSE_ML)
        raw_flow, hz, raw, dot = self._read_mp5y(pulse_ml)
        now = time.monotonic()
        dt = max(now - (self._last_loop_at or now), 0.001)
        self._last_loop_at = now
        flow = self.lpf.update(raw_flow, dt, 1.0)
        self.filtered_flow = flow
        hz_text = "—" if math.isnan(hz) else f"{hz:.2f}"
        mode_name = MODE_NAMES.get(self.mp5y.last_mode, f"mode={self.mp5y.last_mode}")
        r0, r1, r2 = self.mp5y.last_regs
        self.mon_flow.configure(text=f"{flow:.1f} cc/min")
        if self.mp5y_config.value_mode == "flow_ccpm":
            self.mon_hz.configure(text=f"MP5Y 표시: {raw_flow:.2f} cc/min")
            self.mon_conv.configure(
                text=f"추정 Hz: {hz_text}  (표시÷27.6) · 이중환산 없음"
            )
        else:
            self.mon_hz.configure(text=f"MP5Y 표시: {hz_text} Hz")
            self.mon_conv.configure(
                text=f"환산: {hz_text} × {pulse_ml:.2f} × 60 = {raw_flow:.1f} cc/min"
            )
        self.mon_raw.configure(text=f"raw:{raw} DOT={dot} regs=[{r0},{r1},{r2}]")
        self.mon_mode.configure(text=f"모드: {mode_name}")
        self.mon_flow_graph.set_scale(0, max(200.0, flow * 1.4 + 20), "cc/min")
        self.mon_flow_graph.add(flow)
        self.mon_hz_graph.add(0.0 if math.isnan(hz) else hz)

    def _update_feedback(self) -> None:
        pulse_ml = self.number_silent(self.fb_pulse_ml, DEFAULT_PULSE_ML)
        # Keep monitor decimal setting for feedback reads too.
        raw_flow, hz, _raw, _dot = self._read_mp5y(pulse_ml)
        now = time.monotonic()
        dt = max(now - (self._last_loop_at or now), 0.001)
        self._last_loop_at = now

        cutoff = self.number_silent(self.lpf_cutoff, 0.8)
        pv = self.lpf.update(raw_flow, dt, cutoff)
        self.filtered_flow = pv
        sv = self.number_silent(self.sv, 120.0)
        p_gain = self.number_silent(self.p_gain, 0.02)
        i_gain = self.number_silent(self.i_gain, 0.005)

        ao = self.pi.update(sv, pv, p_gain, i_gain, dt)
        self.current_ao = self.daq.write_voltage(ao)

        hz_text = "—" if math.isnan(hz) else f"{hz:.2f}"
        if self.touch_mode:
            self.focus_pv.configure(text=f"PV  {pv:.1f}")
            self.focus_sv.configure(text=f"SV  {sv:.1f}")
        else:
            self.focus_pv.configure(text=f"PV  {pv:.1f} cc/min")
        self.focus_err.configure(text=f"오차: {sv - pv:+.1f} cc/min")
        if self.mp5y_config.value_mode == "flow_ccpm":
            self.fb_hz.configure(text=f"MP5Y 표시: {raw_flow:.2f} cc/min")
        else:
            self.fb_hz.configure(text=f"MP5Y 표시: {hz_text} Hz")
        self.output_value.configure(text=f"AO: {self.current_ao:.3f} V")
        self.feedback_graph.set_scale(0, max(200.0, sv * 1.5), "cc/min")
        self.feedback_graph.add(pv, sv)
        self.feedback_ao_graph.add(self.current_ao)

    # ------------------------------------------------------------- settings
    def save_settings(self) -> None:
        data = {
            "pulse_ml": self.fb_pulse_ml.get(),
            "sv": self.sv.get(),
            "p_gain": self.p_gain.get(),
            "i_gain": self.i_gain.get(),
            "lpf_cutoff": self.lpf_cutoff.get(),
            "ao_pump": self.channels.ao_pump,
            "level_inputs": self.channels.level_inputs,
            "valve_outputs": self.channels.valve_outputs,
            "flame_input": self.channels.flame_input,
            "igniter_output": self.channels.igniter_output,
            "mp5y_port": self.mp5y_config.port,
            "mp5y_slave_id": self.mp5y_config.slave_id,
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
        self.sv.set(str(data.get("sv", "120.0")))
        self.p_gain.set(str(data.get("p_gain", "0.020")))
        self.i_gain.set(str(data.get("i_gain", "0.005")))
        self.lpf_cutoff.set(str(data.get("lpf_cutoff", "0.8")))
        self.channels.ao_pump = str(data.get("ao_pump", self.channels.ao_pump))
        self.channels.level_inputs = str(data.get("level_inputs", self.channels.level_inputs))
        self.channels.valve_outputs = str(data.get("valve_outputs", self.channels.valve_outputs))
        self.channels.flame_input = str(data.get("flame_input", self.channels.flame_input))
        self.channels.igniter_output = str(data.get("igniter_output", self.channels.igniter_output))
        self.daq.channels = self.channels
        self.mp5y_config.port = str(data.get("mp5y_port", "COM3"))
        self.mp5y_config.slave_id = int(data.get("mp5y_slave_id", 1))
        self.mp5y_config.baudrate = int(data.get("mp5y_baudrate", 9600))
        self.mp5y_config.value_mode = str(data.get("mp5y_value_mode", "flow_ccpm"))
        self.mp5y_config.pv_format = str(data.get("mp5y_pv_format", "int16"))
        self.decimal_places.set(str(data.get("decimal_places", "auto")))
        self.mp5y = Mp5yService(self.mp5y_config)

    def on_close(self) -> None:
        try:
            self.feedback_running = False
            self.monitor_running = False
            self.level_running = False
            self.daq.close()
            self.mp5y.close()
        finally:
            self.destroy()


def main() -> None:
    app = FlowControlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
