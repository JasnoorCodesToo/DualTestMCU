# RP2040 test initiator (MicroPython). Save to the board as main.py.
# Host protocol over USB serial, one command per line:
#   HELLO | CFG <uart_baud> <spi_baud> <spi_mode> | RUN <TEST_ID> | STOP
# Replies are one JSON object per line. Test logic is taken from the existing
# Thonny scripts (UART 1-6, SPI 1-3); every result is measured on this board.
import sys, time, json, select
from machine import UART, SPI, Pin

FW = "dualmcu-rp2040-1.0"
uart = UART(0, baudrate=115200, bits=8, parity=None, stop=1, tx=Pin(28), rx=Pin(29))
cs = Pin(17, Pin.OUT, value=1)
spi = SPI(0, baudrate=100000, polarity=0, phase=0, bits=8, firstbit=SPI.MSB,
          sck=Pin(18), mosi=Pin(19), miso=Pin(16))
cfg = {"uart_baud": 115200, "spi_baud": 100000, "spi_mode": 0}
PINS = {"uart_tx": 28, "uart_rx": 29, "sck": 18, "mosi": 19, "miso": 16, "cs": 17}
poll = select.poll(); poll.register(sys.stdin, select.POLLIN)
STOP = False
GOLD = bytes([0xA5, 0x5A, 0x11, 0x22, 0x33, 0x44, 0x55, 0xAA])  # ESP32 SPI reply

def emit(**kw): sys.stdout.write(json.dumps(kw) + "\n")
def hx(b): return "" if b is None else "".join("%02X" % x for x in b)

def stopped():
    global STOP
    if not STOP and poll.poll(0):
        if sys.stdin.readline().strip() == "STOP": STOP = True
    return STOP

def diff(a, b):
    if a is None: return "no data"
    if len(a) != len(b): return "length %d, expected %d" % (len(a), len(b))
    d = ["[%d] got %02X exp %02X" % (i, a[i], b[i]) for i in range(len(a)) if a[i] != b[i]]
    return "mismatch " + "; ".join(d[:6]) + (" ..." if len(d) > 6 else "")

# ---- UART helpers ----
def flush():
    while uart.any(): uart.read()

def read_line(to):
    t = time.ticks_ms(); d = b""
    while time.ticks_diff(time.ticks_ms(), t) < to:
        n = uart.any()
        if n:
            d += uart.read(n)
            if b"\n" in d: return d.split(b"\n", 1)[0].strip()
        time.sleep_ms(1)
    return None

def uart_case(test, cid, frame, exp, to=2000):
    flush(); t = time.ticks_us(); uart.write(frame); rx = read_line(to)
    us = time.ticks_diff(time.ticks_us(), t)
    if rx is None: res, det = "FAIL", "timeout: no response within %d ms" % to
    elif rx == exp: res, det = "PASS", ""
    else: res, det = "FAIL", diff(rx, exp)
    emit(ev="case", test=test, case=cid, tx=hx(frame), rx=hx(rx), exp=hx(exp), res=res,
         dur_us=us, lat_us=None if rx is None else us, det=det)

def frame8(p, bad=False):
    c = sum(p) & 0xFF
    if bad: c = (c + 1) & 0xFF
    return p + b"|" + ("%02X" % c).encode() + b"\n"

def crc16(data):  # CRC-16/CCITT-FALSE
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

def t_u1(): uart_case("UART_1", "PING", b"PING\n", b"PONG")

def t_u2():
    d = b"DATA:RP2040_ESP32_12345"; uart_case("UART_2", "ECHO", d + b"\n", d)

def t_u3():
    for i in range(100):
        if stopped(): return
        m = ("DATA:%03d" % i).encode()
        uart_case("UART_3", "MSG_%03d" % i, m + b"\n", m, 1000); time.sleep_ms(20)

def t_u4():
    for p, bad in ((b"HELLO_ESP32", 0), (b"RP2040_DATA_12345", 0), (b"CORRUPTED_TEST", 1)):
        if stopped(): return
        uart_case("UART_4", p.decode() + (" (bad checksum)" if bad else ""),
                  frame8(p, bad), b"FAIL" if bad else b"PASS")

