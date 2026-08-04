"""Pulse flow feedback control UI for NI 9422 + NI 9264.

Run:
  python -m flow_ui.app
  or
  python flow_ui/app.py
"""

from __future__ import annotations

import json
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
from flow_ui.daq_service import ChannelConfig, DaqService
from flow_ui.flow_math import DEFAULT_PULSE_ML, frequency_to_ccpm


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
        self.daq = DaqService(self.channels)
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
        self.after(1500, self.refresh_connection)
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
            text="NI 9422 Counter  ·  NI 9264 AO  ·  OF05ZAT-AR 0.46 ml/P",
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
        # Outer card widget used by place_panel().
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
        self.mon_pulses = ttk.Label(controls, text="누적 펄스: 0", style="Panel.TLabel")
        self.mon_pulses.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.monitor_button = ttk.Button(
            controls, text="측정 시작", style="Start.TButton", command=self.toggle_monitor
        )
        self.monitor_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        ttk.Label(
            controls,
            text="OF05ZAT-AR → 9422 DI0+/DI0− (PFI0)\n풀업 없이 전압 펄스 카운트",
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
            text="9264 ao0 → 펌프 전압\nAO GND는 유량계/DI0−와 공통 GND",
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
            text="AO = clamp(P×오차 + I×∫오차 dt, 0~5V)\nPV는 펄스 유량 + LPF",
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
        dialog.title("OF05ZAT-AR 배선 가이드")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("560x520")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=22)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(frame, text="OF05ZAT-AR · 전압 펄스 배선", style="PanelTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            frame,
            text="AR 타입은 외부 풀업 저항이 필요 없습니다.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        guide = (
            "【유량계 → NI 9422 DI0】\n"
            "  Red   (+V)     →  PSU +12V 또는 +24V\n"
            "  Black (GND)    →  PSU GND (−)\n"
            "  White (Out)    →  NI 9422  DI0+\n"
            "  NI 9422 DI0−   →  PSU GND (−)   ★ 필수\n"
            "\n"
            "【펌프 → NI 9264 ao0】\n"
            "  펌프 + 입력    →  NI 9264  ao0\n"
            "  펌프 GND/COM   →  PSU GND (−)\n"
            "\n"
            "【공통 GND — 한 점에 묶기】\n"
            "  1) 유량계 Black\n"
            "  2) NI 9422 DI0−\n"
            "  3) NI 9264 AO GND\n"
            "\n"
            "채널: 카운터 cDAQ2/ctr0 ← /cDAQ2Mod2/PFI0\n"
            "      펌프 AO  cDAQ2Mod1/ao0  (0~5 V)"
        )
        box = tk.Text(
            frame,
            height=18,
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
        dialog.title("채널 설정")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("460x280")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=20)
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        ttk.Label(frame, text="DAQmx 채널", style="PanelTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        ao_var = tk.StringVar(value=self.channels.ao_pump)
        ctr_var = tk.StringVar(value=self.channels.counter)
        term_var = tk.StringVar(value=self.channels.counter_term)
        for row, label, var in (
            (1, "AO 펌프 채널", ao_var),
            (2, "카운터 채널", ctr_var),
            (3, "카운터 터미널", term_var),
        ):
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=8)
            ttk.Entry(frame, textvariable=var, width=28).grid(row=row, column=1, padx=(12, 0))

        def apply() -> None:
            self.channels.ao_pump = ao_var.get().strip()
            self.channels.counter = ctr_var.get().strip()
            self.channels.counter_term = term_var.get().strip()
            self.daq.channels = self.channels
            self.refresh_connection()
            dialog.destroy()

        ttk.Button(frame, text="적용", style="Start.TButton", command=apply).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(18, 0)
        )

    # -------------------------------------------------------------- actions
    def toggle_monitor(self) -> None:
        if self.feedback_running:
            messagebox.showinfo("안내", "피드백 제어 중에는 모니터를 따로 시작할 수 없습니다.")
            return
        self.monitor_running = not self.monitor_running
        if self.monitor_running:
            self.daq.start_counter()
            self.lpf.reset()
            self._last_loop_at = time.monotonic()
            self.mon_flow_graph.clear()
            self.mon_hz_graph.clear()
        else:
            self.daq.stop_counter()
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
        self.current_ao = self.daq.write_voltage(voltage)
        self.ao_value.configure(text=f"현재 출력: {self.current_ao:.3f} V")
        self.ao_graph.add(self.current_ao)

    def zero_ao(self) -> None:
        self.ao_voltage.set("0.0")
        self.current_ao = self.daq.write_voltage(0.0)
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

        self.feedback_running = not self.feedback_running
        if self.feedback_running:
            self.daq.start_counter()
            self.pi.reset()
            self.lpf.reset()
            self._last_loop_at = time.monotonic()
            self.feedback_graph.clear()
            self.feedback_ao_graph.clear()
            sv = self.number_silent(self.sv, 120)
            self.feedback_graph.set_scale(0, max(200.0, sv * 1.5), "cc/min")
        else:
            self.daq.stop_counter()
            self.current_ao = self.daq.write_voltage(0.0)
            self.output_value.configure(text=f"AO: {self.current_ao:.3f} V")

        self.feedback_button.configure(
            text="피드백 제어 중지" if self.feedback_running else "피드백 제어 시작",
            style="Stop.TButton" if self.feedback_running else "Start.TButton",
        )

    # --------------------------------------------------------------- loops
    def refresh_connection(self) -> None:
        connected = self.daq.check_connection()
        self.status_on = not self.status_on if connected else False
        color = COLORS["ok"] if connected and self.status_on else (
            "#A8D8C0" if connected else COLORS["bad"]
        )
        self.status_canvas.itemconfigure(self.status_dot, fill=color)
        self.status_label.configure(
            text=self.daq.error if not connected else "NI-DAQ 연결됨"
        )
        self.device_label.configure(text=self.daq.device_summary)
        self.after(1000, self.refresh_connection)

    def update_loop(self) -> None:
        try:
            if self.monitor_running:
                self._update_monitor()
            if self.feedback_running:
                self._update_feedback()
        except Exception as exc:  # noqa: BLE001
            self.status_label.configure(text=f"루프 오류: {exc}")
        self.after(self.POLL_MS, self.update_loop)

    def _update_monitor(self) -> None:
        hz, _elapsed, _delta = self.daq.read_pulse_rate()
        pulse_ml = self.number_silent(self.pulse_ml, DEFAULT_PULSE_ML)
        raw = frequency_to_ccpm(hz, pulse_ml)
        now = time.monotonic()
        dt = max(now - (self._last_loop_at or now), 0.001)
        self._last_loop_at = now
        flow = self.lpf.update(raw, dt, 1.0)
        self.filtered_flow = flow
        self.mon_flow.configure(text=f"{flow:.1f} cc/min")
        self.mon_hz.configure(text=f"주파수: {hz:.2f} Hz")
        self.mon_pulses.configure(text=f"누적 펄스: {self.daq._last_count}")
        self.mon_flow_graph.set_scale(0, max(200.0, flow * 1.4 + 20), "cc/min")
        self.mon_flow_graph.add(flow)
        self.mon_hz_graph.add(hz)

    def _update_feedback(self) -> None:
        hz, _elapsed, _delta = self.daq.read_pulse_rate()
        pulse_ml = self.number_silent(self.fb_pulse_ml, DEFAULT_PULSE_ML)
        raw = frequency_to_ccpm(hz, pulse_ml)
        now = time.monotonic()
        dt = max(now - (self._last_loop_at or now), 0.001)
        self._last_loop_at = now

        cutoff = self.number_silent(self.lpf_cutoff, 0.8)
        pv = self.lpf.update(raw, dt, cutoff)
        self.filtered_flow = pv
        sv = self.number_silent(self.sv, 120.0)
        p_gain = self.number_silent(self.p_gain, 0.02)
        i_gain = self.number_silent(self.i_gain, 0.005)

        ao = self.pi.update(sv, pv, p_gain, i_gain, dt)
        self.current_ao = self.daq.write_voltage(ao)

        self.pv_value.configure(text=f"PV: {pv:.1f} cc/min")
        self.fb_hz.configure(text=f"주파수: {hz:.2f} Hz")
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
            "counter": self.channels.counter,
            "counter_term": self.channels.counter_term,
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
        self.channels.counter = str(data.get("counter", self.channels.counter))
        self.channels.counter_term = str(data.get("counter_term", self.channels.counter_term))
        self.daq.channels = self.channels

    def on_close(self) -> None:
        try:
            self.feedback_running = False
            self.monitor_running = False
            self.daq.close()
        finally:
            self.destroy()


def main() -> None:
    app = FlowControlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
