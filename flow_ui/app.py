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


# Soft blush / rose palette — clean, balanced, feminine.
COLORS = {
    "bg": "#FBF6F8",
    "panel": "#FFFFFF",
    "panel_alt": "#FFF1F5",
    "border": "#F0D5DE",
    "title": "#5A3D4A",
    "text": "#6B4E5B",
    "muted": "#A78996",
    "accent": "#D9789A",
    "accent_deep": "#C45B7C",
    "accent_soft": "#F7C9D8",
    "value": "#C45B7C",
    "ok": "#5BAF8A",
    "warn": "#E2A35A",
    "bad": "#D96B7C",
    "graph_bg": "#FFF8FB",
    "grid": "#F3DDE6",
    "pv": "#D9789A",
    "sv": "#7E9ED9",
    "ao": "#6FBF9A",
    "hz": "#C9A06A",
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
        height = max(self.winfo_height(), 180)
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
    SETTINGS_PATH = Path(__file__).with_name("settings.json")

    def __init__(self) -> None:
        super().__init__()
        self.title("Pulse Flow · Feedback Control")
        screen_w = max(self.winfo_screenwidth(), 1024)
        screen_h = max(self.winfo_screenheight(), 700)
        width = min(1240, int(screen_w * 0.94))
        height = min(800, int(screen_h * 0.9))
        self.geometry(f"{width}x{height}")
        self.minsize(min(960, width), min(620, height))
        self.configure(bg=COLORS["bg"])

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
        self._monitor_popup: tk.Toplevel | None = None
        self._ao_popup: tk.Toplevel | None = None
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
        self.refresh_connection()
        self.after(self.POLL_MS, self.update_loop)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

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
            "TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
            background=COLORS["accent_soft"],
            foreground=COLORS["title"],
            borderwidth=0,
        )
        style.map("TButton", background=[("active", "#F3B7C9")])
        style.configure("Nav.TButton", background="#F6E4EA")
        style.map("Nav.TButton", background=[("active", "#F0D0DB")])
        style.configure("NavActive.TButton", background=COLORS["accent"], foreground="#FFFFFF")
        style.map("NavActive.TButton", background=[("active", COLORS["accent_deep"])])
        style.configure("Start.TButton", background="#8FCBB0", foreground="#214536")
        style.map("Start.TButton", background=[("active", "#7BBC9F")])
        style.configure("Stop.TButton", background="#E7A0AD", foreground="#5A2430")
        style.map("Stop.TButton", background=[("active", "#D98998")])
        style.configure(
            "TEntry",
            fieldbackground=COLORS["panel_alt"],
            foreground=COLORS["title"],
            insertcolor=COLORS["title"],
            padding=7,
            borderwidth=1,
        )

    def _build_layout(self) -> None:
        header = ttk.Frame(self, style="App.TFrame", padding=(28, 18, 28, 8))
        header.pack(fill="x")

        titles = ttk.Frame(header, style="App.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="Pulse Flow Control", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            titles,
            text="MP5Y-25 유량(cc/min)  ·  NI 9264 AO  ·  프리스케일 27.6",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        status = ttk.Frame(header, style="App.TFrame")
        status.pack(side="right")
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
        self.status_canvas = tk.Canvas(
            status, width=18, height=18, bg=COLORS["bg"], highlightthickness=0
        )
        self.status_canvas.pack(side="right", padx=(8, 0))
        self.status_dot = self.status_canvas.create_oval(3, 3, 15, 15, fill=COLORS["bad"], outline="")
        self.status_label = ttk.Label(status, text="통신 확인 중", style="Sub.TLabel")
        self.status_label.pack(side="right")
        ttk.Button(status, text="배선 가이드", command=self.open_wiring_guide).pack(side="right", padx=(0, 8))
        ttk.Button(status, text="채널 설정", command=self.open_settings).pack(side="right", padx=16)

        nav = ttk.Frame(self, style="App.TFrame", padding=(28, 8, 28, 10))
        nav.pack(fill="x")
        self.nav_buttons: dict[str, ttk.Button] = {}
        for key, label in (("feedback", "통합 제어 홈"),):
            button = ttk.Button(nav, text=label, style="Nav.TButton", command=lambda k=key: self.show_page(k))
            button.pack(side="left", fill="x", expand=True, padx=4)
            self.nav_buttons[key] = button

        self.content = ttk.Frame(self, style="App.TFrame", padding=(28, 8, 28, 24))
        self.content.pack(fill="both", expand=True)
        self.pages = {
            "monitor": self._create_monitor_page(),
            "ao": self._create_ao_page(),
            "feedback": self._create_feedback_page(),
        }

        footer = ttk.Frame(self, style="App.TFrame", padding=(28, 0, 28, 16))
        footer.pack(fill="x")
        self.device_label = ttk.Label(footer, text="", style="Sub.TLabel")
        self.device_label.pack(anchor="w")

    def panel(self, parent: tk.Misc) -> ttk.Frame:
        wrapper = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        frame = ttk.Frame(wrapper, style="Panel.TFrame", padding=20)
        frame.pack(fill="both", expand=True)
        frame._card = wrapper  # type: ignore[attr-defined]
        return frame

    def place_panel(self, panel: ttk.Frame, **grid_kwargs) -> None:
        panel._card.grid(**grid_kwargs)  # type: ignore[attr-defined]

    def field(self, parent: tk.Misc, row: int, label: str, default: str, hint: str = "") -> tk.StringVar:
        value = tk.StringVar(value=default)
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=7)
        entry = ttk.Entry(parent, textvariable=value, width=14)
        entry.grid(row=row, column=1, sticky="e", padx=(12, 0))
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
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(0, weight=0)
        page.columnconfigure(1, weight=0)
        page.columnconfigure(2, weight=1)
        page.rowconfigure(0, weight=1)
        page.rowconfigure(1, weight=0)

        controls = self.panel(page)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 14))
        ttk.Label(controls, text="유량 피드백 제어 (메인)", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        self.fb_pulse_ml = self.field(controls, 1, "펄스정수 (ml/P)", "0.46")
        self.sv = self.field(controls, 3, "SV 목표 유량 (cc/min)", "120.0")
        self.p_gain = self.field(controls, 5, "P Gain (V·min/cc)", "0.020")
        self.i_gain = self.field(controls, 7, "I Gain (V·min/cc·s)", "0.005")
        self.lpf_cutoff = self.field(controls, 9, "LPF 차단주파수 (Hz)", "0.8", "0 = 필터 미사용")

        self.pv_value = ttk.Label(controls, text="PV: 0.0 cc/min", style="Value.TLabel")
        self.pv_value.grid(row=11, column=0, columnspan=2, sticky="w", pady=(14, 2))
        self.fb_hz = ttk.Label(controls, text="주파수: 0.00 Hz", style="Panel.TLabel")
        self.fb_hz.grid(row=12, column=0, columnspan=2, sticky="w")
        self.output_value = ttk.Label(controls, text="AO: 0.000 V", style="ValueSmall.TLabel")
        self.output_value.grid(row=13, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.feedback_button = ttk.Button(
            controls, text="피드백 제어 시작", style="Start.TButton", command=self.toggle_feedback
        )
        self.feedback_button.grid(row=14, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(controls, text="설정 저장", command=self.save_settings).grid(
            row=14, column=1, sticky="ew", padx=(8, 0), pady=(16, 0)
        )
        ttk.Label(
            controls,
            text="PV = MP5Y Modbus 값\nAO = clamp(P×오차 + I×∫오차 dt, 0~5V)",
            style="Hint.TLabel",
            wraplength=250,
            justify="left",
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(12, 0))

        ttk.Separator(controls, orient="horizontal").grid(
            row=16, column=0, columnspan=2, sticky="ew", pady=(12, 6)
        )
        self.flame_label = ttk.Label(controls, text="화염 감지(DI6): OFF", style="Panel.TLabel")
        self.flame_label.grid(row=17, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.igniter_button = ttk.Button(
            controls,
            text="점화기 수동 SSR: OFF",
            style="Stop.TButton",
            command=self.toggle_igniter,
        )
        self.igniter_button.grid(row=18, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(
            controls,
            text="주의: 점화기는 수동 제어입니다.\n화염감지 상태는 DI 접점값을 표시합니다.",
            style="Hint.TLabel",
            wraplength=250,
            justify="left",
        ).grid(row=19, column=0, columnspan=2, sticky="w", pady=(10, 0))

        focus = self.panel(page)
        self.place_panel(focus, row=0, column=1, sticky="ns", padx=(0, 10))
        ttk.Label(focus, text="현재값 집중 보기", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 8))
        self.focus_pv = ttk.Label(focus, text="PV\n0.0 cc/min", style="Value.TLabel", justify="center")
        self.focus_pv.pack(fill="x", pady=(8, 12))
        self.focus_sv = ttk.Label(focus, text="SV\n0.0 cc/min", style="Value.TLabel", justify="center")
        self.focus_sv.pack(fill="x", pady=(0, 12))
        self.focus_err = ttk.Label(focus, text="오차: 0.0 cc/min", style="ValueSmall.TLabel")
        self.focus_err.pack(anchor="w", pady=(0, 8))
        ttk.Label(focus, text="그래프보다 현재값 중심 표시", style="Hint.TLabel").pack(anchor="w")

        graphs = ttk.Frame(page, style="App.TFrame", width=330)
        graphs.grid(row=0, column=2, sticky="nsew")
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
        self.place_panel(level_section, row=1, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
        top = ttk.Frame(level_section, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="레벨 / 밸브 자동 제어", style="PanelTitle.TLabel").pack(side="left")
        self.level_button = ttk.Button(
            top, text="자동 제어 시작", style="Start.TButton", command=self.toggle_level_control
        )
        self.level_button.pack(side="right")
        self.level_master_status = ttk.Label(
            top, text="정지 · 모든 밸브 닫힘 명령", style="Panel.TLabel"
        )
        self.level_master_status.pack(side="right", padx=16)

        cards = ttk.Frame(level_section, style="Panel.TFrame")
        cards.pack(fill="x", pady=(12, 0))
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
            card = ttk.Frame(cards, style="Panel.TFrame", padding=(12, 8))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 8, 0))
            ttk.Label(card, text=name, style="PanelTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=rule, style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
            high = ttk.Label(card, text="● HIGH OFF", style="Panel.TLabel")
            high.pack(anchor="w")
            low = ttk.Label(card, text="● LOW  OFF", style="Panel.TLabel")
            low.pack(anchor="w", pady=(2, 8))
            valve = ttk.Label(card, text="닫힘 명령", style="ValueSmall.TLabel")
            valve.pack(anchor="w")
            logic = ttk.Label(card, text="대기", style="Panel.TLabel")
            logic.pack(anchor="w", pady=(4, 0))
            self.level_high_labels.append(high)
            self.level_low_labels.append(low)
            self.valve_status_labels.append(valve)
            self.level_logic_labels.append(logic)
        return page

    def _create_level_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure((0, 1, 2), weight=1)
        page.rowconfigure(1, weight=1)

        header = self.panel(page)
        self.place_panel(header, row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
        ttk.Label(header, text="레벨 센서 · 전동 볼밸브 자동 제어", style="PanelTitle.TLabel").pack(
            side="left"
        )
        self.level_button = ttk.Button(
            header, text="자동 제어 시작", style="Start.TButton", command=self.toggle_level_control
        )
        self.level_button.pack(side="right")
        self.level_master_status = ttk.Label(
            header, text="정지 · 모든 밸브 닫힘 명령", style="Panel.TLabel"
        )
        self.level_master_status.pack(side="right", padx=18)

        self.level_high_labels: list[ttk.Label] = []
        self.level_low_labels: list[ttk.Label] = []
        self.valve_status_labels: list[ttk.Label] = []
        self.level_logic_labels: list[ttk.Label] = []
        definitions = (
            ("밸브 1 · 급수", "LOW → 열림  |  HIGH → 닫힘"),
            ("밸브 2 · 배수", "LOW → 닫힘  |  HIGH → 열림"),
            ("밸브 3 · 배수", "LOW → 닫힘  |  HIGH → 열림"),
        )
        for index, (name, rule) in enumerate(definitions):
            card = self.panel(page)
            self.place_panel(
                card,
                row=1,
                column=index,
                sticky="nsew",
                padx=(0 if index == 0 else 7, 0 if index == 2 else 7),
            )
            ttk.Label(card, text=name, style="PanelTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=rule, style="Hint.TLabel").pack(anchor="w", pady=(3, 18))

            high = ttk.Label(card, text="● HIGH  OFF", style="Panel.TLabel")
            high.pack(anchor="w", pady=5)
            low = ttk.Label(card, text="● LOW   OFF", style="Panel.TLabel")
            low.pack(anchor="w", pady=5)
            self.level_high_labels.append(high)
            self.level_low_labels.append(low)

            ttk.Separator(card, orient="horizontal").pack(fill="x", pady=18)
            valve = ttk.Label(card, text="닫힘 명령", style="Value.TLabel")
            valve.pack(anchor="w")
            logic = ttk.Label(card, text="대기", style="Panel.TLabel")
            logic.pack(anchor="w", pady=(8, 0))
            ttk.Label(
                card,
                text="9477 DO ON = 흰색 SIG를 0V로 당김\n표시는 실제 위치가 아닌 전기적 명령 상태",
                style="Hint.TLabel",
                justify="left",
                wraplength=280,
            ).pack(anchor="w", pady=(18, 0))
            self.valve_status_labels.append(valve)
            self.level_logic_labels.append(logic)

        return page

    # ------------------------------------------------------------- helpers
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

        screen_w = max(self.winfo_screenwidth(), 1024)
        screen_h = max(self.winfo_screenheight(), 700)
        w = min(820, int(screen_w * 0.65))
        h = min(520, int(screen_h * 0.65))
        popup.geometry(f"{w}x{h}")

        frame = ttk.Frame(popup, style="App.TFrame", padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="펌프 수동 전압 TEST", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 10)
        )

        controls = self.panel(frame)
        controls.pack(fill="both", expand=True)

        self.ao_voltage = self.field(controls, 1, "출력 전압 (V)", "0.0", "범위 0.0 ~ 5.0 V")
        ttk.Button(
            controls, text="전압 출력", style="Start.TButton", command=self.output_ao
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(
            controls, text="0 V (정지)", style="Stop.TButton", command=self.zero_ao
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.ao_value = ttk.Label(controls, text="현재 출력: 0.000 V", style="Value.TLabel")
        self.ao_value.grid(row=5, column=0, columnspan=2, sticky="w", pady=(22, 0))

        self.ao_graph = TrendGraph(frame, "AO 출력 추이", [("AO", COLORS["ao"])], 0, 5, "V")
        self.ao_graph.pack(fill="both", expand=True, pady=(12, 0))

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
        dialog.geometry("590x570")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=20)
        frame.pack(fill="both", expand=True, padx=16, pady=16)
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
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=8)
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

        ttk.Button(frame, text="적용", style="Start.TButton", command=apply).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(18, 0)
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
            style="Stop.TButton" if self.feedback_running else "Start.TButton",
        )

    def toggle_level_control(self) -> None:
        if not self.level_running:
            self.daq.check_connection()
            if not self.daq.level_available:
                messagebox.showerror(
                    "레벨 I/O 오류",
                    "NI 9422(cDAQ2Mod2)와 NI 9477(cDAQ2Mod3) 연결을 확인하세요.",
                )
                return
            try:
                # Initial state is fail-safe closed. With both contacts OFF,
                # subsequent scans hold this state as requested.
                self.valve_commands = self.daq.write_valves([False, False, False])
            except DaqError as exc:
                self._show_hardware_error(exc)
                return
            self.level_running = True
            self.level_master_status.configure(text="자동 제어 동작 중")
        else:
            self.level_running = False
            try:
                self.valve_commands = self.daq.write_valves([False, False, False])
            except DaqError as exc:
                self._show_hardware_error(exc)
            self._render_level_states([False] * 6, ["정지 · 안전 닫힘"] * 3)
            self.level_master_status.configure(text="정지 · 모든 밸브 닫힘 명령")

        self.level_button.configure(
            text="자동 제어 중지" if self.level_running else "자동 제어 시작",
            style="Stop.TButton" if self.level_running else "Start.TButton",
        )

    # --------------------------------------------------------------- loops
    def refresh_connection(self) -> None:
        self.daq.check_connection()
        ao_ok = self.daq.available
        level_ok = self.daq.level_available
        # Avoid holding the serial port locked during idle; probe then release.
        mp_ok = self.mp5y.check_connection()
        self.mp5y.close()

        connected = ao_ok or level_ok or mp_ok
        self.status_on = not self.status_on if connected else False
        if ao_ok and level_ok and mp_ok:
            color = COLORS["ok"] if self.status_on else "#A8D8C0"
            text = "MP5Y + NI 9264/9422/9477 연결됨"
        elif mp_ok and ao_ok:
            color = COLORS["warn"]
            text = "유량/AO OK · 레벨 I/O 확인 필요"
        elif mp_ok:
            color = COLORS["warn"]
            text = f"MP5Y OK / NI: {self.daq.error}"
        else:
            color = COLORS["bad"]
            text = "연결 확인 필요 (COM3 / NI)"

        self.status_canvas.itemconfigure(self.status_dot, fill=color)
        self.status_label.configure(text=text)
        self.device_label.configure(
            text=f"{self.mp5y.device_summary}  |  {self.daq.device_summary}"
        )
        self.after(2000, self.refresh_connection)

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
        self.status_canvas.itemconfigure(self.status_dot, fill=COLORS["bad"])
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
        self.feedback_button.configure(text="피드백 제어 시작", style="Start.TButton")
        self.level_button.configure(text="자동 제어 시작", style="Start.TButton")
        if hasattr(self, "igniter_button"):
            self.igniter_button.configure(
                text="점화기 수동 SSR: OFF", style="Stop.TButton"
            )
        self.level_master_status.configure(text="안전 정지 · 모든 밸브 닫힘 명령")
        self._render_level_states([False] * 6, ["오류 · 안전 닫힘"] * 3)
        self.output_value.configure(text=f"AO: {self.current_ao:.3f} V")
        self.status_canvas.itemconfigure(self.status_dot, fill=COLORS["bad"])
        self.status_label.configure(text=f"안전 정지: {error}{stop_error}")

    def toggle_igniter(self) -> None:
        """Manual ignition SSR toggle (DO3)."""
        if not self.daq.level_available:
            # Level availability ~= NI 9422/9477 present; keeps UX consistent.
            messagebox.showerror("NI-DAQ 오류", "NI 9422/9477 연결을 확인하세요.")
            return
        self.igniter_on = not self.igniter_on
        try:
            self.daq.write_igniter(self.igniter_on)
        except DaqError as exc:
            self.igniter_on = False
            self._show_hardware_error(exc)
            return
        self.igniter_button.configure(
            text=f"점화기 수동 SSR: {'ON' if self.igniter_on else 'OFF'}",
            style="Start.TButton" if self.igniter_on else "Stop.TButton",
        )

    def _update_flame_status(self) -> None:
        try:
            flame = self.daq.read_flame()
        except DaqError as exc:
            # Don't hard-fail the loop for a single DI read.
            self.flame_label.configure(text=f"화염 감지(DI6): 에러({exc})")
            return
        self.flame_label.configure(text=f"화염 감지(DI6): {'ON' if flame else 'OFF'}")

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
                text="열림 명령" if opened else "닫힘 명령",
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
        self.pv_value.configure(text=f"PV: {pv:.1f} cc/min")
        self.focus_pv.configure(text=f"PV\n{pv:.1f} cc/min")
        self.focus_sv.configure(text=f"SV\n{sv:.1f} cc/min")
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
