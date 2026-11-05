"""AOM scanner with an explicitly assumed 80% power-meter response."""

from __future__ import annotations

import csv
from datetime import datetime
import math
from pathlib import Path
import queue
import statistics
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib import rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from core import HARD_MAX_MVPP, analyze_stability, build_inclusive_points, efficiency_percent
from devices import (
    PM100D,
    RigolDG,
    SimulatedPM100D,
    SimulatedRigol,
    discover_rigol_generators,
    interruptible_sleep,
)


APP_DIR = Path(__file__).resolve().parent
MEASUREMENT_DIR = APP_DIR / "measurements"
DEFAULT_DG_RESOURCE = "USB0::0x1AB1::0x0647::DG5Q273700076::INSTR"
PM_SERIAL = ""  # Blank means: use the only Thorlabs meter currently attached.
WAVELENGTH_NM = 556.0
RF_FREQUENCY_HZ = 100e6
INCIDENT_DURATION_S = 60.0
# 前 10 秒用于把探头摆稳，不计入统计；平均与入射参考只取后 50 秒。
INCIDENT_WARMUP_S = 10.0
INCIDENT_SAMPLE_INTERVAL_S = 0.20
SCAN_SETTLE_S = 0.40
SCAN_SAMPLES_PER_POINT = 5
SCAN_SAMPLE_INTERVAL_S = 0.10
METER_RESPONSE = 0.80  # Measured / actual; applies to BOTH measurements.


def corrected_power_mw(raw_power_mw: float) -> float:
    return raw_power_mw / METER_RESPONSE

rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False


