"""Normalize Leelen cloud devices into Home Assistant entity candidates."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any


SERVICE_TYPE_CENTRAL_AIR_CONDITIONER = 8259
SERVICE_TYPE_FRESH_AIR = 8261
SERVICE_TYPE_FLOOR_HEATING = 8268
SERVICE_TYPE_SENSOR = 8272

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


def merge_temperature(previous: float | None, value: Any) -> float | None:
    """Keep the last reading when the gateway has not returned a value yet."""
    current = extract_temperature(value)
    return previous if current is None else current
