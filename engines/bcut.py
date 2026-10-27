"""B 站必剪（bcut）免费转录接口。

直接移植自 VideoCaptioner 的 BcutASR，去掉其内部的缓存/限流依赖，
仅保留 HTTP 协议逻辑。无需任何 API key。
"""

from __future__ import annotations

import json
import time
from typing import List, Optional

import requests

from .base import BaseEngine
from subtitle import Segment, Transcript

API_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
URL_UPLOAD = API_BASE + "/resource/create"
URL_COMMIT = API_BASE + "/resource/create/complete"
URL_TASK = API_BASE + "/task"
URL_RESULT = API_BASE + "/task/result"

HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}


class BcutEngine(BaseEngine):
    name = "bcut"

    def __init__(self, poll_interval: float = 1.0, max_wait: float = 600.0):
        self.session = requests.Session()
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    def _post(self, url, data=None, json_data=None):
        r = self.session.post(url, data=data, json=json_data, headers=HEADERS, timeout=60)
        r.raise_for_status()
        return r.json()

    def _poll_result(self, task_id: str):
        """查询任务结果。

        任务未完成时该接口会返回 412 等非 200 状态，必须容忍并继续轮询，
        不能直接 raise_for_status。
        """
        try:
            r = self.session.get(
                URL_RESULT, params={"model_id": 7, "task_id": task_id},
                headers=HEADERS, timeout=60,
            )
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            payload = r.json()
        except ValueError:
            return None
        data = payload.get("data") or {}
        if data.get("state") == 4 and data.get("result"):
            return json.loads(data["result"])
        return None

    def transcribe(self, audio_path: str) -> Transcript:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        # 1. 申请上传
        resp = self._post(
            URL_UPLOAD,
            data=json.dumps(
                {
                    "type": 2,
                    "name": "audio.mp3",
                    "size": len(audio_bytes),
                    "ResourceFileType": "mp3",
                    "model_id": "8",
                }
            ),
        )
        data = resp["data"]
        upload_urls = data["upload_urls"]
        per_size = data["per_size"]

        # 2. 分片上传
        etags = []
        for i, url in enumerate(upload_urls):
            start = i * per_size
            end = (i + 1) * per_size
            up = self.session.put(
                url, data=audio_bytes[start:end], headers=HEADERS, timeout=120
            )
            up.raise_for_status()
            etag = up.headers.get("Etag")
            if etag:
                etags.append(etag)

        # 3. 提交上传
        resp = self._post(
            URL_COMMIT,
            data=json.dumps(
                {
                    "InBossKey": data["in_boss_key"],
                    "ResourceId": data["resource_id"],
                    "Etags": ",".join(etags),
                    "UploadId": data["upload_id"],
                    "model_id": "8",
                }
            ),
        )
        download_url = resp["data"]["download_url"]

        # 4. 创建任务
        resp = self._post(
            URL_TASK,
            json_data={"resource": download_url, "model_id": "8"},
        )
        task_id = resp["data"]["task_id"]

        # 5. 轮询结果（容忍任务未完成时的 412 等非 200 响应）
        result = None
        deadline = time.time() + self.max_wait
        while time.time() < deadline:
            result = self._poll_result(task_id)
            if result is not None:
                break
            time.sleep(self.poll_interval)

        if result is None:
            raise RuntimeError("bcut 转录超时或失败（任务未在预期时间内完成）")

        segments = [
            Segment(u["transcript"], int(u["start_time"]), int(u["end_time"]))
            for u in result.get("utterances", [])
        ]
        return Transcript(segments, source="bcut")
