"""剪映（jianying）免费转录接口。

移植自 VideoCaptioner 的 JianYingASR。⚠️ 尽力而为：
依赖第三方签名服务 https://asrtools-update.bkfeng.top/sign，该服务不在我们
控制内，可能失效/限流。失败时会抛出可读的错误信息。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import time
import uuid
import zlib

import requests

from .base import BaseEngine
from subtitle import Segment, Transcript

HOST = "https://lv-pc-api-sinfonlinec.ulikecam.com"
SIGN_URL = "https://asrtools-update.bkfeng.top/sign"


class JianYingEngine(BaseEngine):
    name = "jianying"

    def __init__(self):
        self.session = requests.Session()
        self.tdid = self._make_tdid()
        self.access_key = None
        self.secret_key = None
        self.session_token = None
        self.store_uri = None
        self.auth = None
        self.upload_id = None
        self.upload_hosts = None

    # ---- 工具 ----
    def _make_tdid(self) -> str:
        year_digit = str(datetime.datetime.now().year)[3]
        fr = 390 + int(year_digit)
        ed = "3278516897751" if int(year_digit) % 2 != 0 else f"{uuid.getnode():013d}"
        return f"{fr}{ed}"

    def _sign(self, url_path: str) -> tuple[str, str]:
        current_time = str(int(time.time()))
        data = {
            "url": url_path,
            "current_time": current_time,
            "pf": "4",
            "appvr": "6.6.0",
            "tdid": self.tdid,
        }
        try:
            r = self.session.post(
                SIGN_URL,
                json=data,
                headers={"User-Agent": "VideoCaptioner/1.0", "tdid": self.tdid, "t": current_time},
                timeout=30,
            )
            r.raise_for_status()
            sign = r.json().get("sign")
            if not sign:
                raise ValueError("签名服务未返回 sign")
            return sign.lower(), current_time
        except Exception as e:
            raise RuntimeError(
                f"剪映签名服务不可用（{SIGN_URL}），剪映接口暂不可用: {e}"
            )

    def _build_headers(self, device_time: str, sign: str) -> dict:
        return {
            "User-Agent": "Cronet/TTNetVersion:d4572e53 2024-06-12 QuicVersion:4bf243e0 2023-04-17",
            "appvr": "6.6.0",
            "device-time": device_time,
            "pf": "4",
            "sign": sign,
            "sign-ver": "1",
            "tdid": self.tdid,
        }

    # ---- AWS 签名 ----
    @staticmethod
    def _sign_hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _aws_signature(self, secret_key, request_params, headers, region="cn", service="vod"):
        def sign_key(k, m):
            return self._sign_hmac(k, m)

        k_date = sign_key(("AWS4" + secret_key).encode("utf-8"), headers["x-amz-date"].split("T")[0])
        k_region = sign_key(k_date, region)
        k_service = sign_key(k_region, service)
        k_signing = sign_key(k_service, "aws4_request")

        canonical_uri = "/"
        canonical_headers = "\n".join(f"{k}:{v}" for k, v in headers.items()) + "\n"
        signed_headers = ";".join(headers.keys())
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_request = (
            f"GET\n{canonical_uri}\n{request_params}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        amzdate = headers["x-amz-date"]
        datestamp = amzdate.split("T")[0]
        scope = f"{datestamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        return hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    # ---- 上传流程 ----
    def _upload_sign(self, audio_path: str):
        sign, device_time = self._sign("/lv/v1/upload_sign")
        headers = self._build_headers(device_time, sign)
        r = self.session.post(
            HOST + "/lv/v1/upload_sign",
            data=json.dumps({"biz": "pc-recognition"}),
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()["data"]
        self.access_key = data["access_key_id"]
        self.secret_key = data["secret_access_key"]
        self.session_token = data["session_token"]

    def _upload_auth(self, audio_path: str):
        file_size = os.path.getsize(audio_path)
        params = (
            f"Action=ApplyUploadInner&FileSize={file_size}&FileType=object&IsInner=1"
            f"&SpaceName=lv-mac-recognition&Version=2020-11-19&s=5y0udbjapi"
        )
        t = datetime.datetime.now(datetime.timezone.utc)
        amz_date = t.strftime("%Y%m%dT%H%M%SZ")
        datestamp = t.strftime("%Y%m%d")
        headers = {"x-amz-date": amz_date, "x-amz-security-token": self.session_token}
        sig = self._aws_signature(self.secret_key, params, headers)
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{datestamp}/cn/vod/aws4_request, "
            f"SignedHeaders=x-amz-date;x-amz-security-token, Signature={sig}"
        )
        headers["authorization"] = authorization
        r = self.session.get(f"https://vod.bytedanceapi.com/?{params}", headers=headers, timeout=30)
        r.raise_for_status()
        address = r.json()["Result"]["UploadAddress"]
        store = address["StoreInfos"][0]
        self.store_uri = store["StoreUri"]
        self.auth = store["Auth"]
        self.upload_id = store["UploadID"]
        # UploadHosts 在 UploadAddress 层级，而不在 StoreInfos 里
        self.upload_hosts = address["UploadHosts"][0]

    def _upload_file(self, audio_path: str):
        with open(audio_path, "rb") as f:
            content = f.read()
        crc32 = f"{zlib.crc32(content) & 0xFFFFFFFF:08x}"
        url = f"https://{self.upload_hosts}/{self.store_uri}?partNumber=1&uploadID={self.upload_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36 Thea/1.0.1",
            "Authorization": self.auth,
            "Content-CRC32": crc32,
        }
        r = self.session.put(url, data=content, headers=headers, timeout=120)
        r.raise_for_status()
        data = r.json()
        if data.get("success") != 0:
            raise RuntimeError(f"剪映文件上传失败: {r.text[:300]}")

        check_url = f"https://{self.upload_hosts}/{self.store_uri}?uploadID={self.upload_id}"
        self.session.post(check_url, data=f"1:{crc32}", headers=headers, timeout=30)

        commit_url = (
            f"https://{self.upload_hosts}/{self.store_uri}?uploadID={self.upload_id}"
            f"&partNumber=1&x-amz-security-token={self.session_token}"
        )
        self.session.put(commit_url, data=content, headers=headers, timeout=120)

    # ---- 转录流程 ----
    def transcribe(self, audio_path: str) -> Transcript:
        self._upload_sign(audio_path)
        self._upload_auth(audio_path)
        self._upload_file(audio_path)

        # 提交任务
        payload = {
            "adjust_endtime": 200,
            "audio": self.store_uri,
            "caption_type": 2,
            "client_request_id": str(uuid.uuid4()),
            "max_lines": 1,
            "songs_info": [{"end_time": 6000, "id": "", "start_time": 0}],
            "words_per_line": 16,
        }
        sign, device_time = self._sign("/lv/v1/audio_subtitle/submit")
        headers = self._build_headers(device_time, sign)
        r = self.session.post(HOST + "/lv/v1/audio_subtitle/submit", json=payload, headers=headers, timeout=30)
        data = r.json()
        if data.get("ret") != "0":
            raise RuntimeError(f"剪映提交失败: {data.get('errmsg', data)}")
        query_id = data["data"]["id"]

        # 查询结果
        sign, device_time = self._sign("/lv/v1/audio_subtitle/query")
        headers = self._build_headers(device_time, sign)
        r = self.session.post(
            HOST + "/lv/v1/audio_subtitle/query",
            json={"id": query_id, "pack_options": {"need_attribute": True}},
            headers=headers,
            timeout=30,
        )
        data = r.json()
        if data.get("ret") != "0":
            raise RuntimeError(f"剪映查询失败: {data.get('errmsg', data)}")

        utterances = data["data"].get("utterances", [])
        segments = [
            Segment(u["text"], int(u["start_time"]), int(u["end_time"]))
            for u in utterances
        ]
        return Transcript(segments, source="jianying")
