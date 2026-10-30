#!/usr/bin/env bash
#
# audio-transcribe 一键安装脚本（macOS / Linux）
#
#   curl -fsSL https://weiyinerzui.github.io/audio-transcribe/install.sh | bash
#
# 想先看看脚本内容再执行（推荐）：
#   curl -fsSL https://weiyinerzui.github.io/audio-transcribe/install.sh -o install.sh
#   less install.sh && bash install.sh
#
# 做四件事：
#   1. 找一个可用的 Python 3.9+
#   2. 建独立 venv 装依赖（不污染系统 Python，不需要 sudo）
#   3. 把 ffmpeg 静态二进制放进 ~/.audio-transcribe/bin（不用 brew/apt）
#   4. 生成启动脚本 audio-transcribe，之后一条命令就能开
#
# 环境变量：
#   AT_VERSION=v1.6.0   指定版本（默认 latest）
#   AT_HOME=/somewhere  自定义安装目录（默认 ~/.audio-transcribe）

set -euo pipefail

REPO="weiyinerzui/audio-transcribe"
HOME_DIR="${AT_HOME:-$HOME/.audio-transcribe}"
PY_DIR="$HOME_DIR/runtime"
BIN_DIR="$HOME_DIR/bin"
SRC_DIR="$HOME_DIR/src"
LOG="$HOME_DIR/install.log"

mkdir -p "$HOME_DIR" "$BIN_DIR"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*"; exit 1; }

echo "audio-transcribe 安装程序"
echo "安装目录：$HOME_DIR"

# ---------------------------------------------------------------- 1. Python

step "检查 Python"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  warn "没找到 Python 3.9 或更新的版本"
  OS="$(uname -s)"
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      step "用 Homebrew 安装 Python"
      brew install python@3.12 || die "brew 安装失败，请手动装 Python 后重跑"
      PY="python3.12"
    else
      echo ""
      echo "macOS 上没有 Python，需要先装一个。三选一："
      echo "  1) 装 Xcode 命令行工具（自带 python3）：xcode-select --install"
      echo "  2) 用 Homebrew：/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
      echo "  3) 去 python.org 下载安装包：https://www.python.org/downloads/"
      die "装好 Python 后重新运行本脚本"
    fi
  else
    echo ""
    echo "Linux 上安装 Python："
    echo "  Debian/Ubuntu: sudo apt update && sudo apt install -y python3 python3-venv"
    echo "  Fedora:        sudo dnf install -y python3"
    echo "  Arch:          sudo pacman -S python"
    die "装好 Python 后重新运行本脚本"
  fi
fi

PYVER="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
ok "$PY（$PYVER）"

# ---------------------------------------------------------------- 2. venv

step "创建独立运行环境"

if [ -x "$PY_DIR/bin/python" ]; then
  ok "已存在，复用：$PY_DIR"
else
  "$PY" -m venv "$PY_DIR" 2>/dev/null || die "建 venv 失败。Debian/Ubuntu 上可能要装 python3-venv"
  ok "已创建：$PY_DIR"
fi

VENV_PY="$PY_DIR/bin/python"

step "安装依赖（要一两分钟，取决于网速）"

PIP_INDEX=""
# 国内网络优先用镜像，失败就回退官方源
for idx in "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple" ""; do
  if [ -n "$idx" ]; then
    if "$VENV_PY" -m pip install -q --disable-pip-version-check -i "$idx" --upgrade pip 2>/dev/null; then
      PIP_INDEX="$idx"; break
    fi
  else
    PIP_INDEX=""; break
  fi
done
[ -n "$PIP_INDEX" ] && ok "使用镜像 $PIP_INDEX" || warn "使用默认源"

PIP_ARGS=(install -q --disable-pip-version-check
          requests "yt-dlp>=2024.1.0" "bilibili-api-python>=16.0.0"
          "fastapi>=0.110" "uvicorn>=0.27" "python-multipart>=0.0.9")
[ -n "$PIP_INDEX" ] && PIP_ARGS+=(-i "$PIP_INDEX")

if ! "$VENV_PY" -m pip "${PIP_ARGS[@]}"; then
  warn "带版本约束安装失败，去掉约束重试（常见于新版 Python 没有旧 wheel）"
  "$VENV_PY" -m pip install -q --disable-pip-version-check \
    requests yt-dlp bilibili-api-python fastapi uvicorn python-multipart \
    ${PIP_INDEX:+-i "$PIP_INDEX"} || die "依赖安装失败，详见 $LOG"
fi
ok "依赖装好了"

step "安装 bilibili-api 的请求库（B 站链接要用）"
# bilibili-api-python 17.x 起不自带 HTTP 客户端，缺了会报「尚未安装第三方请求库」
if "$VENV_PY" -m pip install -q --disable-pip-version-check curl_cffi \
     ${PIP_INDEX:+-i "$PIP_INDEX"} 2>/dev/null; then
  ok "curl_cffi 已装"
