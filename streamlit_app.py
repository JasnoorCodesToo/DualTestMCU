"""Streamlit front end for the Dual-MCU Communication Test Bench.
Run from this folder:  streamlit run streamlit_app.py
Uses the same core (bench.py) as the FastAPI version: serial link to the RP2040, SQLite records, CSV/PDF export.
Every row shown comes from a record the RP2040 reported; nothing is generated here."""
import pandas as pd
import streamlit as st

import bench as B

st.set_page_config(page_title="Dual-MCU Communication Test Bench", layout="wide")
st.markdown("""<style>
.block-container{padding-top:1.2rem;max-width:1500px}h1{font-size:1.25rem!important;font-weight:600}
h3{font-size:.8rem!important;text-transform:uppercase;letter-spacing:.05em;color:#667085;border-bottom:1px solid #d9dee5;padding-bottom:4px}
[data-testid=stMetric]{border:1px solid #d9dee5;border-radius:3px;padding:6px 12px;background:#fff}
[data-testid=stMetricLabel]{color:#667085}[data-testid=stMetricValue]{font-size:1.4rem}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
</style>""", unsafe_allow_html=True)

b = B.bench
PASS_C, FAIL_C, ACC = "#1a7f37", "#c62828", "#0f6e7a"
STATE_COL = {"connected": ACC, "connecting": "#b7791f", "error": FAIL_C, "disconnected": "#98a2b3"}
ss = st.session_state
ss.setdefault("ports", None)


def ms(u): return None if u is None else round(u / 1000.0, 3)
def fmt(h, view): return B._text(h) if view == "Text" else " ".join(h[i:i + 2] for i in range(0, len(h), 2))


def act(fn, *a):
    try: fn(*a)
    except B.BenchError as e: st.error(e.msg)


def result_table(rows, view, spi):
    if not rows:
        st.caption("No tests executed."); return
    data = [{"Time": r["ts"][11:23], "Run": r["run_id"], "Test": r["test"], "Case": r["case_id"],
             "Transmitted": fmt(r["tx"], view), "Received": fmt(r["rx"], view), "Expected": fmt(r["exp"], view),
             "Result": r["result"], **({} if spi else {"Latency ms": ms(r["lat_us"])}),
             "Duration ms": ms(r["dur_us"]), "Detail": r["detail"]} for r in rows]
    df = pd.DataFrame(data)
    css = lambda v: f"color:{PASS_C};font-weight:600" if v == "PASS" else (f"color:{FAIL_C};font-weight:600" if v == "FAIL" else "")
    sty = getattr(df.style, "map", None) or df.style.applymap
    st.dataframe(sty(css, subset=["Result"]), use_container_width=True, hide_index=True, height=280)


def log_line(e, show):
    k = e["ev"]; t = e["ts"][11:23]
    if k == "record":
        r = e["rec"]
        if r["protocol"] in show and r["result"] in show:
            return (f"{t} {r['protocol']} {r['test']}/{r['case_id']} tx={fmt(r['tx'], 'Hex')} rx={fmt(r['rx'], 'Hex')} "
                    f"exp={fmt(r['exp'], 'Hex')} {r['result']} {ms(r['dur_us'])}ms" + (f" | {r['detail']}" if r["detail"] else ""))
        return None
    if k == "diag": return f"{t} RP2040: {e['msg']}" if (e.get("level") != "error" or "Errors" in show) else None
    if k == "run_start": return f"{t} Run {e['run_id']} started: {', '.join(e['tests'])}"
    if k == "run_end": return f"{t} Run {e['run_id']} {e['status']}"
    if k == "status" and e["status"].get("error") and "Errors" in show: return f"{t} ERROR {e['status']['error']}"
    return None


@st.cache_data(show_spinner=False)
def csv_bytes(scope, n): return B.make_csv(scope)


