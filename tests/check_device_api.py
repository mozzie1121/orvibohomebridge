"""查看设备的完整描述信息和可能控制方式"""
import hashlib, json, asyncio, aiohttp, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))
_init = Path(sys.path[0]) / "orvibohomebridge" / "__init__.py"
_orig = _init.read_text()
if "homeassistant" in _orig: _init.write_text("#")
from orvibohomebridge.const import HTTPS_HOST, HTTP_HEADERS
from orvibohomebridge.packet import HomemateJsonData, generate_serial, generate_uuid
from orvibohomebridge.functions import hmac_sha256
if _orig: _init.write_text(_orig)

async def main():
    pw_md5 = hashlib.md5("Sunjian21".encode()).hexdigest().upper()
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"https://{HTTPS_HOST}/getOauthToken?userName=65261217@qq.com&type=0&password={pw_md5}",
                       headers={**HTTP_HEADERS, "Accept":"*/*"}, ssl=False)
        j = json.loads(await r.text())
        token, uid = j["data"]["access_token"], j["data"]["user_id"]
        print(f"✅ 登录成功")

        # 1. 获取设备的完整描述
        print(f"\n📋 getDeviceDesc...")
        url = f"https://{HTTPS_HOST}/getDeviceDesc?source=ZhiJia365&lastUpdateTime=0&accessToken={token}"
        async with s.get(url, headers=HTTP_HEADERS, ssl=False) as r:
            text = await r.text()
            desc = json.loads(text)
            print(json.dumps(desc, ensure_ascii=False)[:3000])

        # 2. 查看插线板的设备配置信息
        print(f"\n📋 queryDeviceById...")
        tid = int(time.time() * 1000)
        url = f"https://{HTTPS_HOST}/v2/device/queryDeviceById"
        data = json.dumps({"accessToken": token, "deviceId": "834a9801ba2d4b729126648329c3473b", "timestamp": tid, "random": generate_uuid()})
        async with s.post(url, data=data, headers=HTTP_HEADERS, ssl=False) as r:
            text = await r.text()
            print(json.dumps(json.loads(text), ensure_ascii=False)[:2000])

        # 3. 试试通过 gateway 接口控制
        print(f"\n📋 尝试 v2/device/send...")
        url = f"https://{HTTPS_HOST}/v2/device/send"
        # COCO插线板可能用的是旧版协议 - order=on/off 走 value1=1/0
        for order, v1 in [("on", 1), ("off", 0)]:
            ts = int(time.time() * 1000)
            rd = generate_uuid()
            params = {
                "accessToken": token, "deviceId": "834a9801ba2d4b729126648329c3473b",
                "order": order, "value1": v1, "value2": 0, "value3": 0, "value4": 0,
                "random": rd, "timestamp": ts, "userId": uid, "userName": "65261217@qq.com",
            }
            keys = sorted(params.keys())
            sb = [f"{k}={params[k]}" for k in keys]
            sb.append("key=nQ45RjPtOws96jmH")
            params["sign"] = hmac_sha256("nQ45RjPtOws96jmH", "&".join(sb))
            params["cmd"] = 15
            params["serial"] = generate_serial()
            params["uniSerial"] = generate_serial(use_time=True)
            params["ver"] = "5.1.3.309"
            params["clientType"] = 1
            params["serverRecord"] = False
            params["debugInfo"] = "Android_ZhiJia365_34_5.1.3.309"
            payload = json.dumps(params, ensure_ascii=False)
            async with s.post(url, data=payload, headers=HTTP_HEADERS, ssl=False) as r:
                resp = json.loads(await r.text())
                print(f"  {order}(v1={v1}): {json.dumps(resp, ensure_ascii=False)}")

        # 4. 试试旧版 API /v2/cmd/app/write
        print(f"\n📋 尝试 v2/cmd/app/write...")
        url = f"https://{HTTPS_HOST}/v2/cmd/app/write"
        for order, v1 in [("on", 1), ("off", 0)]:
            ts = int(time.time() * 1000)
            rd = generate_uuid()
            params = {
                "accessToken": token, "deviceId": "834a9801ba2d4b729126648329c3473b",
                "order": order, "value1": v1, "value2": 0, "value3": 0, "value4": 0,
                "random": rd, "timestamp": ts, "userId": uid, "userName": "65261217@qq.com",
            }
            keys = sorted(params.keys())
            sb = [f"{k}={params[k]}" for k in keys]
            sb.append("key=nQ45RjPtOws96jmH")
            params["sign"] = hmac_sha256("nQ45RjPtOws96jmH", "&".join(sb))
            params["ver"] = "5.1.3.309"
            payload = json.dumps(params, ensure_ascii=False)
            async with s.post(url, data=payload, headers=HTTP_HEADERS, ssl=False) as r:
                resp = json.loads(await r.text())
                print(f"  {order}(v1={v1}): {json.dumps(resp, ensure_ascii=False)}")

asyncio.run(main())
