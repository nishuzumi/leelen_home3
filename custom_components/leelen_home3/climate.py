import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .device_catalog import (
    SERVICE_TYPE_CENTRAL_AIR_CONDITIONER,
    entity_unique_id,
    extract_humidity,
    extract_temperature,
    iter_platform_services,
)
from .leelen.api.HttpApi import HttpApi
from .leelen.api.protocol import pending_read_delay

_LOGGER = logging.getLogger(__name__)

FAN_MODES = {
    0: FAN_LOW,
    1: FAN_MEDIUM,
    2: FAN_HIGH,
}

FIID_CLIMATE = 49411
FIID_HEATER = 49415
FIID_CURRENT_TEMPERATURE = 16641

HVAC_MODE_MAP = {
    0: HVACMode.HEAT,
    1: HVACMode.COOL,
    2: HVACMode.FAN_ONLY,
    3: HVACMode.DRY,
}
REVERSE_HVAC_MODE_MAP = {v: k for k, v in HVAC_MODE_MAP.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    devices = hass.data[DOMAIN].get('devices', {}).get(entry.entry_id, [])
    coordinator = (
        hass.data[DOMAIN].get(entry.entry_id, {}).get("coordinator")
    )
    entities = []

    for device, logic_srv in iter_platform_services(devices, "climate"):
        direct_did = device.get("direct_did")
        siid = logic_srv.get("siid")
        entity_class = (
            LeelenClimate
            if logic_srv.get("service_type") == SERVICE_TYPE_CENTRAL_AIR_CONDITIONER
            else LeelenHeater
        )
        entities.append(
            entity_class(
                hass,
                entry,
                device,
                logic_srv,
                siid,
                direct_did,
                coordinator,
            )
        )
    async_add_entities(entities)



class LeelenClimate(ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 5
    _attr_max_temp = 35
    _attr_target_temperature_step = 1.0
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE  | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
    def __init__(
        self,
        hass,
        entry,
        device,
        logic_srv,
        siid,
        direct_did,
        coordinator=None,
    ):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._logic_srv = logic_srv
        self._did = device.get("dev_addr")
        self._direct_did = direct_did
        self._coordinator = coordinator
        self._siid = siid
        self._name = logic_srv.get("logic_name", "Air Conditioner")
        self._service_type = logic_srv.get("service_type")
        self._fiid = (
            FIID_CLIMATE
            if self._service_type == SERVICE_TYPE_CENTRAL_AIR_CONDITIONER
            else FIID_HEATER
        )
        self._current_temperature = None
        self._current_humidity = None
        self._target_temperature = 26
        self._hvac_mode = HVACMode.OFF
        
        self._fan_mode = FAN_MEDIUM
        self._raw_wind_speed = None
        self._on_off = False
        self._attr_should_poll = coordinator is None

        self._attr_unique_id = entity_unique_id(device, logic_srv, "climate")
        self._apply_coordinator_state()

    @property
    def name(self):
        return self._name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._logic_srv["service_id"])},
            name=self._name,
            manufacturer="Leelen",
            model=str(self._device.get("model")),
        )

    @property
    def current_temperature(self):
        return self._current_temperature

    @property
    def current_humidity(self):
        return self._current_humidity

    @property
    def target_temperature(self):
        return self._target_temperature

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @property
    def hvac_modes(self):
        return self._attr_hvac_modes

    @property
    def fan_mode(self):
        return self._fan_mode

    @property
    def fan_modes(self):
        return list(FAN_MODES.values())

    @property
    def extra_state_attributes(self):
        if self._service_type != SERVICE_TYPE_CENTRAL_AIR_CONDITIONER:
            return {}
        return {"leelen_wind_speed": self._raw_wind_speed}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self._coordinator is not None:
            self.async_on_remove(
                self._coordinator.async_add_listener(
                    self._handle_coordinator_update
                )
            )

    def _handle_coordinator_update(self):
        self._apply_coordinator_state()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        if kwargs.get("temperature") is not None:
            if self._service_type == SERVICE_TYPE_CENTRAL_AIR_CONDITIONER:
                await self._send_control(
                    {
                        "setTemp": int(kwargs["temperature"]),
                        "onOff": 1,
                    }
                )
            else:
                self._target_temperature = kwargs["temperature"]
                await self._send_control(self._complete_control_value())

    async def async_set_hvac_mode(self, hvac_mode):
        if self._service_type == SERVICE_TYPE_CENTRAL_AIR_CONDITIONER:
            if hvac_mode == HVACMode.OFF:
                value = {"onOff": 0}
            else:
                value = {
                    "mode": REVERSE_HVAC_MODE_MAP[hvac_mode],
                    "onOff": 1,
                }
        else:
            if hvac_mode == HVACMode.OFF:
                self._on_off = False
                self._hvac_mode = HVACMode.OFF
            else:
                self._on_off = True
                self._hvac_mode = hvac_mode
            value = self._complete_control_value()

        await self._send_control(value)

    async def async_set_fan_mode(self, fan_mode):
        wind_speed = next(
            value for value, name in FAN_MODES.items() if name == fan_mode
        )
        await self._send_control(
            {
                "windSpeed": wind_speed,
                "onOff": 1,
            }
        )

    def _complete_control_value(self):
        """Keep the existing floor-heating request shape unchanged."""
        value = {
            "onOff": 1 if self._on_off else 0,
            "mode": REVERSE_HVAC_MODE_MAP.get(self._hvac_mode, 0),
            "setTemp": int(self._target_temperature),
        }
        wind_speed = next(
            (
                speed
                for speed, name in FAN_MODES.items()
                if name == self._fan_mode
            ),
            None,
        )
        if wind_speed is not None:
            value["windSpeed"] = wind_speed
        return value

    async def _send_control(self, value):
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.expect_fiid_value(
                self._did,
                self._siid,
                self._fiid,
                value,
            )

        try:
            api = HttpApi.get_instance(self._hass)
            result = await api.encrypt_v1_ctrl_fiids(
                siid=self._siid,
                direct_did=self._direct_did,
                fiids=[{"fiid": self._fiid, "value": value}],
                did=self._did,
            )
            if result.get("result") != 1:
                if coordinator is not None:
                    coordinator.clear_fiid_expectation(
                        self._did,
                        self._siid,
                        self._fiid,
                    )
                return

            retry_delay = pending_read_delay(result)
            if coordinator is not None:
                if retry_delay is not None:
                    confirmed = await coordinator.async_wait_for_fiid_value(
                        self._did,
                        self._siid,
                        self._fiid,
                        retry_delay,
                    )
                    if confirmed:
                        return
                await self._confirm_control(api, coordinator, value)
            else:
                if retry_delay is not None:
                    await asyncio.sleep(retry_delay)
                await self.async_update()
        except Exception as e:
            if coordinator is not None:
                coordinator.clear_fiid_expectation(
                    self._did,
                    self._siid,
                    self._fiid,
                )
            _LOGGER.error(f"控制空调失败: {e}")

    async def _confirm_control(self, api, coordinator, expected):
        """Apply state only after the detailed device read confirms the command."""
        result = await api.read_dids_fiids(
            did=self._did,
            direct_did=self._direct_did,
            fiids=[self._fiid, FIID_CURRENT_TEMPERATURE],
            siid=self._siid,
            is_real_date=1,
        )
        if result.get("result") != 1:
            return

        params = result.get("params") or []
        if not params:
            return
        fiid_data = {
            item.get("fiid"): item
            for item in (params[0].get("fiids") or [])
            if item.get("fiid") is not None
        }
        state_item = fiid_data.get(self._fiid, {})
        state = state_item.get("value")
        if not coordinator._value_matches(state, expected):
            _LOGGER.debug(
                "空调控制等待设备确认: did=%s siid=%s expected=%s",
                self._did,
                self._siid,
                expected,
            )
            return

        coordinator.confirm_fiid_value(
            self._did,
            self._siid,
            self._fiid,
            state,
            state_item.get("time"),
        )
        temperature_item = fiid_data.get(FIID_CURRENT_TEMPERATURE, {})
        current_temperature = temperature_item.get("value")
        if current_temperature is not None:
            coordinator.confirm_fiid_value(
                self._did,
                self._siid,
                FIID_CURRENT_TEMPERATURE,
                current_temperature,
                temperature_item.get("time"),
            )
        coordinator.async_notify_state()

    def _apply_coordinator_state(self):
        if self._coordinator is None:
            return
        values = {
            self._fiid: self._coordinator.get_fiid_value(
                self._did,
                self._siid,
                self._fiid,
            ),
            FIID_CURRENT_TEMPERATURE: (
                self._coordinator.get_fiid_value(
                    self._did,
                    self._siid,
                    FIID_CURRENT_TEMPERATURE,
                )
            ),
        }
        self._apply_values(values)
        current_humidity = extract_humidity(
            self._coordinator.get_climate_humidity(
                self._did,
                self._siid,
            )
        )
        if current_humidity is not None:
            self._current_humidity = current_humidity

    def _apply_values(self, values):
        value = values.get(self._fiid, {})
        if isinstance(value, dict):
            self._on_off = value.get("onOff", 0) == 1
            self._target_temperature = value.get("setTemp", 26)
            if value.get("mode") is not None:
                self._hvac_mode = HVAC_MODE_MAP.get(
                    value.get("mode"), HVACMode.OFF
                )

            if not self._on_off:
                self._hvac_mode = HVACMode.OFF
            elif (
                self._service_type
                != SERVICE_TYPE_CENTRAL_AIR_CONDITIONER
            ):
                self._hvac_mode = HVACMode.HEAT

            wind_speed = value.get("windSpeed")
            self._raw_wind_speed = wind_speed
            if wind_speed in FAN_MODES:
                self._fan_mode = FAN_MODES[wind_speed]
            elif wind_speed is not None:
                self._fan_mode = None

        current_temperature = extract_temperature(
            values.get(FIID_CURRENT_TEMPERATURE)
        )
        if current_temperature is not None:
            self._current_temperature = current_temperature

    async def async_update(self):
        try:
            result = await HttpApi.get_instance(self._hass).read_dids_fiids(
                did=self._did,
                direct_did=self._direct_did,
                fiids=[self._fiid, FIID_CURRENT_TEMPERATURE],
                siid=self._siid,
                is_real_date=1,
            )
            if result.get("result") == 1:
                params = result.get("params", [])
                if params:
                    fiids_data = params[0].get("fiids", [])
                    values = {
                        item.get("fiid"): item.get("value")
                        for item in fiids_data
                        if item.get("fiid") is not None
                    }
                    self._apply_values(values)
        except Exception as e:
            _LOGGER.error(f"更新空调状态失败: {e}")


class LeelenHeater(LeelenClimate):
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE  | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
