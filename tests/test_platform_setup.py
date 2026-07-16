"""Platform-level discovery tests with a minimal Home Assistant API surface."""

from __future__ import annotations

import asyncio
from enum import Enum, IntFlag
import importlib
from pathlib import Path
import sys
import types
import unittest

from tests.test_device_catalog import LIVE_ACCOUNT_FIXTURE, load_catalog_module


INTEGRATION_PATH = (
    Path(__file__).parents[1] / "custom_components" / "leelen_home3"
)


def add_module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def install_home_assistant_stubs():
    homeassistant = add_module("homeassistant")
    homeassistant.__path__ = []
    components = add_module("homeassistant.components")
    components.__path__ = []

    climate = add_module("homeassistant.components.climate")
    climate.ClimateEntity = type("ClimateEntity", (), {})
    climate_const = add_module("homeassistant.components.climate.const")

    class HVACMode(Enum):
        OFF = "off"
        HEAT = "heat"
        COOL = "cool"
        FAN_ONLY = "fan_only"
        DRY = "dry"
        AUTO = "auto"

    class ClimateEntityFeature(IntFlag):
        TARGET_TEMPERATURE = 1
        FAN_MODE = 2
        TURN_OFF = 4
        TURN_ON = 8

    climate_const.HVACMode = HVACMode
    climate_const.ClimateEntityFeature = ClimateEntityFeature
    climate_const.FAN_LOW = "low"
    climate_const.FAN_MEDIUM = "medium"
    climate_const.FAN_HIGH = "high"

    fan = add_module("homeassistant.components.fan")
    fan.FanEntity = type("FanEntity", (), {})

    class FanEntityFeature(IntFlag):
        SET_SPEED = 1
        TURN_ON = 2
        TURN_OFF = 4

    fan.FanEntityFeature = FanEntityFeature

    sensor = add_module("homeassistant.components.sensor")
    sensor.SensorEntity = type("SensorEntity", (), {})

    class SensorDeviceClass(Enum):
        TEMPERATURE = "temperature"

    sensor.SensorDeviceClass = SensorDeviceClass

    config_entries = add_module("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = add_module("homeassistant.core")
    core.HomeAssistant = object
    const = add_module("homeassistant.const")

    class UnitOfTemperature:
        CELSIUS = "°C"

    const.UnitOfTemperature = UnitOfTemperature
    helpers = add_module("homeassistant.helpers")
    helpers.__path__ = []
    entity = add_module("homeassistant.helpers.entity")
    entity.DeviceInfo = dict


def load_platforms():
    install_home_assistant_stubs()
    package_name = "platform_probe"
    package = add_module(package_name)
    package.__path__ = [str(INTEGRATION_PATH)]
    leelen = add_module(f"{package_name}.leelen")
    leelen.__path__ = [str(INTEGRATION_PATH / "leelen")]
    api = add_module(f"{package_name}.leelen.api")
    api.__path__ = [str(INTEGRATION_PATH / "leelen" / "api")]
    http_api = add_module(f"{package_name}.leelen.api.HttpApi")
    http_api.HttpApi = type(
        "HttpApi", (), {"get_instance": classmethod(lambda cls, hass=None: cls())}
    )
    return {
        name: importlib.import_module(f"{package_name}.{name}")
        for name in ("climate", "fan", "sensor")
    }


class PlatformSetupTests(unittest.TestCase):
    def test_platforms_create_entities_from_logical_service_types(self):
        catalog = load_catalog_module()
        devices = [
            catalog.normalize_device(physical, detail)
            for physical, detail in LIVE_ACCOUNT_FIXTURE
        ]
        platforms = load_platforms()

        class Entry:
            entry_id = "entry-1"

        class Hass:
            data = {"leelen3": {"devices": {"entry-1": devices}}}

        created = {}
        for name, platform in platforms.items():
            entities = []
            asyncio.run(
                platform.async_setup_entry(Hass(), Entry(), entities.extend)
            )
            created[name] = entities

        self.assertEqual(11, len(created["climate"]))
        self.assertEqual(1, len(created["fan"]))
        self.assertEqual(6, len(created["sensor"]))
        self.assertEqual(
            ["LeelenClimate"] * 6 + ["LeelenHeater"] * 5,
            [type(entity).__name__ for entity in created["climate"]],
        )
        self.assertEqual(
            "次卧1中央空调",
            created["climate"][0].name,
        )

    def test_floor_heater_reports_heat_when_on_without_mode_field(self):
        catalog = load_catalog_module()
        devices = [
            catalog.normalize_device(physical, detail)
            for physical, detail in LIVE_ACCOUNT_FIXTURE
        ]
        climate = load_platforms()["climate"]

        class Entry:
            entry_id = "entry-1"

        class Hass:
            data = {"leelen3": {"devices": {"entry-1": devices}}}

        entities = []
        asyncio.run(climate.async_setup_entry(Hass(), Entry(), entities.extend))
        heater = next(
            entity for entity in entities if type(entity).__name__ == "LeelenHeater"
        )

        class FakeApi:
            async def read_dids_fiids(self, **kwargs):
                return {
                    "result": 1,
                    "params": [{"fiids": [{"value": {"onOff": 1, "setTemp": 26}}]}],
                }

        climate.HttpApi.get_instance = classmethod(lambda cls, hass=None: FakeApi())
        asyncio.run(heater.async_update())

        self.assertEqual(climate.HVACMode.HEAT, heater.hvac_mode)


if __name__ == "__main__":
    unittest.main()
