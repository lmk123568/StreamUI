FROM python:3.12-slim

WORKDIR /workspace

RUN \
    # 确保 apt 配置目录存在
    mkdir -p /etc/apt && \
    # 清理所有现有源配置
    rm -f /etc/apt/sources.list.d/* && \
    # 写入清华大学 Debian 源配置
    echo 'deb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-backports main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian-security/ bookworm-security main contrib non-free non-free-firmware' > /etc/apt/sources.list && \
    # 使用 --fix-missing 忽略缺失的包，并使用 -y 自动确认
    apt update --fix-missing -y && \
    # 安装依赖包
    apt install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    nginx \
    ffmpeg && \
    # 使用国内 PyPI 源安装 Python 包
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    fastapi uvicorn apscheduler httpx psutil zeep && \
    # 清理缓存
    rm -rf /var/lib/apt/lists/* && \
    apt clean
