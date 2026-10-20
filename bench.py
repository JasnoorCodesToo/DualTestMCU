"""Core of the test bench: RP2040 serial link, test runner, SQLite persistence, reports.
No result is ever produced here; every record comes from a JSON line sent by the RP2040."""
import collections, csv, io, json, os, queue, sqlite3, threading
from datetime import datetime

DB_PATH = os.environ.get("TESTBENCH_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "testbench.db"))
TESTS = {  # id -> (protocol, title)
    "UART_1": ("UART", "Ping-pong"), "UART_2": ("UART", "Data integrity (echo)"),
    "UART_3": ("UART", "Repeated communication (100 msgs)"), "UART_4": ("UART", "Checksum-8 / error detection"),
    "UART_5": ("UART", "Latency (100 requests)"), "UART_6": ("UART", "CRC-16/CCITT-FALSE"),
    "SPI_1": ("SPI", "Basic data transfer"), "SPI_2": ("SPI", "Repeated communication (100 xfers)"),
    "SPI_3": ("SPI", "Data integrity patterns"),
}
UART_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)


class BenchError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg); self.status = status; self.msg = msg


def now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def sql(query, args=(), one=False, write=False):
    con = sqlite3.connect(DB_PATH)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(query, args)
        if write:
            con.commit(); return cur.lastrowid
        rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows
    finally:
        con.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, ended TEXT, status TEXT,
        tests TEXT, port TEXT, config TEXT);
    CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, run_id INTEGER, ts TEXT, protocol TEXT,
        test TEXT, case_id TEXT, tx TEXT, rx TEXT, exp TEXT, result TEXT, dur_us INTEGER, lat_us INTEGER, detail TEXT);
    CREATE INDEX IF NOT EXISTS ix_rec_run ON records(run_id);""")
    con.commit(); con.close()


class Bench:
    def __init__(self):
        self.ser = None; self.port = None; self.state = "disconnected"; self.error = None
        self.cfg = {}; self.hello = None; self.dut = "UART"
        self.running = False; self.stop_flag = False; self.run_id = None; self.progress = None
        self.wlock = threading.Lock(); self.hello_ev = threading.Event()
        self.cfg_ev = threading.Event(); self.done_ev = threading.Event()
        self.clients = []; self.cl = threading.Lock()
        self.events = collections.deque(maxlen=1000)  # recent events (used by the Streamlit log)

    # ---- events to dashboard (SSE) ----
    def pub(self, ev, **d):
        obj = dict(ev=ev, ts=now(), **d); self.events.append(obj)
        msg = json.dumps(obj)
        with self.cl:
            for c in self.clients: c.put(msg)

    def status(self):
        last = sql("SELECT MAX(ts) AS t FROM records", one=True)["t"]
        h = self.hello or {}
        return dict(state=self.state, error=self.error, port=self.port, dut=self.dut, running=self.running,
                    run_id=self.run_id, progress=self.progress, last_test=last,
                    firmware=h.get("fw"), pins=h.get("pins"), cfg=self.cfg or None)

    def _state(self, state, error=None):
        self.state = state; self.error = error; self.pub("status", status=self.status())

    # ---- connection ----
    def connect(self, port, uart_baud, spi_baud, spi_mode, dut):
        if self.state in ("connected", "connecting"): raise BenchError(409, "Already connected; disconnect first.")
        try:
            import serial
        except ImportError:
            raise BenchError(500, "pyserial is not installed (pip install -r requirements.txt).")
        self.port, self.dut, self.cfg, self.hello = port, dut, {}, None
        self._state("connecting")
        try:
            ser = serial.Serial(port, 115200, timeout=0.2, write_timeout=2)
        except Exception as e:
            self._state("error", f"Cannot open {port}: {e}. Close Thonny or any serial monitor using this port.")
            raise BenchError(502, self.error)
        self.ser = ser
        self.hello_ev.clear(); self.cfg_ev.clear()
        threading.Thread(target=self._reader, args=(ser,), daemon=True).start()
        self._send("HELLO")
        if not self.hello_ev.wait(3):
            return self._fail(ser, f"No 'hello' from RP2040 on {port}. Is firmware/rp2040/main.py saved as main.py on the board and running?")
        self._send(f"CFG {uart_baud} {spi_baud} {spi_mode}")
        if not self.cfg_ev.wait(3):
            return self._fail(ser, "RP2040 did not acknowledge the configuration (check values).")
        self._state("connected")

    def _fail(self, ser, msg):
        self.ser = None
        try: ser.close()
        except Exception: pass
        self._state("error", msg); raise BenchError(502, msg)

    def disconnect(self):
        if self.running: raise BenchError(409, "Stop the current run before disconnecting.")
        ser, self.ser = self.ser, None
        if ser:
            try: ser.close()
            except Exception: pass
        self.hello = None; self.cfg = {}; self._state("disconnected")

    def _send(self, cmd):
        if not self.ser: raise BenchError(409, "Hardware not connected.")
        with self.wlock:
            try: self.ser.write((cmd + "\n").encode()); self.ser.flush()
            except Exception as e:
                raise BenchError(502, f"Serial write failed: {e}")

    def _reader(self, ser):
        while self.ser is ser:
            try: line = ser.readline()
            except Exception as e:
                if self.ser is ser:
                    self.ser = None; self._state("error", f"Serial link lost: {e}"); self.done_ev.set()
                return
            txt = line.decode("utf-8", "replace").strip()
            if not txt: continue
            try: m = json.loads(txt); ev = m.get("ev")
            except Exception: self.pub("diag", msg=txt); continue
            if ev == "hello": self.hello = m; self.hello_ev.set()
            elif ev == "cfg": self.cfg = m; self.cfg_ev.set()
            elif ev == "case": self._record(m)
            elif ev == "done": self.done_ev.set()
            elif ev == "error": self.pub("diag", msg=str(m.get("msg")), level="error")

    # ---- records ----
    def _record(self, m):
        t = TESTS.get(m.get("test"))
        if not t or self.run_id is None:
            self.pub("diag", msg=f"Ignored out-of-run record: {m}", level="error"); return
        rec = dict(run_id=self.run_id, ts=now(), protocol=t[0], test=m["test"], case_id=str(m.get("case", "")),
                   tx=m.get("tx", ""), rx=m.get("rx", ""), exp=m.get("exp", ""), result=m.get("res", "FAIL"),
                   dur_us=m.get("dur_us"), lat_us=m.get("lat_us"), detail=m.get("det", ""))
        rec["id"] = sql("INSERT INTO records(run_id,ts,protocol,test,case_id,tx,rx,exp,result,dur_us,lat_us,detail)"
                        " VALUES(:run_id,:ts,:protocol,:test,:case_id,:tx,:rx,:exp,:result,:dur_us,:lat_us,:detail)",
                        rec, write=True)
        self.pub("record", rec=rec)

    # ---- runs ----
    def start(self, tests):
        if self.state != "connected": raise BenchError(409, "Hardware not connected.")
        if self.running: raise BenchError(409, "A test run is already in progress.")
        bad = [t for t in tests if t not in TESTS]
        if bad: raise BenchError(422, f"Unknown test id(s): {', '.join(bad)}")
        wrong = [t for t in tests if TESTS[t][0] != self.dut]
        if wrong:
            raise BenchError(409, f"DUT firmware is declared as {self.dut}; cannot run {', '.join(wrong)}. "
                                  f"Flash the matching ESP32 sketch and reconnect with the other DUT firmware selected.")
        cfg = dict(self.cfg, dut=self.dut, fw=(self.hello or {}).get("fw"))
        self.run_id = sql("INSERT INTO runs(started,status,tests,port,config) VALUES(?,?,?,?,?)",
                          (now(), "running", ",".join(tests), self.port, json.dumps(cfg)), write=True)
        self.running = True; self.stop_flag = False
        self.pub("run_start", run_id=self.run_id, tests=tests); self.pub("status", status=self.status())
        threading.Thread(target=self._run, args=(self.run_id, tests), daemon=True).start()
        return self.run_id

    def _run(self, run_id, tests):
        status = "completed"
        try:
            for i, t in enumerate(tests):
                if self.stop_flag: status = "stopped"; break
                self.progress = dict(test=t, index=i + 1, total=len(tests))
                self.pub("progress", **self.progress)
                self.done_ev.clear(); self._send(f"RUN {t}")
                if not self.done_ev.wait(120):
                    status = "error"; self.pub("diag", level="error", msg=f"Timeout: RP2040 did not finish {t}"); break
                if self.state != "connected": status = "error"; break
                if self.stop_flag: status = "stopped"; break
        except BenchError as e:
            status = "error"; self.pub("diag", level="error", msg=e.msg)
        finally:
            sql("UPDATE runs SET ended=?, status=? WHERE id=?", (now(), status, run_id), write=True)
            self.running = False; self.progress = None
            self.pub("run_end", run_id=run_id, status=status); self.pub("status", status=self.status())

    def stop(self):
        if not self.running: raise BenchError(409, "No test run in progress.")
        self.stop_flag = True; self._send("STOP")


# ---- queries / reports ----
def summary():
    r = sql("SELECT COUNT(*) n, SUM(result='PASS') p, SUM(result='FAIL') f, AVG(lat_us) lat, "
            "COUNT(lat_us) nl FROM records", one=True)
    n = r["n"] or 0
    return dict(total=n, passed=r["p"] or 0, failed=r["f"] or 0,
                success_rate=round(100.0 * (r["p"] or 0) / n, 1) if n else None,
                avg_latency_ms=round(r["lat"] / 1000.0, 3) if r["lat"] is not None else None, latency_samples=r["nl"])


def records(limit=500, protocol=None, result=None, run_id=None):
    w, a = [], []
    for col, v in (("protocol", protocol), ("result", result), ("run_id", run_id)):
        if v is not None: w.append(f"{col}=?"); a.append(v)
    return sql("SELECT * FROM records" + (" WHERE " + " AND ".join(w) if w else "") + " ORDER BY id DESC LIMIT ?",
               a + [max(1, min(limit, 5000))])


def _report_data(run_id):
    if run_id == "latest":
        r = sql("SELECT MAX(id) m FROM runs", one=True)["m"]
        run_id = r
    rows = sql("SELECT r.*, u.port, u.config, u.started FROM records r JOIN runs u ON u.id=r.run_id"
               + (" WHERE r.run_id=?" if run_id else "") + " ORDER BY r.id", (run_id,) if run_id else ())
    if not rows: raise BenchError(404, "No test records to export.")
    n = len(rows); p = sum(x["result"] == "PASS" for x in rows)
    lats = [x["lat_us"] for x in rows if x["lat_us"] is not None]
    s = dict(total=n, passed=p, failed=n - p, rate=round(100.0 * p / n, 1),
             avg_lat=round(sum(lats) / len(lats) / 1000.0, 3) if lats else None)
    return rows, s


def _text(h):
    try:
        b = bytes.fromhex(h or "")
        return "".join(chr(c) if 32 <= c < 127 else ("\\n" if c == 10 else "." ) for c in b)
    except ValueError:
        return ""


def _cfg_str(row):
    c = json.loads(row["config"] or "{}")
    return (f"port={row['port']} dut={c.get('dut')} fw={c.get('fw')} uart_baud={c.get('uart_baud')} "
            f"spi_clk_hz={c.get('spi_baud')} spi_mode={c.get('spi_mode')} spi_bits={c.get('bits')}")


def make_csv(run_id):
    rows, s = _report_data(run_id)
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["run_id", "run_started", "record_timestamp", "hardware_config", "protocol", "test_id", "test_case",
                "tx_hex", "tx_text", "rx_hex", "rx_text", "expected_hex", "expected_text", "result",
                "latency_ms", "duration_ms", "error_detail"])
    for r in rows:
        f = lambda v: "" if v is None else round(v / 1000.0, 3)
        w.writerow([r["run_id"], r["started"], r["ts"], _cfg_str(r), r["protocol"], r["test"], r["case_id"],
                    r["tx"], _text(r["tx"]), r["rx"], _text(r["rx"]), r["exp"], _text(r["exp"]), r["result"],
                    f(r["lat_us"]), f(r["dur_us"]), r["detail"]])
    w.writerow([]); w.writerow(["# summary", f"total={s['total']}", f"passed={s['passed']}", f"failed={s['failed']}",
                                f"success_rate_pct={s['rate']}", f"avg_latency_ms={s['avg_lat']}"])
    return out.getvalue().encode("utf-8-sig")


def make_pdf(run_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    rows, s = _report_data(run_id)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24,
                            title="Dual-MCU Communication Test Report")
    st = getSampleStyleSheet(); cell = ParagraphStyle("c", parent=st["Normal"], fontName="Courier", fontSize=6, leading=7)
    h = ParagraphStyle("h", parent=st["Normal"], fontName="Helvetica-Bold", fontSize=6.5, leading=8)
    P = lambda t, s_=cell: Paragraph(escape(str(t)), s_)
    cut = lambda t: t if len(t) <= 40 else t[:40] + "…"
    runs = sorted({r["run_id"] for r in rows})
    el = [Paragraph("Dual-MCU Communication Test Report", st["Title"]),
          Paragraph(escape(f"Generated {now()} | Runs: {', '.join(map(str, runs))} | Controller: RP2040 | DUT: ESP32"), st["Normal"]),
          Paragraph(escape(f"Total {s['total']} | Passed {s['passed']} | Failed {s['failed']} | Success rate {s['rate']}% | "
                           f"Avg latency {s['avg_lat'] if s['avg_lat'] is not None else 'n/a'} ms"), st["Normal"])]
    seen = set()
    for r in rows:
        if r["run_id"] not in seen:
            seen.add(r["run_id"]); el.append(Paragraph(escape(f"Run {r['run_id']} ({r['started']}): {_cfg_str(r)}"), st["Normal"]))
    el.append(Spacer(1, 8))
    data = [[P(x, h) for x in ("Run", "Time", "Test / case", "TX", "RX", "Expected", "Result", "Lat ms", "Dur ms", "Detail")]]
    for r in rows:
        col = "#1a7f37" if r["result"] == "PASS" else "#c62828"
        rs = ParagraphStyle("r", parent=h, textColor=colors.HexColor(col))
        ms = lambda v: "" if v is None else f"{v / 1000.0:.3f}"
        data.append([P(r["run_id"]), P(r["ts"][11:23]), P(f"{r['test']} {r['case_id']}"), P(cut(r["tx"])),
                     P(cut(r["rx"])), P(cut(r["exp"])), P(r["result"], rs), P(ms(r["lat_us"])), P(ms(r["dur_us"])),
                     P(r["detail"])])
    t = Table(data, repeatRows=1, colWidths=[22, 48, 92, 105, 105, 105, 36, 34, 34, 150])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d9dee5")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f5f7f9")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el.append(t); doc.build(el)
    return buf.getvalue()


init_db()
bench = Bench()
