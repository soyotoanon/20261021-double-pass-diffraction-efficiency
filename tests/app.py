"""Desktop GUI for AOM alignment and diffraction-efficiency amplitude scans."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import csv
import json
import math
from pathlib import Path
import queue
import statistics
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib import rcParams

from core import (
    HARD_MAX_MVPP,
    SweepReading,
    analyze_stability,
    build_inclusive_points,
    efficiency_percent,
    recommend_work_point,
    summarize_readings,
)
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
rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False


class AOMScanApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AOM 衍射效率自动扫描 · DG5352 Pro + PM100D")
        self.root.geometry("1500x900")
        self.root.minsize(1180, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.generator = None
        self.power_meter = None
        self.simulation = False
        self.busy_kind = ""
        self.dark_mw: float | None = None
        self.incident_mw: float | None = None
        self.stability_result = None
        self.stability_samples: list[tuple[float, float]] = []
        self.alignment_samples: list[tuple[float, float]] = []
        self.readings: list[SweepReading] = []
        self.last_output_files: list[Path] = []

        self._build_style()
        self._build_variables()
        self._build_ui()
        self.root.after(100, self._process_events)
        self.log("程序已就绪。连接前 RF 保持关闭。")

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Metric.TLabel", font=("Consolas", 11, "bold"), foreground="#0b5a8c")
        style.configure("Good.TLabel", foreground="#147a36", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Bad.TLabel", foreground="#b42318", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_variables(self) -> None:
        self.mode_var = tk.StringVar(value="真机")
        self.dg_resource_var = tk.StringVar(value=DEFAULT_DG_RESOURCE)
        self.pm_serial_var = tk.StringVar(value="M01306624")
        self.wavelength_var = tk.StringVar(value="556")
        self.frequency_var = tk.StringVar(value="100")
        self.start_var = tk.StringVar(value="300")
        self.stop_var = tk.StringVar(value="550")
        self.step_var = tk.StringVar(value="10")
        self.settle_var = tk.StringVar(value="0.4")
        self.samples_var = tk.StringVar(value="5")
        self.sample_gap_var = tk.StringVar(value="0.10")
        self.stability_seconds_var = tk.StringVar(value="60")
        self.stability_threshold_var = tk.StringVar(value="1.0")
        self.alignment_mvpp_var = tk.StringVar(value="500")
        self.gain_db_var = tk.StringVar(value="")
        self.target_fraction_var = tk.StringVar(value="80")
        self.bidirectional_var = tk.BooleanVar(value=True)
        self.allow_unstable_var = tk.BooleanVar(value=False)
        self.amp_fixed_var = tk.BooleanVar(value=False)
        self.optics_fixed_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="未连接")
        self.live_var = tk.StringVar(value="P = -- mW    η = -- %")
        self.reference_var = tk.StringVar(value="暗功率：未测    入射参考：未测")
        self.stability_var = tk.StringVar(value="入射稳定性：未检查")

    def _build_ui(self) -> None:
        outer = ttk.Panedwindow(self.root, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        left = ttk.Frame(outer, width=500)
        right = ttk.Frame(outer)
        outer.add(left, weight=0)
        outer.add(right, weight=1)

        ttk.Label(left, text="AOM 衍射效率标定", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(
            left,
            text="顺序：测暗功率 → 检查入射稳定性 → 对准 AOM → 往返扫幅",
            wraplength=480,
        ).pack(anchor="w", pady=(0, 8))

        connection = ttk.LabelFrame(left, text="1 · 仪器", style="Section.TLabelframe")
        connection.pack(fill="x", pady=4)
        self._grid_label_entry(connection, 0, "模式", self.mode_var, values=("真机", "模拟"))
        self._grid_label_entry(connection, 1, "DG5352 VISA", self.dg_resource_var, width=42)
        self._grid_label_entry(connection, 2, "PM100D 序列号", self.pm_serial_var)
        self._grid_label_entry(connection, 3, "波长 (nm)", self.wavelength_var)
        connection.columnconfigure(1, weight=1)
        row = ttk.Frame(connection)
        row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        ttk.Button(row, text="扫描 DG", command=self.scan_visa).pack(side="left", padx=(0, 5))
        self.connect_button = ttk.Button(row, text="连接两台仪器", command=self.connect_devices)
        self.connect_button.pack(side="left", padx=5)
        ttk.Label(row, textvariable=self.status_var).pack(side="left", padx=8)

        reference = ttk.LabelFrame(left, text="2 · 入射参考", style="Section.TLabelframe")
        reference.pack(fill="x", pady=4)
        self._grid_label_entry(reference, 0, "检查时长 (s)", self.stability_seconds_var)
        self._grid_label_entry(reference, 1, "允许峰峰波动 (%)", self.stability_threshold_var)
        row = ttk.Frame(reference)
        row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=5)
        self.dark_button = ttk.Button(row, text="测暗功率", command=self.record_dark)
        self.dark_button.pack(side="left", padx=(0, 5))
        self.stability_button = ttk.Button(row, text="检查入射稳定性", command=self.check_stability)
        self.stability_button.pack(side="left", padx=5)
        ttk.Checkbutton(
            reference,
            text="稳定性未通过也允许扫描（数据会标记）",
            variable=self.allow_unstable_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))
        ttk.Label(reference, textvariable=self.reference_var, style="Metric.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=2
        )
        self.stability_label = ttk.Label(reference, textvariable=self.stability_var)
        self.stability_label.grid(row=5, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 6))

        alignment = ttk.LabelFrame(left, text="3 · 一次性 AOM 对准", style="Section.TLabelframe")
        alignment.pack(fill="x", pady=4)
        self._grid_label_entry(alignment, 0, "固定 RF (MHz)", self.frequency_var)
        self._grid_label_entry(alignment, 1, "对准幅度 (mVpp)", self.alignment_mvpp_var)
        self.align_button = ttk.Button(alignment, text="开始实时对准（RF 开）", command=self.toggle_alignment)
        self.align_button.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        ttk.Label(alignment, textvariable=self.live_var, style="Metric.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 5)
        )

        scan = ttk.LabelFrame(left, text="4 · 300–550 mVpp 扫描", style="Section.TLabelframe")
        scan.pack(fill="x", pady=4)
        self._grid_label_entry(scan, 0, "起点 (mVpp)", self.start_var)
        self._grid_label_entry(scan, 1, "终点 (mVpp)", self.stop_var)
        self._grid_label_entry(scan, 2, "步长 (mVpp)", self.step_var)
        self._grid_label_entry(scan, 3, "每点等待 (s)", self.settle_var)
        self._grid_label_entry(scan, 4, "每点采样数", self.samples_var)
        self._grid_label_entry(scan, 5, "采样间隔 (s)", self.sample_gap_var)
        self._grid_label_entry(scan, 6, "固定功放增益 (dB，仅记录)", self.gain_db_var)
        self._grid_label_entry(scan, 7, "推荐点 (% ηmax)", self.target_fraction_var)
        ttk.Checkbutton(scan, text="做 300→550→300 往返扫描", variable=self.bidirectional_var).grid(
            row=8, column=0, columnspan=2, sticky="w", padx=6, pady=2
        )
        ttk.Checkbutton(scan, text="功放增益/档位已固定", variable=self.amp_fixed_var).grid(
            row=9, column=0, columnspan=2, sticky="w", padx=6, pady=2
        )
        ttk.Checkbutton(scan, text="AOM 已对准，所有光机件已锁紧", variable=self.optics_fixed_var).grid(
            row=10, column=0, columnspan=2, sticky="w", padx=6, pady=2
        )
        row = ttk.Frame(scan)
        row.grid(row=11, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        self.start_button = ttk.Button(row, text="开始扫描", command=self.start_scan)
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.stop_button = ttk.Button(row, text="停止并关 RF", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

        plot_header = ttk.Frame(right)
        plot_header.pack(fill="x")
        ttk.Label(plot_header, text="实时数据", style="Title.TLabel").pack(side="left")
        ttk.Label(plot_header, text=f"程序硬上限：{HARD_MAX_MVPP:g} mVpp").pack(side="right")
        self.figure = Figure(figsize=(8, 5.5), dpi=100, constrained_layout=True)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(5, 0))
        toolbar = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(fill="x")
        self._reset_plot("等待测量", "", "")

        log_frame = ttk.LabelFrame(right, text="状态与文件", style="Section.TLabelframe")
        log_frame.pack(fill="x", pady=(6, 0))
        self.log_text = tk.Text(log_frame, height=8, wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _grid_label_entry(
        self,
        parent,
        row: int,
        label: str,
        variable: tk.Variable,
        width: int = 18,
        values: tuple[str, ...] | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        if values:
            widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=width)
        else:
            widget = ttk.Entry(parent, textvariable=variable, width=width)
        widget.grid(row=row, column=1, sticky="ew", padx=6, pady=3)

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")

    def emit(self, kind: str, payload=None) -> None:
        self.events.put((kind, payload))

    def _start_worker(self, kind: str, target) -> bool:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning("仪器忙", "当前操作尚未结束。请先停止或等待完成。")
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

        self.worker = threading.Thread(target=wrapped, daemon=True, name=f"aom-{kind}")
        self.worker.start()
        return True

    def _update_button_states(self) -> None:
        busy = bool(self.busy_kind)
        for button in (self.connect_button, self.dark_button, self.stability_button, self.start_button):
            button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy in ("alignment", "scan", "stability") else "disabled")
        if self.busy_kind == "alignment":
            self.align_button.configure(text="停止对准并关 RF", state="normal")
        else:
            self.align_button.configure(text="开始实时对准（RF 开）", state="disabled" if busy else "normal")

    def _require_connected(self) -> None:
        if self.generator is None or self.power_meter is None:
            raise RuntimeError("请先连接信号发生器和功率计。")

    def _parse_float(self, variable: tk.StringVar, label: str, minimum=None, maximum=None) -> float:
        try:
            value = float(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"{label}必须是数字。") from exc
        if not math.isfinite(value):
            raise ValueError(f"{label}必须是有限数值。")
        if minimum is not None and value < minimum:
            raise ValueError(f"{label}不得小于 {minimum:g}。")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label}不得大于 {maximum:g}。")
        return value

    def _parse_int(self, variable: tk.StringVar, label: str, minimum=1, maximum=10000) -> int:
        try:
            value = int(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"{label}必须是整数。") from exc
        if value < minimum or value > maximum:
            raise ValueError(f"{label}必须在 {minimum}–{maximum} 之间。")
        return value

    def scan_visa(self) -> None:
        if self.mode_var.get() == "模拟":
            self.dg_resource_var.set("SIM::DG5352")
            return

        def work() -> None:
            self.emit("status", "正在扫描 VISA…")
            found = discover_rigol_generators()
            self.emit("visa_found", found)

        self._start_worker("visa", work)

    def connect_devices(self) -> None:
        mode = self.mode_var.get()
        resource = self.dg_resource_var.get().strip()
        serial = self.pm_serial_var.get().strip()
        try:
            wavelength = self._parse_float(self.wavelength_var, "波长", 200, 2000)
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if self.generator is not None or self.power_meter is not None:
            self._close_devices()
            self.status_var.set("旧连接已关闭，正在重连")

        def work() -> None:
            self.emit("status", "正在连接…")
            new_generator = None
            new_meter = None
            try:
                if mode == "模拟":
                    new_generator = SimulatedRigol(self._thread_log)
                    new_meter = SimulatedPM100D(new_generator, wavelength)
                    simulation = True
                else:
                    new_generator = RigolDG(resource, self._thread_log)
                    new_meter = PM100D(serial, wavelength)
                    simulation = False
                self.emit(
                    "connected",
                    {"generator": new_generator, "meter": new_meter, "simulation": simulation},
                )
            except Exception:
                if new_generator is not None:
                    new_generator.close()
                if new_meter is not None:
                    new_meter.close()
                raise

        self._start_worker("connect", work)

    def _thread_log(self, message: str) -> None:
        self.emit("log", message)

    def _set_sim_mode(self, mode: str) -> None:
        if self.simulation and self.power_meter is not None:
            self.power_meter.mode = mode

    def record_dark(self) -> None:
        try:
            self._require_connected()
        except Exception as exc:
            messagebox.showerror("未连接", str(exc))
            return
        if not messagebox.askokcancel(
            "测暗功率",
            "请遮住功率计探头或挡住全部光；程序会先关闭 RF，再采 20 个点。",
        ):
            return

        def work() -> None:
            self.generator.output_off()
            self._set_sim_mode("dark")
            values = []
            for _ in range(20):
                if self.stop_event.is_set():
                    return
                values.append(self.power_meter.read_mw())
                if not interruptible_sleep(self.stop_event, 0.05):
                    return
            self.emit("dark_done", {"mean": statistics.fmean(values), "std": statistics.stdev(values)})

        self._start_worker("dark", work)

    def check_stability(self) -> None:
        try:
            self._require_connected()
            if self.dark_mw is None:
                raise RuntimeError("请先测暗功率。")
            duration = self._parse_float(self.stability_seconds_var, "检查时长", 5, 3600)
            threshold = self._parse_float(self.stability_threshold_var, "稳定性阈值", 0.01, 100)
        except Exception as exc:
            messagebox.showerror("不能开始", str(exc))
            return
        if not messagebox.askokcancel(
            "检查入射光",
            "把功率计探头放到 AOM 入射位置，确认光斑全部落在探头有效面内并解除挡光。\n\n"
            "检查期间不要碰光路；RF 会保持关闭。",
        ):
            return

        def work() -> None:
            self.generator.output_off()
            self._set_sim_mode("incident")
            actual_duration = min(duration, 4.0) if self.simulation else duration
            interval = 0.2
            started = time.monotonic()
            samples: list[tuple[float, float]] = []
            self.emit("stability_reset")
            while True:
                elapsed = time.monotonic() - started
                if elapsed > actual_duration and len(samples) >= 3:
                    break
                if self.stop_event.is_set():
                    self.emit("status", "稳定性检查已停止。")
                    return
                value = self.power_meter.read_mw()
                samples.append((elapsed, value))
                self.emit("stability_sample", (elapsed, value))
                if not interruptible_sleep(self.stop_event, interval):
                    return
            result = analyze_stability(samples, self.dark_mw, threshold)
            MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = MEASUREMENT_DIR / f"incident_stability_{stamp}.csv"
            with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["elapsed_s", "power_raw_mW", "power_dark_corrected_mW"])
                for elapsed, value in samples:
                    writer.writerow([f"{elapsed:.6f}", f"{value:.12g}", f"{value-self.dark_mw:.12g}"])
            json_path = csv_path.with_suffix(".json")
            json_path.write_text(
                json.dumps(
                    {
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "simulation": self.simulation,
                        "dark_mw": self.dark_mw,
                        "result": asdict(result),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.emit(
                "stability_done",
                {"result": result, "samples": samples, "files": [csv_path, json_path]},
            )

        self._start_worker("stability", work)

    def toggle_alignment(self) -> None:
        if self.busy_kind == "alignment":
            self.request_stop()
            return
        try:
            self._require_connected()
            frequency_mhz = self._parse_float(self.frequency_var, "RF 频率", 0.001, 350)
            amplitude = self._parse_float(
                self.alignment_mvpp_var, "对准幅度", 1, HARD_MAX_MVPP
            )
        except Exception as exc:
            messagebox.showerror("不能开始", str(exc))
            return
        if not messagebox.askokcancel(
            "实时对准",
            f"请把功率计探头放在 +1 级衍射光上。程序将输出 {frequency_mhz:g} MHz、"
            f"{amplitude:g} mVpp。\n\n只调一次 AOM 角度/位置使读数最大；扫描期间不要再调。",
        ):
            return

        def work() -> None:
            samples = 0
            try:
                self._set_sim_mode("diffracted")
                self.generator.prepare(frequency_mhz * 1e6)
                self._ramp_rf_on(amplitude)
                started = time.monotonic()
                self.emit("alignment_reset")
                while not self.stop_event.is_set():
                    power = self.power_meter.read_mw()
                    efficiency = (
                        efficiency_percent(power, self.incident_mw, self.dark_mw)
                        if self.incident_mw is not None and self.dark_mw is not None
                        else math.nan
                    )
                    samples += 1
                    self.emit("alignment_sample", (time.monotonic() - started, power, efficiency))
                    if not interruptible_sleep(self.stop_event, 0.15):
                        break
            finally:
                self.generator.output_off()
                self.emit("alignment_done", samples)

        self._start_worker("alignment", work)

    def _ramp_rf_on(self, target_mvpp: float) -> None:
        start = min(25.0, target_mvpp)
        self.generator.set_amplitude_mvpp(start)
        self.generator.output_on()
        current = start
        while current < target_mvpp - 1e-9:
            if self.stop_event.is_set():
                raise RuntimeError("操作已停止。")
            current = min(target_mvpp, current + 25.0)
            self.generator.set_amplitude_mvpp(current)
            if not interruptible_sleep(self.stop_event, 0.05):
                raise RuntimeError("操作已停止。")

    def _scan_config(self) -> dict:
        frequency_mhz = self._parse_float(self.frequency_var, "RF 频率", 0.001, 350)
        start = self._parse_float(self.start_var, "扫描起点", 1, HARD_MAX_MVPP)
        stop = self._parse_float(self.stop_var, "扫描终点", 1, HARD_MAX_MVPP)
        step = self._parse_float(self.step_var, "扫描步长", 0.01, HARD_MAX_MVPP)
        points = build_inclusive_points(start, stop, step)
        settle = self._parse_float(self.settle_var, "每点等待", 0, 60)
        sample_count = self._parse_int(self.samples_var, "每点采样数", 1, 1000)
        sample_gap = self._parse_float(self.sample_gap_var, "采样间隔", 0, 60)
        gain_db = self._parse_float(self.gain_db_var, "功放增益", -100, 200)
        target_pct = self._parse_float(self.target_fraction_var, "推荐点比例", 1, 99)
        return {
            "frequency_mhz": frequency_mhz,
            "start_mvpp": start,
            "stop_mvpp": stop,
            "step_mvpp": step,
            "points": points,
            "settle_s": settle,
            "samples_per_point": sample_count,
            "sample_gap_s": sample_gap,
            "amplifier_gain_db_recorded": gain_db,
            "target_fraction_of_max": target_pct / 100.0,
            "bidirectional": self.bidirectional_var.get(),
        }

    def start_scan(self) -> None:
        try:
            self._require_connected()
            if self.dark_mw is None:
                raise RuntimeError("请先测暗功率。")
            if self.incident_mw is None or self.stability_result is None:
                raise RuntimeError("请先完成入射稳定性检查，程序才能得到效率分母。")
            if not self.stability_result.passed and not self.allow_unstable_var.get():
                raise RuntimeError("入射稳定性未通过；先修好光路，或明确勾选允许带标记扫描。")
            if not self.amp_fixed_var.get():
                raise RuntimeError("请确认并勾选“功放增益/档位已固定”。")
            if not self.optics_fixed_var.get():
                raise RuntimeError("请先对准 AOM、锁紧光机件，再勾选确认项。")
            config = self._scan_config()
        except Exception as exc:
            messagebox.showerror("不能开始扫描", str(exc))
            return
        if not messagebox.askokcancel(
            "最后确认",
            "探头是否已经只接收完整的 +1 级光斑，并且不会收到 0 级散射光？\n\n"
            f"将固定 {config['frequency_mhz']:g} MHz，扫描 {config['start_mvpp']:g}–"
            f"{config['stop_mvpp']:g} mVpp。停止、异常或完成都会关 RF。",
        ):
            return

        def work() -> None:
            self._run_scan(config)

        self._start_worker("scan", work)

    def _run_scan(self, config: dict) -> None:
        points = list(config["points"])
        passes: list[tuple[int, str, list[float]]] = [(1, "up", points)]
        if config["bidirectional"]:
            passes.append((2, "down", list(reversed(points))))
        total = sum(len(item[2]) for item in passes)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_suffix = "_SIM" if self.simulation else ""
        MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
        raw_path = MEASUREMENT_DIR / f"aom_efficiency_{stamp}{mode_suffix}_raw.csv"
        summary_path = MEASUREMENT_DIR / f"aom_efficiency_{stamp}{mode_suffix}_summary.csv"
        json_path = MEASUREMENT_DIR / f"aom_efficiency_{stamp}{mode_suffix}.json"
        png_path = MEASUREMENT_DIR / f"aom_efficiency_{stamp}{mode_suffix}.png"
        readings: list[SweepReading] = []
        stopped = False
        self.emit("scan_reset", {"total": total})
        self._set_sim_mode("diffracted")
        try:
            self.generator.prepare(config["frequency_mhz"] * 1e6)
            self._ramp_rf_on(points[0])
            with raw_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "timestamp",
                        "pass_index",
                        "direction",
                        "amplitude_mVpp",
                        "power_mean_mW",
                        "power_std_mW",
                        "incident_reference_raw_mW",
                        "dark_power_mW",
                        "efficiency_percent",
                        "efficiency_std_percent",
                        "sample_count",
                    ]
                )
                point_index = 0
                for pass_index, direction, pass_points in passes:
                    for amplitude in pass_points:
                        if self.stop_event.is_set():
                            stopped = True
                            break
                        point_index += 1
                        self.generator.set_amplitude_mvpp(amplitude)
                        if not interruptible_sleep(self.stop_event, config["settle_s"]):
                            stopped = True
                            break
                        values: list[float] = []
                        for sample_index in range(config["samples_per_point"]):
                            if self.stop_event.is_set():
                                stopped = True
                                break
                            values.append(self.power_meter.read_mw())
                            if (
                                sample_index + 1 < config["samples_per_point"]
                                and not interruptible_sleep(self.stop_event, config["sample_gap_s"])
                            ):
                                stopped = True
                                break
                        if stopped or not values:
                            break
                        power_mean = statistics.fmean(values)
                        power_std = statistics.stdev(values) if len(values) > 1 else 0.0
                        efficiency = efficiency_percent(power_mean, self.incident_mw, self.dark_mw)
                        efficiency_std = 100.0 * power_std / (self.incident_mw - self.dark_mw)
                        reading = SweepReading(
                            timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                            pass_index=pass_index,
                            direction=direction,
                            amplitude_mvpp=amplitude,
                            power_mw=power_mean,
                            power_std_mw=power_std,
                            efficiency_pct=efficiency,
                            efficiency_std_pct=efficiency_std,
                            sample_count=len(values),
                        )
                        readings.append(reading)
                        writer.writerow(
                            [
                                reading.timestamp,
                                pass_index,
                                direction,
                                f"{amplitude:.9g}",
                                f"{power_mean:.12g}",
                                f"{power_std:.12g}",
                                f"{self.incident_mw:.12g}",
                                f"{self.dark_mw:.12g}",
                                f"{efficiency:.12g}",
                                f"{efficiency_std:.12g}",
                                len(values),
                            ]
                        )
                        handle.flush()
                        self.emit(
                            "scan_point",
                            {"reading": reading, "index": point_index, "total": total},
                        )
                    if stopped:
                        break
                    if pass_index == 1 and len(passes) > 1:
                        if not interruptible_sleep(self.stop_event, max(0.5, config["settle_s"])):
                            stopped = True
                            break
        finally:
            self.generator.output_off()

        summaries = summarize_readings(readings)
        work_point = None
        if len(summaries) >= 2:
            work_point = recommend_work_point(summaries, config["target_fraction_of_max"])
        with summary_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "amplitude_mVpp",
                    "power_mean_mW",
                    "power_std_between_passes_mW",
                    "efficiency_mean_percent",
                    "efficiency_std_between_passes_percent",
                    "repeat_count",
                ]
            )
            for item in summaries:
                writer.writerow(
                    [
                        f"{item.amplitude_mvpp:.9g}",
                        f"{item.power_mean_mw:.12g}",
                        f"{item.power_std_between_passes_mw:.12g}",
                        f"{item.efficiency_mean_pct:.12g}",
                        f"{item.efficiency_std_between_passes_pct:.12g}",
                        item.repeat_count,
                    ]
                )
        metadata = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "stopped" if stopped else "complete",
            "simulation": self.simulation,
            "efficiency_definition": "100*(P_first_order-P_dark)/(P_incident-P_dark)",
            "single_meter_assumption": "Incident power remains equal to the stored stability-test mean after moving the sensor.",
            "generator": {
                "identity": getattr(self.generator, "identity", ""),
                "resource": getattr(self.generator, "resource", ""),
            },
            "power_meter": getattr(self.power_meter, "identity", {}),
            "dark_mw": self.dark_mw,
            "incident_reference_raw_mw": self.incident_mw,
            "stability": asdict(self.stability_result),
            "config": {key: value for key, value in config.items() if key != "points"},
            "points_mvpp": points,
            "readings": [asdict(item) for item in readings],
            "summary": [asdict(item) for item in summaries],
            "recommended_work_point": asdict(work_point) if work_point else None,
            "files": {
                "raw_csv": raw_path.name,
                "summary_csv": summary_path.name,
                "plot_png": png_path.name,
            },
        }
        json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit(
            "scan_done",
            {
                "stopped": stopped,
                "readings": readings,
                "summaries": summaries,
                "work_point": work_point,
                "files": [raw_path, summary_path, json_path, png_path],
                "png_path": png_path,
            },
        )

    def request_stop(self) -> None:
        if not self.busy_kind:
            return
        self.stop_event.set()
        try:
            if self.generator is not None:
                self.generator.output_off()
        except Exception as exc:
            self.log(f"立即关闭 RF 时出错：{exc}")
        self.status_var.set("正在停止；RF 关闭命令已发送")
        self.log("用户请求停止；RF 关闭命令已发送。")

    def _reset_plot(self, title: str, xlabel: str, ylabel: str) -> None:
        self.axes.clear()
        self.axes.set_title(title)
        self.axes.set_xlabel(xlabel)
        self.axes.set_ylabel(ylabel)
        self.axes.grid(True, alpha=0.28)
        self.canvas.draw_idle()

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
        if kind == "log":
            self.log(str(payload))
        elif kind == "status":
            self.status_var.set(str(payload))
            self.log(str(payload))
        elif kind == "visa_found":
            found = payload
            if found:
                self.dg_resource_var.set(found[0]["resource"])
                self.log("发现 DG：" + "；".join(item["identity"] for item in found))
                self.status_var.set(f"发现 {len(found)} 台 RIGOL DG")
            else:
                self.status_var.set("未发现 RIGOL DG")
                self.log("VISA 扫描完成，但没有发现 RIGOL DG 系列。")
        elif kind == "connected":
            self._close_devices()
            self.generator = payload["generator"]
            self.power_meter = payload["meter"]
            self.simulation = payload["simulation"]
            self.dark_mw = None
            self.incident_mw = None
            self.stability_result = None
            self.reference_var.set("暗功率：未测    入射参考：未测")
            self.stability_var.set("入射稳定性：未检查")
            self.status_var.set("已连接（模拟）" if self.simulation else "两台仪器已连接")
            self.log(f"DG：{self.generator.identity}")
            self.log(f"PM：{self.power_meter.identity}")
        elif kind == "dark_done":
            self.dark_mw = payload["mean"]
            self.reference_var.set(f"暗功率：{self.dark_mw:.6g} mW    入射参考：未测")
            self.log(f"暗功率 = {self.dark_mw:.6g} ± {payload['std']:.2g} mW")
            self.status_var.set("暗功率已记录")
        elif kind == "stability_reset":
            self.stability_samples = []
            self._reset_plot("AOM 入射功率稳定性", "时间 (s)", "功率 (mW)")
        elif kind == "stability_sample":
            self.stability_samples.append(payload)
            x = [item[0] for item in self.stability_samples]
            y = [item[1] for item in self.stability_samples]
            self.axes.clear()
            self.axes.plot(x, y, color="#1677b8", linewidth=1.2)
            self.axes.set(title="AOM 入射功率稳定性", xlabel="时间 (s)", ylabel="功率 (mW)")
            self.axes.grid(True, alpha=0.28)
            self.canvas.draw_idle()
            self.live_var.set(f"P_in = {y[-1]:.6g} mW")
        elif kind == "stability_done":
            result = payload["result"]
            self.stability_result = result
            self.incident_mw = result.mean_raw_mw
            self.reference_var.set(
                f"暗功率：{self.dark_mw:.6g} mW    入射参考：{self.incident_mw:.6g} mW"
            )
            verdict = "通过" if result.passed else "未通过"
            self.stability_var.set(
                f"入射稳定性：{verdict}；峰峰 {result.peak_to_peak_pct:.3f}%，"
                f"拟合漂移 {result.fitted_drift_pct:+.3f}%/{result.duration_s:.0f}s"
            )
            self.stability_label.configure(style="Good.TLabel" if result.passed else "Bad.TLabel")
            self.last_output_files = payload["files"]
            self.log(self.stability_var.get())
            self.log("稳定性文件：" + "；".join(str(path) for path in payload["files"]))
            self.status_var.set(f"入射稳定性{verdict}")
        elif kind == "alignment_reset":
            self.alignment_samples = []
            self._reset_plot("AOM 对准：调到一级衍射光最大", "时间 (s)", "效率 (%)")
            self.status_var.set("实时对准中；RF 已开启")
        elif kind == "alignment_sample":
            elapsed, power, efficiency = payload
            self.alignment_samples.append((elapsed, efficiency))
            self.alignment_samples = self.alignment_samples[-400:]
            if math.isfinite(efficiency):
                self.live_var.set(f"P₊₁ = {power:.6g} mW    η = {efficiency:.3f} %")
                self.axes.clear()
                self.axes.plot(
                    [item[0] for item in self.alignment_samples],
                    [item[1] for item in self.alignment_samples],
                    color="#d05a24",
                )
                self.axes.set(title="AOM 对准：调到一级衍射光最大", xlabel="时间 (s)", ylabel="效率 (%)")
                self.axes.grid(True, alpha=0.28)
                self.canvas.draw_idle()
            else:
                self.live_var.set(f"P₊₁ = {power:.6g} mW    η = 未建立入射参考")
        elif kind == "alignment_done":
            self.status_var.set("对准模式结束；RF 已关闭")
            if payload:
                self.optics_fixed_var.set(True)
            self.log(f"对准模式结束，共读取 {payload} 点；RF 已关闭。")
        elif kind == "scan_reset":
            self.readings = []
            self._reset_plot("AOM 幅度—衍射效率", "发生器幅度 (mVpp)", "一级衍射效率 (%)")
            self.status_var.set(f"扫描开始，共 {payload['total']} 点")
        elif kind == "scan_point":
            reading = payload["reading"]
            self.readings.append(reading)
            self.live_var.set(
                f"{reading.amplitude_mvpp:g} mVpp    P₊₁ = {reading.power_mw:.6g} mW    "
                f"η = {reading.efficiency_pct:.3f} %"
            )
            self.status_var.set(f"扫描 {payload['index']}/{payload['total']}")
            self._draw_live_scan()
        elif kind == "scan_done":
            self._draw_summary(payload["summaries"], payload["work_point"])
            try:
                self.figure.savefig(payload["png_path"], dpi=180)
            except Exception as exc:
                self.log(f"保存 PNG 失败：{exc}")
            self.last_output_files = payload["files"]
            if payload["stopped"]:
                self.status_var.set("扫描已停止；RF 已关闭")
                self.log("扫描已停止，已保留现有数据；RF 已关闭。")
            else:
                self.status_var.set("扫描完成；RF 已关闭")
                self.log("扫描完成；RF 已关闭。")
            work_point = payload["work_point"]
            if work_point is not None:
                boundary = "（落在扫描边界，建议扩展/细扫范围）" if work_point.at_scan_boundary else ""
                self.log(
                    f"推荐粗工作点：{work_point.amplitude_mvpp:.2f} mVpp，"
                    f"η≈{work_point.target_efficiency_pct:.2f}% = "
                    f"{100*work_point.target_fraction_of_max:.0f}% ηmax；"
                    f"实测 ηmax={work_point.max_measured_efficiency_pct:.2f}%{boundary}"
                )
            self.log("输出文件：" + "；".join(str(path) for path in payload["files"]))
        elif kind == "error":
            self.status_var.set("发生错误；RF 关闭")
            self.log("错误：" + payload["message"])
            self.log(payload["traceback"])
            try:
                if self.generator is not None:
                    self.generator.output_off()
            except Exception:
                pass
            messagebox.showerror("操作失败", payload["message"])
        elif kind == "worker_finished":
            if payload == self.busy_kind:
                self.busy_kind = ""
                self._update_button_states()

    def _draw_live_scan(self) -> None:
        self.axes.clear()
        colors = {"up": "#1677b8", "down": "#d05a24"}
        labels = {"up": "上扫", "down": "下扫"}
        for direction in ("up", "down"):
            group = [item for item in self.readings if item.direction == direction]
            if group:
                self.axes.errorbar(
                    [item.amplitude_mvpp for item in group],
                    [item.efficiency_pct for item in group],
                    yerr=[item.efficiency_std_pct for item in group],
                    marker="o",
                    markersize=4,
                    linewidth=1.1,
                    capsize=2,
                    color=colors[direction],
                    label=labels[direction],
                )
        self.axes.set(title="AOM 幅度—衍射效率（实时）", xlabel="发生器幅度 (mVpp)", ylabel="一级衍射效率 (%)")
        self.axes.grid(True, alpha=0.28)
        self.axes.legend()
        self.canvas.draw_idle()

    def _draw_summary(self, summaries, work_point) -> None:
        self.axes.clear()
        if summaries:
            self.axes.errorbar(
                [item.amplitude_mvpp for item in summaries],
                [item.efficiency_mean_pct for item in summaries],
                yerr=[item.efficiency_std_between_passes_pct for item in summaries],
                marker="o",
                markersize=4,
                linewidth=1.4,
                capsize=3,
                color="#1677b8",
                label="往返均值",
            )
        if work_point is not None:
            self.axes.axhline(work_point.target_efficiency_pct, color="#4c956c", linestyle="--", alpha=0.8)
            self.axes.axvline(work_point.amplitude_mvpp, color="#d05a24", linestyle="--", alpha=0.8)
            self.axes.scatter(
                [work_point.amplitude_mvpp],
                [work_point.target_efficiency_pct],
                color="#d05a24",
                zorder=5,
                label=f"推荐 {work_point.amplitude_mvpp:.1f} mVpp",
            )
        self.axes.set(title="AOM 幅度—衍射效率", xlabel="发生器幅度 (mVpp)", ylabel="一级衍射效率 (%)")
        self.axes.grid(True, alpha=0.28)
        self.axes.legend()
        self.canvas.draw_idle()

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
        self.stop_event.set()
        try:
            if self.generator is not None:
                self.generator.output_off()
        except Exception:
            pass
        self._close_devices()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = AOMScanApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
