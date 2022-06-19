from __future__ import annotations

import enum
from typing import Any, Optional, Tuple

import attrs
import numpy as np
import numpy.typing as npt
from scipy.signal import butter, filtfilt

from acconeer.exptool import a121
from acconeer.exptool.a121.algo import ProcessorBase


class ProcessorMode(enum.Enum):
    DISTANCE_ESTIMATION = enum.auto()
    LEAKAGE_CALIBRATION = enum.auto()
    RECORDED_THRESHOLD_CALIBRATION = enum.auto()


class ThresholdMethod(enum.Enum):
    CFAR = enum.auto()
    FIXED = enum.auto()
    RECORDED = enum.auto()


@attrs.mutable(kw_only=True)
class ProcessorConfig:
    processor_mode: ProcessorMode = attrs.field(default=ProcessorMode.DISTANCE_ESTIMATION)
    threshold_method: ThresholdMethod = attrs.field(default=ThresholdMethod.CFAR)
    sc_bg_num_std_dev: float = attrs.field(default=3.0)
    fixed_threshold_value: float = attrs.field(default=100.0)

    cfar_guard_length_m: Optional[float] = attrs.field(default=None)
    cfar_window_length_m: Optional[float] = attrs.field(default=None)
    cfar_sensitivity: float = attrs.field(default=0.5)
    cfar_one_sided: bool = attrs.field(default=False)


@attrs.frozen(kw_only=True)
class ProcessorContext:
    recorded_threshold: Optional[npt.NDArray[np.float_]] = attrs.field(default=None)
    abs_noise_std: Optional[float] = attrs.field(default=None)


@attrs.frozen(kw_only=True)
class ProcessorExtraResult:
    """
    Contains information for visualization in ET.
    """

    abs_sweep: Optional[npt.NDArray[np.float_]] = attrs.field(default=None)
    used_threshold: Optional[npt.NDArray[np.float_]] = attrs.field(default=None)
    distances_m: Optional[npt.NDArray[np.float_]] = attrs.field(default=None)


@attrs.frozen(kw_only=True)
class ProcessorResult:
    estimated_distances: Optional[list[float]] = attrs.field(default=None)
    estimated_amplitudes: Optional[list[float]] = attrs.field(default=None)
    recorded_threshold: Optional[npt.NDArray[np.float_]] = attrs.field(default=None)
    extra_result: ProcessorExtraResult = attrs.field(factory=ProcessorExtraResult)


