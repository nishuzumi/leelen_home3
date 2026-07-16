import asyncio
import hashlib
import json
import random
import string
import threading
import time
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ...device_catalog import normalize_device
from ..entity.BaseParam import BaseParam, CodeLoginRequestParam, GetVerifyCodeRequestParam
from ..entity.BaseRequest import BaseRequest
from ..utils.AesCoder import AesCoder
from ..utils.LogUtils import LogUtils
from ..utils.RSAEncrypt import RSAEncrypt
from .protocol import pending_read_delay


class HttpApi:
    _instance = None
    _lock = threading.Lock()

    def __init__(self, hass: HomeAssistant):
        self.BASE_URL = "https://iot.leelen.com"
        self.RD_BASE_URL = "https://rd.iot.leelen.com"
        self.device_addr = ""
        self.appTerminalId = f"ANDROID-{self.get_terminal_id()}"
        self.appTerminalModel = "REP-AN00"
        self.uuid = None
        self.verifyCodeSign = ""
        self.username = ""
        self._hass = hass
        self._device_list = []
        self._access_token = ""
        self._refresh_token = ""
        self._group_id = ""
        self._entry_id = None

    def get_secret(self, num: int) -> str:
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(num))

    @classmethod
    def get_instance(cls, hass: HomeAssistant = None):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = HttpApi(hass)
        return cls._instance

    def get_terminal_id(self):
        return hashlib.md5(''.join(random.choices(string.ascii_letters + string.digits, k=32)).encode()).hexdigest()

    async def _do_refresh_token(self):
        """用 refreshToken 刷新 accessToken"""
        if not self._refresh_token:
            LogUtils.e("无 refreshToken，无法刷新")
            return False

        try:
            url = f"{self.BASE_URL}/rest/app/community/security/refreshToken"
            session = async_get_clientsession(self._hass)
            params = {
                "accessToken": self._access_token,
                "refreshToken": self._refresh_token
            }
            async with session.post(
                url,
                verify_ssl=False,
                json={
                    "params": params,
                    "seq": 65,
                    "version": "V1.0"
                },
            ) as res:
                res.raise_for_status()
                data = await res.json(encoding="utf-8")
                LogUtils.d(
                    "HttpApi",
                    f"refreshToken 请求完成: result={data.get('result')}",
                )
                if data.get("result") == 1:
                    p = data.get("params", {})
                    new_token = p.get("accessToken")
                    new_refresh = p.get("refreshToken")
                    if new_token:
                        self._access_token = new_token
                        if new_refresh:
                            self._refresh_token = new_refresh
                        # 持久化保存新 token，防止重启后使用旧 token
                        await self._persist_tokens()
                        LogUtils.d("HttpApi", "token刷新成功(refreshToken方式)")
                        return True
                elif data.get("result") == 10002:
                    LogUtils.e("refreshToken 已过期，触发重新认证")
                    raise ConfigEntryAuthFailed(
                        "refreshToken 已过期，请重新验证码登录"
                    )
        except ConfigEntryAuthFailed:
            raise
        except Exception as e:
            LogUtils.e(f"refreshToken方式失败: {e}")

        return False

    async def _persist_tokens(self):
        """将最新的 token 保存到 config entry，重启后不会丢失。"""
        if not self._hass or not self._entry_id:
            return
        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if not entry:
            return
        self._hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                "accessToken": self._access_token,
                "refreshToken": self._refresh_token,
            }
        )
        LogUtils.d("HttpApi", "token 已持久化保存到 config entry")

    async def _make_request(self, url, params, seq, version="V1.0"):
        session = async_get_clientsession(self._hass)
        headers = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        async with session.post(
            url,
            verify_ssl=False,
            headers=headers,
            json={
                "params": params,
                "seq": seq,
                "version": version
            },
        ) as res:
            res.raise_for_status()
            res_dict = await res.json(encoding="utf-8")
            response_params = res_dict.get("params")
            response_count = len(response_params) if isinstance(response_params, list) else None
            LogUtils.d(
                "HttpApi",
                f"请求完成: url={url} seq={seq} result={res_dict.get('result')} params_count={response_count}",
            )

            # token 过期（10001），自动刷新并重试
            if res_dict.get("result") == 10001 and self._refresh_token:
                LogUtils.d("HttpApi", "token已过期，尝试刷新token...")
                refresh_ok = await self._do_refresh_token()
                if refresh_ok:
                    headers["Authorization"] = f"Bearer {self._access_token}"
                    async with session.post(
                        url,
                        verify_ssl=False,
                        headers=headers,
                        json={
                            "params": params,
                            "seq": seq,
                            "version": version
                        },
                    ) as res2:
                        res2.raise_for_status()
                        res_dict2 = await res2.json(encoding="utf-8")
                        LogUtils.d(
                            "HttpApi",
                            f"token刷新后重试完成: url={url} result={res_dict2.get('result')}",
                        )
                        return res_dict2
                else:
                    LogUtils.e("token刷新失败，无法重试请求")

            return res_dict

    async def get_user(self, accessToken):
        session = async_get_clientsession(self._hass)
        headers = {
            "Authorization": f"Bearer {accessToken}"
        }
        async with session.post(
                f"{self.BASE_URL}/rest/app/community/platform/getUser",
                verify_ssl=False,
                headers=headers,
                json={},
        ) as res:
            res.raise_for_status()
            res_dict = await res.json(encoding="utf-8")
            return res_dict

    async def get_physics_device_list(self, groupId):
        url = f"{self.BASE_URL}/rest/app/community/smartHome/getPhysicsDeviceList"
        params = {
            "groupId": groupId
        }
        return await self._make_request(url, params, 85)

    async def get_device_list_v2(self):
        url = f"{self.BASE_URL}/rest/app/community/smartHome/getPhysicsDeviceList"
        params = {
            "groupId": self._group_id
        }
        LogUtils.d("HttpApi", f"getPhysicsDeviceList 请求: groupId={self._group_id}")
        result = await self._make_request(url, params, 100)
        LogUtils.d("HttpApi", f"getPhysicsDeviceList 返回: result={result.get('result')}")

        devices = []
        if result.get("result") == 1:
            params_data = result.get("params", [])
            LogUtils.d("HttpApi", f"物理设备数量: {len(params_data)}")
            for device_info in params_data:
                did = device_info.get("did")
                direct_did = device_info.get("directDid")
                detail_result = {"result": 0, "params": []}
                if did and direct_did:
                    try:
                        detail_result = await self.get_devices(
                            did=did,
                            direct_did=direct_did
                        )
                        if detail_result.get("result") != 1:
                            LogUtils.d("HttpApi", f"设备 {did} getDevices 失败: result={detail_result.get('result')}")
                    except Exception as e:
                        LogUtils.e(f"获取设备 {did} 详情失败: {e}")

                devices.append(normalize_device(device_info, detail_result))
        else:
            LogUtils.d("HttpApi", f"getPhysicsDeviceList 返回失败: result={result.get('result')}")

        LogUtils.d("HttpApi", f"get_device_list_v2 返回设备数: {len(devices)}")
        return devices

    async def get_online_status(self, devices):
        if not devices:
            return devices
        
        params = []
        for device in devices:
            params.append({
                "did": device.get("dev_addr"),
                "directDid": device.get("direct_did")
            })
        
        try:
            result = await self._make_request(
                f"{self.BASE_URL}/rest/app/community/dc/batchGetDeviceOnlineInfo",
                params,
                101
            )
            
            if result.get("result") == 1:
                online_info_list = result.get("params", [])
                online_map = {}
                for info in online_info_list:
                    did = info.get("did")
                    online_map[did] = {
                        "isOnline": info.get("isOnline", 0),
                        "isDormancy": info.get("isDormancy", False)
                    }
                
                for device in devices:
                    did = device.get("dev_addr")
                    if did in online_map:
                        device["online_info"] = online_map[did]
        except Exception as e:
            LogUtils.e(f"获取在线状态失败: {e}")
        
        return devices

    async def control_device_fiids(self, accessToken, siid, directDid, fiids, did):
        session = async_get_clientsession(self._hass)
        headers = {
            "Authorization": f"Bearer {accessToken}"
        }
        async with session.post(
                f"{self.BASE_URL}/rest/app/community/encryptV1CtrlFIIDS",
                verify_ssl=False,
                headers=headers,
                json={
                    "params": {
                        "siid": siid,
                        "directDid": directDid,
                        "fiids": fiids,
                        "did": did
                    },
                    "seq": 74,
                    "version": "V1.1"
                },
        ) as res:
            res.raise_for_status()
            res_dict = await res.json(encoding="utf-8")
            return res_dict

    async def third_login(self, username, password):
        headers = {}
        data = {
            "appTerminalId": self.appTerminalId,
            "password": password,
            "appTerminalModel": self.appTerminalModel,
            "loginMark": "0",
            "osVersion": "12",
            "appTerminalName": "null",
            "osType": "1",
            "packageName": "com.leelen.luxdomo",
            "userName": username,
            "autoLogin": "0"
        }
        session = async_get_clientsession(self._hass)

        async with session.post(
                f"{self.RD_BASE_URL}/rest/api/third/app/user/login",
                verify_ssl=False,
                headers=headers,
                data=data,
        ) as res:
            res.raise_for_status()
            res_dict = await res.json(encoding="utf-8")
            return res_dict

    async def get_homes(self):
        url = f"{self.BASE_URL}/rest/app/community/mergeHomes/getHomes"
        return await self._make_request(url, {}, 151)

    async def get_device_list(self, userName):
        headers = {}
        data = {
            "userName": userName
        }
        session = async_get_clientsession(self._hass)

        async with session.post(
                f"{self.RD_BASE_URL}/rest/app/device/deviceList",
                verify_ssl=False,
                headers=headers,
                data=data,
        ) as res:
            res.raise_for_status()
            res_dict = await res.json(encoding="utf-8")
            return res_dict

    async def VerifyCode(self, username):
        params = GetVerifyCodeRequestParam(username=username)
        baseRequest = BaseRequest()
        baseRequest.params = params.to_dict()
        baseRequest.seq = 93
        session = async_get_clientsession(self._hass)
        async with session.post(
                f"{self.BASE_URL}/rest/app/community/security/getVerifyCode",
                verify_ssl=False,
                json=baseRequest.to_dict(),
        ) as res:
            res.raise_for_status()
            data = await res.json(encoding="utf-8")
            self.verifyCodeSign = data.get("params")
            self.username = username
            return data

    async def verifyCodeLogin(self, username=None, verifyCode=None, verifyCodeSign=None, publicKey=None):
        params = CodeLoginRequestParam()
        params.username = username
        params.Phone = username
        params.verifyCode = verifyCode
        params.verifyCodeSign = verifyCodeSign
        params.terminalId = self.appTerminalId
        params = self.encrypt_params(params.to_dict(), publicKey)
        baseRequest = BaseRequest()
        baseRequest.params = params.to_dict()
        baseRequest.seq = 93
        session = async_get_clientsession(self._hass)

        async with session.post(
                f"{self.BASE_URL}/rest/app/community/user/verifyCodeLogin",
                verify_ssl=False,
                json=baseRequest.to_dict(),
        ) as res:
            res.raise_for_status()
            data = await res.json(encoding="utf-8")
            if data["result"] != 1:
                raise Exception(data["message"])
            self.verifyCodeSign = data.get("params")
            self.username = username
            return data

    async def code_login(self, verifyCode):
        self.uuid = await self.get_uuid()
        code_login_result = await self.verifyCodeLogin(self.username, verifyCode, self.verifyCodeSign, self.uuid)
        accessToken = code_login_result.get("params", {}).get("accessToken")
        self._access_token = accessToken
        # 验证码登录也可能返回 refreshToken
        refresh_token = code_login_result.get("params", {}).get("refreshToken")
        if refresh_token:
            self._refresh_token = refresh_token
            LogUtils.d("HttpApi", "从 verifyCodeLogin 获取到 refreshToken")
        user_data = await self.get_user(accessToken)
        username = user_data.get("params", {}).get("userName")
        password = user_data.get("params", {}).get("password")

        third_result = await self.third_login(username, password)
        # 如果 verifyCodeLogin 没给 refreshToken，试试从 third_login 拿
        if not self._refresh_token:
            rt = third_result.get("refreshToken") or third_result.get("token") or third_result.get("accessToken")
            if rt:
                self._refresh_token = rt
                LogUtils.d("HttpApi", "从 third_login 获取到 refreshToken")
        else:
            # 已经有 refreshToken 了（可能是老的），但也从 third_login 拿一份新的覆盖
            third_rt = third_result.get("refreshToken") or third_result.get("token") or third_result.get("accessToken")
            if third_rt:
                self._refresh_token = third_rt
                LogUtils.d("HttpApi", "从 third_login 覆盖 refreshToken")
        bindCallers = third_result.get("bindCallers")
        accountId = third_result.get("accountId")
        group_name  = "我的家"
        homes_result = await self.get_homes()
        homes = homes_result.get("params", [])
        if len(homes) > 0:
            self._group_id = homes[0].get("groupId")
            group_name = homes[0].get("groupName")
        elif len(bindCallers) > 0:
            self._group_id = bindCallers[0].get("groupId")
            group_name = bindCallers[0].get("groupName")
        else:
            raise Exception("未找到有效的家庭组")

        if len(bindCallers) > 0:
            deviceAddr = bindCallers[0].get("deviceAddr")
        else:
            deviceAddr = None

        # 保存内部账号密码，用于 token 过期后重新登录
        self._saved_username = username
        self._saved_password = password

        return {
            "username": username,
            "password": password,
            "deviceAddr": deviceAddr,
            "accountId": accountId,
            "accessToken": accessToken,
            "refreshToken": self._refresh_token,
            "groupId": self._group_id,
            "groupName": group_name
        }

    async def get_uuid(self):
        session = async_get_clientsession(self._hass)
        async with session.post(
                f"{self.BASE_URL}/rest/app/community/safe/getUuid",
                verify_ssl=False,
                json={},
        ) as res:
            res.raise_for_status()
            data = await res.json(encoding="utf-8")
            self.uuid = data.get("params", {}).get("uuid")
            return data.get("params", {}).get("uuid")

    def encrypt_params(self, obj: Any, public_key: str) -> 'BaseParam':
        json_string = json.dumps(obj).replace(" ", "")
        secret = self.get_secret(16)
        sha256_hash = hashlib.sha256(json_string.encode())
        encrypted_hash = sha256_hash.hexdigest()

        base_param = BaseParam()
        base_param.data = AesCoder.http_encrypt(json_string, secret)
        base_param.value = RSAEncrypt.rsa_encrypt(secret, public_key)
        base_param.hash = encrypted_hash
        return base_param

    async def login(self, username, password, publicKey):
        params = {
            "accountType": 1,
            "appVersion": "5.1.13",
            "intlPhoneCode": 86,
            "osType": 2,
            "osVersion": "12",
            "password": hashlib.sha256(password.encode('utf-8')).hexdigest(),
            "terminalId": self.appTerminalId,
            "terminalModel": self.appTerminalModel,
            "terminalName": self.appTerminalModel,
            "timestamp": int(time.time() * 1000),
            "uniqueCode": str(uuid.uuid4()),
            "username": username
        }

        params = self.encrypt_params(params, publicKey)
        baseRequest = BaseRequest()
        baseRequest.params = params.to_dict()
        baseRequest.seq = 93

        session = async_get_clientsession(self._hass)
        async with session.post(
                f"{self.BASE_URL}/rest/app/community/user/encryptV1Login",
                verify_ssl=False,
                json=baseRequest.to_dict(),
        ) as res:
            res.raise_for_status()
            data = await res.json(encoding="utf-8")
            return data



  

    async def get_devices(self, did, direct_did, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/smartHome/getDevices"
        params = {
            "did": did,
            "directDid": direct_did
        }
        LogUtils.d("HttpApi", f"getDevices 请求: did={did}, directDid={direct_did}")
        result = await self._make_request(url, params, seq)
        LogUtils.d("HttpApi", f"getDevices 返回: result={result.get('result')}")
        return result

    async def query_logic_service_detail(self, did, direct_did, group_id, siid, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/smartHome/queryLogicServiceDetail"
        params = {
            "did": did,
            "directDid": direct_did,
            "groupId": group_id,
            "siid": siid
        }
        return await self._make_request(url, params, seq)

    async def read_dids_fiids(self, did, direct_did, fiids, siid, is_real_date=0, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/readDidsFIIDS"
        params = [{
            "did": did,
            "directDid": direct_did,
            "fiids": fiids,
            "isRealDate": is_real_date,
            "siid": siid
        }]
        result = await self._make_request(url, params, seq)
        retry_delay = pending_read_delay(result)
        if retry_delay is not None:
            await asyncio.sleep(retry_delay)
            return await self._make_request(url, params, seq)
        return result

    async def encrypt_v1_ctrl_fiids(self, siid, direct_did, fiids, did, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/encryptV1CtrlFIIDS"
        params = {
            "siid": siid,
            "directDid": direct_did,
            "fiids": fiids,
            "did": did
        }
        LogUtils.d("HttpApi", f"encrypt_v1_ctrl_fiids 请求: siid={siid}, directDid={direct_did}, fiids={fiids}, did={did}")
        return await self._make_request(url, params, seq)

    async def batch_get_device_online_info(self, did, direct_did, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/dc/batchGetDeviceOnlineInfo"
        params = [{
            "did": did,
            "directDid": direct_did
        }]
        LogUtils.d("HttpApi", f"batch_get_device_online_info 请求: did={did}, directDid={direct_did}")
        return await self._make_request(url, params, seq)

    async def get_device_permission(self, siid, group_id, direct_did, is_shared=0, did=None, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/mergeHomes/getDevicePermission"
        params = {
            "siid": siid,
            "groupId": group_id,
            "directDid": direct_did,
            "isShared": is_shared
        }
        if did:
            params["did"] = did
        return await self._make_request(url, params, seq)

    async def batch_get_device_fiid_value(self, did, direct_did, fiid_list, profile_id, siid, seq=1):
        url = f"{self.BASE_URL}/rest/app/community/dp/batchGetDeviceFiidValue"
        params = [{
            "did": did,
            "directDid": direct_did,
            "fiidList": fiid_list,
            "profileId": profile_id,
            "siid": siid
        }]
        return await self._make_request(url, params, seq)
