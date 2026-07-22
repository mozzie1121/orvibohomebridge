"""尝试通过 HTTPS 带签名的 API 控制 COCO 插线板"""
import hashlib, json, asyncio, aiohttp, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))
_init = Path(sys.path[0]) / "orvibohomebridge" / "__init__.py"
_orig = _init.read_text()
if "homeassistant" in _orig: _init.write_text("#")
from orvibohomebridge.const import HTTPS_HOST, HTTP_HEADERS
from orvibohomebridge.packet import HomemateJsonData, generate_serial, generate_uuid, get_api_host
from orvibohomebridge.functions import hmac_sha256, generate_timestamp
if _orig: _init.write_text(_orig)

USER = "65261217@qq.com"
PASS = "Sunjian21"
DEV = "834a9801ba2d4b729126648329c3473b"
UID = "accf23852d1c"
FID = "00000000000018111433753460517481"

async def main():
    pw_md5 = hashlib.md5(PASS.encode()).hexdigest().upper()
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"https://{HTTPS_HOST}/getOauthToken?userName={USER}&type=0&password={pw_md5}",
                       headers={**HTTP_HEADERS, "Accept":"*/*"}, ssl=False)
        j = json.loads(await r.text())
        token = j["data"]["access_token"]
        uid = j["data"]["user_id"]
        print(f"✅ 登录成功")

        def sign_params(params: dict) -> dict:
            p = dict(params)
            keys = sorted(p.keys())
            sb = [f"{k}={p[k]}" for k in keys]
            sb.append("key=nQ45RjPtOws96jmH")
            p["sign"] = hmac_sha256("nQ45RjPtOws96jmH", "&".join(sb))
            return p

        # 方式1: /v2/device/write (旧版设备接口)
        print(f"\n📡 方式1: POST /v2/device/write")
        url = f"https://{HTTPS_HOST}/v2/device/write"
        for order, v1 in [("on", 0), ("on", 1)]:
            ts = int(time.time() * 1000)
            rd = generate_uuid()
            params = sign_params({
                "accessToken": token, "deviceId": DEV,
                "order": order, "value1": v1, "value2": 0, "value3": 0, "value4": 0,
                "random": rd, "timestamp": ts, "userId": uid, "userName": USER,
            })
            params.update({
                "cmd": 15, "serial": generate_serial(), "clientType": 1,
                "uniSerial": generate_serial(use_time=True), "ver": "5.1.3.309",
                "serverRecord": False, "debugInfo": "Android_ZhiJia365_34_5.1.3.309",
                "delayTime": 0, "qualityOfService": 1, "defaultResponse": 1, "propertyResponse": 0,
            })
            async with s.post(url, data=json.dumps(params, ensure_ascii=False), headers=HTTP_HEADERS, ssl=False) as r:
                print(f"  {order}(v1={v1}): {json.loads(await r.text())}")

        # 方式2: /v2/cmd/app/readtable (写模式)
        print(f"\n📡 方式2: POST /v2/cmd/app/write")
        url = f"https://{HTTPS_HOST}/v2/cmd/app/write"
        for order, v1 in [("on", 0), ("on", 1)]:
            ts = int(time.time() * 1000)
            rd = generate_uuid()
            params = sign_params({
                "accessToken": token, "deviceId": DEV,
                "order": order, "value1": v1, "value2": 0, "value3": 0, "value4": 0,
                "random": rd, "timestamp": ts, "userId": uid, "userName": USER,
            })
            params["ver"] = "5.1.3.309"
            async with s.post(url, data=json.dumps(params), headers=HTTP_HEADERS, ssl=False) as r:
                print(f"  {order}(v1={v1}): {json.loads(await r.text())}")

        # 方式3: /v2/cmd/app/sendCmd
        print(f"\n📡 方式3: POST /v2/cmd/app/sendCmd")
        url = f"https://{HTTPS_HOST}/v2/cmd/app/sendCmd"
        for order, v1 in [("on", 0), ("on", 1)]:
            ts = int(time.time() * 1000)
            rd = generate_uuid()
            params = sign_params({
                "accessToken": token, "deviceId": DEV,
                "order": order, "value1": v1, "value2": 0, "value3": 0, "value4": 0,
                "random": rd, "timestamp": ts, "userId": uid, "userName": USER,
            })
            async with s.post(url, data=json.dumps(params), headers=HTTP_HEADERS, ssl=False) as r:
                print(f"  {order}(v1={v1}): {json.loads(await r.text())}")

        # 方式4: 查询当前状态确认
        print(f"\n📡 验证状态...")
        ret = HomemateJsonData.get_family_statistics_users(uid, token)
        async with s.post(ret["url"], data=ret["data"], headers=HTTP_HEADERS, ssl=False) as r:
            families = json.loads(await r.text()).get("data", [])
        for f in families:
            fid = f.get("familyId","")
            ret2 = HomemateJsonData.get_devices_status(token, "", uid, USER, fid, 1)
            async with s.post(ret2["url"], data=ret2["data"], headers=HTTP_HEADERS, ssl=False) as r:
                d = json.loads(await r.text()).get("data", {})
                for st in d.get("deviceStatus", []):
                    if st.get("deviceId") == DEV:
                        print(f"  最终状态: value1={st.get('value1')}")
                        break

asyncio.run(main())
