"""查网关和设备的绑定关系"""
import hashlib, json, asyncio, aiohttp, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))
_init = Path(sys.path[0]) / "orvibohomebridge" / "__init__.py"
_orig = _init.read_text()
if "homeassistant" in _orig: _init.write_text("#")
from orvibohomebridge.const import HTTPS_HOST, HTTP_HEADERS
from orvibohomebridge.packet import HomemateJsonData
from orvibohomebridge.functions import hmac_sha256, generate_uuid
if _orig: _init.write_text(_orig)

USER = "65261217@qq.com"
PASS = "Sunjian21"
DEV = "834a9801ba2d4b729126648329c3473b"

async def main():
    pw_md5 = hashlib.md5(PASS.encode()).hexdigest().upper()
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"https://{HTTPS_HOST}/getOauthToken?userName={USER}&type=0&password={pw_md5}",
                       headers={**HTTP_HEADERS, "Accept":"*/*"}, ssl=False)
        j = json.loads(await r.text())
        token = j["data"]["access_token"]
        uid = j["data"]["user_id"]
        print(f"✅ 登录成功")

        # 查询所有家庭的信息（检查是否有网关绑定）
        ret = HomemateJsonData.get_family_statistics_users(uid, token)
        async with s.post(ret["url"], data=ret["data"], headers=HTTP_HEADERS, ssl=False) as r:
            families = json.loads(await r.text()).get("data", [])

        for f in families:
            fid = f.get("familyId","")
            fname = f.get("familyName","?")
            print(f"\n🏠 {fname}")
            
            ret2 = HomemateJsonData.get_devices_status(token, "", uid, USER, fid, 1)
            async with s.post(ret2["url"], data=ret2["data"], headers=HTTP_HEADERS, ssl=False) as r:
                data = json.loads(await r.text()).get("data", {})
                
                # 网关
                gws = data.get("gateway", [])
                print(f"  网关: {len(gws)}")
                for g in gws:
                    print(f"    {json.dumps(g, ensure_ascii=False)[:300]}")
                
                # 用户网关绑定
                binds = data.get("userGatewayBind", [])
                print(f"  用户-网关绑定: {len(binds)}")
                for b in binds:
                    print(f"    {json.dumps(b, ensure_ascii=False)[:200]}")
                
                # 设备
                devs = data.get("device", [])
                for d in devs:
                    if d.get("deviceId") == DEV:
                        print(f"\n  🔌 插线板详细信息:")
                        print(f"    {json.dumps(d, ensure_ascii=False)}")
                        # 查它的 endpoint/profileID/zoneId
                        print(f"    endpoint={d.get('endpoint')}, profileID={d.get('profileID')}, zoneId={d.get('zoneId')}")
                        print(f"    appDeviceId={d.get('appDeviceId')}, gdid={d.get('gdid')}")
                
                # 设备状态
                sts = data.get("deviceStatus", [])
                for st in sts:
                    if st.get("deviceId") == DEV:
                        print(f"\n  🔌 插线板状态:")
                        print(f"    {json.dumps(st, ensure_ascii=False)}")
                
                # 看看有没有其他家庭有插线板的endpoint配置
                ret3 = HomemateJsonData.get_devices_status(token, "", uid, USER, fid, 0)
                async with s.post(ret3["url"], data=ret3["data"], headers=HTTP_HEADERS, ssl=False) as r:
                    data2 = json.loads(await r.text()).get("data", {})
                    devs2 = data2.get("device", [])
                    for d in devs2:
                        if d.get("deviceId") == DEV:
                            print(f"\n  🔌 插线板 (deviceFlag=0):")
                            print(f"    {json.dumps(d, ensure_ascii=False)[:500]}")

asyncio.run(main())