class Processor(ProcessorBase[ProcessorConfig, ProcessorResult]):
    """Distance processor

    For all used subsweeps, the ``profile`` and ``step_length`` must be the same.

    :param sensor_config: Sensor configuration
    :param metadata: Metadata yielded by the sensor config
    :param processor_config: Processor configuration
    :param subsweep_indexes:
        The subsweep indexes to be processed. If ``None``, all subsweeps will be used.
    :param context: Context
    """

    ENVELOPE_WIDTH_M = {
        a121.Profile.PROFILE_1: 0.04,
        a121.Profile.PROFILE_2: 0.07,
        a121.Profile.PROFILE_3: 0.14,
        a121.Profile.PROFILE_4: 0.19,
        a121.Profile.PROFILE_5: 0.32,
    }

    APPROX_BASE_STEP_LENGTH_M = 2.5e-3
    CFAR_GUARD_LENGTH_ADJUSTMENT = 2
    CFAR_WINDOW_LENGTH_ADJUSTMENT = 0.25

    def __init__(
        self,
        *,
        sensor_config: a121.SensorConfig,
        metadata: a121.Metadata,
        processor_config: ProcessorConfig,
        subsweep_indexes: Optional[list[int]] = None,
        context: Optional[ProcessorContext] = None,
    ) -> None:
        if subsweep_indexes is None:
            subsweep_indexes = list(range(sensor_config.num_subsweeps))

        subsweep_configs = self._get_subsweep_configs(sensor_config, subsweep_indexes)

        self._validate(subsweep_configs)

        self.sensor_config = sensor_config
        self.metadata = metadata
        self.processor_config = processor_config
        self.subsweep_indexes = subsweep_indexes
        self.context = context

        self.profile = self._get_profile(subsweep_configs)
        self.step_length = self._get_step_length(subsweep_configs)
        self.approx_step_length_m = self.step_length * self.APPROX_BASE_STEP_LENGTH_M
        self.start_point = self._get_start_point(subsweep_configs)
        self.num_points = self._get_num_points(subsweep_configs)

        assert self.metadata.base_step_length_m is not None
        self.step_length_m = self.step_length * self.metadata.base_step_length_m

        (_, self.margin_p) = self.distance_filter_init_margin(self.profile, self.step_length)

        self.start_point_cropped = self.start_point + self.margin_p
        self.num_points_cropped = self.num_points - 2 * self.margin_p

        self.distances_m = (
            self.start_point_cropped + np.arange(self.num_points_cropped) * self.step_length
        ) * self.metadata.base_step_length_m

        (self.b, self.a) = self._get_distance_filter_coeffs(self.profile, self.step_length)

        self.processor_mode = processor_config.processor_mode
        self.threshold_method = processor_config.threshold_method

        if self.processor_mode == ProcessorMode.DISTANCE_ESTIMATION:
            self._init_process_distance_estimation()
        elif self.processor_mode == ProcessorMode.LEAKAGE_CALIBRATION:
            pass
        elif self.processor_mode == ProcessorMode.RECORDED_THRESHOLD_CALIBRATION:
            self._init_recorded_threshold_calibration()
        else:
            raise RuntimeError

    @classmethod
    def _get_subsweep_configs(
        cls, sensor_config: a121.SensorConfig, subsweep_indexes: list[int]
    ) -> list[a121.SubsweepConfig]:
        return [sensor_config.subsweeps[i] for i in subsweep_indexes]

    @classmethod
    def _get_profile(cls, subsweep_configs: list[a121.SubsweepConfig]) -> a121.Profile:
        profiles = {c.profile for c in subsweep_configs}

        if len(profiles) > 1:
            raise ValueError

        (profile,) = profiles
        return profile

    @classmethod
    def _get_step_length(cls, subsweep_configs: list[a121.SubsweepConfig]) -> int:
        step_lengths = {c.step_length for c in subsweep_configs}

        if len(step_lengths) > 1:
            raise ValueError

        (step_length,) = step_lengths
        return step_length

    @classmethod
    def _get_start_point(cls, subsweep_configs: list[a121.SubsweepConfig]) -> int:
        return subsweep_configs[0].start_point

    @classmethod
    def _get_num_points(cls, subsweep_configs: list[a121.SubsweepConfig]) -> int:
        return sum(c.num_points for c in subsweep_configs)

    @classmethod
    def _validate(cls, subsweep_configs: list[a121.SubsweepConfig]) -> None:
        cls._validate_range(subsweep_configs)

        for c in subsweep_configs:
            if not c.phase_enhancement:
                raise ValueError

    @classmethod
    def _validate_range(cls, subsweep_configs: list[a121.SubsweepConfig]) -> None:
        step_length = cls._get_step_length(subsweep_configs)

        next_expected_start_point = None

        for c in subsweep_configs:
            if next_expected_start_point is not None:
                if c.start_point != next_expected_start_point:
                    raise ValueError

            next_expected_start_point = c.start_point + c.num_points * step_length

    def process(self, result: a121.Result) -> ProcessorResult:
        subframes = [result.subframes[i] for i in self.subsweep_indexes]
        frame = np.concatenate(subframes, axis=1)
        sweep = frame.mean(axis=0)
        filtered_sweep = filtfilt(self.b, self.a, sweep)
        abs_sweep = np.abs(filtered_sweep)
        abs_sweep = abs_sweep[self.margin_p : -self.margin_p]

        if self.processor_mode == ProcessorMode.DISTANCE_ESTIMATION:
            return self._process_distance_estimation(abs_sweep)
        elif self.processor_mode == ProcessorMode.LEAKAGE_CALIBRATION:
            pass
        elif self.processor_mode == ProcessorMode.RECORDED_THRESHOLD_CALIBRATION:
            return self._process_recorded_threshold_calibration(abs_sweep)

        raise RuntimeError

    def _init_process_distance_estimation(self) -> None:
        if self.threshold_method == ThresholdMethod.RECORDED:
            if self.context is None or self.context.recorded_threshold is None:
                raise ValueError("Missing recorded threshold in context")
            else:
                self.threshold = self.context.recorded_threshold
        elif self.threshold_method == ThresholdMethod.FIXED:
            self.threshold = np.full(
                self.num_points_cropped, self.processor_config.fixed_threshold_value
            )
        elif self.threshold_method == ThresholdMethod.CFAR:
            if self.processor_config.cfar_guard_length_m is None:
                self.cfar_guard_length_m = (
                    self.ENVELOPE_WIDTH_M[self.profile] * self.CFAR_GUARD_LENGTH_ADJUSTMENT
                )
            else:
                self.cfar_guard_length_m = self.processor_config.cfar_guard_length_m
            if self.processor_config.cfar_window_length_m is None:
                self.cfar_window_length_m = (
                    self.ENVELOPE_WIDTH_M[self.profile] * self.CFAR_WINDOW_LENGTH_ADJUSTMENT
                )
            else:
                self.cfar_window_length_m = self.processor_config.cfar_window_length_m

            self.cfar_one_sided = self.processor_config.cfar_one_sided
            self.cfar_sensitivity = self.processor_config.cfar_sensitivity
            guard_half_length = int(np.round(self.cfar_guard_length_m / 2.0 / self.step_length_m))
            window_length = int(np.round(self.cfar_window_length_m / self.approx_step_length_m))
            self.idx_cfar_pts = guard_half_length + np.arange(window_length)

    def _process_distance_estimation(self, abs_sweep: npt.NDArray[np.float_]) -> ProcessorResult:
        self.threshold = self._update_threshold(abs_sweep)

        found_peaks_idx = self._find_peaks(abs_sweep, self.threshold)
        (estimated_distances, estimated_amplitudes) = self._interpolate_peaks(
            abs_sweep, found_peaks_idx, self.start_point_cropped, self.step_length
        )
        extra_result = ProcessorExtraResult(
            abs_sweep=abs_sweep, used_threshold=self.threshold, distances_m=self.distances_m
        )
        return ProcessorResult(
            estimated_distances=estimated_distances,
            estimated_amplitudes=estimated_amplitudes,
            extra_result=extra_result,
        )

    def _init_recorded_threshold_calibration(self) -> None:
        self.bg_sc_mean = np.zeros(self.num_points_cropped)
        self.bg_sc_sum_squared_bg_sweeps = np.zeros(self.num_points_cropped)
        self.sc_bg_num_sweeps = 1.0
        self.sc_bg_num_std_dev = self.processor_config.sc_bg_num_std_dev

    def _process_recorded_threshold_calibration(
        self, abs_sweep: npt.NDArray[np.float_]
    ) -> ProcessorResult:
        min_num_sweeps_in_valid_threshold = 2

        self.bg_sc_mean += abs_sweep
        self.bg_sc_sum_squared_bg_sweeps += np.square(abs_sweep)
        mean_sweep = self.bg_sc_mean / self.sc_bg_num_sweeps
        mean_square = self.bg_sc_sum_squared_bg_sweeps / self.sc_bg_num_sweeps
        square_mean = np.square(mean_sweep)

        if min_num_sweeps_in_valid_threshold <= self.sc_bg_num_sweeps:
            sc_bg_sweep_std = np.sqrt(
                np.abs(mean_square - square_mean)
                * self.sc_bg_num_sweeps
                / (self.sc_bg_num_sweeps - 1)
            )
            threshold = mean_sweep + self.sc_bg_num_std_dev * sc_bg_sweep_std
        else:
            threshold = None

        self.sc_bg_num_sweeps += 1

        extra_result = ProcessorExtraResult(abs_sweep=abs_sweep)
        return ProcessorResult(extra_result=extra_result, recorded_threshold=threshold)

    def update_config(self, config: ProcessorConfig) -> None:
        ...

    @classmethod
    def _get_distance_filter_coeffs(cls, profile: a121.Profile, step_length: int) -> Any:
        wnc = cls.APPROX_BASE_STEP_LENGTH_M * step_length / cls.ENVELOPE_WIDTH_M[profile]
        return butter(N=2, Wn=wnc)

    def _update_threshold(self, abs_sweep: npt.NDArray[np.float_]) -> npt.NDArray[np.float_]:
        if self.threshold_method == ThresholdMethod.CFAR:
            return self._calculate_cfar_threshold(
                abs_sweep,
                self.idx_cfar_pts,
                self.cfar_sensitivity,
                self.cfar_one_sided,
                self.context,
            )
        elif self.threshold_method == ThresholdMethod.FIXED:
            return self.threshold
        elif self.threshold_method == ThresholdMethod.RECORDED:
            return self.threshold
        else:
            raise RuntimeError

    @staticmethod
    def _calculate_cfar_threshold(
        abs_sweep: npt.NDArray[np.float_],
        idx_cfar_pts: npt.NDArray[np.int_],
        alpha: float,
        one_side: bool,
        context: Optional[ProcessorContext],
    ) -> npt.NDArray[np.float_]:
        threshold = np.full(abs_sweep.shape, np.nan)
        start_idx = np.max(idx_cfar_pts)
        if one_side:
            take_relative_indexes = -idx_cfar_pts
            end_idx = abs_sweep.size
        else:
            take_relative_indexes = np.concatenate((-idx_cfar_pts, +idx_cfar_pts), axis=0)
            end_idx = abs_sweep.size - start_idx

        for idx in np.arange(start_idx, end_idx):
            take_indexes = idx + take_relative_indexes
            threshold[idx] = np.mean(np.take(abs_sweep, take_indexes))

        if context is not None and context.abs_noise_std is not None:
            threshold += context.abs_noise_std

        threshold *= 1.0 / (alpha + 1e-10)
        return threshold

    @staticmethod
    def _find_peaks(
        abs_sweep: npt.NDArray[np.float_], threshold: npt.NDArray[np.float_]
    ) -> list[int]:
        if threshold is None:
            raise ValueError
        found_peaks = []
        d = 1
        N = len(abs_sweep)
        while d < (N - 1):
            if np.isnan(threshold[d - 1]):
                d += 1
                continue
            if np.isnan(threshold[d + 1]):
                break
            if abs_sweep[d] <= threshold[d]:
                d += 2
                continue
            if abs_sweep[d - 1] <= threshold[d - 1]:
                d += 1
                continue
            if abs_sweep[d - 1] >= abs_sweep[d]:
                d += 1
                continue
            d_upper = d + 1
            while True:
                if (d_upper) >= (N - 1):
                    break
                if np.isnan(threshold[d_upper]):
                    break
                if abs_sweep[d_upper] <= threshold[d_upper]:
                    break
                if abs_sweep[d_upper] > abs_sweep[d]:
                    break
                elif abs_sweep[d_upper] < abs_sweep[d]:
                    found_peaks.append(int(np.argmax(abs_sweep[d:d_upper]) + d))
                    break
                else:
                    d_upper += 1
            d = d_upper
        return found_peaks

    @staticmethod
    def _interpolate_peaks(
        abs_sweep: npt.NDArray[np.float_],
        peak_idxs: list[int],
        start_point: int,
        step_length: int,
    ) -> Tuple[list[float], list[float]]:
        estimated_distances = []
        estimated_amplitudes = []
        for peak_idx in peak_idxs:
            # (https://math.stackexchange.com/questions/680646/get-polynomial-function-from-3-points)
            x = np.arange(peak_idx - 1, peak_idx + 2, 1)
            y = abs_sweep[peak_idx - 1 : peak_idx + 2]
            a = (x[0] * (y[2] - y[1]) + x[1] * (y[0] - y[2]) + x[2] * (y[1] - y[0])) / (
                (x[0] - x[1]) * (x[0] - x[2]) * (x[1] - x[2])
            )
            b = (y[1] - y[0]) / (x[1] - x[0]) - a * (x[0] + x[1])
            c = y[0] - a * x[0] ** 2 - b * x[0]
            peak_loc = -b / (2 * a)
            estimated_distances.append((start_point + peak_loc * step_length) * 2.5)
            estimated_amplitudes.append(a * peak_loc**2 + b * peak_loc + c)
        return estimated_distances, estimated_amplitudes

    @classmethod
    def distance_filter_init_margin(
        cls, profile: a121.Profile, step_length: int
    ) -> Tuple[int, int]:
        margin_p = np.ceil(
            cls.ENVELOPE_WIDTH_M[profile] / (cls.APPROX_BASE_STEP_LENGTH_M * step_length)
        ).astype(int)
        margin_m = margin_p * cls.APPROX_BASE_STEP_LENGTH_M * step_length
        return (margin_m, margin_p)
