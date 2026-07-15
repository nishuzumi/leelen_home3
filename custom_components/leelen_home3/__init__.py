import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import DOMAIN, SUPPORTED_PLATFORMS, CONF_DEVICE_ADDR, CONF_USERNAME, CONF_PASSWORD, CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_GROUP_ID
from .coordinator import LeelenCoordinator
from .leelen.api.HttpApi import HttpApi
from .leelen.utils.LogUtils import LogUtils

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 老配置没有 refreshToken，提示用户删除重新添加
    if not entry.data.get(CONF_REFRESH_TOKEN):
        raise ConfigEntryNotReady(
            "配置已过期，缺少 refreshToken。请删除此集成后重新添加（设置 → 设备与服务 → 集成 → 立林3.0 → 删除）"
        )
    hass.data.setdefault(DOMAIN, {})
    LogUtils.d(__name__, f"开始设置集成, domain={DOMAIN}")
    LogUtils.d(__name__, f"entry.data: {entry.data}")

    hass.data[DOMAIN].setdefault('devices', {})
    hass.data[DOMAIN].setdefault('entities', {})

    for platform in SUPPORTED_PLATFORMS:
        hass.data[DOMAIN]['entities'][platform] = []

    api = HttpApi.get_instance(hass)
    api._entry_id = entry.entry_id
    api.device_addr = entry.data.get(CONF_DEVICE_ADDR, "")
    api.username = entry.data.get(CONF_USERNAME, "")
    api._access_token = entry.data.get(CONF_ACCESS_TOKEN, "")
    api._refresh_token = entry.data.get(CONF_REFRESH_TOKEN, "")
    api._group_id = entry.data.get(CONF_GROUP_ID, "")

    LogUtils.d(__name__, f"API实例: {api}")
    LogUtils.d(__name__, f"api._group_id: {api._group_id if hasattr(api, '_group_id') else 'N/A'}")

    try:
        all_devices = await api.get_device_list_v2()
        LogUtils.d(__name__, f"get_device_list_v2 返回: {len(all_devices) if all_devices else 0} 个设备")
        if all_devices is None:
            all_devices = []
            LogUtils.d(__name__, "设备列表为 None，已转换为空列表")
    except ConfigEntryAuthFailed:
        raise
    except Exception as e:
        LogUtils.e(f"获取设备列表异常: {e}")
        all_devices = []

    hass.data[DOMAIN]['devices'][entry.entry_id] = all_devices
    LogUtils.d(__name__, f"已保存设备列表到 hass.data[{DOMAIN}]['devices'][{entry.entry_id}]")

    LogUtils.d(__name__, f"获取设备列表成功，共 {len(all_devices)} 个设备")
    for device in all_devices:
        dev_name = device.get('dev_name', 'Unknown')
        dev_addr = device.get('dev_addr', '')
        profile_id = device.get('profile_id', 'N/A')
        logic_srvs = device.get('logic_srv', [])
        is_online = device.get('online_info', {}).get('isOnline', 0) == 1
        online_status = "在线" if is_online else "离线"
        LogUtils.d(__name__, f"设备: {dev_name} ({dev_addr}), profile_id={profile_id}, 服务数量: {len(logic_srvs)}, 状态: {online_status}")
        for srv in logic_srvs:
            LogUtils.d(__name__, f"  - 服务: fiid={srv.get('fiid')}, siid={device.get('siid')}")

    coordinator = LeelenCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_timer()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "devices": all_devices,
    }

    await hass.config_entries.async_forward_entry_setups(entry, SUPPORTED_PLATFORMS)
    LogUtils.d(__name__, f"平台设置完成: {SUPPORTED_PLATFORMS}")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, SUPPORTED_PLATFORMS)

    if DOMAIN not in hass.data:
        return unload_ok

    if entry.entry_id in hass.data[DOMAIN]:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator = data.get("coordinator")
        if coordinator:
            await coordinator.async_stop_timer()
        LogUtils.d(__name__, f"已卸载配置项: {entry.entry_id}")

    if entry.entry_id in hass.data[DOMAIN].get('devices', {}):
        hass.data[DOMAIN]['devices'].pop(entry.entry_id)

    if not hass.data[DOMAIN].get('devices', {}):
        hass.data.pop(DOMAIN, None)

    return unload_ok
