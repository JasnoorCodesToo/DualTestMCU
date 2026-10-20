# Dual-MCU Communication Test Bench

RP2040 (MicroPython, test initiator) -> ESP32 (Arduino, DUT) over UART and SPI.
PC <-USB serial-> RP2040 only. The ESP32 needs no PC connection.

## Wiring (3.3 V logic, common GND)
| Signal | RP2040 | ESP32 |
|---|---|---|
| UART | TX GP28 -> | RX GPIO16 |
| UART | RX GP29 <- | TX GPIO17 |
| SPI SCK / MOSI / MISO / CS | GP18 / GP19 / GP16 / GP17 | GPIO18 / GPIO23 / GPIO19 / GPIO5 |

## One-time setup
1. RP2040: copy `firmware/rp2040/main.py` to the board as `main.py` (Thonny), then close Thonny (it holds the port).
2. ESP32, UART tests: flash `firmware/esp32/uart_dut/uart_dut.ino`.
3. ESP32, SPI tests: flash your existing SPI test 1 sketch (needs the ESP32SPISlave library). Tests 2 and 3 use the same slave behaviour.
4. `cd backend && python -m venv .venv && source .venv/bin/activate` (Windows: `.venv\Scripts\activate`)
5. `pip install -r ../requirements.txt`

## Run
    cd backend
    uvicorn app:app --host 127.0.0.1 --port 8000
Open http://127.0.0.1:8000, pick the RP2040 port, choose which ESP32 sketch is flashed, Connect.

## Deployment
Serial access requires the backend to run on the PC the RP2040 is plugged into. A cloud-hosted frontend cannot reach
a local serial port; to use one, keep this backend as the local bridge and expose only its HTTP API. Records are
stored in `backend/testbench.db` (override with `TESTBENCH_DB`).

## API
GET /api/ports, /api/status, /api/tests, /api/summary, /api/records, /api/runs, /api/events (SSE),
/api/report.csv, /api/report.pdf (optional `?run_id=N|latest`); POST /api/connect, /api/disconnect, /api/run, /api/stop.

## Hardware assumptions / not yet validated on hardware
- Nothing here has been run on your RP2040 or ESP32. First hardware run: UART Suite, then SPI Suite.
- Test logic is ported from your Thonny scripts. Your UART test 2 script had TX/RX pins swapped (GP29/GP28); this uses GP28 TX / GP29 RX like the others.
- SPI PASS/FAIL checks only the ESP32 reply seen on MISO. ESP32-side MOSI verification stays on its serial console.
- UART and SPI DUT sketches are separate (the SPI slave call blocks), so the declared DUT firmware limits which tests run.
- UART baud changes only reconfigure the RP2040; the ESP32 sketch must be set to the same rate.
- Latency = RP2040 round-trip (write start to full reply line), recorded for UART cases only.

## Streamlit version (alternative front end)
Same core (`backend/bench.py`), same firmware, same database. No FastAPI needed.

    pip install -r requirements-streamlit.txt
    cd backend
    streamlit run streamlit_app.py
Needs Streamlit 1.37+ (the page auto-refreshes every 2 s via `st.fragment`). Run either front end, not both at once
(they would compete for the serial port). Use the PDF button's two steps: "Prepare PDF report", then "Download PDF".
