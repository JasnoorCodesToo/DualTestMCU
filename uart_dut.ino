// ESP32 UART DUT: merges the existing UART sketches (tests 1, 2, 4, 5, 6) so one
// upload serves every UART test. UART2: RX=GPIO16, TX=GPIO17, 115200 8N1.
#include <Arduino.h>
HardwareSerial TestUART(2);

uint8_t sum8(const String &s) { uint16_t t = 0; for (size_t i = 0; i < s.length(); i++) t += (uint8_t)s[i]; return t & 0xFF; }
uint16_t crc16(const String &s) {  // CRC-16/CCITT-FALSE
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < s.length(); i++) {
    crc ^= (uint16_t)(uint8_t)s[i] << 8;
    for (int b = 0; b < 8; b++) crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}
bool parseHex(const String &t, unsigned long &v) {
  if (!t.length()) return false;
  char *e; v = strtoul(t.c_str(), &e, 16); return *e == '\0';
}

void setup() {
  Serial.begin(115200);
  TestUART.begin(115200, SERIAL_8N1, 16, 17);
  TestUART.setTimeout(200);
  Serial.println("ESP32 UART DUT ready");
}

void loop() {
  if (!TestUART.available()) return;
  String m = TestUART.readStringUntil('\n'); m.trim();
  if (m == "PING") { TestUART.println("PONG"); return; }
  if (m.startsWith("DATA:")) { TestUART.println(m); return; }   // echo
  int sep = m.lastIndexOf('|');
  if (sep < 0) { TestUART.println("UNKNOWN"); return; }
  String p = m.substring(0, sep), c = m.substring(sep + 1); unsigned long v;
  if (!parseHex(c, v)) { TestUART.println("ERROR"); return; }
  if (c.length() == 2) {                       // checksum-8 frames (tests 4, 5)
    bool ok = sum8(p) == v;
    if (p.startsWith("LATENCY:")) {
      if (!ok) { TestUART.println("ERROR"); return; }
      String r = "ACK:" + p.substring(8); uint8_t k = sum8(r);
      TestUART.print(r); TestUART.print("|");
      if (k < 16) TestUART.print("0");
      TestUART.println(k, HEX);
    } else TestUART.println(ok ? "PASS" : "FAIL");
  } else if (c.length() == 4) {                // CRC-16 frames (test 6)
    TestUART.println(crc16(p) == v ? "PASS" : "FAIL");
  } else TestUART.println("ERROR");
}
