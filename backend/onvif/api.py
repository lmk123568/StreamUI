from .client import ONVIFCamera

from fastapi import APIRouter, Query

WSDL_DIR = "./onvif/wsdl"

router = APIRouter()


@router.get("/api/onvif/scan", tags=["ONVIF"], summary="扫描 ONVIF 设备")
async def get_scan():
    pass


@router.get("/api/onvif/profiles", summary="获取 ONVIF 配置（含 PTZ 信息）")
def get_profiles(
    cameraip: str = Query(..., description="ONVIF 设备 IP 地址"),
    username: str = Query(..., description="ONVIF 设备用户名"),
    password: str = Query(..., description="ONVIF 设备密码"),
    port: int = Query(80, description="ONVIF 设备端口"),
):
    try:
        mycam = ONVIFCamera(
            cameraip, port, username, password, WSDL_DIR, encrypt=True, adjust_time=True
        )

        # 获取设备信息
        devicemgmt = mycam.create_devicemgmt_service()
        info = devicemgmt.GetDeviceInformation()
        device_info = {
            "model": info.Model,
            "manufacturer": info.Manufacturer,
            "firmware_version": info.FirmwareVersion,
            "serial_number": info.SerialNumber
        }

        # 获取媒体服务和所有 profiles
        media_service = mycam.create_media_service()
        profiles = media_service.GetProfiles()

        # 尝试创建 PTZ 服务（一次即可，用于后续查询）
        ptz_service = None
        try:
            ptz_service = mycam.create_ptz_service()
        except Exception:
            ptz_service = None

        parsed_profiles = []

        for i, profile in enumerate(profiles):
            name = str(profile.Name) if profile.Name else f"Profile-{i}"
            token = profile.token

            # --- 视频编码 ---
            video = None
            if hasattr(profile, 'VideoEncoderConfiguration') and profile.VideoEncoderConfiguration:
                ve = profile.VideoEncoderConfiguration
                video = {
                    "encoding": ve.Encoding,
                    "width": ve.Resolution.Width,
                    "height": ve.Resolution.Height,
                    "framerate": getattr(ve.RateControl, 'FrameRateLimit', 0),
                    "bitrate": getattr(ve.RateControl, 'BitrateLimit', 0)
                }

            # --- 音频编码 ---
            audio = None
            if hasattr(profile, 'AudioEncoderConfiguration') and profile.AudioEncoderConfiguration:
                ae = profile.AudioEncoderConfiguration
                audio = {
                    "encoding": ae.Encoding,
                    "samplerate": ae.SampleRate,
                    "bitrate": ae.Bitrate
                }

            # --- RTSP 流地址 ---
            rtsp_url = ""
            try:
                stream_uri = media_service.GetStreamUri({
                    "StreamSetup": {
                        "Stream": "RTP-Unicast",
                        "Transport": {"Protocol": "RTSP"}
                    },
                    "ProfileToken": token
                })
                raw_uri = stream_uri.Uri
                if cameraip not in raw_uri:
                    path_part = raw_uri.lstrip('/')
                    rtsp_url = f"rtsp://{username}:{password}@{cameraip}:{port}/{path_part}"
                else:
                    if raw_uri.startswith("rtsp://"):
                        if "@" not in raw_uri.split("://", 1)[1]:
                            host_part = raw_uri.split("://", 1)[1]
                            rtsp_url = f"rtsp://{username}:{password}@{host_part}"
                        else:
                            rtsp_url = raw_uri
                    else:
                        rtsp_url = raw_uri
            except Exception as e:
                rtsp_url = f"获取失败: {str(e)}"

            # --- PTZ 信息 ---
            ptz_info = None
            if ptz_service is not None and hasattr(profile, 'PTZConfiguration') and profile.PTZConfiguration:
                try:
                    ptz_config = profile.PTZConfiguration
                    config_token = ptz_config.token

                    # 获取能力选项（即使范围是 inf，也能获取支持的操作类型）
                    options = ptz_service.GetConfigurationOptions(config_token)
                    spaces = getattr(options, 'Spaces', None)

                    ptz_data = {
                        "config_token": config_token,
                        "config_name": str(ptz_config.Name) if ptz_config.Name else "",
                        "supported_operations": [],
                        "control_mode": "generic",  # 表示使用 GenericSpace
                        "default_speed": {
                            "pan_tilt": {"x": 1.0, "y": 1.0},
                            "zoom": {"x": 1.0}
                        },
                        "limits": {
                            "pan_tilt": {"has_finite_limits": False},
                            "zoom": {"has_finite_limits": False}
                        }
                    }

                    # 尝试读取默认速度（从配置中）
                    if hasattr(ptz_config, 'DefaultPTZSpeed'):
                        d = ptz_config.DefaultPTZSpeed
                        if hasattr(d, 'PanTilt'):
                            ptz_data["default_speed"]["pan_tilt"] = {
                                "x": float(d.PanTilt.x) if d.PanTilt.x is not None else 1.0,
                                "y": float(d.PanTilt.y) if d.PanTilt.y is not None else 1.0
                            }
                        if hasattr(d, 'Zoom'):
                            ptz_data["default_speed"]["zoom"] = {
                                "x": float(d.Zoom.x) if d.Zoom.x is not None else 1.0
                            }

                    # === 判断支持哪些操作 ===
                    if spaces:
                        if getattr(spaces, 'ContinuousPanTiltVelocitySpace', None):
                            ptz_data["supported_operations"].append("Continuous")
                        if getattr(spaces, 'RelativePanTiltTranslationSpace', None):
                            ptz_data["supported_operations"].append("Relative")
                        if getattr(spaces, 'AbsolutePanTiltPositionSpace', None):
                            ptz_data["supported_operations"].append("Absolute")

                    # === 检查是否有有限范围（非 inf）===
                    pan_tilt_limits = getattr(ptz_config, 'PanTiltLimits', None)
                    zoom_limits = getattr(ptz_config, 'ZoomLimits', None)

                    if pan_tilt_limits and hasattr(pan_tilt_limits, 'Range'):
                        x_min = pan_tilt_limits.Range.XRange.Min
                        x_max = pan_tilt_limits.Range.XRange.Max
                        y_min = pan_tilt_limits.Range.YRange.Min
                        y_max = pan_tilt_limits.Range.YRange.Max
                        if all(abs(v) != float('inf') for v in [x_min, x_max, y_min, y_max]):
                            ptz_data["limits"]["pan_tilt"] = {
                                "has_finite_limits": True,
                                "pan_min": x_min,
                                "pan_max": x_max,
                                "tilt_min": y_min,
                                "tilt_max": y_max
                            }

                    if zoom_limits and hasattr(zoom_limits, 'Range'):
                        z_min = zoom_limits.Range.XRange.Min
                        z_max = zoom_limits.Range.XRange.Max
                        if abs(z_min) != float('inf') and abs(z_max) != float('inf'):
                            ptz_data["limits"]["zoom"] = {
                                "has_finite_limits": True,
                                "min": z_min,
                                "max": z_max
                            }

                    # === 预设位支持 ===
                    ptz_data["presets_supported"] = hasattr(options, 'SupportedPresetConfigurations')

                    ptz_info = ptz_data

                except Exception as e:
                    ptz_info = {"error": f"解析 PTZ 配置失败: {str(e)}"}

            # 组装 profile 数据
            profile_dict = {
                "name": name,
                "token": token,
                "video": video,
                "audio": audio,
                "rtsp_url": rtsp_url,
                "ptz": ptz_info  # 可能为 None 或 dict
            }

            parsed_profiles.append(profile_dict)

        return {
            "code": 0,
            "data": {
                "device_info": device_info,
                "profiles": parsed_profiles
            }
        }

    except Exception as e:
        return {"code": 1, "msg": str(e)}