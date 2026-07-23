from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from time import monotonic, time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .device_catalog import (
    SERVICE_TYPE_CENTRAL_AIR_CONDITIONER,
    SERVICE_TYPE_FLOOR_HEATING,
    SERVICE_TYPE_SENSOR,
)
from .leelen.api.HttpApi import HttpApi

_LOGGER = logging.getLogger(__name__)

FIID_CLIMATE = 49411
FIID_HEATER = 49415
FIID_TEMPERATURE = 16641
FIID_HUMIDITY = 16642

SERVICE_FIIDS = {
    SERVICE_TYPE_CENTRAL_AIR_CONDITIONER: [
        FIID_CLIMATE,
        FIID_TEMPERATURE,
    ],
    SERVICE_TYPE_FLOOR_HEATING: [
        FIID_HEATER,
        FIID_TEMPERATURE,
    ],
    SERVICE_TYPE_SENSOR: [
        FIID_TEMPERATURE,
        FIID_HUMIDITY,
    ],
}
SUPPORTED_FIIDS = {
    fiid for service_fiids in SERVICE_FIIDS.values() for fiid in service_fiids
}

REST_FALLBACK_INTERVAL = timedelta(seconds=30)
REST_PUSH_INTERVAL = timedelta(minutes=5)
CONTROL_PENDING_SECONDS = 60.0
CONTROL_CONFIRMED_GRACE_SECONDS = 20.0

_ROOM_NAME_SUFFIXES = re.compile(
    r"(中央空调|空调|地暖|温湿度传感器|温湿度|传感器|温控面板|温控器|温控|面板)"
)
_ROOM_NAME_SEPARATORS = re.compile(r"[\s_\-（）()]+")


class LeelenCoordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, entry):
        super().__init__(
            hass,
            _LOGGER,
            name="leelen api",
            update_interval=REST_FALLBACK_INTERVAL,
            config_entry=entry,
        )
        self._entry = entry
        self._device_addr = entry.data.get("deviceAddr")
        self._data = {}
        self._control_expectations = {}
        self._mqtt_connected = False

    async def _async_update_data(self):
        _LOGGER.debug("=== 开始 REST 状态同步 ===")
        try:
            api = HttpApi.get_instance(self.hass)
            devices = await api.get_device_list_v2()
            devices = await api.get_online_status(devices)
            reads = self._build_state_reads(devices)
            state_response = (
                await api.read_dids_fiids_batch(reads)
                if reads
                else {"result": 1, "params": []}
            )
            states, state_times = self._state_index(state_response)

            hass_data = self.hass.data.setdefault(DOMAIN, {})
            hass_data.setdefault("devices", {})[self._entry.entry_id] = devices

            self._data = {
                "devices": devices,
                "states": states,
                "state_times": state_times,
                "humidity_sources": self._build_humidity_sources(devices),
                "mqtt_connected": self._mqtt_connected,
            }

            _LOGGER.debug("=== REST 状态同步完成，%s 个设备 ===", len(devices))
            return self._data
        except Exception as exc:
            _LOGGER.error("=== REST 状态同步失败: %s ===", exc)
            raise

    async def async_stop_timer(self):
        """Compatibility hook retained for existing unload code."""

    @property
    def mqtt_connected(self):
        return self._mqtt_connected

    def async_set_mqtt_connected(self, connected):
        """Switch REST between disconnected fallback and push safety refresh."""
        connected = bool(connected)
        if self._mqtt_connected == connected:
            return
        self._mqtt_connected = connected
        self.update_interval = (
            REST_PUSH_INTERVAL if connected else REST_FALLBACK_INTERVAL
        )
        self._data["mqtt_connected"] = connected
        if self.data is not None:
            self.async_set_updated_data(self._data)
        _LOGGER.info(
            "Leelen MQTT %s，REST 同步间隔调整为 %s 秒",
            "已连接" if connected else "已断开",
            int(self.update_interval.total_seconds()),
        )

    def get_device_state(self, did, fiid):
        return self._data.get(f"{did}_{fiid}")

    def get_devices(self):
        return self._data.get("devices", [])

    def get_fiid_value(self, did, siid, fiid):
        return self._data.get("states", {}).get((did, siid, fiid))

    def get_climate_humidity(self, did, siid):
        source = self._data.get("humidity_sources", {}).get((did, siid))
        if source is None:
            return None
        return self.get_fiid_value(*source)

    def expect_fiid_value(self, did, siid, fiid, expected):
        """Protect a pending control from stale cloud snapshots."""
        self._control_expectations[(did, siid, fiid)] = {
            "expected": dict(expected),
            "confirmed": False,
            "deadline": monotonic() + CONTROL_PENDING_SECONDS,
            "event": asyncio.Event(),
        }

    async def async_wait_for_fiid_value(
        self,
        did,
        siid,
        fiid,
        timeout,
    ):
        """Wait for MQTT or REST to confirm the expected FIID value."""
        expectation = self._control_expectations.get((did, siid, fiid))
        if expectation is None:
            return False
        if expectation["confirmed"]:
            return True
        if timeout <= 0:
            return False
        try:
            await asyncio.wait_for(expectation["event"].wait(), timeout)
        except TimeoutError:
            return False
        return True

    def confirm_fiid_value(
        self,
        did,
        siid,
        fiid,
        value,
        state_time=None,
    ):
        key = (did, siid, fiid)
        self._accept_state_value(
            self._data.setdefault("states", {}),
            self._data.setdefault("state_times", {}),
            key,
            value,
            state_time,
        )

    def async_notify_state(self):
        """Notify coordinator listeners after a targeted state confirmation."""
        self.async_set_updated_data(self._data)

    def clear_fiid_expectation(self, did, siid, fiid):
        self._control_expectations.pop((did, siid, fiid), None)

    @staticmethod
    def _value_matches(value, expected):
        return isinstance(value, dict) and all(
            value.get(key) == expected_value
            for key, expected_value in expected.items()
        )

    @staticmethod
    def _build_state_reads(devices):
        reads = []
        for device in devices:
            did = device.get("dev_addr")
            direct_did = device.get("direct_did")
            if not did or not direct_did:
                continue
            for service in device.get("logic_srv") or []:
                fiids = SERVICE_FIIDS.get(service.get("service_type"))
                siid = service.get("siid")
                if not fiids or siid is None:
                    continue
                reads.append(
                    {
                        "did": did,
                        "siid": siid,
                        "directDid": direct_did,
                        "fiids": list(fiids),
                        "isRealDate": 1,
                    }
                )
        return reads

    @classmethod
    def _build_humidity_sources(cls, devices):
        sensors_by_group = {}
        sensors = []
        climates = []
        for device in devices:
            did = device.get("dev_addr")
            for service in device.get("logic_srv") or []:
                siid = service.get("siid")
                sub_group_id = service.get("sub_group_id")
                if did is None or siid is None:
                    continue
                item = (device, service)
                if service.get("service_type") == SERVICE_TYPE_SENSOR:
                    sensors.append(item)
                    if sub_group_id is not None:
                        sensors_by_group.setdefault(
                            sub_group_id,
                            [],
                        ).append(item)
                elif service.get("service_type") in (
                    SERVICE_TYPE_CENTRAL_AIR_CONDITIONER,
                    SERVICE_TYPE_FLOOR_HEATING,
                ):
                    climates.append(item)

        sources = {}
        unresolved = []
        used_sensor_names = set()
        for device, service in climates:
            climate_name = cls._normalized_room_name(
                service.get("logic_name")
            )
            exact_candidates = [
                candidate
                for candidate in sensors
                if cls._normalized_room_name(
                    candidate[1].get("logic_name")
                )
                == climate_name
            ]
            partial_candidates = [
                candidate
                for candidate in sensors
                if climate_name
                and (
                    climate_name
                    in cls._normalized_room_name(
                        candidate[1].get("logic_name")
                    )
                    or cls._normalized_room_name(
                        candidate[1].get("logic_name")
                    )
                    in climate_name
                )
            ]
            room_candidates = sensors_by_group.get(
                service.get("sub_group_id"),
                [],
            )
            candidates = (
                exact_candidates
                or partial_candidates
                or room_candidates
            )
            if not candidates:
                unresolved.append((device, service, climate_name))
                continue
            source_device, source_service = cls._select_room_sensor(
                service,
                candidates,
            )
            sources[(device.get("dev_addr"), service.get("siid"))] = (
                source_device.get("dev_addr"),
                source_service.get("siid"),
                FIID_HUMIDITY,
            )
            used_sensor_names.add(
                cls._normalized_room_name(
                    source_service.get("logic_name")
                )
            )

        unresolved_names = {
            climate_name
            for _, _, climate_name in unresolved
            if climate_name
        }
        remaining_sensors = [
            item
            for item in sensors
            if cls._normalized_room_name(item[1].get("logic_name"))
            not in used_sensor_names
        ]
        remaining_names = {
            cls._normalized_room_name(service.get("logic_name"))
            for _, service in remaining_sensors
        }
        if len(unresolved_names) == 1 and len(remaining_names) == 1:
            source_device, source_service = remaining_sensors[0]
            source = (
                source_device.get("dev_addr"),
                source_service.get("siid"),
                FIID_HUMIDITY,
            )
            for device, service, _ in unresolved:
                sources[(device.get("dev_addr"), service.get("siid"))] = source
        return sources

    @classmethod
    def _select_room_sensor(cls, climate_service, candidates):
        if len(candidates) == 1:
            return candidates[0]

        climate_name = cls._normalized_room_name(
            climate_service.get("logic_name")
        )
        exact_matches = [
            candidate
            for candidate in candidates
            if cls._normalized_room_name(candidate[1].get("logic_name"))
            == climate_name
        ]
        if exact_matches:
            return exact_matches[0]

        partial_matches = [
            candidate
            for candidate in candidates
            if climate_name
            and (
                climate_name
                in cls._normalized_room_name(candidate[1].get("logic_name"))
                or cls._normalized_room_name(candidate[1].get("logic_name"))
                in climate_name
            )
        ]
        return partial_matches[0] if partial_matches else candidates[0]

    @staticmethod
    def _normalized_room_name(name):
        value = _ROOM_NAME_SUFFIXES.sub("", str(name or ""))
        return _ROOM_NAME_SEPARATORS.sub("", value).casefold()

    def _state_index(self, response):
        states = dict(self._data.get("states", {}))
        state_times = dict(self._data.get("state_times", {}))
        if response.get("result") != 1:
            return states, state_times
        for item in response.get("params") or []:
            did = item.get("did")
            siid = item.get("siid")
            for fiid_data in item.get("fiids") or []:
                fiid = fiid_data.get("fiid")
                if did is None or siid is None or fiid is None:
                    continue
                self._accept_state_value(
                    states,
                    state_times,
                    (did, siid, fiid),
                    fiid_data.get("value"),
                    fiid_data.get("time"),
                )
        return states, state_times

    def _accept_state_value(
        self,
        states,
        state_times,
        key,
        value,
        state_time,
    ):
        previous_time = state_times.get(key)
        if self._is_older(state_time, previous_time):
            return False

        expectation = self._control_expectations.get(key)
        now = monotonic()
        if expectation is not None:
            if self._value_matches(value, expectation["expected"]):
                if not expectation["confirmed"]:
                    expectation["confirmed"] = True
                    expectation["deadline"] = (
                        now + CONTROL_CONFIRMED_GRACE_SECONDS
                    )
                    expectation["event"].set()
            elif now < expectation["deadline"]:
                return False
            else:
                self._control_expectations.pop(key, None)

        changed = states.get(key) != value
        states[key] = value
        if state_time is not None:
            state_times[key] = state_time
        return changed

    @staticmethod
    def _is_older(state_time, previous_time):
        if state_time is None or previous_time is None:
            return False
        try:
            return float(state_time) < float(previous_time)
        except (TypeError, ValueError):
            return False

    def async_apply_mqtt_payload(self, payload):
        """Merge one original-format dmgr.notifyFIIDS push into HA state."""
        if not isinstance(payload, dict):
            return
        if payload.get("method") != "dmgr.notifyFIIDS":
            return
        params = payload.get("params")
        if not isinstance(params, dict):
            return

        did = params.get("did")
        siid = params.get("siid")
        if did is None or siid is None:
            return

        states = self._data.setdefault("states", {})
        state_times = self._data.setdefault("state_times", {})
        received_at = int(time() * 1000)
        changed = False
        for fiid_data in params.get("fiids") or []:
            fiid = fiid_data.get("fiid")
            if fiid not in SUPPORTED_FIIDS:
                continue
            changed = self._accept_state_value(
                states,
                state_times,
                (did, siid, fiid),
                fiid_data.get("value"),
                fiid_data.get("time", received_at),
            ) or changed

        if changed:
            self.async_set_updated_data(self._data)
