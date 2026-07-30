"""NI-9206 / NI-9264 test and feedback-control desktop UI.

Run with: python app.py
NI hardware access is optional while developing: when nidaqmx is unavailable,
the UI stays usable in simulation mode and the status light is red.
"""

from __future__ import annotations

import math
import random
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
from tkinter import messagebox, ttk


# Confirmed from NI MAX:
#   Slot 2: NI 9206 -> cDAQ1Mod2 (S/N 01F4D2D3)
#   Slot 3: NI 9264 -> cDAQ1Mod3 (S/N 01FA4B72)
# First module/slot is reserved for another system and left unused.
AI_MODULE = "cDAQ1Mod2"
AO_MODULE = "cDAQ1Mod3"


@dataclass
class ChannelConfig:
    """DAQmx physical channels matching the installed NI 9206 / NI 9264."""

    # 2nd channel (ai1/ao1) = individual AI/AO TEST
    # 3rd channel (ai2/ao2) = feedback control PV/AO
    ai_test: str = f"{AI_MODULE}/ai1"
    ao_test: str = f"{AO_MODULE}/ao1"
    feedback_ai: str = f"{AI_MODULE}/ai2"
    feedback_ao: str = f"{AO_MODULE}/ao2"


class DaqService:
    """Small hardware boundary that makes the GUI runnable without a DAQ."""

    REQUIRED_DEVICES = (AI_MODULE, AO_MODULE)

    def __init__(self, channels: ChannelConfig) -> None:
        self.channels = channels
        self.available = False
        self.error = "NI-DAQmx를 확인하는 중입니다."
        self.device_summary = "NI 9206(cDAQ1Mod2) / NI 9264(cDAQ1Mod3) 대기"
        self._nidaqmx = None
        try:
            import nidaqmx  # type: ignore

            self._nidaqmx = nidaqmx
        except ImportError:
            self.error = "nidaqmx 패키지가 없습니다 (시뮬레이션 값 표시 중)."

    def check_connection(self) -> bool:
        if self._nidaqmx is None:
            self.available = False
            return False
        try:
            devices = {device.name: device for device in self._nidaqmx.system.System.local().devices}
            missing = [name for name in self.REQUIRED_DEVICES if name not in devices]
            if missing:
                self.available = False
                self.error = f"장치 없음: {', '.join(missing)}"
                self.device_summary = "NI MAX에서 cDAQ1Mod2 / cDAQ1Mod3 확인 필요"
                return False
            ai = devices[AI_MODULE]
            ao = devices[AO_MODULE]
            self.available = True
            self.error = "연결됨"
            self.device_summary = (
                f"NI 9206 {AI_MODULE} / NI 9264 {AO_MODULE} "
                f"(S/N {getattr(ai, 'serial_num', '?')}/{getattr(ao, 'serial_num', '?')})"
            )
        except Exception as exc:  # Hardware/driver failures should not stop UI.
            self.available = False
            self.error = f"통신 오류: {exc}"
        return self.available

    def read_voltage(self, channel: str) -> float:
        if self.available and self._nidaqmx:
            try:
                with self._nidaqmx.Task() as task:
                    task.ai_channels.add_ai_voltage_chan(channel, min_val=0, max_val=5)
                    return float(task.read())
            except Exception as exc:
                self.error = f"AI 읽기 오류: {exc}"
        return 2.5 + 1.15 * math.sin(time.monotonic() * 1.7) + random.uniform(-0.08, 0.08)

    def write_voltage(self, channel: str, voltage: float) -> None:
        voltage = max(0.0, min(5.0, voltage))
        if not (self.available and self._nidaqmx):
            return
        try:
            with self._nidaqmx.Task() as task:
                task.ao_channels.add_ao_voltage_chan(channel, min_val=0, max_val=5)
                task.write(voltage, auto_start=True)
        except Exception as exc:
            self.error = f"AO 출력 오류: {exc}"


