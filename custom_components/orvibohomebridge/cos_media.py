"""门锁媒体 COS 凭证管理：cmd=313 换凭证 → 对象键签成临时 URL。

背景（APK 逆向 + 实机验证）：
- 门锁事件（撬锁告警/门铃）里的 videoUrl/picUrl 是腾讯云 COS 对象键
  （如 /uid/picturePicklockEvent/xxx.jpg）；
- REST 接口 v2/cosToken/getCosToken 只签发日志桶凭证，读门锁媒体会
  AccessDenied（实测确认）；正确路径是 SSL 长连接发 cmd=313
  （Skill.GetCOSAuthorization），返回门锁专用桶（如
  p20-doorlock-1251222210）的 STS 凭证；
- 拿到凭证后用 COS 签名 v5 把对象键签成临时 URL（Authorization 参数
  直接作为 query 附加，即官方预签名 URL 格式）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)

# 凭证到期前提前刷新的余量（秒）
_RENEW_LEAD_TIME = 300


def _hmac_hex(key: str, msg: str, algo) -> str:
    return hmac.new(key.encode(), msg.encode(), algo).hexdigest()


def cos_authorization(
    secret_id: str,
    secret_key: str,
    method: str,
    path: str,
    host: str,
    start: int,
    end: int,
    query_params: Optional[Mapping[str, str]] = None,
) -> str:
    """COS 签名 v5（与 tests/cos_probe.py 同算法，实机验证通过）。

    query_params 参与签名（如 response-content-type），按 URL 编码后的
    参数名排序；签名参数自身与 x-cos-security-token 不参与计算。
    """
    key_time = f"{start};{end}"
    sign_key = _hmac_hex(secret_key, key_time, hashlib.sha1)
    query_part = ""
    q_url_param_list = ""
    if query_params:
        encoded = {
            quote(str(k), safe="-"): quote(str(v), safe="")
            for k, v in query_params.items()
        }
        items = sorted(encoded.items())
        query_part = "&".join(f"{k}={v}" for k, v in items)
        q_url_param_list = ";".join(k for k, _ in items)
    http_string = f"{method.lower()}\n{path}\n{query_part}\nhost={host}\n"
    string_to_sign = (
        f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    )
    signature = _hmac_hex(sign_key, string_to_sign, hashlib.sha1)
    return (
        f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}"
        f"&q-key-time={key_time}&q-header-list=host&q-url-param-list={q_url_param_list}"
        f"&q-signature={signature}"
    )


@dataclasses.dataclass(slots=True)
class CosCredentials:
    """门锁 COS STS 凭证（已归一化）。"""

    secret_id: str
    secret_key: str
    session_token: str
    bucket: str
    region: str
    expires_at: float  # 本地时间戳，秒
    system_time_ms: Optional[int] = None

    @property
    def valid(self) -> bool:
        return time.time() < self.expires_at - _RENEW_LEAD_TIME


_CONTENT_TYPE_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".h264": "video/h264",
    ".mp4": "video/mp4",
}


def _infer_content_type(object_key: str) -> Optional[str]:
    """按对象键扩展名推断 Content-Type（用于 response-content-type 覆盖）。"""
    dot = object_key.rfind(".")
    if dot < 0:
        return None
    return _CONTENT_TYPE_BY_EXT.get(object_key[dot:].lower())


def parse_cos_response(resp: Mapping[str, Any]) -> Optional[CosCredentials]:
    """解析 cmd=313 响应，返回归一化凭证；格式不符返回 None。"""
    if not isinstance(resp, Mapping):
        return None
    response_json = resp.get("response")
    try:
        if isinstance(response_json, str):
            q = json.loads(response_json)
        elif isinstance(response_json, Mapping):
            q = response_json
        else:
            return None
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(q, Mapping):
        return None
    if q.get("namespace") != "Skill.GetCOSAuthorization":
        return None
    status = q.get("status")
    if status not in (None, 0, "0"):
        return None
    security = q.get("security") or {}
    if not isinstance(security, Mapping):
        return None
    secret_id = security.get("accessKeyId") or ""
    secret_key = security.get("accessKeySecret") or ""
    session_token = security.get("securityToken") or ""
    bucket = security.get("bucketName") or ""
    if not secret_id or not secret_key or not session_token or not bucket:
        return None
    region = security.get("region") or "ap-guangzhou"
    expiration = int(security.get("expiration") or 1800)
    system_time_ms = security.get("systemCurrentTime")
    base = int(system_time_ms / 1000) if system_time_ms else int(time.time())
    return CosCredentials(
        secret_id=secret_id,
        secret_key=secret_key,
        session_token=session_token,
        bucket=bucket,
        region=region,
        expires_at=base + expiration,
        system_time_ms=int(system_time_ms) if system_time_ms else None,
    )


def signed_media_url(
    creds: CosCredentials,
    object_key: str,
    ttl: int = 600,
    content_type: Optional[str] = None,
) -> str:
    """把 COS 对象键签成临时可访问 URL（预签名 URL 格式）。

    STS 临时凭证必须把 x-cos-security-token 放进 URL query（浏览器无法带
    自定义 header），否则直接访问会 AccessDenied。该参数不参与签名计算。
    COS 存储的 Content-Type 可能是 text/html（实测），导致浏览器把图片
    当文本渲染；通过 response-content-type 参数覆盖响应头（参与签名）。
    """
    host = f"{creds.bucket}.cos.{creds.region}.myqcloud.com"
    path = object_key if object_key.startswith("/") else "/" + object_key
    now = max(int(time.time()), int(creds.system_time_ms / 1000) if creds.system_time_ms else 0)
    duration = min(ttl, max(1, int(creds.expires_at - now - _RENEW_LEAD_TIME)))
    content_type = content_type or _infer_content_type(object_key)
    query_params: dict[str, str] = {}
    if content_type:
        query_params["response-content-type"] = content_type
    auth = cos_authorization(
        creds.secret_id,
        creds.secret_key,
        "get",
        path,
        host,
        now,
        now + duration,
        query_params=query_params or None,
    )
    query_prefix = ""
    if query_params:
        encoded = {
            quote(str(k), safe="-"): quote(str(v), safe="")
            for k, v in query_params.items()
        }
        query_prefix = "&".join(f"{k}={v}" for k, v in sorted(encoded.items())) + "&"
    token_part = f"&x-cos-security-token={quote(creds.session_token, safe='')}"
    return f"https://{host}{path}?{query_prefix}{auth}{token_part}"


class CosMediaManager:
    """按门锁设备缓存 COS 凭证，并把对象键签成临时 URL。

    凭证通过 SSL 长连接 cmd=313 获取（36 小时有效），按 device_id 缓存，
    到期前自动续期。同一把锁的多个事件共享一份凭证。
    """

    def __init__(self, ssl_client, user_id: str, family_id: str) -> None:
        self._ssl = ssl_client
        self._user_id = user_id
        self._family_id = family_id
        self._cache: dict[str, CosCredentials] = {}

    def clear(self) -> None:
        self._cache.clear()

    def cached_credentials(self, device_id: str) -> Optional[CosCredentials]:
        """返回仍有效的缓存凭证（不联网）；缺失或过期返回 None。"""
        cached = self._cache.get(device_id)
        if cached is not None and cached.valid:
            return cached
        return None

    def try_signed_url(
        self,
        device_id: str,
        device_uid: str,
        object_key: str,
        ttl: int = 600,
    ) -> Optional[str]:
        """用缓存凭证同步签名（事件发布路径，零网络等待）。"""
        if not object_key:
            return None
        cached = self.cached_credentials(device_id)
        if cached is None:
            return None
        try:
            return signed_media_url(cached, object_key, ttl=ttl)
        except Exception:  # noqa: BLE001 - 签名失败不应影响事件发布
            _LOGGER.exception("COS 对象键签名失败: %s", object_key)
            return None

    async def get_credentials(
        self,
        device_id: str,
        device_uid: str,
    ) -> Optional[CosCredentials]:
        """返回有效凭证；缓存过期或缺失时通过 cmd=313 重新获取。"""
        cached = self._cache.get(device_id)
        if cached is not None and cached.valid:
            return cached
        if self._ssl is None:
            return None
        resp = await self._ssl.send_cos_auth(
            user_id=self._user_id,
            device_id=device_id,
            device_uid=device_uid,
        )
        creds = parse_cos_response(resp) if resp else None
        if creds is None:
            _LOGGER.warning(
                "门锁媒体凭证获取失败 device=%s（响应格式不符或超时）", device_id
            )
            return None
        self._cache[device_id] = creds
        _LOGGER.debug(
            "门锁媒体凭证已刷新 device=%s bucket=%s region=%s 有效%.0fh",
            device_id,
            creds.bucket,
            creds.region,
            (creds.expires_at - time.time()) / 3600,
        )
        return creds

    async def signed_url(
        self,
        device_id: str,
        device_uid: str,
        object_key: str,
        ttl: int = 600,
    ) -> Optional[str]:
        """把对象键签成临时 URL；凭证不可用时返回 None。"""
        if not object_key:
            return None
        creds = await self.get_credentials(device_id, device_uid)
        if creds is None:
            return None
        try:
            return signed_media_url(creds, object_key, ttl=ttl)
        except Exception:  # noqa: BLE001 - 签名失败不应影响事件发布
            _LOGGER.exception("COS 对象键签名失败: %s", object_key)
            return None
