import numpy as np

from acconeer.exptool import configs, utils
from acconeer.exptool.structs import configbase


def get_sensor_config():
    config = configs.IQServiceConfig()
    config.range_interval = [0.2, 0.8]
    config.update_rate = 30
    return config


class ProcessingConfig(configbase.ProcessingConfig):
    VERSION = 2

    history_length = configbase.IntParameter(
        default_value=100,
        limits=(10, 1000),
        label="History length",
        order=0,
    )

    sf = configbase.FloatParameter(
        label="Smoothing factor",
        default_value=None,
        limits=(0.1, 0.999),
        decimals=3,
        optional=True,
        optional_label="Enable filter",
        optional_default_set_value=0.9,
        updateable=True,
        order=10,
    )


get_processing_config = ProcessingConfig


class Processor:
    def __init__(self, sensor_config, processing_config, session_info):
        depths = utils.get_range_depths(sensor_config, session_info)
        num_depths = depths.size
        num_sensors = len(sensor_config.sensor)
        history_length = processing_config.history_length
        self.history = np.zeros([history_length, num_sensors, num_depths], dtype="complex")
        self.lp_data = np.zeros([num_sensors, num_depths], dtype="complex")
        self.update_index = 0
        self.update_processing_config(processing_config)

    def update_processing_config(self, processing_config):
        self.sf = processing_config.sf if processing_config.sf is not None else 0.0

    def dynamic_sf(self, static_sf):
        return min(static_sf, 1.0 - 1.0 / (1.0 + self.update_index))

    def process(self, data, data_info):
        self.history = np.roll(self.history, -1, axis=0)
        self.history[-1] = data

        sf = self.dynamic_sf(self.sf)
        self.lp_data = sf * self.lp_data + (1 - sf) * data

        self.update_index += 1

        return {
            "data": self.lp_data,
            "history": self.history,
        }
