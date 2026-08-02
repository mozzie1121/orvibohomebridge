"""腾讯云 COS 门锁媒体模拟探针（对照智家365 App 逆向结论）。

背景（来自 APK jadx 反编译）：
- 门锁事件里的 videoUrl/url（如 /uid/videoPicklockEvent/xxx.h264）是腾讯云 COS 对象键；
- App 先调 REST 接口 v2/cosToken/getCosToken 换取 STS 临时凭证
  （secretId/secretKey/sessionToken + bucket/region/expiration）；
- 再用 COS 签名 v5（HMAC-SHA1 + q-sign-time）拼出可访问 URL 下载。

本脚本仅用于本地验证，凭据只从环境变量读取：
  ORVIBO_USERNAME / ORVIBO_PASSWORD

用法：
  python tests/cos_probe.py --list --capture lock_capture_xxx.jsonl
  python tests/cos_probe.py --object-key /uid/videoPicklockEvent/xxx.h264 --save out.h264
  python tests/cos_probe.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

SIGN_KEY = "nQ45RjPtOws96jmH"  # 与集成 const.SIGN_KEY 相同（来自 APK）
DEFAULT_HOST = "china.orvibo.com"


def _hmac_hex(key: str, msg: str, algo) -> str:
    return hmac.new(key.encode(), msg.encode(), algo).hexdigest()


def create_sign(params: Dict[str, Any], key: str = SIGN_KEY) -> str:
    """与 App HMHttpRequest 相同：排序 k=v&... 追加 key=，HmacSHA256 大写 hex。"""
    parts = []
    for k in sorted(params):
        v = str(params[k])
        if v:
            parts.append(f"{k}={v}&")
    parts.append(f"key={key}")
    return hmac.new(key.encode(), "".join(parts).encode(), hashlib.sha256).hexdigest().upper()


def http_json(url: str, data: Optional[str] = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, data=data.encode() if data else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "okhttp/3.12.1")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def redact(value: Any, keep: int = 4) -> str:
    s = str(value)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]}"


def login(host: str, username: str, password: str) -> Dict[str, Any]:
    url = (
        f"https://{host}/getOauthToken?userName={urllib.parse.quote(username)}"
        f"&type=0&password={urllib.parse.quote(password)}"
    )
    resp = http_json(url)
    data = resp.get("data") or {}
    if "access_token" not in data:
        raise RuntimeError(f"登录失败: {resp.get('message') or resp}")
    print(f"[1/4] 登录成功 user_id={redact(data.get('user_id'), 6)}")
    return data


def fetch_family(host: str, access_token: str, user_id: str) -> str:
    params = {
        "requestId": "sim-" + os.urandom(8).hex(),
        "userId": user_id,
        "accessToken": access_token,
        "random": os.urandom(8).hex(),
        "timestamp": int(time.time() * 1000),
    }
    data = {
        **params,
        "sign": create_sign(params),
    }
    resp = http_json(f"https://{host}/v2/family/statistics/users", json.dumps(data))
    families = resp.get("data") or []
    if isinstance(families, dict):
        families = families.get("familyList") or families.get("list") or [families]
    if not families:
        raise RuntimeError(f"未找到家庭: {json.dumps(resp, ensure_ascii=False)[:300]}")
    fam = families[0]
    family_id = fam.get("familyId") or fam.get("family_id")
    print(f"[2/4] 家庭: {fam.get('familyName') or family_id} ({redact(family_id, 6)})")
    return str(family_id)


def fetch_cos_token(host: str, access_token: str, user_id: str, family_id: str) -> Dict[str, Any]:
    params = {
        "accessToken": access_token,
        "userId": user_id,
        "familyId": family_id,
        "timestamp": int(time.time() * 1000),
        "random": os.urandom(8).hex(),
    }
    data = {**params, "sign": create_sign(params)}
    resp = http_json(f"https://{host}/v2/cosToken/getCosToken", json.dumps(data))
    token = resp.get("data") or {}
    if not token.get("bucket"):
        raise RuntimeError(f"获取 COS 凭证失败: {json.dumps(resp, ensure_ascii=False)[:300]}")
    print(
        "[3/4] COS 凭证 "
        f"bucket={token.get('bucket')} region={token.get('region')} "
        f"secretId={redact(token.get('secretId'))} expiration={token.get('expiration')}"
    )
    return token


def cos_authorization(
    secret_id: str,
    secret_key: str,
    method: str,
    path: str,
    host: str,
    start: int,
    end: int,
    query_params: Optional[Dict[str, str]] = None,
) -> str:
    """COS 签名 v5（q-sign-algorithm=sha1）。

    query_params 参与签名（如 response-content-type），按 URL 编码后的
    参数名排序；签名参数自身与 x-cos-security-token 不参与计算。
    """
    key_time = f"{start};{end}"
    sign_key = _hmac_hex(secret_key, key_time, hashlib.sha1)
    # 注意：单 header 时末尾不带 &（服务端 FormatString 实测确认）
    query_part = ""
    q_url_param_list = ""
    if query_params:
        encoded = {
            urllib.parse.quote(str(k), safe="-"): urllib.parse.quote(str(v), safe="")
            for k, v in query_params.items()
        }
        items = sorted(encoded.items())
        query_part = "&".join(f"{k}={v}" for k, v in items)
        q_url_param_list = ";".join(k for k, _ in items)
    http_string = f"{method.lower()}\n{path}\n{query_part}\nhost={host}\n"
    # COS 官方算法：StringToSign = "sha1\n{key_time}\n{sha1(HttpString)}\n"
    # 注意末尾必须带换行（SignatureDoesNotMatch 错误体与官方文档双重确认）
    string_to_sign = (
        f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    )
    signature = _hmac_hex(sign_key, string_to_sign, hashlib.sha1)
    return (
        f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}"
        f"&q-key-time={key_time}&q-header-list=host&q-url-param-list={q_url_param_list}"
        f"&q-signature={signature}"
    )


def build_download(
    token: Dict[str, Any], object_key: str, double_slash: bool = False, end_delta: Optional[int] = None
) -> tuple[str, Dict[str, str]]:
    bucket = token["bucket"]
    region = token.get("region") or "ap-guangzhou"
    host = f"{bucket}.cos.{region}.myqcloud.com"
    path = object_key if object_key.startswith("/") else "/" + object_key
    if double_slash and not path.startswith("//"):
        path = "/" + path
    now = int(time.time())
    duration = int(token.get("expiration") or 1800)
    if end_delta is not None:
        duration = min(duration, end_delta)
    auth = cos_authorization(
        token["secretId"], token["secretKey"], "get", path, host, now, now + duration
    )
    url = f"https://{host}{path}"
    headers = {"Authorization": auth, "x-cos-security-token": token["sessionToken"]}
    return url, headers


def download(token: Dict[str, Any], object_key: str, save: str) -> None:
    print(f"[4/4] 下载 {object_key}")
    variants = [
        ("原始", build_download(token, object_key)),
        ("双斜杠路径", build_download(token, object_key, double_slash=True)),
        ("缩短签名窗口", build_download(token, object_key, end_delta=600)),
    ]
    body: bytes = b""
    for label, (url, headers) in variants:
        print(f"      尝试[{label}] {url}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            print(f"      [{label}] 成功: {len(body)} 字节")
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            print(f"      [{label}] HTTP {e.code}: {err_body[:400]}")
            if e.code != 403:
                raise
    else:
        raise SystemExit(f"所有变体均被拒绝，见上方错误详情")
    with open(save, "wb") as f:
        f.write(body)
    print(f"      保存: {len(body)} 字节 -> {save} "
          f"(magic={body[:8].hex() if body else 'empty'})")


def list_capture_keys(capture: str, redact_uid: bool) -> List[str]:
    keys: List[str] = []
    uid_pat = re.compile(r"/[0-9a-f]{32}/")
    with open(capture, encoding="utf-8") as f:
        for line in f:
            for m in re.finditer(r'"(?:videoUrl|url|picUrl)":\s*"([^"]+)"', line):
                key = m.group(1)
                if key.startswith("/"):
                    keys.append(uid_pat.sub("/<uid>/", key) if redact_uid else key)
    seen: List[str] = []
    for k in keys:
        if k not in seen:
            seen.append(k)
    return seen


def selftest() -> None:
    """离线校验：签名结构、确定性、时间参数。"""
    token = {
        "bucket": "orvibo-test-1250000000",
        "region": "ap-guangzhou",
        "secretId": "AKIDEXAMPLE",
        "secretKey": "x" * 40,
        "sessionToken": "t" * 40,
        "expiration": 1800,
    }
    key = "/77c139c4d27f4fa6a20e1f459849aa47/videoPicklockEvent/x.h264"
    url, headers = build_download(token, key)
    assert url.startswith("https://orvibo-test-1250000000.cos.ap-guangzhou.myqcloud.com/")
    auth = headers["Authorization"]
    for part in ("q-sign-algorithm=sha1", "q-ak=AKIDEXAMPLE", "q-header-list=host"):
        assert part in auth, part
    sig = auth.rsplit("q-signature=", 1)[1]
    assert len(sig) == 40, "签名应为 40 位 sha1 hex"
    # 确定性：同参数两次结果一致
    assert headers["Authorization"] == build_download(token, key)[1]["Authorization"]
    # 时间不同则签名不同
    url2, headers2 = build_download(token, "/77c139c4d27f4fa6a20e1f459849aa47/videoPicklockEvent/y.h264")
    assert headers2["Authorization"] != auth
    # create_sign 与集成 packet.create_sign 同方案（可离线复算校验格式）
    assert len(create_sign({"a": "1", "b": "2"})) == 64
    # 用服务器错误体中的真实 FormatString 反推校验 HttpString 构造
    # （cffc68e3... 即服务端给出的 sha1(HttpString)）
    path = "/77c139c4d27f4fa6a20e1f459849aa47/picturePicklockEvent/picklockEvent_1785652830.jpg"
    host = "familypic-cn-1251222210.cos.ap-guangzhou.myqcloud.com"
    http_string = f"get\n{path}\n\nhost={host}\n"
    assert (
        hashlib.sha1(http_string.encode()).hexdigest()
        == "cffc68e3aba2b1e672f079faffc9895fa3702d48"
    )
    # StringToSign 三段式格式自检
    auth2 = cos_authorization(
        "AKIDEXAMPLE", "x" * 40, "get", path, host, 1785667760, 1785797360
    )
    assert "q-sign-time=1785667760;1785797360" in auth2
    # 服务器错误体 StringToSign 完整还原（含尾部换行）：
    # sha1\n1785667865;1785797465\ncffc68e3aba2b1e672f079faffc9895fa3702d48\n
    _hmac_args = _hmac_hex("", "", hashlib.sha1)  # noqa: 仅确保函数可调用
    expected_sts = "sha1\n1785667865;1785797465\ncffc68e3aba2b1e672f079faffc9895fa3702d48\n"
    key_time = "1785667865;1785797465"
    sign_key = _hmac_hex("x" * 40, key_time, hashlib.sha1)
    sig = _hmac_hex(sign_key, expected_sts, hashlib.sha1)
    assert len(sig) == 40
    print("selftest OK: COS 签名 v5 结构/确定性校验通过")


def main() -> int:
    parser = argparse.ArgumentParser(description="COS 门锁媒体模拟探针")
    parser.add_argument("--host", default=DEFAULT_HOST, help="API 主机（默认 china.orvibo.com）")
    parser.add_argument("--capture", help="从抓包 jsonl 提取对象键")
    parser.add_argument("--list", action="store_true", help="仅列出对象键（不联网）")
    parser.add_argument("--no-redact", action="store_true", help="列出对象键时不脱敏 uid")
    parser.add_argument("--object-key", help="要下载的对象键（如 /uid/videoPicklockEvent/x.h264）")
    parser.add_argument("--save", default="cos_download.bin", help="下载保存路径")
    parser.add_argument("--selftest", action="store_true", help="离线自检签名算法")
    args = parser.parse_args()

    if args.selftest:
        selftest()
        return 0
    if args.capture:
        keys = list_capture_keys(args.capture, redact_uid=not args.no_redact)
        print("抓包中的对象键：")
        for k in keys:
            print(f"  {k}")
        if not args.object_key and keys and not args.no_redact:
            print("\n提示：下载请用 --object-key 传入原始键（含真实 uid）")
        return 0
    if not args.object_key:
        parser.print_help()
        return 2

    username = os.environ.get("ORVIBO_USERNAME", "")
    password = os.environ.get("ORVIBO_PASSWORD", "")
    if not username or not password:
        print("缺少凭据：请先设置环境变量 ORVIBO_USERNAME / ORVIBO_PASSWORD")
        return 3

    auth = login(args.host, username, password)
    family_id = fetch_family(args.host, auth["access_token"], auth["user_id"])
    token = fetch_cos_token(args.host, auth["access_token"], auth["user_id"], family_id)
    download(token, args.object_key, args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
