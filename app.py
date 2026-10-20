"""HTTP API for the Dual-MCU Communication Test Bench. Run: uvicorn app:app --host 127.0.0.1 --port 8000"""
import os, queue
from typing import List, Literal, Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

import bench as B

app = FastAPI(title="Dual-MCU Communication Test Bench")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.exception_handler(B.BenchError)
async def bench_error(_, e: B.BenchError):
    return JSONResponse({"detail": e.msg}, status_code=e.status)


class ConnectReq(BaseModel):
    port: str = Field(min_length=1)
    uart_baud: int = 115200
    spi_baud: int = Field(100000, ge=1000, le=10_000_000)
    spi_mode: int = Field(0, ge=0, le=3)
    dut: Literal["UART", "SPI"] = "UART"

    @field_validator("uart_baud")
    @classmethod
    def _baud(cls, v):
        if v not in B.UART_BAUDS: raise ValueError(f"uart_baud must be one of {B.UART_BAUDS}")
        return v


class RunReq(BaseModel):
    tests: List[str] = Field(min_length=1)


@app.get("/api/tests")
def tests():
    return [dict(id=k, protocol=v[0], title=v[1]) for k, v in B.TESTS.items()]


@app.get("/api/ports")
def ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        raise B.BenchError(500, "pyserial is not installed.")
    return [dict(device=p.device, description=p.description, likely_rp2040=(p.vid == 0x2E8A))
            for p in list_ports.comports()]


@app.get("/api/status")
def status():
    return B.bench.status()


@app.post("/api/connect")
def connect(r: ConnectReq):
    B.bench.connect(r.port, r.uart_baud, r.spi_baud, r.spi_mode, r.dut)
    return B.bench.status()


@app.post("/api/disconnect")
def disconnect():
    B.bench.disconnect(); return B.bench.status()


@app.post("/api/run")
def run(r: RunReq):
    return dict(run_id=B.bench.start(r.tests))


@app.post("/api/stop")
def stop():
    B.bench.stop(); return dict(stopping=True)


@app.get("/api/summary")
def summary():
    return B.summary()


@app.get("/api/records")
def records(limit: int = 500, protocol: Optional[Literal["UART", "SPI"]] = None,
            result: Optional[Literal["PASS", "FAIL"]] = None, run_id: Optional[int] = None):
    return B.records(limit, protocol, result, run_id)


@app.get("/api/runs")
def runs():
    return B.sql("SELECT * FROM runs ORDER BY id DESC LIMIT 200")


@app.get("/api/report.csv")
def report_csv(run_id: Optional[str] = Query(None, description="run number, 'latest', or omit for all runs")):
    rid = run_id if run_id == "latest" else (int(run_id) if run_id else None)
    return Response(B.make_csv(rid), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="dualmcu_report.csv"'})


@app.get("/api/report.pdf")
def report_pdf(run_id: Optional[str] = None):
    rid = run_id if run_id == "latest" else (int(run_id) if run_id else None)
    return Response(B.make_pdf(rid), media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="dualmcu_report.pdf"'})


@app.get("/api/events")
def events():
    q: "queue.Queue[str]" = queue.Queue(maxsize=2000)
    with B.bench.cl: B.bench.clients.append(q)

    def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try: yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty: yield ": keepalive\n\n"
        finally:
            with B.bench.cl:
                if q in B.bench.clients: B.bench.clients.remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
