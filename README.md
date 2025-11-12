<div align="center">
  <img src="./frontend/assets/logo.svg" width="64px"/>
  <h1>StreamUI</h1>
</div>

### 概述

一个极简、轻便、易于二次开发的视频流媒体管理平台

### 支持功能

- 支持 RTSP/RTMP/HLS/WebRTC/RTP/GB28181 等主流协议的拉流推流接入

- 支持 ONVIF 设备识别

- 支持分发 RTSP/WebRTC/RTMP/FLV/HLS/HLS-fMP4/HTTP-TS/HTTP-fMP4 等协议

- 支持多屏播放

- 支持流本地录制、回放、下载、自动清理，支持事件回溯录制

- 支持 GB28181 接入/级联（coming soon ...）

### 快速启动

本项目推荐 docker compose 部署

```bash
cd ./docker
docker compose up -d
```

开启后，访问 `http://{服务器地址}:10800` 即可登录，默认密码为 streamui

如果修改配置后需要重启

```bash
docker compose restart
```


### 效果图

<img src="./snapshots/login.png" alt="wall" style="zoom:33%;" />

<img src="./snapshots/home.png" alt="home" style="zoom: 33%;" />
