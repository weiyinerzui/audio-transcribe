# 部署与访问控制

三种部署形态，按需要挑一种：

| 形态 | 适合谁 | 访问控制 |
|---|---|---|
| **Windows 绿色包** | 不懂命令行的新手，单机用 | 不需要（只监听本机） |
| **Railway / 自有服务器** | 想在其他电脑、手机上用 | 建议开密码 |
| **公共演示站** | 分享给别人试 | **必须**开密码 + 每日限额 |

---

## 一、访问控制的三种模式

由环境变量决定，**什么都不设 = 完全开放**（行为和以前一样，本地自用不受影响）。

### 模式 1：完全开放

```bash
python web/server.py
```

没有登录，没有限额。只适合自己电脑上用，因为默认只监听 `127.0.0.1`，外部根本连不进来。

### 模式 2：固定密码（推荐给演示站）

```bash
export AT_ACCESS_PASSWORD='你的密码'
export AT_DAILY_LIMIT=10          # 每人每天最多 10 次转录
export AT_DAILY_MINUTES=60        # 每人每天最多 60 分钟音频
python web/server.py --host 0.0.0.0
```

所有人共用一个密码，进来后按**账户**记每日额度。只有一个 `guest` 账户。

### 模式 3：多账户（推荐给正式部署）

```bash
export AT_USERS_JSON='{
  "admin": {"password": "换成你的强密码", "role": "admin"},
  "alice": {"password": "她自己的密码", "role": "guest", "daily_tasks": 10, "daily_minutes": 60},
  "bob":   {"password": "另一个密码",   "role": "guest", "daily_tasks": 5,  "daily_minutes": 30}
}'
python web/server.py --host 0.0.0.0
```

**`admin` 账户没有限额**，能看到所有人的任务；`guest` 只能看到自己的任务，超过额度返回 429。

密码可以直接写明文，也可以写 `sha256:哈希值`：

```bash
# 生成哈希（不会把明文写进配置文件）
python -c "import hashlib,sys;print('sha256:'+hashlib.sha256(sys.argv[1].encode()).hexdigest())" '你的密码'
```

---

## 二、环境变量全集

| 变量 | 默认 | 说明 |
|---|---|---|
| `AT_ACCESS_PASSWORD` | 空 | 设了就开单密码模式 |
| `AT_USERS_JSON` | 空 | 设了就开多账户模式，优先级高于上面那个 |
| `AT_SECRET_KEY` | 随机 | 签名 Cookie 用。**固定它，重启后登录状态才不会掉** |
| `AT_DAILY_LIMIT` | 10 | 每个 guest 每天转录次数，`0` 表示不限 |
| `AT_DAILY_MINUTES` | 60 | 每个 guest 每天音频分钟数，`0` 表示不限 |
| `AT_MAX_UPLOAD_MB` | 2048 | 单文件大小上限。演示站建议设 `100` |
| `AT_CONCURRENCY` | 1 | 同时处理几个任务。演示站别超过 2 |
| `AT_HOST` | 127.0.0.1 | 监听地址。容器/云平台设 `0.0.0.0` |
| `PORT` | 8787 | 云平台（Railway / Render / Fly）会自动注入 |
| `AUDIO_TRANSCRIBE_HOME` | `~/.audio-transcribe` | 数据目录：任务记录、产物、模型 |

**关于 `AT_SECRET_KEY`**：不设的话每次重启都随机生成，后果是所有用户被强制重新登录。正式部署一定要固定它：

```bash
python -c "import secrets;print(secrets.token_hex(32))"
```

---

## 三、Railway 一键部署

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/weiyinerzui/audio-transcribe)

点按钮后填四个变量即可：

| 变量 | 填什么 |
|---|---|
| `AT_ACCESS_PASSWORD` | 你的访问密码 |
| `AT_SECRET_KEY` | 上面那条命令生成的一串 |
| `AT_HOST` | `0.0.0.0` |
| `AUDIO_TRANSCRIBE_HOME` | `/data` |

然后到 **Settings → Volumes** 挂一个卷到 `/data`，否则重启后任务历史全丢。

`PORT` 不用填，Railway 会注入，代码已经支持。

### 手动部署（不用模板）

```bash
# 装 Railway CLI
npm i -g @railway/cli
railway login
railway init
railway up
```

Railway 检测到仓库里有 `Dockerfile` 会自动用它构建，不需要额外配置。

**免费额度提醒**：Railway 免费层每月 500 小时执行时间 + $5 额度。转录是 CPU 密集任务，长音频会消耗较多，建议 `AT_CONCURRENCY=1`。

---

## 四、部署在自己的服务器上

```bash
git clone https://github.com/weiyinerzui/audio-transcribe.git
cd audio-transcribe

docker build -t audio-transcribe .

docker run -d --name at \
  -p 127.0.0.1:8787:8787 \
  -v at-data:/data \
  -e AT_ACCESS_PASSWORD='你的密码' \
  -e AT_SECRET_KEY='那串固定密钥' \
  -e AT_DAILY_LIMIT=10 \
  -e AT_DAILY_MINUTES=60 \
  -e AT_MAX_UPLOAD_MB=100 \
  -e AT_CONCURRENCY=1 \
  --restart unless-stopped \
  audio-transcribe
```

**注意 `-p 127.0.0.1:8787:8787`**：端口锁在回环地址，外部访问必须先过反向代理。别写成 `-p 8787:8787`，那等于把它暴露在公网。

### 配 Nginx 反向代理 + HTTPS

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    client_max_body_size 200M;      # 要 >= AT_MAX_UPLOAD_MB

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;

        # SSE 长连接必须关缓冲，否则进度条不动
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`X-Forwarded-For` 必须传，否则限额记账会把所有访客当成同一个 IP。

---

## 五、安全须知

这个设计里 **API Key 和 Cookie 只存在浏览器 localStorage**，服务端只透传、不落盘、不写日志。所以：

- 你的 OpenAI / 硅基流动额度不会被部署服务的人偷走
- 但**不要**把带 key 的浏览器借给别人
- 演示站上访客用自己填的 key，服务端看不到

反过来说，以下三件事必须由部署者自己把住：

1. **一定要开密码**。公开的裸实例 10 分钟就会被爬虫刷爆。
2. **一定要设 `AT_MAX_UPLOAD_MB`**。否则有人传 2 GB 文件就能打满磁盘。
3. **`AT_CONCURRENCY` 别调高**。转录是 CPU 密集的，开 4 个并发等于让四个人都变慢。

---

## 六、常见问题

**Q：登录后刷新页面又要重新登录？**

没设 `AT_SECRET_KEY`，重启后签名密钥变了，旧 Cookie 全部失效。固定一个就好。

**Q：进度条卡住不动？**

反向代理开了缓冲。检查 Nginx 配置里有没有 `proxy_buffering off`。

**Q：限额不准确，所有人共享额度？**

没传 `X-Forwarded-For`，服务端只能看到反代 IP。补上那个 header。

**Q：想临时关掉限额？**

`AT_DAILY_LIMIT=0 AT_DAILY_MINUTES=0`。

**Q：怎么清空某天的用量？**

删掉 `$AUDIO_TRANSCRIBE_HOME/usage.json` 即可，不影响任务历史。