else
  "$VENV_PY" -m pip install -q --disable-pip-version-check requests \
       ${PIP_INDEX:+-i "$PIP_INDEX"} 2>/dev/null || warn "请求库没装上，B 站链接可能不可用"
fi

# ---------------------------------------------------------------- 3. ffmpeg

step "准备 ffmpeg"

if [ -x "$BIN_DIR/ffmpeg" ] || command -v ffmpeg >/dev/null 2>&1; then
  ok "已可用"
else
  OS="$(uname -s)"
  ARCH="$(uname -m)"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT

  if [ "$OS" = "Linux" ]; then
    case "$ARCH" in
      x86_64|amd64) FF_ARCH=amd64 ;;
      aarch64|arm64) FF_ARCH=arm64 ;;
      *) die "不支持的架构 $ARCH，请手动装 ffmpeg" ;;
    esac
    URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FF_ARCH}-static.tar.xz"
    echo "  下载 $URL"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL --retry 3 -o "$TMP/ff.tar.xz" "$URL" || die "ffmpeg 下载失败"
    else
      wget -q -O "$TMP/ff.tar.xz" "$URL" || die "ffmpeg 下载失败"
    fi
    tar -xJf "$TMP/ff.tar.xz" -C "$TMP" || die "解压失败"
    for n in ffmpeg ffprobe; do
      found="$(find "$TMP" -name "$n" -type f | head -1)"
      [ -n "$found" ] && cp "$found" "$BIN_DIR/$n" && chmod +x "$BIN_DIR/$n"
    done
  else
    # macOS 官方不给静态构建，用 evermeet 的
    for n in ffmpeg ffprobe; do
      URL="https://evermeet.cx/ffmpeg/getrelease/$n/zip"
      echo "  下载 $n"
      curl -fsSL --retry 3 -o "$TMP/$n.zip" "$URL" || die "$n 下载失败"
      unzip -qo "$TMP/$n.zip" -d "$TMP" && cp "$TMP/$n" "$BIN_DIR/$n" && chmod +x "$BIN_DIR/$n"
    done
  fi
  ok "已装到 $BIN_DIR"
fi

# ---------------------------------------------------------------- 4. 源码

step "获取程序"

VERSION="${AT_VERSION:-latest}"
if [ "$VERSION" = "latest" ]; then
  TARBALL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
else
  TARBALL="https://github.com/$REPO/archive/refs/tags/${VERSION}.tar.gz"
fi

if [ -d "$SRC_DIR/.git" ] && command -v git >/dev/null 2>&1; then
  echo "  已有仓库，尝试 git pull 更新"
  git -C "$SRC_DIR" pull --ff-only -q 2>/dev/null && ok "已更新" || warn "更新失败，用现有版本"
else
  rm -rf "$SRC_DIR"; mkdir -p "$SRC_DIR"
  echo "  下载 $TARBALL"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 "$TARBALL" | tar -xz -C "$SRC_DIR" --strip-components=1 \
      || die "下载源码失败，检查网络"
  else
    wget -qO- "$TARBALL" | tar -xz -C "$SRC_DIR" --strip-components=1 \
      || die "下载源码失败，检查网络"
  fi
  ok "已下载到 $SRC_DIR"
fi

# ---------------------------------------------------------------- 5. 启动器

step "生成启动命令"

LAUNCHER="$HOME_DIR/audio-transcribe"
cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
export AUDIO_TRANSCRIBE_HOME="$HOME_DIR"
export PATH="$BIN_DIR:\$PATH"
exec "$VENV_PY" "$SRC_DIR/web/server.py" "\$@"
LAUNCH
chmod +x "$LAUNCHER"
ok "$LAUNCHER"

# PyPI 装的话还能有个短命令，这里手动链到 ~/.local/bin
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$LAUNCHER" "$LOCAL_BIN/audio-transcribe" 2>/dev/null || true

# ---------------------------------------------------------------- 6. 自检

step "自检"
"$BIN_DIR/ffmpeg" -version >/dev/null 2>&1 || command -v ffmpeg >/dev/null 2>&1 \
  && ok "ffmpeg 正常" || warn "ffmpeg 可能有问题"
"$VENV_PY" -c "import fastapi, uvicorn, requests; print('  依赖导入正常')" \
  || die "依赖导入失败，详见 $LOG"

case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *) warn "$LOCAL_BIN 不在 PATH 里，需要手动加一行到 ~/.bashrc 或 ~/.zshrc" ;;
esac

cat <<DONE

$(printf '\033[1;32m安装完成\033[0m')

启动：

  $LAUNCHER

或者（如果 PATH 配好了）：

  audio-transcribe

然后浏览器打开 http://127.0.0.1:8787

常用参数：
  audio-transcribe --port 9000      换端口
  audio-transcribe --host 0.0.0.0   允许局域网访问（自己注意安全）
  audio-transcribe --no-browser     不自动开浏览器

数据目录：$HOME_DIR
安装日志：$LOG

卸载：rm -rf $HOME_DIR

DONE