@st.fragment(run_every=2)
def dashboard():
    s = b.status(); connected = s["state"] == "connected"; busy = s["running"]
    # ---- header ----
    h1, h2, h3, h4 = st.columns([3, 1.2, 1.4, 2.4])
    h1.markdown("# Dual-MCU Communication Test Bench")
    h2.markdown(f"Controller: **RP2040**  \nDUT: **ESP32**")
    h3.markdown(f"<span class='dot' style='background:{STATE_COL[s['state']]}'></span>**{s['state'].capitalize()}**",
                unsafe_allow_html=True)
    h4.markdown(f"Last test: **{s['last_test'][:23].replace('T', ' ') if s['last_test'] else 'none'}**")

    # ---- summary ----
    m = B.summary(); c = st.columns(5)
    c[0].metric("Total tests executed", m["total"] if m["total"] else "No tests executed")
    c[1].metric("Passed tests", m["passed"] if m["total"] else "—")
    c[2].metric("Failed tests", m["failed"] if m["total"] else "—")
    c[3].metric("Success rate", f"{m['success_rate']} %" if m["total"] else "—")
    c[4].metric("Avg communication latency", f"{m['avg_latency_ms']} ms" if m["avg_latency_ms"] is not None else "—")

    left, right = st.columns(2)
    # ---- connection ----
    with left:
        st.markdown("### Hardware connection")
        if ss.ports is None or st.button("Rescan ports"):
            try:
                from serial.tools import list_ports
                ss.ports = [(p.device, f"{p.device} - {p.description}" + (" (RP2040)" if p.vid == 0x2E8A else ""))
                            for p in list_ports.comports()]
            except ImportError:
                ss.ports = []; st.error("pyserial is not installed.")
        lock = s["state"] in ("connected", "connecting")
        a, bb = st.columns(2)
        port = a.selectbox("Serial port", [p[0] for p in ss.ports], format_func=dict(ss.ports).get, disabled=lock,
                           placeholder="No serial ports found")
        ubaud = bb.selectbox("UART baud", B.UART_BAUDS, index=B.UART_BAUDS.index(115200), disabled=lock)
        a, bb, cc = st.columns(3)
        sclk = a.number_input("SPI clock (Hz)", 1000, 10_000_000, 100_000, 1000, disabled=lock)
        smode = bb.selectbox("SPI mode", [0, 1, 2, 3], disabled=lock)
        dut = cc.selectbox("DUT firmware (declared)", ["UART", "SPI"], disabled=lock,
                           help="Which ESP32 sketch is flashed: uart_dut.ino or your SPI slave sketch. It cannot be detected.")
        a, bb = st.columns(2)
        if a.button("Connect", type="primary", disabled=lock or not port, use_container_width=True):
            with st.spinner("Waiting for RP2040 …"):
                act(b.connect, port, int(ubaud), int(sclk), int(smode), dut)
            st.rerun(scope="fragment")
        if bb.button("Disconnect", disabled=s["state"] != "connected" or busy, use_container_width=True):
            act(b.disconnect); st.rerun(scope="fragment")
        if connected:
            cf, p = s["cfg"], s["pins"]
            st.code(f"Link: {s['port']}  firmware: {s['firmware']}\n"
                    f"Active: UART {cf['uart_baud']} 8N1 | SPI {cf['spi_baud']} Hz, mode {cf['spi_mode']}, {cf['bits']}-bit | DUT: {s['dut']}\n"
                    f"Pins: UART TX GP{p['uart_tx']} RX GP{p['uart_rx']} | SPI SCK GP{p['sck']} MOSI GP{p['mosi']} "
                    f"MISO GP{p['miso']} CS GP{p['cs']}", language=None)
        else:
            st.code("Hardware not connected." if not s["error"] else s["error"], language=None)
        st.caption("Form values apply only after the RP2040 acknowledges them.")

    # ---- execution ----
    with right:
        st.markdown("### Test execution")
        tests = B.TESTS
        sel = st.selectbox("Test", list(tests), format_func=lambda k: f"{k.replace('_', ' ')} - {tests[k][1]}")
        can = connected and not busy
        a, bb, cc = st.columns(3)
        if a.button("Run Selected Test", type="primary", disabled=not can, use_container_width=True):
            act(b.start, [sel])
        if bb.button("Run All Tests", disabled=not can, use_container_width=True):
            act(b.start, [k for k, v in tests.items() if v[0] == s["dut"]])
        if cc.button("Stop Test Run", disabled=not busy, use_container_width=True):
            act(b.stop)
        if busy and s["progress"]:
            pr = s["progress"]; st.progress((pr["index"] - 1) / pr["total"], text=f"Running {pr['test']} ({pr['index']}/{pr['total']})")
        else:
            st.caption("Idle. Run All Tests runs every test for the declared DUT firmware; Stop acts after the current case.")
        a, bb, cc = st.columns(3)
        if a.button("Clear Log", use_container_width=True, help="Clears the on-screen log only; stored records are kept."):
            b.events.clear()
        scope = bb.selectbox("Report scope", ["All stored runs", "Latest run"], label_visibility="collapsed")
        rid = "latest" if scope == "Latest run" else None
        n = m["total"]
        if not n:
            cc.button("Export CSV", disabled=True, use_container_width=True); st.caption("No test records to export.")
        elif busy:
            cc.button("Export CSV", disabled=True, use_container_width=True); st.caption("Exports are disabled while a run is in progress.")
        else:
            cc.download_button("Export CSV", csv_bytes(rid, n), "dualmcu_report.csv", "text/csv", use_container_width=True)
            if st.button("Prepare PDF report"):
                ss.pdf = (rid, n, B.make_pdf(rid))
            if ss.get("pdf") and ss.pdf[:2] == (rid, n):
                st.download_button("Download PDF", ss.pdf[2], "dualmcu_report.pdf", "application/pdf")

    # ---- protocol sections ----
    view = st.radio("Data view", ["Text", "Hex"], horizontal=True)
    for proto, first, label, pins in (
            ("UART", "UART_1", "UART testing", "RP2040 UART0 TX GP28 / RX GP29 <-> ESP32 UART2 RX GPIO16 / TX GPIO17"),
            ("SPI", "SPI_1", "SPI testing", "RP2040 = master (SPI0), ESP32 = slave (VSPI)")):
        st.markdown(f"### {label}"); st.caption(pins)
        ok = can and s["dut"] == proto
        a, bb, _ = st.columns([1, 1, 3])
        if a.button(f"Run {proto} Test", key=f"t{proto}", disabled=not ok, help=None if ok else f"DUT firmware is declared as {s['dut']}"):
            act(b.start, [first])
        if bb.button(f"Run {proto} Suite", key=f"s{proto}", disabled=not ok):
            act(b.start, [k for k, v in B.TESTS.items() if v[0] == proto])
        if proto == "SPI":
            cf = s["cfg"]
            st.caption((f"Active: {cf['spi_baud']} Hz, mode {cf['spi_mode']}, {cf['bits']}-bit, 8-byte frames. " if cf else "Configuration: not connected. ") +
                       "PASS/FAIL reflects the bytes read on MISO; ESP32-side MOSI verification is printed only on its serial console.")
        result_table(B.records(150, proto), view, proto == "SPI")

    # ---- log + performance ----
    l, p = st.columns(2)
    with l:
        st.markdown("### Live test log")
        show = st.multiselect("Filter", ["UART", "SPI", "PASS", "FAIL", "Errors"], default=["UART", "SPI", "PASS", "FAIL", "Errors"],
                              label_visibility="collapsed")
        lines = [x for x in (log_line(e, show) for e in list(b.events)) if x]
        with st.container(height=300):
            st.code("\n".join(lines[-300:]) or "No log entries since the backend started.", language=None)
    with p:
        st.markdown("### Performance and results")
        if m["total"]:
            pct = 100 * m["passed"] / m["total"]
            st.markdown(f"<div style='height:8px;border:1px solid #d9dee5;background:{FAIL_C}'><div style='height:100%;width:{pct}%;background:{PASS_C}'></div></div>"
                        f"<span style='color:{PASS_C}'>PASS {m['passed']}</span> &nbsp; <span style='color:{FAIL_C}'>FAIL {m['failed']}</span> &nbsp; of {m['total']} records",
                        unsafe_allow_html=True)
        else:
            st.caption("No tests executed.")
        pts = [ms(r["lat_us"]) for r in reversed(B.records(100)) if r["lat_us"] is not None]
        st.caption("UART round-trip latency, last 100 measured samples (ms)")
        if len(pts) >= 2: st.line_chart(pd.DataFrame({"latency ms": pts}), height=170, color=ACC)
        else: st.caption("Insufficient data (need at least 2 latency samples).")


dashboard()
