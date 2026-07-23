import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ADDR,
    CONF_GROUP_ID,
    CONF_MQTT_CLIENT_ID,
    CONF_MQTT_USERNAME,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
    SUPPORTED_PLATFORMS,
)
from .coordinator import LeelenCoordinator
from .leelen.api.HttpApi import HttpApi
from .leelen.utils.LogUtils import LogUtils
from .mqtt_client import LeelenMqttClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 老配置没有 refreshToken，提示用户删除重新添加
    if not entry.data.get(CONF_REFRESH_TOKEN):
        raise ConfigEntryNotReady(
            "配置已过期，缺少 refreshToken。请删除此集成后重新添加（设置 → 设备与服务 → 集成 → 立林3.0 → 删除）"
        )
    hass.data.setdefault(DOMAIN, {})
    LogUtils.d(__name__, f"开始设置集成, domain={DOMAIN}")

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
            LogUtils.d(
                __name__,
                f"  - 服务: service_type={srv.get('service_type')}, siid={srv.get('siid')}",
            )

    coordinator = LeelenCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "devices": all_devices,
        "options": dict(entry.options),
    }

    await hass.config_entries.async_forward_entry_setups(entry, SUPPORTED_PLATFORMS)
    LogUtils.d(__name__, f"平台设置完成: {SUPPORTED_PLATFORMS}")

    mqtt_client_id = entry.options.get(CONF_MQTT_CLIENT_ID, "").strip()
    mqtt_username = entry.options.get(CONF_MQTT_USERNAME, "").strip()
    if mqtt_client_id and mqtt_username:
        mqtt_client = await hass.async_add_executor_job(
            LeelenMqttClient,
            hass,
            coordinator,
            mqtt_client_id,
            mqtt_username,
        )
        hass.data[DOMAIN][entry.entry_id]["mqtt_client"] = mqtt_client
        await hass.async_add_executor_job(mqtt_client.start)
    else:
        _LOGGER.info("未配置 Leelen MQTT 注册身份，使用 REST 状态同步")

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    mqtt_client = runtime_data.get("mqtt_client")
    if mqtt_client:
        await hass.async_add_executor_job(mqtt_client.stop)

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


async def _async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload only when integration options changed, not on token refresh."""
    runtime_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime_data is None:
        return
    if runtime_data.get("options") != dict(entry.options):
        await hass.config_entries.async_reload(entry.entry_id)
