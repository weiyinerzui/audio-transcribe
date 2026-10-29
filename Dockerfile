# audio-transcribe Web 界面
#
# 构建：docker build -t audio-transcribe .
# 运行：docker run --rm -p 127.0.0.1:8787:8787 -v at-data:/data audio-transcribe
#
# 说明：
#   - 容器内监听 0.0.0.0（容器网络与宿主机隔离），宿主机侧必须用
#     -p 127.0.0.1:8787:8787 把端口锁在回环地址上，不要用 -p 8787:8787
#   - API Key 只存在浏览器 localStorage，容器里不落任何密钥
#   - /data 挂卷持久化：任务历史、转录产物、下载的模型

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIO_TRANSCRIBE_HOME=/data \
    HF_ENDPOINT=https://hf-mirror.com

# ffmpeg 用于抽音轨；镜像里预装，省掉运行时下载静态二进制
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-web.txt requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements-web.txt

# 可选：本地转录后端（体积较大，默认不装）
# 需要离线转录时取消注释：
#   RUN pip install faster-whisper huggingface_hub

COPY . .

RUN mkdir -p /data/outputs /data/uploads /data/models /data/bin

VOLUME ["/data"]
EXPOSE 8787

# 健康检查：服务起来后 /api/health 返回 {"ok": true}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health',timeout=4).status==200 else 1)"

# 容器内不自动开浏览器（没有桌面环境）
CMD ["python", "web/server.py", "--host", "0.0.0.0", "--port", "8787", "--no-browser"]
