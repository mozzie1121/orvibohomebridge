"""快速查看设备状态信息"""
import hashlib, json, asyncio, aiohttp, sys
from pathlib import Path

# 临时绕过 homeassistant 依赖
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))
_init = Path(sys.path[0]) / "orvibohomebridge" / "__init__.py"
_orig = _init.read_text()
if "homeassistant" in _orig: _init.write_text("#")
from orvibohomebridge.packet import HomemateJsonData
from orvibohomebridge.const import HTTPS_HOST, HTTP_HEADERS
if _orig: _init.write_text(_orig)

async def main():
    pw_md5 = hashlib.md5("Sunjian21".encode()).hexdigest().upper()
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"https://{HTTPS_HOST}/getOauthToken?userName=65261217@qq.com&type=0&password={pw_md5}",
                       headers={**HTTP_HEADERS, "Accept":"*/*"}, ssl=False)
        j = json.loads(await r.text())
        token, uid = j["data"]["access_token"], j["data"]["user_id"]
        print(f"✅ 登录成功")

        ret = HomemateJsonData.get_family_statistics_users(uid, token)
        r = await s.post(ret["url"], data=ret["data"], headers=HTTP_HEADERS, ssl=False)
        j = json.loads(await r.text())
        for f in j.get("data", []):
            fid, fn = f.get("familyId",""), f.get("familyName","?")
            print(f"\n🏠 {fn}")
            ret2 = HomemateJsonData.get_devices_status(token, "", uid, "65261217@qq.com", fid, 1)
            r = await s.post(ret2["url"], data=ret2["data"], headers=HTTP_HEADERS, ssl=False)
            d = json.loads(await r.text()).get("data", {})
            devs = d.get("device", [])
            sts = d.get("deviceStatus", [])
            print(f"  设备: {len(devs)}, 状态: {len(sts)}")

            target = "834a9801ba2d4b729126648329c3473b"
            for st in sts:
                if st.get("deviceId") == target:
                    print(f"  🔌 插线板: {json.dumps(st, ensure_ascii=False, indent=2)}")
                    break
            else:
                for st in sts:
                    if "deviceId" in st:
                        print(f"    状态: deviceId={st.get('deviceId','?')[:20]} {json.dumps(st, ensure_ascii=False)}")

            # 试试发控制命令
            print(f"\n  尝试发送控制命令...")
            for order, v1 in [("on", 1), ("off", 0), ("set property", 0)]:
                ctrl = {
                    "uid": "accf23852d1c",
                    "userName": "65261217@qq.com",
                    "deviceId": target,
                    "order": order, "value1": v1, "value2": 0, "value3": 0, "value4": 0,
                    "delayTime": 0, "qualityOfService": 1, "defaultResponse": 1, "propertyResponse": 0,
                }
                # 构造带签名的控制请求
                from orvibohomebridge.packet import generate_serial, generate_uuid
                ctrl.update({
                    "cmd": 15, "serial": generate_serial(), "clientType": 1,
                    "uniSerial": generate_serial(use_time=True),
                    "ver": "5.1.3.309", "serverRecord": False,
                    "debugInfo": "Android_ZhiJia365_34_5.1.3.309",
                })
                import time
                ts = int(time.time() * 1000)
                params = {
                    "accessToken": token, "deviceId": target, "order": order,
                    "random": generate_uuid(), "serial": ctrl["serial"],
                    "timestamp": ts, "userId": uid, "userName": "65261217@qq.com",
                }
                from orvibohomebridge.functions import hmac_sha256
                keys = sorted(params.keys())
                sb = []
                for k in keys: sb.append(f"{k}={params[k]}")
                sb.append("key=nQ45RjPtOws96jmH")
                ctrl["sign"] = hmac_sha256("nQ45RjPtOws96jmH", "&".join(sb))
                ctrl["timestamp"] = ts
                ctrl["random"] = params["random"]

                url = f"https://{HTTPS_HOST}/v2/cmd/app/sendCommand"
                payload = json.dumps(ctrl, ensure_ascii=False)
                r = await s.post(url, data=payload, headers=HTTP_HEADERS, ssl=False)
                resp = json.loads(await r.text())
                print(f"    {order}(v1={v1}): {json.dumps(resp, ensure_ascii=False)}")

asyncio.run(main())
