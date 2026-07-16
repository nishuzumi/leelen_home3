import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .device_catalog import entity_unique_id, iter_platform_services, merge_temperature
from .leelen.api.HttpApi import HttpApi

_LOGGER = logging.getLogger(__name__)
FIID_READ = 49415



async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    devices = hass.data[DOMAIN].get('devices', {}).get(entry.entry_id, [])
    entities = []

    for device, logic_srv in iter_platform_services(devices, "sensor"):
        direct_did = device.get("direct_did")
        siid = logic_srv.get("siid")
        entities.append(LeelenSensor(hass, entry, device, logic_srv, siid, direct_did))

    async_add_entities(entities)


class LeelenSensor(SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, hass, entry, device, logic_srv, siid, direct_did):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._logic_srv = logic_srv
        self._siid = siid
        self._direct_did = direct_did
        self._did = device.get("dev_addr")
        self._state = None

        self._name = logic_srv.get("logic_name", "Temperature")
        self._attr_unique_id = entity_unique_id(device, logic_srv, "sensor")
        self._attr_icon = "mdi:thermometer"

    @property
    def name(self):
        return self._name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._did)},
            name=self._device.get("dev_name", "Leelen Device"),
            manufacturer="Leelen",
            model=str(self._device.get("model")),
        )

    @property
    def native_value(self):
        state = self._state
        if state is not None:
            try:
                return float(state)
            except (ValueError, TypeError):
                return state
        return None

    async def async_update(self):
        try:
            result = await HttpApi.get_instance(self._hass).read_dids_fiids(
                did=self._did,
                direct_did=self._direct_did,
                fiids=[FIID_READ],
                siid=self._siid
            )

            if result.get("result") == 1:
                params = result.get("params", [])
                if params:
                    fiids_data = params[0].get("fiids", [])
                    if fiids_data:
                        self._state = merge_temperature(
                            self._state, fiids_data[0].get("value")
                        )
        except Exception as e:
            _LOGGER.error(f"更新传感器状态失败: {e}")
