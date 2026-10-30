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

配置长了塞进环境变量容易出错，也可以指向一个文件：

```bash
chmod 600 ~/users.json
export AT_USERS_JSON=~/users.json
```

字段语义（容易搞混，留意）：

| 写法 | 含义 |
|---|---|
| `"role": "admin"` | 无限额，不看下面两个字段 |
| `"daily_tasks": 10` | 每天 10 次 |
| `"daily_tasks": 0` | **一次都不给**（临时停用某人） |
| 不写 `daily_tasks` | **不限**（走这个账户时不卡次数） |

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
| `AT_USERS_JSON` | 空 | 设了就开多账户模式，优先级高于上面那个。填 JSON 或一个文件路径 |
| `AT_SECRET_KEY` | 随机 | 签名 Cookie 用。**固定它，重启后登录状态才不会掉** |
| `AT_DAILY_LIMIT` | 10 | 每个 guest 每天转录次数。`0` = 一次都不给（可用来临时停用某人）；**不设置** = 用默认值 |
| `AT_DAILY_MINUTES` | 60 | 每个 guest 每天音频分钟数，规则同上 |
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


## 六、Windows 免安装包

给完全不想碰命令行的用户：

1. 到 [Releases](https://github.com/weiyinerzui/audio-transcribe/releases/latest) 选版本
2. **完整解压**到一个纯英文路径，例如 `D:\audio-transcribe`
3. 双击 `启动.bat`
4. 浏览器自动打开 `http://127.0.0.1:8787`

| 版本 | 大小 | 含 ffmpeg | 适用 |
|---|---|---|---|
| `audio-transcribe-win64.zip` | 约 124 MB | 是 | 推荐。解压即用，全程不联网 |
| `audio-transcribe-win64-slim.zip` | 约 59 MB | 否 | 带宽紧张。首次用时在界面「环境检查」点一下装 ffmpeg |

两个版本都带便携 Python 3.12 和全部 pip 依赖，**用户侧不跑 pip**。
差别只在 ffmpeg：精简版少了约 65 MB 的编解码器库，需要用户点一次安装。

> 为什么完整版这么大：ffmpeg 的 `avcodec` 一个 DLL 就 88 MB，
> BtbN 的构建把全部编解码器都编进去了，没法只保留本项目用到的几个格式。
> 自己从源码 `--disable-everything` 编译可以压到 10 MB 以内，但需要
> MinGW 构建链，CI 里跑一遍要二三十分钟，不值得。

### 精简版怎么装 ffmpeg

界面左侧「环境检查」→ 找到 `ffmpeg` 那一项 → 点「一键安装」，
它会把静态二进制下到 `%LOCALAPPDATA%\audio-transcribe\bin\`，约 80 MB，只需一次。

网络访问不了 GitHub 时手动装：下
[BtbN FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) 的
`ffmpeg-master-latest-win64-lgpl-shared.zip`，解压后把 `bin\` 里的
`ffmpeg.exe`、`ffprobe.exe` **和所有 `.dll`** 一起拷到包的 `bin\` 目录。
只拷 exe 不拷 dll 会起不来，这点很关键。

服务启动时会把包内 `bin\` 注入 `PATH`，但**如果你在数据目录里装过 ffmpeg，
那个版本优先**——用户自己装的不会被包内版本盖掉。

数据落在包内 `data\` 目录，整个文件夹拷到别的电脑（U 盘也行）继续用。

### 它做了什么

`启动.bat` 依次：

1. `chcp 65001` 切换 UTF-8，中文不乱码
2. 校验 `python\python.exe` 和 `app\web\server.py`，缺文件直接提示「重新完整解压」
3. 设 `AUDIO_TRANSCRIBE_HOME=%CD%\data`，并把 `bin\` 加进 `PATH`
4. `netstat` 查 8787 端口，被占用就告诉用户怎么办（关旧窗口或改端口）
5. 后台启动服务，轮询 `/api/health`，最多等 60 秒
6. 起来了才开浏览器；失败则 `pause` 保留窗口并打印日志尾部

`停止.bat` 按端口找到进程并 `taskkill`。

### 自己构建

```bash
# 必须在 Windows 上跑 pip 那一步：装的是 Windows 版 wheel
python packaging/build_windows.py           # 完整版，约 124 MB
python packaging/build_windows.py --slim    # 精简版，约 59 MB
# 产物：packaging/dist/audio-transcribe-win64.zip
#       packaging/dist/audio-transcribe-win64-slim.zip

# 打完包验证真能跑（会启动服务、请求接口、逐项 import）
python packaging/smoke_test.py
```

构建脚本只用标准库，构建者不用装任何东西。

`smoke_test.py` 是最后一道关，检查的是「能不能用」而不只是「文件在不在」：
包内 Python 起不起得来、七个依赖能不能 import、ffmpeg 能不能执行、
服务能不能起、首页和接口通不通。CI 里两个版本各跑一次，
任何一项失败都不发布——宁可没 Release，也不发一个解压后打不开的包。

正式产物由 GitHub Actions 在 `windows-latest` 上构建，打 tag 自动附到 Release：

```bash
git tag v1.6.0
git push origin v1.6.0
```

### 已知限制

- **只在 Windows x64 上测试**。ARM 版 Windows 没测（可跑 x64 模拟层）
- 杀毒软件可能误报（打包后的 Python 常见），加信任即可
- 绿色包不预置本地 whisper 模型，要离线转录得在界面里下载一次
- 路径带中文或括号时，个别 Windows 环境仍可能出问题，建议纯英文路径

## 七、macOS / Linux 一键安装

```bash
curl -fsSL https://weiyinerzui.github.io/audio-transcribe/install.sh | bash
```

脚本做的事：

1. 找 Python 3.9+；macOS 上没有 Homebrew 会提示三条安装途径
2. 在 `~/.audio-transcribe/runtime` 建独立 venv，**不碰系统 Python、不要 sudo**
3. 装依赖，pip 源三级回退：清华镜像 → 阿里云 → 官方
4. 带版本约束失败时去掉约束重试（旧版锁 `lxml 4.6.5` 在新 Python 上编译不过）
5. 补装 `curl_cffi`（`bilibili-api-python` 17.x 不自带请求库）
6. 下载 ffmpeg 静态二进制到 `~/.audio-transcribe/bin`，Linux 与 macOS 分别取源
7. 生成 `~/.audio-transcribe/audio-transcribe` 启动器，软链到 `~/.local/bin`

装完直接：

```bash
audio-transcribe                # 开服务并自动开浏览器
audio-transcribe --port 9000    # 换端口
```

想先审查脚本再执行（推荐）：

```bash
curl -fsSL https://weiyinerzui.github.io/audio-transcribe/install.sh -o install.sh
less install.sh
bash install.sh
```

可用环境变量：`AT_HOME` 自定义安装目录、`AT_VERSION` 指定版本（例如 `AT_VERSION=v1.6.0`，默认拉 main 最新）。

## 八、常见问题

**Q：登录后刷新页面又要重新登录？**

没设 `AT_SECRET_KEY`，重启后签名密钥变了，旧 Cookie 全部失效。固定一个就好。

**Q：进度条卡住不动？**

反向代理开了缓冲。检查 Nginx 配置里有没有 `proxy_buffering off`。

**Q：限额不准确，所有人共享额度？**

没传 `X-Forwarded-For`，服务端只能看到反代 IP。补上那个 header。

**Q：想临时关掉限额？**

做法是给这个账户改成 admin，而不是把限额设成 0 ——
**`AT_DAILY_LIMIT=0` 的意思是「一次都不给」**，不是「不限」。

```bash
# 方法一：AT_USERS_JSON 里 role 设 admin
export AT_USERS_JSON='{"boss":{"password":"...","role":"admin"}}'

# 方法二：固定密码模式下，admin 账户不受限
```

想给某人临时停用，把限额设成 `0` 即可。要表示「不限」，
用 `AT_USERS_JSON` 并且不写 `daily_tasks` / `daily_minutes` 这两个字段。

**Q：怎么清空某天的用量？**

删掉 `$AUDIO_TRANSCRIBE_HOME/usage.json` 即可，不影响任务历史。
