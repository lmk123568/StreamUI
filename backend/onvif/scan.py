import socket
import xml.etree.ElementTree as ET
import time
import re

# ================== 配置区 ==================
# 扫描超时（秒）
TIMEOUT = 3

# 目标子网（可改为你的局域网）
SUBNET = "172.16.34.169/24"  # 或直接广播到 255.255.255.255
# ===========================================

# WS-Discovery 探测消息（Probe）
PROBE_MSG = """<?xml version="1.0" encoding="utf-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:e2="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl"
            xmlns:dp="http://schemas.xmlsoap.org/ws/2004/09/transfer"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dnx="http://www.onvif.org/ver10/network/wsdl/extended">
  <e:Header>
    <e2:MessageID>uuid:12345678-1234-1234-1234-123456789012</e2:MessageID>
    <e2:To e:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</e2:To>
    <d:AppSequence InstanceId="1" MessageNumber="1"/>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


def send_probe():
    """发送 WS-Discovery Probe 广播"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(TIMEOUT)

    # 发送到 ONVIF 发现端口
    discovery_addr = ("239.255.255.255", 3702)  # 组播
    broadcast_addr = ("255.255.255.255", 3702)  # 广播
    # broadcast_addr = ("172.60.34.110", 3702)  # 广播

    print("📡 正在发送 ONVIF 发现请求（广播 + 组播）...")
    try:
        sock.sendto(PROBE_MSG.encode("utf-8"), discovery_addr)
        sock.sendto(PROBE_MSG.encode("utf-8"), broadcast_addr)
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        sock.close()
        return None

    return sock


def parse_probe_match(data):
    """解析设备返回的 ProbeMatch 响应"""
    try:
        xml = data.decode("utf-8", errors="ignore")
        root = ET.fromstring(xml)

        # 命名空间（必须定义）
        ns = {
            "a": "http://www.w3.org/2003/05/soap-envelope",
            "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
            "dn": "http://www.onvif.org/ver10/network/wsdl",
        }

        # 提取 XAddrs（设备服务地址）
        xaddrs = root.find(".//d:XAddrs", ns)
        if xaddrs is None:
            return None

        xaddr_text = xaddrs.text.strip()
        urls = xaddr_text.split()

        # 找到第一个 http:// 开头的地址
        device_url = None
        for url in urls:
            if url.startswith("http"):
                device_url = url
                break

        if not device_url:
            return None

        # 从 URL 中提取 IP 和端口
        match = re.search(r"http://([^:/]+):?(\d*)?", device_url)
        if not match:
            return None

        ip = match.group(1)
        port = match.group(2) or "80"

        # 尝试提取设备类型（可选）
        types = root.find(".//d:Types", ns)
        type_text = types.text if types is not None else "Unknown"

        return {"ip": ip, "port": port, "url": device_url, "type": type_text.strip()}

    except Exception as e:
        print(f"⚠️ 解析失败: {e}")
        return None


def scan_onvif_devices():
    """主扫描函数"""
    sock = send_probe()
    if not sock:
        return

    print("👂 正在监听响应...（等待 {} 秒）".format(TIMEOUT))
    devices = []

    start_time = time.time()
    while time.time() - start_time < TIMEOUT:
        try:
            data, addr = sock.recvfrom(65535)
            # print(f"收到数据来自 {addr}: {data[:100]}...")

            result = parse_probe_match(data)
            if result and result not in devices:
                devices.append(result)
                print(f"\n✅ 发现设备:")
                print(f"   IP:     {result['ip']}")
                print(f"   Port:   {result['port']}")
                print(f"   URL:    {result['url']}")
                print(f"   Type:   {result['type']}")

        except socket.timeout:
            break
        except Exception as e:
            # 忽略小错误
            pass

    sock.close()

    print(f"\n🎉 扫描完成！共发现 {len(devices)} 个 ONVIF 设备。")
    return devices


# ============ 主程序 ============
if __name__ == "__main__":
    devices = scan_onvif_devices()

    if not devices:
        print("💡 提示：")
        print("   - 确保摄像头支持 ONVIF 并已开启")
        print("   - 确保摄像头和电脑在同一局域网")
        print("   - 某些品牌（如海康）可能需要在网页开启‘ONVIF’功能")
