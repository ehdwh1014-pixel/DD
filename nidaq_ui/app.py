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
from tkinter import messagebox, ttk


@dataclass
class ChannelConfig:
    """DAQmx physical channels; edit these for the actual cDAQ chassis layout."""

    ai_test: str = "cDAQ1Mod2/ai1"
    ao_test: str = "cDAQ1Mod3/ao1"
    feedback_ai: str = "cDAQ1Mod2/ai2"
    feedback_ao: str = "cDAQ1Mod3/ao2"


class DaqService:
    """Small hardware boundary that makes the GUI runnable without a DAQ."""

    def __init__(self, channels: ChannelConfig) -> None:
        self.channels = channels
        self.available = False
        self.error = "NI-DAQmx를 확인하는 중입니다."
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
            devices = list(self._nidaqmx.system.System.local().devices)
            self.available = bool(devices)
            self.error = "연결됨" if self.available else "NI-DAQ 장치를 찾을 수 없습니다."
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
    def __init__(self, parent: tk.Misc, title: str, lines: list[tuple[str, str]]) -> None:
        super().__init__(parent, bg="#131b2a", highlightthickness=0, height=300)
        self.title, self.lines = title, lines
        self.series = [deque(maxlen=120) for _ in lines]
        self.bind("<Configure>", lambda _event: self.draw())

    def add(self, *values: float) -> None:
        for values_, value in zip(self.series, values):
            values_.append(float(value))
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 340), max(self.winfo_height(), 200)
        left, top, right, bottom = 52, 34, width - 20, height - 36
        self.create_text(left, 16, text=self.title, fill="#eef4ff", anchor="w",
                         font=("Arial", 12, "bold"))
        for i in range(5):
            y = top + (bottom - top) * i / 4
            value = 5 - 5 * i / 4
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
                y = bottom - (max(0.0, min(5.0, value)) / 5.0) * (bottom - top)
                coords.extend((x, y))
            self.create_line(*coords, fill=color, width=2, smooth=True)


class NidaqApp(tk.Tk):
    POLL_MS = 150

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
        self._build_style()
        self._build_layout()
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
        controls = self.panel(page)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        ttk.Label(controls, text="피드백 제어 설정", style="Panel.TLabel",
                  font=("Arial", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))
        self.sv = self.field(controls, 1, "SV 설정값 (V)", "2.50")
        self.gain = self.field(controls, 2, "Gain (P)", "1.00")
        self.pv_value = ttk.Label(controls, text="PV: 0.000 V", style="Value.TLabel")
        self.pv_value.grid(row=3, column=0, columnspan=2, sticky="w", pady=(20, 2))
        self.output_value = ttk.Label(controls, text="AO 출력: 0.000 V", style="Panel.TLabel")
        self.output_value.grid(row=4, column=0, columnspan=2, sticky="w")
        self.feedback_button = ttk.Button(controls, text="피드백 제어 시작", style="Start.TButton",
                                          command=self.toggle_feedback)
        self.feedback_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        ttk.Label(controls, text="출력 = clamp(Gain × (SV − PV), 0, 5V)", style="Panel.TLabel",
                  wraplength=225).grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.feedback_graph = TrendGraph(page, "피드백 제어 추이", [("PV", "#52c7f5"), ("SV", "#eb6e99"), ("AO", "#72d19d")])
        self.feedback_graph.grid(row=0, column=1, sticky="nsew")
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
        self.feedback_running = not self.feedback_running
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
            pv = self.daq.read_voltage(self.channels.feedback_ai)
            sv, gain = self.number_silent(self.sv, 0), self.number_silent(self.gain, 1)
            output = max(0.0, min(5.0, gain * (sv - pv)))
            self.daq.write_voltage(self.channels.feedback_ao, output)
            self.pv_value.configure(text=f"PV: {pv:.3f} V")
            self.output_value.configure(text=f"AO 출력: {output:.3f} V")
            self.feedback_graph.add(pv, sv, output)
        self.after(self.POLL_MS, self.update_loop)

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
        text = "NI-DAQ 통신 연결됨" if connected else self.daq.error
        self.status_label.configure(text=text)
        self.after(700 if connected else 2000, self.refresh_connection)

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("DAQmx 물리 채널 설정")
        dialog.configure(bg="#131b2a")
        dialog.transient(self)
        dialog.grab_set()
        values: dict[str, tk.StringVar] = {}
        labels = [("ai_test", "AI TEST (NI-9206, 2번째 채널)"),
                  ("ao_test", "AO TEST (NI-9264, 2번째 채널)"),
                  ("feedback_ai", "Feedback PV (NI-9206, 3번째 채널)"),
                  ("feedback_ao", "Feedback AO (NI-9264, 3번째 채널)")]
        for row, (field, label) in enumerate(labels):
            ttk.Label(dialog, text=label, style="Panel.TLabel").grid(row=row, column=0, padx=18, pady=10, sticky="w")
            value = tk.StringVar(value=getattr(self.channels, field))
            values[field] = value
            ttk.Entry(dialog, textvariable=value, width=28).grid(row=row, column=1, padx=(0, 18), pady=10)

        def save() -> None:
            for field, value in values.items():
                setattr(self.channels, field, value.get().strip())
            dialog.destroy()

        ttk.Button(dialog, text="저장", style="Start.TButton", command=save).grid(
            row=len(labels), column=0, columnspan=2, padx=18, pady=(12, 18), sticky="ew")


if __name__ == "__main__":
    NidaqApp().mainloop()