class SimpleAOMApp:
    def __init__(self, root: tk.Tk, simulation: bool = False) -> None:
        self.root = root
        self.root.title("AOM 双程衍射效率扫描 · 功率计80%修正版")
        self.root.geometry("1380x860")
        self.root.minsize(1120, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.simulation_requested = simulation
        self.simulation = False
        self.generator = None
        self.power_meter = None
        self.worker: threading.Thread | None = None
        self.busy_kind = ""
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.closing = False

        self.incident_samples: list[tuple[float, float]] = []
        self.pending_incident_mean_mw: float | None = None
        self.accepted_incident_mw: float | None = None
        self.scan_points: list[dict[str, float]] = []

        self._build_style()
        self._build_variables()
        self._build_ui()
        self._reset_all_plots()
        self.load_latest_incident_csv()
        self._update_button_states()
        self.root.after(100, self._process_events)
        self.root.after(350, self.connect_devices)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Metric.TLabel", font=("Consolas", 11, "bold"), foreground="#0b6680")
        style.configure("Accepted.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground="#16733a")
        style.configure("Status.TLabel", foreground="#4b5963")

    def _build_variables(self) -> None:
        self.connection_var = tk.StringVar(value="正在连接仪器…")
        self.status_var = tk.StringVar(value="程序已启动；RF 保持关闭。")
        self.incident_live_var = tk.StringVar(value="当前功率：-- mW")
        self.incident_stats_var = tk.StringVar(value="尚未完成入射测量（前 10 秒不计入）")
        self.accepted_var = tk.StringVar(value="尚未采用入射平均值（后 50 秒）")
        self.scan_live_var = tk.StringVar(value="Pdouble-pass = -- mW    η = -- %")
        self.start_mvpp_var = tk.StringVar(value="0")
        self.stop_mvpp_var = tk.StringVar(value="550")
        self.step_mvpp_var = tk.StringVar(value="10")
        self.progress_var = tk.DoubleVar(value=0.0)

    def _build_ui(self) -> None:
        outer = ttk.Panedwindow(self.root, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        left = ttk.Frame(outer, width=430)
        right = ttk.Frame(outer)
        outer.add(left, weight=0)
        outer.add(right, weight=1)

        ttk.Label(left, text="AOM 双程效率扫描 · 80%修正", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="功率计读数 = 实际功率 × 80%；显示与曲线：实际功率 = 读数 ÷ 0.80", foreground="#a03d12", wraplength=410).pack(anchor="w", pady=(2, 2))
        ttk.Label(
            left,
            text="先测 60 秒入射功率（前 10 秒用于摆稳探头、不计入；采用后 50 秒平均值），再把同一只探头移到 double-pass 输出做幅度扫描。",
            wraplength=410,
        ).pack(anchor="w", pady=(4, 10))

        connection = ttk.LabelFrame(left, text="仪器", style="Section.TLabelframe")
        connection.pack(fill="x", pady=4)
        ttk.Label(
            connection,
            text="RIGOL DG5352 Pro · CH1 · 100 MHz\nThorlabs 功率计 · 自动识别 · 556 nm",
            justify="left",
        ).pack(anchor="w", padx=8, pady=(7, 3))
        connection_row = ttk.Frame(connection)
        connection_row.pack(fill="x", padx=8, pady=(3, 7))
        self.connect_button = ttk.Button(connection_row, text="重新连接", command=self.connect_devices)
        self.connect_button.pack(side="left")
        ttk.Label(connection_row, textvariable=self.connection_var, style="Status.TLabel").pack(
            side="left", padx=10
        )

        incident = ttk.LabelFrame(left, text="1 · 测 AOM 入射功率", style="Section.TLabelframe")
        incident.pack(fill="x", pady=6)
        ttk.Label(
            incident,
            text=(
                "把探头放到 AOM 入射位置。点击后连续测量 60 秒；"
                "统计和入射参考只使用第 10 秒之后的数据（后 50 秒）。"
                "DG 已连接时程序会关 RF，未连接时请手动确认 RF 已关闭。"
            ),
            wraplength=390,
        ).pack(anchor="w", padx=8, pady=(7, 4))
        incident_buttons = ttk.Frame(incident)
        incident_buttons.pack(fill="x", padx=8, pady=4)
        self.incident_button = ttk.Button(
            incident_buttons, text="开始测量 60 秒", command=self.start_incident_measurement
        )
        self.incident_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.accept_button = ttk.Button(
            incident_buttons, text="采用这个平均值", command=self.accept_incident_mean
        )
        self.accept_button.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.progress = ttk.Progressbar(
            incident,
            variable=self.progress_var,
            maximum=INCIDENT_DURATION_S,
            mode="determinate",
        )
        self.progress.pack(fill="x", padx=8, pady=(4, 5))
        ttk.Label(incident, textvariable=self.incident_live_var, style="Metric.TLabel").pack(
            anchor="w", padx=8, pady=2
        )
        ttk.Label(incident, textvariable=self.incident_stats_var, wraplength=390).pack(
            anchor="w", padx=8, pady=2
        )
        ttk.Label(incident, textvariable=self.accepted_var, style="Accepted.TLabel", wraplength=390).pack(
            anchor="w", padx=8, pady=(2, 8)
        )

        scan = ttk.LabelFrame(left, text="2 · 扫描 double-pass 输出", style="Section.TLabelframe")
        scan.pack(fill="x", pady=6)
        ttk.Label(
            scan,
            text="采用入射平均值后，把探头移到 double-pass 输出；扫描期间不要再动光路。",
            wraplength=390,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(7, 5))
        self._add_entry(scan, 1, "起点 (mVpp)", self.start_mvpp_var)
        self._add_entry(scan, 2, "终点 (mVpp)", self.stop_mvpp_var)
        self._add_entry(scan, 3, "步长 (mVpp)", self.step_mvpp_var)
        ttk.Label(
            scan,
            text=f"固定 100 MHz；程序上限 {HARD_MAX_MVPP:g} mVpp",
            foreground="#5a6570",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(3, 1))
        ttk.Label(
            scan,
            text="η = Pdouble-pass / 已采用的入射平均功率 × 100%",
            foreground="#5a6570",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(1, 5))
        scan_buttons = ttk.Frame(scan)
        scan_buttons.grid(row=6, column=0, columnspan=2, sticky="ew", padx=8, pady=5)
        self.scan_button = ttk.Button(scan_buttons, text="开始扫描", command=self.start_scan)
        self.scan_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.stop_button = ttk.Button(scan_buttons, text="停止并关闭 RF", command=self.request_stop)
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Label(scan, textvariable=self.scan_live_var, style="Metric.TLabel", wraplength=390).grid(
            row=7, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 8)
        )
        scan.columnconfigure(1, weight=1)

        note = (
            "简化模式不单独扣暗功率；若探头无光时读数相对信号不可忽略，"
            "请先在 Thorlabs 功率计上执行归零。"
        )
        ttk.Label(left, text=note, wraplength=410, foreground="#7a4b14").pack(anchor="w", pady=(6, 3))
        ttk.Separator(left).pack(fill="x", pady=(7, 6))
        ttk.Label(left, textvariable=self.status_var, wraplength=410, style="Status.TLabel").pack(anchor="w")

        ttk.Label(right, text="实时曲线", style="Title.TLabel").pack(anchor="w", padx=4)
        self.figure = Figure(figsize=(8.6, 7.6), dpi=100, constrained_layout=True)
        grid = self.figure.add_gridspec(3, 1)
        self.incident_axes = self.figure.add_subplot(grid[0, 0])
        self.power_axes = self.figure.add_subplot(grid[1, 0])
        self.efficiency_axes = self.figure.add_subplot(grid[2, 0])
        self.canvas = FigureCanvasTkAgg(self.figure, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(5, 0))

    def _add_entry(self, parent, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(parent, textvariable=variable, width=14).grid(
            row=row, column=1, sticky="ew", padx=8, pady=3
        )

    def _reset_all_plots(self) -> None:
        self.incident_axes.clear()
        self.incident_axes.set(
            title="AOM 入射功率稳定性（60 秒；前 10 秒不计入）",
            xlabel="时间 (s)",
            ylabel="修正后入射功率 (mW)",
        )
        self.incident_axes.grid(True, alpha=0.28)
        (self.incident_line,) = self.incident_axes.plot([], [], color="#1479b8", linewidth=1.25)

        self.power_axes.clear()
        self.power_axes.set(
            title="Double-pass 输出功率（80%修正）", xlabel="发生器幅度 (mVpp)", ylabel="修正后功率 (mW)"
        )
        self.power_axes.grid(True, alpha=0.28)
        (self.power_line,) = self.power_axes.plot([], [], "o-", color="#d35f2a", markersize=4)

        self.efficiency_axes.clear()
        self.efficiency_axes.set(
            title="Double-pass 衍射效率", xlabel="发生器幅度 (mVpp)", ylabel="效率 (%)"
        )
        self.efficiency_axes.grid(True, alpha=0.28)
        (self.efficiency_line,) = self.efficiency_axes.plot(
            [], [], "o-", color="#27894b", markersize=4
        )
        self.canvas.draw_idle()

    def _reset_incident_plot(self) -> None:
        self.incident_axes.clear()
        self.incident_axes.set(
            title="AOM 入射功率稳定性（60 秒；前 10 秒不计入）",
            xlabel="时间 (s)",
            ylabel="修正后入射功率 (mW)",
        )
        self.incident_axes.grid(True, alpha=0.28)
        (self.incident_line,) = self.incident_axes.plot([], [], color="#1479b8", linewidth=1.25)
        self.canvas.draw_idle()

    def _reset_scan_plots(self) -> None:
        for axes, title, ylabel, color in (
            (self.power_axes, "Double-pass 输出功率（80%修正）", "修正后功率 (mW)", "#d35f2a"),
            (self.efficiency_axes, "Double-pass 衍射效率（实时）", "效率 (%)", "#27894b"),
        ):
            axes.clear()
            axes.set(title=title, xlabel="发生器幅度 (mVpp)", ylabel=ylabel)
            axes.grid(True, alpha=0.28)
            line = axes.plot([], [], "o-", color=color, markersize=4)[0]
            if axes is self.power_axes:
                self.power_line = line
            else:
                self.efficiency_line = line
        self.canvas.draw_idle()

    def emit(self, kind: str, payload=None) -> None:
        self.events.put((kind, payload))

    def _thread_log(self, message: str) -> None:
        self.emit("status", message)

    def _start_worker(self, kind: str, target) -> bool:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning("仪器忙", "当前操作还没有结束。")
            return False
        self.busy_kind = kind
        self.stop_event = threading.Event()
        self._update_button_states()

        def wrapped() -> None:
            try:
                target()
            except Exception as exc:
                self.emit("error", {"message": str(exc), "traceback": traceback.format_exc()})
            finally:
                self.emit("worker_finished", kind)

        self.worker = threading.Thread(target=wrapped, daemon=True, name=f"aom-simple-{kind}")
        self.worker.start()
        return True

    def _update_button_states(self) -> None:
        busy = bool(self.busy_kind)
        meter_connected = self.power_meter is not None
        scan_connected = self.generator is not None and meter_connected
        self.connect_button.configure(state="disabled" if busy else "normal")
        self.incident_button.configure(state="normal" if meter_connected and not busy else "disabled")
        self.accept_button.configure(
            state="normal" if self.pending_incident_mean_mw is not None and not busy else "disabled"
        )
        self.scan_button.configure(
            state=(
                "normal"
                if scan_connected and self.accepted_incident_mw is not None and not busy
                else "disabled"
            )
        )
        self.stop_button.configure(
            state="normal" if self.busy_kind in ("incident", "scan") else "disabled"
        )

    def connect_devices(self) -> None:
        if self.busy_kind:
            return
        self._close_devices()
        self.connection_var.set("正在连接…")
        self.status_var.set("正在连接信号发生器和红色 Thorlabs 功率计。")

        def work() -> None:
            generator = None
            meter = None
            errors: list[str] = []
            if self.simulation_requested:
                generator = SimulatedRigol(self._thread_log)
                meter = SimulatedPM100D(generator, WAVELENGTH_NM)
                simulation = True
            else:
                simulation = False
                try:
                    meter = PM100D(PM_SERIAL, WAVELENGTH_NM)
                except Exception as exc:
                    errors.append(f"功率计：{exc}")
                try:
                    try:
                        generator = RigolDG(DEFAULT_DG_RESOURCE, self._thread_log)
                    except Exception as first_error:
                        found = discover_rigol_generators()
                        if not found:
                            raise RuntimeError("没有发现 RIGOL DG 系列信号发生器。") from first_error
                        generator = RigolDG(found[0]["resource"], self._thread_log)
                except Exception as exc:
                    errors.append(f"信号发生器：{exc}")
            if generator is None and meter is None:
                if generator is not None:
                    generator.close()
                if meter is not None:
                    meter.close()
                raise RuntimeError("两台仪器都未连接。" + "；".join(errors))
            self.emit(
                "connected",
                {
                    "generator": generator,
                    "meter": meter,
                    "simulation": simulation,
                    "errors": errors,
                },
            )

        self._start_worker("connect", work)

    def start_incident_measurement(self) -> None:
        if self.power_meter is None:
            messagebox.showerror("未连接", "请先连接 Thorlabs 功率计。")
            return

        self.pending_incident_mean_mw = None
        self.accepted_incident_mw = None
        self.accepted_var.set("尚未采用入射平均值（后 50 秒）")
        self.incident_stats_var.set("正在测量…")
        self._update_button_states()
        duration = 5.0 if self.simulation else INCIDENT_DURATION_S
        warmup = INCIDENT_WARMUP_S * duration / INCIDENT_DURATION_S

        def work() -> None:
            if self.generator is not None:
                self.generator.output_off()
            if self.simulation:
                self.power_meter.mode = "incident"
            self.emit("incident_reset", {"duration": duration})
            samples: list[tuple[float, float]] = []
            started = time.monotonic()
            while not self.stop_event.is_set():
                elapsed = time.monotonic() - started
                if elapsed >= duration and samples:
                    break
                power_mw = corrected_power_mw(self.power_meter.read_mw())
                elapsed = time.monotonic() - started
                samples.append((elapsed, power_mw))
                self.emit("incident_sample", (elapsed, power_mw))
                remaining = duration - (time.monotonic() - started)
                if remaining <= 0:
                    break
                if not interruptible_sleep(
                    self.stop_event, min(INCIDENT_SAMPLE_INTERVAL_S, remaining)
                ):
                    break

            if self.stop_event.is_set():
                self.emit("incident_stopped", len(samples))
                return
            if len(samples) < 3:
                raise RuntimeError("入射功率样本不足。")
            effective = [item for item in samples if item[0] >= warmup - 1e-9]
            if len(effective) < 3:
                raise RuntimeError(f"第 {warmup:g} 秒之后的有效样本不足 3 个。")

            result = analyze_stability(effective, dark_mw=0.0, threshold_pct=1e9)
            MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = MEASUREMENT_DIR / f"{stamp}_incident_60s_corrected80.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["elapsed_s", "incident_power_mW", "meter_raw_mW", "meter_response_factor"])
                for elapsed, power_mw in samples:
                    writer.writerow([f"{elapsed:.6f}", f"{power_mw:.12g}", f"{power_mw * METER_RESPONSE:.12g}", METER_RESPONSE])
            self.emit(
                "incident_done",
                {
                    "result": result,
                    "path": path,
                    "warmup_s": warmup,
                    "total": len(samples),
                    "effective_count": len(effective),
                },
            )

        self._start_worker("incident", work)

    def _apply_incident_result(
        self,
        result,
        warmup_s: float,
        total: int,
        effective_count: int,
        source: str = "",
    ) -> None:
        """采用后 50 秒统计；整条曲线保留在图上，只把预热段单独标出。"""
        self.pending_incident_mean_mw = result.mean_raw_mw
        prefix = f"［{source}］" if source else ""
        self.incident_stats_var.set(
            f"{prefix}后 {result.duration_s:.1f} 秒平均 {result.mean_raw_mw:.6g} mW"
            f"（{effective_count}/{total} 点，第 {warmup_s:g} 秒后计入）；"
            f"相对 σ {result.relative_std_pct:.3f}%；峰峰 {result.peak_to_peak_pct:.3f}%；"
            f"漂移 {result.fitted_drift_pct_per_min:+.3f}%/min"
        )
        self.incident_axes.axvline(warmup_s, color="#8a8a8a", linestyle=":", linewidth=1.0)
        self.incident_axes.axhline(
            result.mean_raw_mw,
            color="#d35f2a",
            linestyle="--",
            linewidth=1.1,
            label=f"后 50 秒平均 {result.mean_raw_mw:.6g} mW",
        )
        self.incident_axes.legend(loc="best")
        self.canvas.draw_idle()

    def load_latest_incident_csv(self) -> None:
        """载入最近一次保存的 60 秒入射数据，避免为了一个新口径重测。"""
        if not MEASUREMENT_DIR.is_dir():
            return
        try:
            candidates = sorted(
                MEASUREMENT_DIR.glob("*_incident_60s_corrected80.csv"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        if not candidates:
            return
        path = candidates[0]
        samples: list[tuple[float, float]] = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    samples.append((float(row["elapsed_s"]), float(row["incident_power_mW"])))
        except (OSError, KeyError, TypeError, ValueError):
            return
        if len(samples) < 3:
            return
        effective = [item for item in samples if item[0] >= INCIDENT_WARMUP_S - 1e-9]
        if len(effective) < 3:
            return
        self.incident_samples = samples
        self.incident_line.set_data(
            [item[0] for item in samples], [item[1] for item in samples]
        )
        self.incident_axes.relim()
        self.incident_axes.autoscale_view()
        result = analyze_stability(effective, dark_mw=0.0, threshold_pct=1e9)
        self._apply_incident_result(
            result,
            warmup_s=INCIDENT_WARMUP_S,
            total=len(samples),
            effective_count=len(effective),
            source=path.name,
        )
        self.incident_live_var.set(f"已载入 {path.name}，共 {len(samples)} 点")

    def accept_incident_mean(self) -> None:
        if self.pending_incident_mean_mw is None:
            return
        self.accepted_incident_mw = self.pending_incident_mean_mw
        self.accepted_var.set(
            f"已采用后 50 秒入射平均值：{self.accepted_incident_mw:.6g} mW"
        )
        self.status_var.set("入射参考已采用。现在把探头移到 double-pass 输出，再开始扫描。")
        self._update_button_states()

    def _parse_scan_points(self) -> list[float]:
        values = []
        for variable, label in (
            (self.start_mvpp_var, "起点"),
            (self.stop_mvpp_var, "终点"),
            (self.step_mvpp_var, "步长"),
        ):
            try:
                value = float(variable.get().strip())
            except ValueError as exc:
                raise ValueError(f"{label}必须是数字。") from exc
            if not math.isfinite(value):
                raise ValueError(f"{label}必须是有限数值。")
            values.append(value)
        return build_inclusive_points(*values, allow_zero=True)

    def start_scan(self) -> None:
        if self.generator is None or self.power_meter is None:
            messagebox.showerror("未连接", "请先连接两台仪器。")
            return
        if self.accepted_incident_mw is None:
            messagebox.showerror("缺少入射参考", "请先完成 60 秒测量并点击“采用这个平均值”。")
            return
        try:
            points = self._parse_scan_points()
        except Exception as exc:
            messagebox.showerror("扫描参数错误", str(exc))
            return

        message = (
            "请确认：\n\n"
            "1. 功率计探头已经移到 double-pass 输出，并且只接收到目标输出光；\n"
            "2. AOM、反射镜和光阑都不再移动；\n"
            "3. 功放增益与供电已经固定。\n\n"
            f"确定后将固定 100 MHz，从 {points[0]:g} 扫到 {points[-1]:g} mVpp。"
            + ("\n0 mVpp 点将关闭 RF 测量残光。" if points[0] == 0 else "")
        )
        if not messagebox.askokcancel("开始扫描", message):
            return

        incident_reference = self.accepted_incident_mw

        def work() -> None:
            if self.simulation:
                self.power_meter.mode = "scan"
            self.emit("scan_reset", {"total": len(points)})
            MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = MEASUREMENT_DIR / f"{stamp}_double_pass_scan_corrected80.csv"
            stopped = False
            readings: list[dict[str, float]] = []
            try:
                with path.open("w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(
                        [
                            "timestamp",
                            "amplitude_mVpp",
                            "double_pass_power_mW",
                            "power_std_mW",
                            "incident_reference_mW",
                            "efficiency_percent",
                            "sample_count",
                            "meter_raw_mean_mW",
                            "meter_raw_std_mW",
                            "incident_meter_raw_mW",
                            "meter_response_factor",
                        ]
                    )
                    self.generator.prepare(RF_FREQUENCY_HZ)
                    for index, amplitude_mvpp in enumerate(points, start=1):
                        if self.stop_event.is_set():
                            stopped = True
                            break
                        if amplitude_mvpp == 0:
                            self.generator.output_off()
                        else:
                            self.generator.set_amplitude_mvpp(amplitude_mvpp)
                            if not self.generator.output_enabled and not self.stop_event.is_set():
                                self.generator.output_on()
                        if not interruptible_sleep(self.stop_event, SCAN_SETTLE_S):
                            stopped = True
                            break

                        values: list[float] = []
                        for sample_index in range(SCAN_SAMPLES_PER_POINT):
                            if self.stop_event.is_set():
                                stopped = True
                                break
                            values.append(corrected_power_mw(self.power_meter.read_mw()))
                            if sample_index + 1 < SCAN_SAMPLES_PER_POINT:
                                if not interruptible_sleep(
                                    self.stop_event, SCAN_SAMPLE_INTERVAL_S
                                ):
                                    stopped = True
                                    break
                        if stopped or not values:
                            break

                        power_mean = statistics.fmean(values)
                        power_std = statistics.stdev(values) if len(values) > 1 else 0.0
                        efficiency = efficiency_percent(power_mean, incident_reference, 0.0)
                        reading = {
                            "amplitude_mvpp": amplitude_mvpp,
                            "power_mw": power_mean,
                            "power_std_mw": power_std,
                            "efficiency_pct": efficiency,
                        }
                        readings.append(reading)
                        writer.writerow(
                            [
                                datetime.now().astimezone().isoformat(timespec="milliseconds"),
                                f"{amplitude_mvpp:.9g}",
                                f"{power_mean:.12g}",
                                f"{power_std:.12g}",
                                f"{incident_reference:.12g}",
                                f"{efficiency:.12g}",
                                len(values),
                                f"{power_mean * METER_RESPONSE:.12g}",
                                f"{power_std * METER_RESPONSE:.12g}",
                                f"{incident_reference * METER_RESPONSE:.12g}",
                                METER_RESPONSE,
                            ]
                        )
                        handle.flush()
                        self.emit(
                            "scan_point",
                            {"reading": reading, "index": index, "total": len(points)},
                        )
            finally:
                self.generator.output_off()

            self.emit(
                "scan_done",
                {"stopped": stopped, "readings": readings, "path": path},
            )

        self._start_worker("scan", work)

    def request_stop(self) -> None:
        if self.busy_kind not in ("incident", "scan"):
            return
        self.stop_event.set()
        try:
            if self.generator is not None:
                self.generator.output_off()
        except Exception:
            pass
        self.status_var.set("正在停止；RF 关闭命令已发送。")

    def _process_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._process_events)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "status":
            self.status_var.set(str(payload))
        elif kind == "connected":
            if self.closing:
                for device in (payload["generator"], payload["meter"]):
                    if device is not None:
                        device.close()
                return
            self.generator = payload["generator"]
            self.power_meter = payload["meter"]
            self.simulation = payload["simulation"]
            suffix = "（模拟）" if self.simulation else ""
            parts = []
            if self.power_meter is not None:
                meter_id = self.power_meter.identity
                parts.append(f"功率计 {meter_id['model']} · {meter_id['serial']}")
            else:
                parts.append("功率计未连接")
            parts.append("DG 已连接" if self.generator is not None else "DG 未连接")
            self.connection_var.set("；".join(parts) + suffix)
            if self.power_meter is not None and self.generator is None:
                self.status_var.set("功率计已连接，可先测入射；扫描前请接好 DG 控制线并重新连接。")
            elif self.power_meter is None:
                self.status_var.set("DG 已连接，但功率计未连接。请检查功率计后重新连接。")
            else:
                self.status_var.set("两台仪器已连接；RF 关闭。请先测入射功率。")
        elif kind == "incident_reset":
            self.incident_samples = []
            self.pending_incident_mean_mw = None
            self.progress.configure(maximum=payload["duration"])
            self.progress_var.set(0.0)
            self._reset_incident_plot()
            self.status_var.set("正在测 AOM 入射功率…")
        elif kind == "incident_sample":
            elapsed, power_mw = payload
            self.incident_samples.append((elapsed, power_mw))
            self.progress_var.set(min(elapsed, float(self.progress.cget("maximum"))))
            self.incident_live_var.set(f"当前功率：{power_mw:.6g} mW")
            self.incident_line.set_data(
                [item[0] for item in self.incident_samples],
                [item[1] for item in self.incident_samples],
            )
            self.incident_axes.relim()
            self.incident_axes.autoscale_view()
            self.canvas.draw_idle()
        elif kind == "incident_done":
            self.progress_var.set(float(self.progress.cget("maximum")))
            self._apply_incident_result(
                payload["result"],
                warmup_s=payload["warmup_s"],
                total=payload["total"],
                effective_count=payload["effective_count"],
            )
            self.status_var.set(
                f"60 秒测量完成（前 {payload['warmup_s']:g} 秒不计入平均）；"
                f"数据已保存：{payload['path'].name}"
            )
        elif kind == "incident_stopped":
            self.pending_incident_mean_mw = None
            self.incident_stats_var.set(f"测量已停止（只采到 {payload} 点），平均值未采用。")
            self.status_var.set("入射功率测量已停止；RF 关闭。")
        elif kind == "scan_reset":
            self.scan_points = []
            self._reset_scan_plots()
            self.status_var.set(f"扫描开始，共 {payload['total']} 个幅度点；0 点关闭 RF。")
        elif kind == "scan_point":
            reading = payload["reading"]
            self.scan_points.append(reading)
            x = [item["amplitude_mvpp"] for item in self.scan_points]
            power = [item["power_mw"] for item in self.scan_points]
            efficiency = [item["efficiency_pct"] for item in self.scan_points]
            self.power_line.set_data(x, power)
            self.efficiency_line.set_data(x, efficiency)
            for axes in (self.power_axes, self.efficiency_axes):
                axes.relim()
                axes.autoscale_view()
            self.scan_live_var.set(
                f"{reading['amplitude_mvpp']:g} mVpp    "
                f"Pdouble-pass = {reading['power_mw']:.6g} mW    "
                f"η = {reading['efficiency_pct']:.3f}%"
            )
            self.status_var.set(f"扫描 {payload['index']}/{payload['total']}")
            self.canvas.draw_idle()
        elif kind == "scan_done":
            readings = payload["readings"]
            if readings:
                maximum = max(item["efficiency_pct"] for item in readings)
                suffix = f"；本次最高效率 {maximum:.3f}%"
            else:
                suffix = ""
            state = "扫描已停止" if payload["stopped"] else "扫描完成"
            self.status_var.set(f"{state}；RF 已关闭{suffix}；数据：{payload['path'].name}")
            image_path = payload["path"].with_suffix(".png")
            try:
                self.figure.savefig(image_path, dpi=170)
            except Exception:
                pass
        elif kind == "error":
            try:
                if self.generator is not None:
                    self.generator.output_off()
            except Exception:
                pass
            self.connection_var.set("连接/测量失败")
            self.status_var.set(f"错误：{payload['message']}；RF 已关闭。")
            if not self.closing:
                messagebox.showerror("操作失败", payload["message"])
            print(payload["traceback"], file=sys.stderr)
        elif kind == "worker_finished":
            if payload == self.busy_kind:
                self.busy_kind = ""
                self._update_button_states()

    def _close_devices(self) -> None:
        for device in (self.generator, self.power_meter):
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
        self.generator = None
        self.power_meter = None

    def on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.stop_event.set()
        try:
            if self.generator is not None:
                self.generator.output_off()
        except Exception:
            pass
        self.root.withdraw()

        def finish_close() -> None:
            if self.worker is not None and self.worker.is_alive():
                self.root.after(100, finish_close)
                return
            self._close_devices()
            self.root.destroy()

        finish_close()


def main() -> None:
    simulation = "--simulate" in sys.argv[1:]
    root = tk.Tk()
    SimpleAOMApp(root, simulation=simulation)
    root.mainloop()


if __name__ == "__main__":
    main()