class TrendGraph(tk.Canvas):
    def __init__(self, parent: tk.Misc, title: str, lines: list[tuple[str, str]],
                 low: float = 0.0, high: float = 5.0, unit: str = "V") -> None:
        super().__init__(parent, bg="#131b2a", highlightthickness=0, height=300)
        self.title, self.lines = title, lines
        self.series = [deque(maxlen=120) for _ in lines]
        self.low, self.high, self.unit = low, high, unit
        self.bind("<Configure>", lambda _event: self.draw())

    def add(self, *values: float) -> None:
        for values_, value in zip(self.series, values):
            values_.append(float(value))
        self.draw()

    def set_scale(self, low: float, high: float, unit: str) -> None:
        if high > low:
            self.low, self.high, self.unit = low, high, unit

    def draw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 340), max(self.winfo_height(), 200)
        left, top, right, bottom = 52, 34, width - 20, height - 36
        self.create_text(left, 16, text=self.title, fill="#eef4ff", anchor="w",
                         font=("Arial", 12, "bold"))
        for i in range(5):
            y = top + (bottom - top) * i / 4
            value = self.high - (self.high - self.low) * i / 4
            self.create_line(left, y, right, y, fill="#26354c")
            self.create_text(left - 8, y, text=f"{value:.1f}", fill="#8fa2bc",
                             anchor="e", font=("Arial", 9))
        self.create_line(left, top, left, bottom, fill="#60738e")
        self.create_line(left, bottom, right, bottom, fill="#60738e")
        for index, ((name, color), points) in enumerate(zip(self.lines, self.series)):
            self.create_rectangle(right - 125, 10 + index * 18, right - 115, 20 + index * 18,
                                  fill=color, outline="")
            self.create_text(right - 108, 15 + index * 18, text=name, fill="#c9d6e9",
                             anchor="w", font=("Arial", 9))
            if len(points) < 2:
                continue
            coords: list[float] = []
            for point_index, value in enumerate(points):
                x = left + (right - left) * point_index / (len(points) - 1)
                fraction = (value - self.low) / (self.high - self.low)
                y = bottom - max(0.0, min(1.0, fraction)) * (bottom - top)
                coords.extend((x, y))
            self.create_line(*coords, fill=color, width=2, smooth=True)


