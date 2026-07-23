"""Normalize Leelen cloud devices into Home Assistant entity candidates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any


SERVICE_TYPE_CENTRAL_AIR_CONDITIONER = 8259
SERVICE_TYPE_FRESH_AIR = 8261
SERVICE_TYPE_FLOOR_HEATING = 8268
SERVICE_TYPE_SENSOR = 8272
CLIMATE_SERVICE_TYPES = {
    SERVICE_TYPE_CENTRAL_AIR_CONDITIONER,
    SERVICE_TYPE_FLOOR_HEATING,
}

PLATFORM_SERVICE_TYPES = {
    "climate": {
        SERVICE_TYPE_CENTRAL_AIR_CONDITIONER,
        SERVICE_TYPE_FLOOR_HEATING,
    },
    "fan": {SERVICE_TYPE_FRESH_AIR},
    "sensor": {SERVICE_TYPE_SENSOR},
}

TEMPERATURE_KEYS = (
    "temperature",
    "curTemp",
    "currentTemp",
    "currentTemperature",
    "temp",
)

HUMIDITY_KEYS = (
    "humidity",
    "relativeHumidity",
)

_ROOM_NAME_SUFFIXES = re.compile(
    r"(中央空调|空调|地暖|温湿度传感器|温湿度|传感器|温控面板|温控器|温控|面板)"
)
_ROOM_NAME_SEPARATORS = re.compile(r"[\s_\-（）()]+")


def _matching_detail(did: Any, detail_response: Mapping[str, Any]) -> Mapping[str, Any]:
    detail_items = detail_response.get("params") or []
    for detail in detail_items:
        physic_device = detail.get("physicDevice") or {}
        if physic_device.get("did") == did:
            return detail
    if len(detail_items) == 1:
        return detail_items[0]
    return {}


def normalize_device(
    physical_device: Mapping[str, Any],
    detail_response: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one physical device with independently typed logical services."""
    did = physical_device.get("did")
    detail = _matching_detail(did, detail_response)
    logical_services = []

    for logical_device in detail.get("logicDevices") or []:
        siid = logical_device.get("siid")
        service_type = logical_device.get("serviceType")
        if siid is None or service_type is None:
            continue
        logical_services.append(
            {
                "service_id": f"{did}_{siid}",
                "siid": siid,
                "logic_name": logical_device.get("logicName") or "Unknown",
                "profile_id": logical_device.get("profileId"),
                "service_type": service_type,
                "service_name": logical_device.get("purposeTypeName") or "",
                "sub_group_id": logical_device.get("subGroupId"),
            }
        )

    return {
        "dev_addr": did,
        "dev_name": physical_device.get("name") or "Unknown",
        "dev_type": str(physical_device.get("profileId") or ""),
        "direct_did": physical_device.get("directDid"),
        "room_name": physical_device.get("roomName") or "",
        "profile_id": physical_device.get("profileId"),
        "device_type": physical_device.get("deviceType"),
        "model": physical_device.get("softModel") or "",
        "logic_srv": logical_services,
    }


def iter_platform_services(
    devices: Iterable[Mapping[str, Any]], platform: str
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Yield device/service pairs supported by a Home Assistant platform."""
    supported_types = PLATFORM_SERVICE_TYPES.get(platform, set())
    for device in devices:
        for service in device.get("logic_srv") or []:
            if service.get("service_type") in supported_types:
                yield device, service


def entity_unique_id(
    device: Mapping[str, Any], service: Mapping[str, Any], platform: str
) -> str:
    """Return the unique ID shared by discovery and registry reconciliation."""
    return f"leelen_{platform}_{device.get('dev_addr')}_{service.get('siid')}"


def build_climate_sensor_sources(devices):
    """Associate climate services with their room's panel sensor service."""
    sensors = []
    climates = []
    for device in devices:
        did = device.get("dev_addr")
        for service in device.get("logic_srv") or []:
            siid = service.get("siid")
            if did is None or siid is None:
                continue
            item = {
                "key": (did, siid),
                "name": _normalized_room_name(service.get("logic_name")),
                "group": service.get("sub_group_id"),
            }
            service_type = service.get("service_type")
            if service_type == SERVICE_TYPE_SENSOR:
                sensors.append(item)
            elif service_type in CLIMATE_SERVICE_TYPES:
                climates.append(item)

    sources = {}
    unresolved = []
    used_sensor_names = set()
    for climate in climates:
        candidates = _matching_sensors(climate, sensors)
        if not candidates:
            unresolved.append(climate)
            continue
        sensor = candidates[0]
        sources[climate["key"]] = sensor["key"]
        used_sensor_names.add(sensor["name"])

    # Some cloud responses return every physical panel in the currently
    # selected room. A unique unmatched room/panel pair is still unambiguous.
    unresolved_names = {
        item["name"] for item in unresolved if item["name"]
    }
    remaining_sensors = [
        item for item in sensors if item["name"] not in used_sensor_names
    ]
    remaining_names = {item["name"] for item in remaining_sensors}
    if len(unresolved_names) == 1 and len(remaining_names) == 1:
        source = remaining_sensors[0]["key"]
        for climate in unresolved:
            sources[climate["key"]] = source

    return sources


def _matching_sensors(climate, sensors):
    name = climate["name"]
    exact = [sensor for sensor in sensors if sensor["name"] == name]
    if exact:
        return exact

    partial = [
        sensor
        for sensor in sensors
        if name
        and sensor["name"]
        and (name in sensor["name"] or sensor["name"] in name)
    ]
    if partial:
        return partial

    group = climate["group"]
    if group is None:
        return []
    return [sensor for sensor in sensors if sensor["group"] == group]


def _normalized_room_name(name):
    value = _ROOM_NAME_SUFFIXES.sub("", str(name or ""))
    return _ROOM_NAME_SEPARATORS.sub("", value).casefold()


def extract_temperature(value: Any) -> float | None:
    """Extract a Celsius value from Leelen sensor FIID payloads."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, Mapping):
        return None
    for key in TEMPERATURE_KEYS:
        if key in value:
            return extract_temperature(value[key])
    return None


def extract_humidity(value: Any) -> float | None:
    """Extract a relative-humidity percentage from a sensor FIID payload."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, Mapping):
        return None
    for key in HUMIDITY_KEYS:
        if key in value:
            return extract_humidity(value[key])
    return None
