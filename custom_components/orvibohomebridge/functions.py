import uuid
import time
import hmac
import hashlib

def text_utils_is_empty(value):
    return value is None or str(value).strip() == ""

def hmac_sha256(key, message):
    if isinstance(key, str):
        key = key.encode('utf-8')
    if isinstance(message, str):
        message = message.encode('utf-8')
    return hmac.new(key, message, hashlib.sha256).hexdigest().upper()

def generate_timestamp():
    return int(time.time() * 1000)

def generate_serial(use_time=False):
    if use_time:
        return int(time.time() * 1000)
    return int(str(uuid.uuid4().int)[:9])

def generate_uuid():
    return str(uuid.uuid4()).replace('-', '')

def md5_hex(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.md5(data).hexdigest().upper()