class NidaqApp(tk.Tk):
    POLL_MS = 150
    SETTINGS_PATH = Path(__file__).with_name("settings.json")

    def __init__(self) -> None:
        super().__init__()
        self.title("NI-DAQ 제어 및 시험 UI")
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.configure(bg="#0c1220")
        self.channels = ChannelConfig()
        self.daq = DaqService(self.channels)
        self.active_page = "ai"
        self.ai_running = False
        self.feedback_running = False
        self.status_on = False
        self.feedback_integral = 0.0
        self.filtered_pv: float | None = None
        self._last_feedback_at: float | None = None
        self._build_style()
        self._build_layout()
        self.load_feedback_settings()
        self.show_page("ai")
        self.refresh_connection()
        self.after(self.POLL_MS, self.update_loop)
        self.after(1000, self.refresh_connection)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#0c1220")
        style.configure("Panel.TFrame", background="#131b2a")
        style.configure("Title.TLabel", background="#0c1220", foreground="#eef4ff",
                        font=("Arial", 20, "bold"))
        style.configure("Sub.TLabel", background="#0c1220", foreground="#91a4bf",
                        font=("Arial", 10))
        style.configure("Panel.TLabel", background="#131b2a", foreground="#dbe7f8",
                        font=("Arial", 11))
        style.configure("Value.TLabel", background="#131b2a", foreground="#74d7ff",
                        font=("Arial", 22, "bold"))
        style.configure("TButton", font=("Arial", 10, "bold"), padding=(16, 10),
                        background="#263a59", foreground="#ffffff")
        style.map("TButton", background=[("active", "#34527d")])
        style.configure("Active.TButton", background="#1e7bbb")
        style.configure("Start.TButton", background="#167a52")
        style.map("Start.TButton", background=[("active", "#1c9866")])
        style.configure("Stop.TButton", background="#a53545")
        style.map("Stop.TButton", background=[("active", "#c14557")])
        style.configure("TEntry", fieldbackground="#0c1220", foreground="#eef4ff",
                        insertcolor="#eef4ff", padding=8)
        style.configure("TCombobox", fieldbackground="#0c1220", foreground="#eef4ff",
                        padding=7)

    def _build_layout(self) -> None:
        header = ttk.Frame(self, style="App.TFrame", padding=(28, 20, 28, 14))
        header.pack(fill="x")
        ttk.Label(header, text="NI-DAQ 제어 패널", style="Title.TLabel").pack(side="left")
        self.status_canvas = tk.Canvas(header, width=18, height=18, bg="#0c1220",
                                       highlightthickness=0)
        self.status_canvas.pack(side="right", padx=(8, 0))
        self.status_dot = self.status_canvas.create_oval(3, 3, 15, 15, fill="#c03d4b", outline="")
        self.status_label = ttk.Label(header, text="NI-DAQ 통신 확인 중", style="Sub.TLabel")
        self.status_label.pack(side="right")
        ttk.Button(header, text="채널 설정", command=self.open_settings).pack(side="right", padx=20)

        nav = ttk.Frame(self, style="App.TFrame", padding=(28, 0, 28, 18))
        nav.pack(fill="x")
        self.nav_buttons: dict[str, ttk.Button] = {}
        for page, label in (("ai", "AI TEST"), ("ao", "AO TEST"), ("feedback", "피드백 제어")):
            button = ttk.Button(nav, text=label, command=lambda p=page: self.show_page(p))
            button.pack(side="left", fill="x", expand=True, padx=4)
            self.nav_buttons[page] = button

        self.content = ttk.Frame(self, style="App.TFrame", padding=(28, 16, 28, 28))
        self.content.pack(fill="both", expand=True)
        self.pages = {"ai": self._create_ai_page(), "ao": self._create_ao_page(),
                      "feedback": self._create_feedback_page()}

    def panel(self, parent: tk.Misc, padding: int = 18) -> ttk.Frame:
        return ttk.Frame(parent, style="Panel.TFrame", padding=padding)

    def _create_ai_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(1, weight=1)
        controls = self.panel(page)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        ttk.Label(controls, text="AI TEST 설정", style="Panel.TLabel",
                  font=("Arial", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))
        self.ai_voltage_min = self.field(controls, 1, "전압 범위 (V)", "0.0")
        self.ai_voltage_max = self.field(controls, 2, "전압 최대 (V)", "5.0")
        self.process_type = tk.StringVar(value="유량")
        ttk.Label(controls, text="측정 단위", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Combobox(controls, textvariable=self.process_type, values=["유량", "압력"],
                     state="readonly", width=12).grid(row=3, column=1, padx=(15, 0))
        self.process_min = self.field(controls, 4, "공정 범위 최소", "0.0")
        self.process_max = self.field(controls, 5, "공정 범위 최대", "100.0")
        self.ai_value = ttk.Label(controls, text="0.000 V", style="Value.TLabel")
        self.ai_value.grid(row=6, column=0, columnspan=2, sticky="w", pady=(22, 5))
        self.ai_process_value = ttk.Label(controls, text="환산값: 0.00", style="Panel.TLabel")
        self.ai_process_value.grid(row=7, column=0, columnspan=2, sticky="w")
        self.ai_button = ttk.Button(controls, text="AI 측정 시작", style="Start.TButton",
                                    command=self.toggle_ai)
        self.ai_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        self.ai_graph = TrendGraph(page, "AI 입력 전압 추이", [("AI Voltage", "#52c7f5")])
        self.ai_graph.grid(row=0, column=1, sticky="nsew")
        return page

    def _create_ao_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(1, weight=1)
        controls = self.panel(page)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        ttk.Label(controls, text="AO TEST 설정", style="Panel.TLabel",
                  font=("Arial", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))
        self.ao_voltage = self.field(controls, 1, "출력 전압 (V)", "0.0")
        ttk.Label(controls, text="허용 범위: 0.0 ~ 5.0 V", style="Panel.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(controls, text="전압 출력", style="Start.TButton",
                   command=self.output_ao).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        self.ao_value = ttk.Label(controls, text="현재 출력: 0.000 V", style="Value.TLabel")
        self.ao_value.grid(row=4, column=0, columnspan=2, sticky="w", pady=(24, 0))
        self.ao_graph = TrendGraph(page, "AO 출력 전압 추이", [("AO Voltage", "#f8b84e")])
        self.ao_graph.grid(row=0, column=1, sticky="nsew")
        return page

    def _create_feedback_page(self) -> ttk.Frame:
        page = ttk.Frame(self.content, style="App.TFrame")
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)
        controls = self.panel(page)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        ttk.Label(controls, text="피드백 제어 설정", style="Panel.TLabel",
                  font=("Arial", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))
        self.feedback_voltage_min = self.field(controls, 1, "AI 최소 전압 (V)", "0.0")
        self.feedback_voltage_max = self.field(controls, 2, "AI 최대 전압 (V)", "5.0")
        self.feedback_process_type = tk.StringVar(value="유량")
        ttk.Label(controls, text="공정 단위", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Combobox(controls, textvariable=self.feedback_process_type, values=["유량", "압력"],
                     state="readonly", width=12).grid(row=3, column=1, padx=(15, 0))
        self.feedback_process_min = self.field(controls, 4, "공정 범위 최소", "0.0")
        self.feedback_process_max = self.field(controls, 5, "공정 범위 최대", "100.0")
        self.sv = self.field(controls, 6, "SV 설정값 (공정값)", "50.0")
        self.p_gain = self.field(controls, 7, "P Gain (V/공정값)", "0.10")
        self.i_gain = self.field(controls, 8, "I Gain (V/공정값·s)", "0.01")
        self.lpf_cutoff = self.field(controls, 9, "LPF 차단 주파수 (Hz)", "2.0")
        self.pv_value = ttk.Label(controls, text="PV: 0.00", style="Value.TLabel")
        self.pv_value.grid(row=10, column=0, columnspan=2, sticky="w", pady=(14, 2))
        self.ai_voltage_value = ttk.Label(controls, text="AI 입력: 0.000 V", style="Panel.TLabel")
        self.ai_voltage_value.grid(row=11, column=0, columnspan=2, sticky="w")
        self.output_value = ttk.Label(controls, text="AO 현재 출력: 0.000 V", style="Panel.TLabel")
        self.output_value.grid(row=12, column=0, columnspan=2, sticky="w")
        self.feedback_button = ttk.Button(controls, text="피드백 제어 시작", style="Start.TButton",
                                          command=self.toggle_feedback)
        self.feedback_button.grid(row=13, column=0, sticky="ew", pady=(18, 0))
        ttk.Button(controls, text="설정 저장", command=self.save_feedback_settings).grid(
            row=13, column=1, sticky="ew", padx=(8, 0), pady=(18, 0))
        ttk.Label(controls, text="AO = clamp(P×오차 + I×∫오차dt, 0~5V)", style="Panel.TLabel",
                  wraplength=250).grid(row=14, column=0, columnspan=2, sticky="w", pady=(12, 0))
        graphs = ttk.Frame(page, style="App.TFrame")
        graphs.grid(row=0, column=1, sticky="nsew")
        graphs.rowconfigure((0, 1), weight=1)
        graphs.columnconfigure(0, weight=1)
        self.feedback_graph = TrendGraph(
            graphs, "PV / SV 공정값 추이", [("PV", "#52c7f5"), ("SV", "#eb6e99")], 0, 100, "공정값")
        self.feedback_graph.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.feedback_ao_graph = TrendGraph(graphs, "AO 출력 전압 추이", [("AO", "#72d19d")])
        self.feedback_ao_graph.grid(row=1, column=0, sticky="nsew")
        return page

    def field(self, parent: tk.Misc, row: int, label: str, default: str) -> tk.StringVar:
        value = tk.StringVar(value=default)
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=8)
        ttk.Entry(parent, textvariable=value, width=13).grid(row=row, column=1, padx=(15, 0))
        return value

    def show_page(self, page: str) -> None:
        self.active_page = page
        for name, frame in self.pages.items():
            frame.pack_forget()
            self.nav_buttons[name].configure(style="Active.TButton" if name == page else "TButton")
        self.pages[page].pack(fill="both", expand=True)

    def number(self, variable: tk.StringVar, label: str) -> float | None:
        try:
            return float(variable.get())
        except ValueError:
            messagebox.showerror("입력 오류", f"{label}에 숫자를 입력하세요.")
            return None

    def toggle_ai(self) -> None:
        self.ai_running = not self.ai_running
        self.ai_button.configure(text="AI 측정 중지" if self.ai_running else "AI 측정 시작",
                                 style="Stop.TButton" if self.ai_running else "Start.TButton")

    def output_ao(self) -> None:
        voltage = self.number(self.ao_voltage, "출력 전압")
        if voltage is None:
            return
        if not 0 <= voltage <= 5:
            messagebox.showerror("범위 오류", "AO 출력 전압은 0.0 ~ 5.0 V여야 합니다.")
            return
        self.daq.write_voltage(self.channels.ao_test, voltage)
        self.ao_value.configure(text=f"현재 출력: {voltage:.3f} V")
        self.ao_graph.add(voltage)

    def toggle_feedback(self) -> None:
        if not self.feedback_running and not self.validate_feedback_inputs():
            return
        self.feedback_running = not self.feedback_running
        if self.feedback_running:
            self.feedback_integral = 0.0
            self.filtered_pv = None
            self._last_feedback_at = time.monotonic()
        self.feedback_button.configure(
            text="피드백 제어 중지" if self.feedback_running else "피드백 제어 시작",
            style="Stop.TButton" if self.feedback_running else "Start.TButton")

    def update_loop(self) -> None:
        if self.ai_running:
            voltage = self.daq.read_voltage(self.channels.ai_test)
            self.ai_value.configure(text=f"{voltage:.3f} V")
            low_v, high_v = self.number_silent(self.ai_voltage_min, 0), self.number_silent(self.ai_voltage_max, 5)
            low_p, high_p = self.number_silent(self.process_min, 0), self.number_silent(self.process_max, 100)
            if high_v != low_v:
                converted = low_p + (voltage - low_v) / (high_v - low_v) * (high_p - low_p)
                self.ai_process_value.configure(text=f"{self.process_type.get()} 환산값: {converted:.2f}")
            self.ai_graph.add(voltage)
        if self.feedback_running:
            raw_voltage = self.daq.read_voltage(self.channels.feedback_ai)
            low_v = self.number_silent(self.feedback_voltage_min, 0)
            high_v = self.number_silent(self.feedback_voltage_max, 5)
            low_p = self.number_silent(self.feedback_process_min, 0)
            high_p = self.number_silent(self.feedback_process_max, 100)
            raw_pv = low_p + (raw_voltage - low_v) / (high_v - low_v) * (high_p - low_p)
            now = time.monotonic()
            dt = max(now - (self._last_feedback_at or now), 0.001)
            self._last_feedback_at = now
            lpf_hz = self.number_silent(self.lpf_cutoff, 0)
            if self.filtered_pv is None or lpf_hz <= 0:
                self.filtered_pv = raw_pv
            else:
                tau = 1 / (2 * math.pi * lpf_hz)
                self.filtered_pv += dt / (tau + dt) * (raw_pv - self.filtered_pv)
            pv, sv = self.filtered_pv, self.number_silent(self.sv, 0)
            p_gain, i_gain = self.number_silent(self.p_gain, 0), self.number_silent(self.i_gain, 0)
            error = sv - pv
            integral_candidate = self.feedback_integral + error * dt
            unconstrained = p_gain * error + i_gain * integral_candidate
            output = max(0.0, min(5.0, unconstrained))
            # Integrate only when it will not increase output saturation.
            if output == unconstrained or (output == 5.0 and error < 0) or (output == 0.0 and error > 0):
                self.feedback_integral = integral_candidate
            self.daq.write_voltage(self.channels.feedback_ao, output)
            unit = self.feedback_process_type.get()
            self.pv_value.configure(text=f"PV: {pv:.2f} {unit}  |  SV: {sv:.2f} {unit}")
            self.ai_voltage_value.configure(text=f"AI 입력: {raw_voltage:.3f} V  (LPF 적용)")
            self.output_value.configure(text=f"AO 현재 출력: {output:.3f} V")
            self.feedback_graph.set_scale(low_p, high_p, unit)
            self.feedback_graph.add(pv, sv)
            self.feedback_ao_graph.add(output)
        self.after(self.POLL_MS, self.update_loop)

    def validate_feedback_inputs(self) -> bool:
        fields = [
            (self.feedback_voltage_min, "AI 최소 전압"), (self.feedback_voltage_max, "AI 최대 전압"),
            (self.feedback_process_min, "공정 범위 최소"), (self.feedback_process_max, "공정 범위 최대"),
            (self.sv, "SV"), (self.p_gain, "P Gain"), (self.i_gain, "I Gain"),
            (self.lpf_cutoff, "LPF 차단 주파수"),
        ]
        values = {label: self.number(variable, label) for variable, label in fields}
        if any(value is None for value in values.values()):
            return False
        if values["AI 최대 전압"] <= values["AI 최소 전압"]:
            messagebox.showerror("범위 오류", "AI 최대 전압은 최소 전압보다 커야 합니다.")
            return False
        if values["공정 범위 최대"] <= values["공정 범위 최소"]:
            messagebox.showerror("범위 오류", "공정 범위 최대값은 최소값보다 커야 합니다.")
            return False
        if not 0 <= values["AI 최소 전압"] < values["AI 최대 전압"] <= 5:
            messagebox.showerror("범위 오류", "AI 전압 범위는 0.0 ~ 5.0 V 안에 있어야 합니다.")
            return False
        if values["P Gain"] < 0 or values["I Gain"] < 0 or values["LPF 차단 주파수"] < 0:
            messagebox.showerror("입력 오류", "P Gain, I Gain, LPF 값은 0 이상이어야 합니다.")
            return False
        return True

    def save_feedback_settings(self) -> None:
        if not self.validate_feedback_inputs():
            return
        fields = ("feedback_voltage_min", "feedback_voltage_max", "feedback_process_min",
                  "feedback_process_max", "sv", "p_gain", "i_gain", "lpf_cutoff")
        data = {field: getattr(self, field).get() for field in fields}
        data["feedback_process_type"] = self.feedback_process_type.get()
        try:
            self.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("저장 완료", f"피드백 설정을 저장했습니다.\n{self.SETTINGS_PATH.name}")
        except OSError as exc:
            messagebox.showerror("저장 오류", f"설정을 저장할 수 없습니다.\n{exc}")

    def load_feedback_settings(self) -> None:
        try:
            data = json.loads(self.SETTINGS_PATH.read_text(encoding="utf-8"))
            for field in ("feedback_voltage_min", "feedback_voltage_max", "feedback_process_min",
                          "feedback_process_max", "sv", "p_gain", "i_gain", "lpf_cutoff"):
                if field in data:
                    getattr(self, field).set(str(data[field]))
            if data.get("feedback_process_type") in ("유량", "압력"):
                self.feedback_process_type.set(data["feedback_process_type"])
        except (OSError, json.JSONDecodeError):
            pass

    @staticmethod
    def number_silent(variable: tk.StringVar, fallback: float) -> float:
        try:
            return float(variable.get())
        except ValueError:
            return fallback

    def refresh_connection(self) -> None:
        connected = self.daq.check_connection()
        self.status_on = not self.status_on
        color = "#39cf86" if connected and self.status_on else "#1d6e50" if connected else "#c03d4b"
        self.status_canvas.itemconfigure(self.status_dot, fill=color)
        text = self.daq.device_summary if connected else self.daq.error
        self.status_label.configure(text=text)
        self.after(700 if connected else 2000, self.refresh_connection)

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("DAQmx 물리 채널 설정")
        dialog.configure(bg="#131b2a")
        dialog.transient(self)
        dialog.grab_set()
        values: dict[str, tk.StringVar] = {}
        ttk.Label(dialog, text="MAX 기준: NI 9206=cDAQ1Mod2, NI 9264=cDAQ1Mod3",
                  style="Panel.TLabel").grid(row=0, column=0, columnspan=2, padx=18, pady=(14, 4), sticky="w")
        labels = [("ai_test", "AI TEST  cDAQ1Mod2/ai1 (2번째)"),
                  ("ao_test", "AO TEST  cDAQ1Mod3/ao1 (2번째)"),
                  ("feedback_ai", "Feedback PV  cDAQ1Mod2/ai2 (3번째)"),
                  ("feedback_ao", "Feedback AO  cDAQ1Mod3/ao2 (3번째)")]
        for row, (field, label) in enumerate(labels, start=1):
            ttk.Label(dialog, text=label, style="Panel.TLabel").grid(row=row, column=0, padx=18, pady=10, sticky="w")
            value = tk.StringVar(value=getattr(self.channels, field))
            values[field] = value
            ttk.Entry(dialog, textvariable=value, width=28).grid(row=row, column=1, padx=(0, 18), pady=10)

        def save() -> None:
            for field, value in values.items():
                setattr(self.channels, field, value.get().strip())
            dialog.destroy()

        ttk.Button(dialog, text="저장", style="Start.TButton", command=save).grid(
            row=len(labels) + 1, column=0, columnspan=2, padx=18, pady=(12, 18), sticky="ew")


if __name__ == "__main__":
    NidaqApp().mainloop()
