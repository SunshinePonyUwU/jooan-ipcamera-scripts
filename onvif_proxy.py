import http.server
import urllib.request
import sys
import re

LISTEN_PORT = 8999
TARGET_PTZ_URL = "http://172.18.0.154:8899/onvif/Ptz"

# 1. 完善的能力集声明
CAPABILITIES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Device><tt:XAddr>http://172.18.0.131:{port}/onvif/device_service</tt:XAddr></tt:Device>
        <tt:Media><tt:XAddr>http://172.18.0.131:{port}/onvif/media</tt:XAddr></tt:Media>
        <tt:PTZ><tt:XAddr>http://172.18.0.131:{port}/onvif/Ptz</tt:XAddr></tt:PTZ>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# 2. 核心：让 Frigate 认出来的 Profile
# 这里必须包含 <tt:PTZConfiguration> 节点
GET_PROFILES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="MainProfile" fixed="true">
        <tt:Name>MainProfile</tt:Name>
        <tt:VideoSourceConfiguration token="VSC_01"><tt:SourceToken>VS_01</tt:SourceToken><tt:Bounds x="0" y="0" width="1920" height="1080"/></tt:VideoSourceConfiguration>
        <tt:VideoEncoderConfiguration token="VEC_01"><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration>
        <tt:PTZConfiguration token="PTZ_CONF_01">
          <tt:NodeToken>Node_01</tt:NodeToken>
          <tt:DefaultContinuousPanTiltVelocitySpace>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:DefaultContinuousPanTiltVelocitySpace>
        </tt:PTZConfiguration>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

# 如果支持缩放加上
# <tt:DefaultContinuousZoomVelocitySpace>http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace</tt:DefaultContinuousZoomVelocitySpace>

MOCK_RESPONSES = {
    "GetDeviceInformation": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><SOAP-ENV:Body><tds:GetDeviceInformationResponse><tds:Manufacturer>Fake-Camera</tds:Manufacturer><tds:Model>Proxy-01</tds:Model><tds:FirmwareVersion>1.0</tds:FirmwareVersion><tds:SerialNumber>12345</tds:SerialNumber><tds:HardwareId>1.0</tds:HardwareId></tds:GetDeviceInformationResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>""",
    "GetNodes": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver10/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><SOAP-ENV:Body><tptz:GetNodesResponse><tptz:PTZNode token="Node_01"><tt:Name>Node1</tt:Name><tt:SupportedPTZSpaces/></tptz:PTZNode></tptz:GetNodesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>""",
    "GetConfigurations": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver10/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><SOAP-ENV:Body><tptz:GetConfigurationsResponse><tptz:PTZConfiguration token="PTZ_CONF_01"><tt:Name>Config1</tt:Name><tt:NodeToken>Node_01</tt:NodeToken></tptz:PTZConfiguration></tptz:GetConfigurationsResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
}

class FrigateOnvifProxy(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        # 核心逻辑区分
        if any(cmd in body for cmd in ["ContinuousMove", "Stop", "AbsoluteMove", "RelativeMove"]):
            self.forward_to_real_camera(body)
        elif "GetCapabilities" in body:
            self.send_xml(CAPABILITIES_XML.format(port=LISTEN_PORT))
        elif "GetProfiles" in body:
            self.send_xml(GET_PROFILES_XML)
        else:
            self.match_and_mock(body)

    def forward_to_real_camera(self, body):
        print(f"[FORWARD] 正在提取速度并重组报文...")
        try:
            # 1. 提取速度值
            pan_match = re.search(r'x="([-+]?\d*\.?\d+)"', body)
            tilt_match = re.search(r'y="([-+]?\d*\.?\d+)"', body)

            x = pan_match.group(1) if pan_match else "0"
            y = tilt_match.group(1) if tilt_match else "0"

            print(f"[DEBUG] 提取到速度: x={x}, y={y}")

            # 2. 判断是移动还是停止
            # 如果 Frigate 发送了 Stop 节点，或者速度全为 0，则构造 Stop 报文
            if "Stop" in body or (x == "0" and y == "0"):
                print("[ACTION] 构造 Stop 指令")
                payload = f'''<?xml version="1.0" encoding="UTF-8"?>
    <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
      <s:Body>
        <tptz:Stop>
          <tptz:ProfileToken>Profile000</tptz:ProfileToken>
          <tptz:PanTilt>true</tptz:PanTilt>
          <tptz:Zoom>true</tptz:Zoom>
        </tptz:Stop>
      </s:Body>
    </s:Envelope>'''
                action = "http://www.onvif.org/ver20/ptz/wsdl/Stop"
            else:
                print("[ACTION] 构造 ContinuousMove 指令")
                payload = f'''<?xml version="1.0" encoding="UTF-8"?>
    <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
      <s:Body>
        <tptz:ContinuousMove>
          <tptz:ProfileToken>Profile000</tptz:ProfileToken>
          <tptz:Velocity>
            <tt:PanTilt x="{x}" y="{y}" />
          </tptz:Velocity>
        </tptz:ContinuousMove>
      </s:Body>
    </s:Envelope>'''
                action = "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove"

            # 3. 准备正确的 Headers (修复了之前的 dict 错误)
            headers = {
                "Content-Type": "application/soap+xml; charset=utf-8",
                "SOAPAction": action
            }

            # 4. 执行请求
            req = urllib.request.Request(
                TARGET_PTZ_URL,
                data=payload.encode('utf-8'),
                headers=headers,
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=5) as res:
                res_data = res.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(res_data)))
                self.end_headers()
                self.wfile.write(res_data)
                print(f"[SUCCESS] 目标已响应")

        except Exception as e:
            print(f"[ERROR] 转发失败: {e}")
            # 兜底返回，防止客户端卡死
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>')

    def match_and_mock(self, body):
        for key, xml in MOCK_RESPONSES.items():
            if key in body:
                print(f"[MOCK] 命中请求: {key}")
                self.send_xml(xml)
                return

        # 对于 Frigate 可能发起的其他探测，返回空 Envelope 保证不报错
        print(f"[DEBUG] 未捕获请求 (可能需要模拟): {body[:80]}...")
        empty_soap = """<?xml version="1.0" encoding="utf-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body/></SOAP-ENV:Envelope>"""
        self.send_xml(empty_soap)

    def send_xml(self, xml_string):
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(xml_string)))
        self.end_headers()
        self.wfile.write(xml_string.encode('utf-8'))

if __name__ == "__main__":
    httpd = http.server.HTTPServer(('', LISTEN_PORT), FrigateOnvifProxy)
    print(f"ONVIF 代理服务器已启动: {LISTEN_PORT}")
    httpd.serve_forever()
