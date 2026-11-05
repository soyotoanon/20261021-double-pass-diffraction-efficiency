"""Pure calculations for the AOM diffraction-efficiency scanner."""

from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Iterable, Sequence


HARD_MAX_MVPP = 550.0


@dataclass(frozen=True)
class StabilityResult:
    sample_count: int
    duration_s: float
    mean_raw_mw: float
    mean_corrected_mw: float
    std_mw: float
    relative_std_pct: float
    peak_to_peak_pct: float
    fitted_drift_pct: float
    fitted_drift_pct_per_min: float
    threshold_pct: float
    passed: bool


@dataclass(frozen=True)
class SweepReading:
    timestamp: str
    pass_index: int
    direction: str
    amplitude_mvpp: float
    power_mw: float
    power_std_mw: float
    efficiency_pct: float
    efficiency_std_pct: float
    sample_count: int


@dataclass(frozen=True)
class SweepSummary:
    amplitude_mvpp: float
    power_mean_mw: float
    power_std_between_passes_mw: float
    efficiency_mean_pct: float
    efficiency_std_between_passes_pct: float
    repeat_count: int


@dataclass(frozen=True)
class WorkPoint:
    amplitude_mvpp: float
    target_efficiency_pct: float
    max_measured_efficiency_pct: float
    target_fraction_of_max: float
    at_scan_boundary: bool


def build_inclusive_points(start_mvpp: float, stop_mvpp: float, step_mvpp: float, *, allow_zero: bool = False) -> list[float]:
    """Return an inclusive ascending grid without floating-point endpoint loss."""
    for name, value in (
        ("起点", start_mvpp),
        ("终点", stop_mvpp),
        ("步长", step_mvpp),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name}必须是有限数值。")
    if start_mvpp < 0 or (start_mvpp == 0 and not allow_zero):
        raise ValueError("扫描起点必须大于或等于 0 mVpp。" if allow_zero else "扫描起点必须大于 0 mVpp。")
    if stop_mvpp < start_mvpp:
        raise ValueError("扫描终点必须不小于起点。")
    if stop_mvpp > HARD_MAX_MVPP + 1e-9:
        raise ValueError(f"本程序把硬上限锁在 {HARD_MAX_MVPP:g} mVpp。")
    if step_mvpp <= 0:
        raise ValueError("扫描步长必须大于 0。")

    count = int(math.floor((stop_mvpp - start_mvpp) / step_mvpp + 1e-12))
    points = [round(start_mvpp + index * step_mvpp, 9) for index in range(count + 1)]
    if not math.isclose(points[-1], stop_mvpp, rel_tol=0.0, abs_tol=1e-8):
        points.append(round(stop_mvpp, 9))
    if allow_zero and any(0 < point < 1 for point in points):
        raise ValueError("100 MHz、50 Ω 下最小正幅度为 1 mVpp；请调整起点或步长。")
    return points


def efficiency_percent(power_mw: float, incident_mw: float, dark_mw: float) -> float:
    """Calculate absolute first-order efficiency, deliberately without clipping."""
    denominator = incident_mw - dark_mw
    if not math.isfinite(denominator) or denominator <= 0:
        raise ValueError("入射参考功率扣除暗功率后必须大于 0。")
    numerator = power_mw - dark_mw
    if not math.isfinite(numerator):
        raise ValueError("一级衍射光功率不是有效数值。")
    return 100.0 * numerator / denominator


def analyze_stability(
    timed_power_samples: Sequence[tuple[float, float]],
    dark_mw: float,
    threshold_pct: float,
) -> StabilityResult:
    """Evaluate input stability using peak-to-peak variation and fitted drift."""
    if len(timed_power_samples) < 3:
        raise ValueError("稳定性检查至少需要 3 个样本。")
    if not math.isfinite(threshold_pct) or threshold_pct <= 0:
        raise ValueError("稳定性阈值必须大于 0%。")

    times = [float(item[0]) for item in timed_power_samples]
    raw = [float(item[1]) for item in timed_power_samples]
    if any(not math.isfinite(value) for value in times + raw):
        raise ValueError("稳定性样本中含无效数值。")
    if any(times[index] <= times[index - 1] for index in range(1, len(times))):
        raise ValueError("稳定性样本时间必须严格递增。")

    corrected = [value - dark_mw for value in raw]
    mean_corrected = statistics.fmean(corrected)
    if mean_corrected <= 0:
        raise ValueError("入射功率扣除暗功率后必须大于 0。")
    mean_raw = statistics.fmean(raw)
    std_mw = statistics.stdev(corrected)
    relative_std_pct = 100.0 * std_mw / mean_corrected
    peak_to_peak_pct = 100.0 * (max(corrected) - min(corrected)) / mean_corrected

    centered_times = [value - statistics.fmean(times) for value in times]
    centered_power = [value - mean_corrected for value in corrected]
    denominator = sum(value * value for value in centered_times)
    slope_mw_per_s = (
        sum(t * p for t, p in zip(centered_times, centered_power)) / denominator
        if denominator > 0
        else 0.0
    )
    duration_s = times[-1] - times[0]
    fitted_drift_pct = 100.0 * slope_mw_per_s * duration_s / mean_corrected
    fitted_drift_pct_per_min = 100.0 * slope_mw_per_s * 60.0 / mean_corrected
    passed = (
        peak_to_peak_pct <= threshold_pct
        and abs(fitted_drift_pct) <= threshold_pct
    )
    return StabilityResult(
        sample_count=len(raw),
        duration_s=duration_s,
        mean_raw_mw=mean_raw,
        mean_corrected_mw=mean_corrected,
        std_mw=std_mw,
        relative_std_pct=relative_std_pct,
        peak_to_peak_pct=peak_to_peak_pct,
        fitted_drift_pct=fitted_drift_pct,
        fitted_drift_pct_per_min=fitted_drift_pct_per_min,
        threshold_pct=threshold_pct,
        passed=passed,
    )