def t_u5():
    for n in range(1, 101):
        if stopped(): return
        s = ("%04d" % n).encode(); r = b"ACK:" + s
        exp = r + b"|" + ("%02X" % (sum(r) & 0xFF)).encode()
        uart_case("UART_5", "LAT_%04d" % n, frame8(b"LATENCY:" + s), exp, 500); time.sleep_ms(5)

def t_u6():
    for p, bad in ((b"HELLO_ESP32", 0), (b"RP2040_DATA_12345", 0), (b"CORRUPTED_TEST", 1)):
        if stopped(): return
        c = crc16(p) ^ (1 if bad else 0)
        uart_case("UART_6", p.decode() + (" (bad CRC)" if bad else ""),
                  p + b"|" + ("%04X" % c).encode() + b"\n", b"FAIL" if bad else b"PASS")
        time.sleep_ms(100)

# ---- SPI helpers (RP2040 = master, ESP32 = slave) ----
def spi_case(test, cid, tx):
    rx = bytearray(len(tx)); t = time.ticks_us()
    cs.value(0); time.sleep_us(10); spi.write_readinto(tx, rx); cs.value(1)
    us = time.ticks_diff(time.ticks_us(), t); rx = bytes(rx); ok = rx == GOLD
    emit(ev="case", test=test, case=cid, tx=hx(tx), rx=hx(rx), exp=hx(GOLD),
         res="PASS" if ok else "FAIL", dur_us=us, lat_us=None, det="" if ok else diff(rx, GOLD))

TX8 = bytes([0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80])

def t_s1(): time.sleep_ms(100); spi_case("SPI_1", "XFER_8B", TX8)

def t_s2():
    time.sleep(1)
    for i in range(1, 101):
        if stopped(): return
        spi_case("SPI_2", "XFER_%03d" % i, TX8); time.sleep_ms(10)

def t_s3():
    time.sleep(1)
    for name, p in (("ALL ZEROS", b"\x00" * 8), ("ALL ONES", b"\xFF" * 8),
                    ("ALTERNATING", b"\xAA\x55" * 4),
                    ("MIXED", bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0]))):
        if stopped(): return
        spi_case("SPI_3", name, p); time.sleep_ms(50)

TESTS = {"UART_1": t_u1, "UART_2": t_u2, "UART_3": t_u3, "UART_4": t_u4, "UART_5": t_u5,
         "UART_6": t_u6, "SPI_1": t_s1, "SPI_2": t_s2, "SPI_3": t_s3}

def run(t):
    global STOP
    STOP = False; f = TESTS.get(t)
    if f is None:
        emit(ev="error", msg="unknown test " + t)
    else:
        try: flush(); f()
        except Exception as e: emit(ev="error", msg="exception in %s: %r" % (t, e))
    emit(ev="done", test=t, stopped=STOP)

def hello(): emit(ev="hello", fw=FW, pins=PINS, **cfg)

def configure(cmd):
    try:
        _, ub, sb, sm = cmd.split()
        ub, sb, sm = int(ub), int(sb), int(sm)
        if not (1200 <= ub <= 2000000 and 1000 <= sb <= 10000000 and 0 <= sm <= 3): raise ValueError("out of range")
        uart.init(baudrate=ub, bits=8, parity=None, stop=1)
        spi.init(baudrate=sb, polarity=sm >> 1, phase=sm & 1)
        cfg.update(uart_baud=ub, spi_baud=sb, spi_mode=sm)
        emit(ev="cfg", bits=8, **cfg)
    except Exception as e:
        emit(ev="error", msg="bad CFG: %r" % (e,))

while True:
    if poll.poll(100):
        c = sys.stdin.readline().strip()
        if c == "HELLO": hello()
        elif c.startswith("CFG "): configure(c)
        elif c.startswith("RUN "): run(c[4:])
