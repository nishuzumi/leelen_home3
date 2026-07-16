from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from .const import DOMAIN
from .leelen.api.HttpApi import HttpApi

_LOGGER = logging.getLogger(__name__)


class LeelenCoordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, entry):
        super().__init__(
            hass,
            _LOGGER,
            name="leelen api",
            update_interval=timedelta(seconds=30),
        )
        self._entry = entry
        self._device_addr = entry.data.get("deviceAddr")
        self._data = {}
        self._unsub_timer = None

    async def _async_update_data(self):
        _LOGGER.debug("=== 开始心跳更新 ===")
        try:
            api = HttpApi.get_instance(self.hass)
            devices = await api.get_device_list_v2()
            devices = await api.get_online_status(devices)

            hass_data = self.hass.data.setdefault(DOMAIN, {})
            hass_data.setdefault('devices', {})[self._entry.entry_id] = devices

            self._data = {"devices": devices}

            _LOGGER.debug(f"=== 心跳更新完成，{len(devices)} 个设备 ===")
            return self._data
        except Exception as e:
            _LOGGER.error(f"=== 心跳更新失败: {e} ===")
            raise

    async def async_start_timer(self):
        """手动启动定时心跳，不依赖 DataUpdateCoordinator 内置定时器。"""
        _LOGGER.debug("=== 定时心跳已启动，间隔 30 秒 ===")
        self._unsub_timer = async_track_time_interval(
            self.hass,
            self._async_refresh,
            timedelta(seconds=30),
        )

    async def async_stop_timer(self):
        """停止定时心跳。"""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
            _LOGGER.debug("=== 定时心跳已停止 ===")

    def get_device_state(self, did, fiid):
        return self._data.get(f"{did}_{fiid}")

    def get_devices(self):
        return self._data.get("devices", [])