def summarize_readings(readings: Iterable[SweepReading]) -> list[SweepSummary]:
    grouped: dict[float, list[SweepReading]] = {}
    for reading in readings:
        grouped.setdefault(reading.amplitude_mvpp, []).append(reading)
    summaries: list[SweepSummary] = []
    for amplitude in sorted(grouped):
        group = grouped[amplitude]
        powers = [item.power_mw for item in group]
        efficiencies = [item.efficiency_pct for item in group]
        summaries.append(
            SweepSummary(
                amplitude_mvpp=amplitude,
                power_mean_mw=statistics.fmean(powers),
                power_std_between_passes_mw=statistics.stdev(powers) if len(powers) > 1 else 0.0,
                efficiency_mean_pct=statistics.fmean(efficiencies),
                efficiency_std_between_passes_pct=(
                    statistics.stdev(efficiencies) if len(efficiencies) > 1 else 0.0
                ),
                repeat_count=len(group),
            )
        )
    return summaries


def recommend_work_point(
    summaries: Sequence[SweepSummary], target_fraction_of_max: float = 0.80
) -> WorkPoint:
    """Find the rising-branch crossing at a fraction of measured maximum efficiency.

    A cumulative-maximum envelope is used only for choosing the crossing, so a noisy
    point cannot create a spurious backwards crossing. Raw and averaged data remain
    untouched in the saved files.
    """
    if len(summaries) < 2:
        raise ValueError("至少需要两个幅度点才能推荐工作点。")
    if not 0 < target_fraction_of_max < 1:
        raise ValueError("目标比例必须在 0 与 1 之间。")

    ordered = sorted(summaries, key=lambda item: item.amplitude_mvpp)
    maximum = max(item.efficiency_mean_pct for item in ordered)
    if maximum <= 0:
        raise ValueError("测得的最大衍射效率必须大于 0。")
    target = maximum * target_fraction_of_max
    envelope: list[float] = []
    running = -math.inf
    for item in ordered:
        running = max(running, item.efficiency_mean_pct)
        envelope.append(running)

    if target <= envelope[0]:
        return WorkPoint(
            amplitude_mvpp=ordered[0].amplitude_mvpp,
            target_efficiency_pct=target,
            max_measured_efficiency_pct=maximum,
            target_fraction_of_max=target_fraction_of_max,
            at_scan_boundary=True,
        )

    for index in range(1, len(ordered)):
        low_y, high_y = envelope[index - 1], envelope[index]
        if high_y < target:
            continue
        low_x = ordered[index - 1].amplitude_mvpp
        high_x = ordered[index].amplitude_mvpp
        if high_y <= low_y:
            crossing = high_x
        else:
            crossing = low_x + (target - low_y) * (high_x - low_x) / (high_y - low_y)
        return WorkPoint(
            amplitude_mvpp=crossing,
            target_efficiency_pct=target,
            max_measured_efficiency_pct=maximum,
            target_fraction_of_max=target_fraction_of_max,
            at_scan_boundary=(
                math.isclose(crossing, ordered[0].amplitude_mvpp, abs_tol=1e-9)
                or math.isclose(crossing, ordered[-1].amplitude_mvpp, abs_tol=1e-9)
            ),
        )

    return WorkPoint(
        amplitude_mvpp=ordered[-1].amplitude_mvpp,
        target_efficiency_pct=target,
        max_measured_efficiency_pct=maximum,
        target_fraction_of_max=target_fraction_of_max,
        at_scan_boundary=True,
    )
