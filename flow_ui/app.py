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
from flow_ui.mp5y_service import Mp5yConfig, Mp5yError, Mp5yService


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
        self.geometry("1240x800")
        self.minsize(1040, 700)
        self.configure(bg=COLORS["bg"])

        self.channels = ChannelConfig()
        self.mp5y_config = Mp5yConfig(port="COM3")
        self.daq = DaqService(self.channels)
        self.mp5y = Mp5yService(self.mp5y_config)
        self.lpf = LowPassFilter()
        self.pi = PIController(0.0, 5.0)

        self.active_page = "monitor"
        self.monitor_running = False
        self.feedback_running = False
        self.status_on = False
        self.current_ao = 0.0
        self.filtered_flow = 0.0
        self._last_loop_at: float | None = None

        self._build_style()
        self._build_layout()
        self.load_settings()
        self.show_page("monitor")
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
            text="MP5Y-25 RS485  ·  NI 9264 AO  ·  0.46 ml/P",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        status = ttk.Frame(header, style="App.TFrame")
        status.pack(side="right")
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
        for key, label in (
            ("monitor", "유량 모니터"),
            ("ao", "AO 수동 출력"),
            ("feedback", "유량 피드백 제어"),
        ):
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
        self.mon_flow = ttk.Label(controls, text="0.0 cc/min", style="Value.TLabel")
        self.mon_flow.grid(row=3, column=0, columnspan=2, sticky="w", pady=(18, 4))
        self.mon_hz = ttk.Label(controls, text="주파수: 0.00 Hz", style="ValueSmall.TLabel")
        self.mon_hz.grid(row=4, column=0, columnspan=2, sticky="w")
        self.mon_raw = ttk.Label(controls, text="MP5Y raw: 0", style="Panel.TLabel")
        self.mon_raw.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.monitor_button = ttk.Button(
            controls, text="측정 시작", style="Start.TButton", command=self.toggle_monitor
        )
        self.monitor_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        ttk.Label(
            controls,
            text="유량계 → MP5Y-25 → USB-RS485 (COM3)\n기본: MP5Y 주파수(Hz) × 0.46 × 60",
            style="Hint.TLabel",
            wraplength=250,
            justify="left",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))

        graphs = ttk.Frame(page, style="App.TFrame")
        graphs.grid(row=0, column=1, sticky="nsew")
        graphs.rowconfigure((0, 1), weight=1)
        graphs.columnconfigure(0, weight=1)
        self.mon_flow_graph = TrendGraph(
            graphs, "유량 추이", [("Flow", COLORS["pv"])], 0, 200, "cc/min"
        )
        self.mon_flow_graph.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.mon_hz_graph = TrendGraph(
            graphs, "펄스 주파수", [("Hz", COLORS["hz"])], 0, 20, "Hz"
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
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        controls = self.panel(page)
        self.place_panel(controls, row=0, column=0, sticky="nsw", padx=(0, 14))
        ttk.Label(controls, text="유량 피드백 제어", style="PanelTitle.TLabel").grid(
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

        graphs = ttk.Frame(page, style="App.TFrame")
        graphs.grid(row=0, column=1, sticky="nsew")
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
        self.feedback_graph.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.feedback_ao_graph = TrendGraph(
            graphs, "AO 펌프 전압", [("AO", COLORS["ao"])], 0, 5, "V"
        )
        self.feedback_ao_graph.grid(row=1, column=0, sticky="nsew")
        return page

    # ------------------------------------------------------------- helpers
    def show_page(self, page: str) -> None:
        self.active_page = page
        for name, frame in self.pages.items():
            frame.pack_forget()
            self.nav_buttons[name].configure(
                style="NavActive.TButton" if name == page else "Nav.TButton"
            )
        self.pages[page].pack(fill="both", expand=True)

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
        dialog.title("MP5Y-25 + NI 9264 배선 가이드")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("600x560")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=22)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(frame, text="펄스 유량계 · MP5Y-25 · NI 9264", style="PanelTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            frame,
            text="유량은 RS485(Modbus)로 읽고, 펌프만 NI-DAQ AO로 제어합니다.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        guide = (
            "【유량계 → MP5Y-25】\n"
            "  펄스 Out / GND  →  MP5Y 입력 단자 (NPN 또는 PNP 설정 일치)\n"
            "  펄스정수 0.46 ml/P\n"
            "  MP5Y 동작모드 권장: F1 주파수\n"
            "\n"
            "【MP5Y-25 → USB-RS485】\n"
            "  MP5Y A(+)  →  컨버터 A\n"
            "  MP5Y B(−)  →  컨버터 B\n"
            "  PC COM 포트: COM3\n"
            "  통신: 9600 / 8 / None / Stop2 / Addr 1 (출하 기본)\n"
            "\n"
            "【펌프 → NI 9264 ao0】\n"
            "  펌프 + 입력    →  NI 9264  ao0\n"
            "  펌프 GND/COM   →  AO GND\n"
            "\n"
            "환산: 유량(cc/min) = Hz × 0.46 × 60\n"
            "MP5Y에 프리스케일로 cc/min을 띄우면\n"
            "채널 설정에서 표시모드를 'flow_ccpm'으로 바꾸세요."
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
        dialog.geometry("520x420")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=20)
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        ttk.Label(frame, text="MP5Y / NI-DAQ", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        ao_var = tk.StringVar(value=self.channels.ao_pump)
        port_var = tk.StringVar(value=self.mp5y_config.port)
        slave_var = tk.StringVar(value=str(self.mp5y_config.slave_id))
        baud_var = tk.StringVar(value=str(self.mp5y_config.baudrate))
        mode_var = tk.StringVar(value=self.mp5y_config.value_mode)
        for row, label, var in (
            (1, "AO 펌프 채널", ao_var),
            (2, "MP5Y COM 포트", port_var),
            (3, "MP5Y 주소", slave_var),
            (4, "MP5Y Baud", baud_var),
            (5, "표시모드 (frequency_hz/flow_ccpm)", mode_var),
        ):
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=8)
            ttk.Entry(frame, textvariable=var, width=28).grid(row=row, column=1, padx=(12, 0))

        def apply() -> None:
            self.channels.ao_pump = ao_var.get().strip() or self.channels.ao_pump
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
            self.mp5y_config.value_mode = mode
            self.mp5y.close()
            self.mp5y = Mp5yService(self.mp5y_config)
            self.refresh_connection()
            dialog.destroy()

        ttk.Button(frame, text="적용", style="Start.TButton", command=apply).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(18, 0)
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

    # --------------------------------------------------------------- loops
    def refresh_connection(self) -> None:
        ao_ok = self.daq.check_connection()
        # Avoid holding the serial port locked during idle; probe then release.
        mp_ok = self.mp5y.check_connection()
        self.mp5y.close()

        connected = ao_ok or mp_ok
        self.status_on = not self.status_on if connected else False
        if ao_ok and mp_ok:
            color = COLORS["ok"] if self.status_on else "#A8D8C0"
            text = "MP5Y + NI-DAQ 연결됨"
        elif mp_ok:
            color = COLORS["warn"]
            text = f"MP5Y OK / AO: {self.daq.error}"
        elif ao_ok:
            color = COLORS["warn"]
            text = f"AO OK / MP5Y: {self.mp5y.error}"
        else:
            color = COLORS["bad"]
            text = "시뮬레이션 (COM3/NI 미연결)"

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
        stop_error = ""
        try:
            self.current_ao = self.daq.write_voltage(0.0)
        except DaqError as exc:
            stop_error = f" / 0 V 출력 실패: {exc}"
        self.mp5y.close()
        self.monitor_button.configure(text="측정 시작", style="Start.TButton")
        self.feedback_button.configure(text="피드백 제어 시작", style="Start.TButton")
        self.output_value.configure(text=f"AO: {self.current_ao:.3f} V")
        self.status_canvas.itemconfigure(self.status_dot, fill=COLORS["bad"])
        self.status_label.configure(text=f"안전 정지: {error}{stop_error}")

    def _read_mp5y(self, pulse_ml: float) -> tuple[float, float, int, int]:
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
        self.mon_flow.configure(text=f"{flow:.1f} cc/min")
        self.mon_hz.configure(text=f"주파수: {hz_text} Hz")
        self.mon_raw.configure(text=f"MP5Y raw: {raw} (DOT={dot})")
        self.mon_flow_graph.set_scale(0, max(200.0, flow * 1.4 + 20), "cc/min")
        self.mon_flow_graph.add(flow)
        self.mon_hz_graph.add(0.0 if math.isnan(hz) else hz)

    def _update_feedback(self) -> None:
        pulse_ml = self.number_silent(self.fb_pulse_ml, DEFAULT_PULSE_ML)
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
        self.fb_hz.configure(text=f"주파수: {hz_text} Hz")
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
            "mp5y_port": self.mp5y_config.port,
            "mp5y_slave_id": self.mp5y_config.slave_id,
            "mp5y_baudrate": self.mp5y_config.baudrate,
            "mp5y_value_mode": self.mp5y_config.value_mode,
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
        self.daq.channels = self.channels
        self.mp5y_config.port = str(data.get("mp5y_port", "COM3"))
        self.mp5y_config.slave_id = int(data.get("mp5y_slave_id", 1))
        self.mp5y_config.baudrate = int(data.get("mp5y_baudrate", 9600))
        self.mp5y_config.value_mode = str(data.get("mp5y_value_mode", "frequency_hz"))
        self.mp5y = Mp5yService(self.mp5y_config)

    def on_close(self) -> None:
        try:
            self.feedback_running = False
            self.monitor_running = False
            self.daq.close()
            self.mp5y.close()
        finally:
            self.destroy()


def main() -> None:
    app = FlowControlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
