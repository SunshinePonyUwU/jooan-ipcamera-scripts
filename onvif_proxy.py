import http.server
import urllib.request
import socket
import argparse
import sys
import re

print = lambda *args, **kwargs: None

CAPABILITIES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Device><tt:XAddr>http://{host}:{port}/onvif/device_service</tt:XAddr></tt:Device>
        <tt:Media><tt:XAddr>http://{host}:{port}/onvif/media</tt:XAddr></tt:Media>
        <tt:PTZ><tt:XAddr>http://{host}:{port}/onvif/Ptz</tt:XAddr></tt:PTZ>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

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
          <tt:DefaultContinuousPanTiltVelocitySpace>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:DefaultContinuousPanTiltVelocitySpace>{zoom_space}
        </tt:PTZConfiguration>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

MOCK_RESPONSES = {
    "GetDeviceInformation": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><SOAP-ENV:Body><tds:GetDeviceInformationResponse><tds:Manufacturer>Fake-Camera</tds:Manufacturer><tds:Model>Proxy-01</tds:Model><tds:FirmwareVersion>1.0</tt:FirmwareVersion><tds:SerialNumber>12345</tds:SerialNumber><tds:HardwareId>1.0</tds:HardwareId></tds:GetDeviceInformationResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>""",
    "GetNodes": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver10/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><SOAP-ENV:Body><tptz:GetNodesResponse><tptz:PTZNode token="Node_01"><tt:Name>Node1</tt:Name><tt:SupportedPTZSpaces/></tptz:PTZNode></tptz:GetNodesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>""",
    "GetConfigurations": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver10/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><SOAP-ENV:Body><tptz:GetConfigurationsResponse><tptz:PTZConfiguration token="PTZ_CONF_01"><tt:Name>Config1</tt:Name><tt:NodeToken>Node_01</tt:NodeToken></tptz:PTZConfiguration></tptz:GetConfigurationsResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>""",
    "GetPresets": """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver10/ptz/wsdl"><SOAP-ENV:Body><tptz:GetPresetsResponse></tptz:GetPresetsResponse></s:Body></s:Envelope>"""
}

class FrigateOnvifProxy(http.server.BaseHTTPRequestHandler):
    listen_port = 8891
    target_ptz_url = "http://172.18.0.154:8899/onvif/Ptz"
    enable_zoom = False

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        if any(cmd in body for cmd in ["ContinuousMove", "Stop", "AbsoluteMove", "RelativeMove"]):
            self.forward_to_real_camera(body)
        elif "GetCapabilities" in body:
            self.send_xml(CAPABILITIES_XML.format(host=self.headers.get('Host'), port=self.listen_port))
        elif "GetProfiles" in body:
            zoom_str = "\n          <tt:DefaultContinuousZoomVelocitySpace>http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace</tt:DefaultContinuousZoomVelocitySpace>" if self.enable_zoom else ""
            self.send_xml(GET_PROFILES_XML.format(zoom_space=zoom_str))
        else:
            self.match_and_mock(body)

    def forward_to_real_camera(self, body):
        print(f"[FORWARD] 正在提取速度并重组报文...")
        try:
            pan_match = re.search(r'x="([-+]?\d*\.?\d+)"', body)
            tilt_match = re.search(r'y="([-+]?\d*\.?\d+)"', body)

            x = pan_match.group(1) if pan_match else "0"
            y = tilt_match.group(1) if tilt_match else "0"

            print(f"[DEBUG] 提取到速度: x={x}, y={y}")

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

            headers = {
                "Content-Type": "application/soap+xml; charset=utf-8",
                "SOAPAction": action
            }

            req = urllib.request.Request(
                self.target_ptz_url,
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
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>')

    def match_and_mock(self, body):
        for key, xml in MOCK_RESPONSES.items():
            if key in body:
                print(f"[MOCK] 命中请求: {key}")
                self.send_xml(xml)
                return

        print(f"[DEBUG] 未捕获请求 (可能需要模拟): {body[:80]}...")
        empty_soap = """<?xml version="1.0" encoding="utf-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body/></SOAP-ENV:Envelope>"""
        self.send_xml(empty_soap)

    def send_xml(self, xml_string):
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(xml_string)))
        self.end_headers()
        self.wfile.write(xml_string.encode('utf-8'))

class HTTPServer(http.server.HTTPServer):
    address_family = socket.AF_INET6
    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Frigate ONVIF PTZ PROXY")
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8891
    )
    parser.add_argument(
        "-t", "--target",
        type=str,
        default="http://172.18.0.154:8899/onvif/Ptz"
    )
    parser.add_argument(
        "-z", "--zoom",
        action="store_true"
    )

    args = parser.parse_args()

    FrigateOnvifProxy.listen_port = args.port
    FrigateOnvifProxy.target_ptz_url = args.target
    FrigateOnvifProxy.enable_zoom = args.zoom

    httpd = HTTPServer(('', args.port), FrigateOnvifProxy)
    print(f"ONVIF PROXY LISTENING: {args.port}")
    print(f"PTZ URL: {args.target}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
