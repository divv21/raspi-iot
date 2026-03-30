"""
subscriber.py  —  Remote PC

Receives binary burst packets from Pi over MQTT.
Provides:
  1. Rich TUI — channel select, start/stop, sparkline, stale/alert badges
  2. TCP bridge to LabVIEW — forwards raw binary packets on demand
  3. CSV save — unpacked samples with Pi timestamps

Controls:
  [1–4] select channel   [s] start stream   [x] stop
  [w]   toggle CSV save  [l] toggle LabVIEW forward   [q] quit

LabVIEW bridge:
  - Listens on LV_HOST:LV_PORT (default 127.0.0.1:9876)
  - When LabVIEW connects, subscriber forwards every packet for the
    active channel as raw bytes prefixed with a 4-byte big-endian length.
  - LabVIEW reads length, then reads exactly that many bytes, then
    Type Cast / Unflatten using the protocol layout in labview_spec/.
"""

import csv
import json
import logging
import logging.handlers
import os
import signal
import socket
import ssl
import struct
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from protocol import unpack, ADCPacket, CODE_TO_FSV

# ── Logger (file only) ────────────────────────────────────────────────────────

os.makedirs(config.LOG_DIR, exist_ok=True)

class _JsonFmt(logging.Formatter):
    def format(self, r):
        return json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                           "lvl": r.levelname, "msg": r.getMessage()})

def _make_log(name):
    log = logging.getLogger(name)
    log.setLevel(config.LOG_LEVEL)
    fh = logging.handlers.RotatingFileHandler(
        f"{config.LOG_DIR}/{name}.log", maxBytes=5<<20, backupCount=5)
    fh.setFormatter(_JsonFmt())
    log.addHandler(fh)
    return log

log = _make_log("subscriber")

# ── Sparkline ─────────────────────────────────────────────────────────────────

_SP = " ▁▂▃▄▅▆▇█"

def sparkline(vals: deque) -> str:
    if not vals: return "—"
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    return "".join(_SP[int((v-lo)/span*(len(_SP)-1))] for v in vals)

# ── Per-channel state ─────────────────────────────────────────────────────────

class ChanState:
    def __init__(self, ch_id: int):
        self.ch_id     = ch_id
        cfg            = config.CHANNELS[ch_id]
        self.name      = cfg["name"]
        self.unit      = cfg["unit"]
        self.alert_hi  = cfg.get("alert_hi")
        self.alert_lo  = cfg.get("alert_lo")

        self.last_pkt: Optional[ADCPacket] = None
        self.last_value: Optional[float]   = None
        self.pkt_count  = 0
        self.smp_count  = 0
        self.updated_mono: Optional[float] = None
        self.history: deque = deque(maxlen=config.SPARKLINE_LEN)

    @property
    def age(self) -> Optional[float]:
        if self.updated_mono is None: return None
        return time.monotonic() - self.updated_mono

    @property
    def is_stale(self) -> bool:
        a = self.age
        return a is not None and a > config.STALE_AFTER

    @property
    def alert(self) -> Optional[str]:
        v = self.last_value
        if v is None: return None
        if self.alert_hi is not None and v > self.alert_hi: return "HIGH"
        if self.alert_lo is not None and v < self.alert_lo: return "LOW"
        return None

    def ingest(self, pkt: ADCPacket):
        self.last_pkt   = pkt
        self.last_value = pkt.values[-1]
        self.pkt_count += 1
        self.smp_count += pkt.n_samples
        self.updated_mono = time.monotonic()
        for v in pkt.values:
            self.history.append(v)

# ── CSV writer ────────────────────────────────────────────────────────────────

class ChanCSV:
    def __init__(self, ch_id: int):
        self.ch_id    = ch_id
        self._f       = None
        self._w       = None
        self._rows    = 0
        self.filepath: Optional[Path] = None

    def open(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        ts            = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filepath = Path(config.DATA_DIR) / f"ch{self.ch_id}_{ts}.csv"
        self._f       = open(self.filepath, "w", newline="")
        self._w       = csv.writer(self._f)
        self._w.writerow(["pi_timestamp_utc", "sample_index", "channel",
                          "name", "value", "unit", "raw_v",
                          "seq_global", "seq_packet", "dt_us"])
        self._f.flush()

    def write_packet(self, pkt: ADCPacket, state: ChanState):
        if not self._w: return
        for i, (val, raw, seq) in enumerate(zip(pkt.values, pkt.raw_v, pkt.seq)):
            pi_ts = datetime.fromtimestamp(
                pkt.t_epoch + i * pkt.dt_us / 1e6, tz=timezone.utc
            ).isoformat()
            self._w.writerow([pi_ts, i, f"ch{pkt.channel_id}",
                              state.name, val, state.unit, raw,
                              seq, pkt.seq_packet, pkt.dt_us])
            self._rows += 1
        if self._rows % config.SAVE_FLUSH == 0:
            self._f.flush()

    def close(self):
        if self._f:
            self._f.flush(); self._f.close()
            self._f = None; self._w = None
            log.info(f"CSV closed ch{self.ch_id} rows={self._rows} path={self.filepath}")

# ── LabVIEW TCP bridge ────────────────────────────────────────────────────────

class LVBridge:
    """
    Listens for a single LabVIEW TCP connection.
    When connected, forwards binary packets as:
        [4 bytes big-endian length][N bytes raw packet]
    Thread-safe send via lock.
    """
    def __init__(self):
        self._lock   = threading.Lock()
        self._conn: Optional[socket.socket] = None
        self._active = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((config.LV_HOST, config.LV_PORT))
        srv.listen(1)
        srv.settimeout(1.0)
        log.info(f"LabVIEW bridge listening {config.LV_HOST}:{config.LV_PORT}")
        while True:
            try:
                conn, addr = srv.accept()
                log.info(f"LabVIEW connected from {addr}")
                with self._lock:
                    self._conn   = conn
                    self._active = True
            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"Bridge accept error: {e}")

    def send(self, raw_bytes: bytes) -> bool:
        with self._lock:
            if not self._active or self._conn is None:
                return False
            try:
                length = struct.pack(">I", len(raw_bytes))
                self._conn.sendall(length + raw_bytes)
                return True
            except Exception as e:
                log.warning(f"LabVIEW send failed: {e}")
                self._conn   = None
                self._active = False
                return False

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._active

# ── App state ─────────────────────────────────────────────────────────────────

CH_IDS = list(config.CHANNELS.keys())

class AppState:
    def __init__(self):
        self.lock      = threading.Lock()
        self.channels  = {i: ChanState(i) for i in CH_IDS}
        self.csvs      = {i: ChanCSV(i)   for i in CH_IDS}

        self.active_ch: Optional[int] = None
        self.streaming  = False
        self.saving     = False
        self.lv_forward = False

        self.pi_status  = "unknown"
        self.connected  = False

        self.stream_log: list[str] = []
        self.MAX_LOG    = 20
        self._shutdown  = False

    def add_log(self, line: str):
        self.stream_log.append(line)
        if len(self.stream_log) > self.MAX_LOG:
            self.stream_log.pop(0)

    def start_save(self, ch_id: int):
        self.csvs[ch_id].open()
        self.saving = True
        self.add_log(f"[green]Saving → {self.csvs[ch_id].filepath.name}[/green]")

    def stop_save(self, ch_id: int):
        fp = self.csvs[ch_id].filepath
        self.csvs[ch_id].close()
        self.saving = False
        if fp: self.add_log(f"[dim]Saved → {fp.name}[/dim]")

state = AppState()
lv    = LVBridge()

# ── MQTT callbacks ────────────────────────────────────────────────────────────

def on_connect(client, u, f, rc):
    if rc == 0:
        with state.lock: state.connected = True
        for ch_id in CH_IDS:
            client.subscribe(f"{config.TOPIC_ROOT}/ch{ch_id}", qos=config.QOS)
        client.subscribe(f"{config.TOPIC_ROOT}/status", qos=1)
        log.info("MQTT connected")
    else:
        log.error(f"MQTT connect failed rc={rc}")

def on_disconnect(client, u, rc):
    with state.lock: state.connected = False
    if rc != 0: log.warning(f"MQTT disconnect rc={rc}")

def on_message(client, u, msg):
    topic = msg.topic
    raw   = msg.payload

    # Status (JSON)
    if topic == f"{config.TOPIC_ROOT}/status":
        try:
            d = json.loads(raw)
            with state.lock: state.pi_status = d.get("status", "unknown")
        except Exception: pass
        return

    # Sensor packet (binary)
    ch_str = topic.split("/")[-1]   # "ch0" → 0
    try:
        ch_id = int(ch_str.replace("ch", ""))
    except ValueError:
        return

    try:
        pkt = unpack(raw)
    except Exception as e:
        log.warning(f"Bad packet on {topic}: {e}  len={len(raw)}")
        return

    with state.lock:
        if ch_id not in state.channels: return
        cs = state.channels[ch_id]
        cs.ingest(pkt)

        # CSV
        if state.saving and state.active_ch == ch_id:
            state.csvs[ch_id].write_packet(pkt, cs)

        # LabVIEW forward
        if state.lv_forward and state.active_ch == ch_id:
            lv.send(raw)

        # Stream log
        if state.streaming and state.active_ch == ch_id:
            ts_s  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            last  = pkt.values[-1]
            mn    = min(pkt.values)
            mx    = max(pkt.values)
            alert = cs.alert
            a_tag = f" [bold red]{alert}[/bold red]" if alert else ""
            state.add_log(
                f"[dim]{ts_s}[/dim]  "
                f"pkt#{pkt.seq_packet}  "
                f"[bold cyan]{last}[/bold cyan] {cs.unit}  "
                f"[dim]min={mn} max={mx}  "
                f"dt={pkt.dt_us}µs  raw={pkt.raw_v[-1]:.4f}V[/dim]{a_tag}"
            )

# ── MQTT thread ───────────────────────────────────────────────────────────────

def mqtt_thread(client: mqtt.Client):
    backoff = 1
    while not state._shutdown:
        try:
            client.connect(config.BROKER, config.PORT, keepalive=config.KEEPALIVE)
            client.loop_forever()
        except Exception as e:
            log.error(f"MQTT error: {e} — retry {backoff}s")
            with state.lock: state.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

def build_mqtt() -> mqtt.Client:
    cid    = f"sub-{uuid.uuid4().hex[:8]}"
    client = mqtt.Client(client_id=cid, clean_session=False)
    if config.USERNAME: client.username_pw_set(config.USERNAME, config.PASSWORD)
    if config.USE_TLS:
        ctx = ssl.create_default_context()
        if config.CA_CERT: ctx.load_verify_locations(config.CA_CERT)
        client.tls_set_context(ctx)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client

# ── Rich TUI ──────────────────────────────────────────────────────────────────

console = Console()

def _dot(ok, label, style_ok="green", style_no="red") -> Text:
    return Text(f"● {label}", style=style_ok if ok else style_no)

def build_header() -> Panel:
    t = Text()
    t.append("Pi ADC Binary Monitor   ", style="bold")
    with state.lock:
        t.append_text(_dot(state.connected,  "broker"))
        t.append("   ")
        t.append_text(_dot(state.pi_status == "online", f"pi {state.pi_status}"))
        t.append("   ")
        t.append_text(_dot(lv.connected, "labview", "cyan", "dim"))
    return Panel(t, box=box.MINIMAL, padding=(0,1))

def build_overview() -> Panel:
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold",
                expand=True, padding=(0,1))
    tbl.add_column("#",        width=3,  style="dim")
    tbl.add_column("Ch",       width=5)
    tbl.add_column("Sensor",   width=14)
    tbl.add_column("Last",     width=10, justify="right")
    tbl.add_column("Unit",     width=5)
    tbl.add_column("Sparkline",min_width=22)
    tbl.add_column("Age",      width=6,  justify="right")
    tbl.add_column("Pkts",     width=6,  justify="right", style="dim")
    tbl.add_column("Samples",  width=8,  justify="right", style="dim")
    tbl.add_column("Alert",    width=6)

    with state.lock:
        for i, ch_id in enumerate(CH_IDS, 1):
            cs     = state.channels[ch_id]
            active = (ch_id == state.active_ch)
            stale  = cs.is_stale
            age    = cs.age
            alert  = cs.alert

            tbl.add_row(
                Text(f"[{i}]", style="bold yellow" if active else "dim"),
                Text(f"ch{ch_id}", style="bold white" if active else "default"),
                Text(cs.name, style="dim" if stale else "default"),
                Text("—" if cs.last_value is None else str(cs.last_value),
                     style="dim" if stale else ("bold cyan" if active else "default")),
                Text(cs.unit, style="dim"),
                Text(sparkline(cs.history), style="dim" if stale else "blue"),
                Text(f"{age:.0f}s" if age else "—", style="red" if stale else "dim"),
                Text(str(cs.pkt_count), style="dim"),
                Text(str(cs.smp_count), style="dim"),
                Text(alert or "", style="bold red" if alert else "dim"),
            )
    return Panel(tbl, title="Channels", box=box.SIMPLE)

def build_stream() -> Panel:
    with state.lock:
        active    = state.active_ch
        streaming = state.streaming
        saving    = state.saving
        lv_fwd    = state.lv_forward
        lines     = list(state.stream_log)
        fp        = state.csvs[active].filepath.name if (
                        active is not None and state.csvs[active].filepath) else None

    if active is None:
        body = Text("Select a channel (1–4) then press 's'.", style="dim")
    elif not streaming:
        body = Text(f"ch{active} selected — press 's' to stream.", style="dim")
    else:
        body = Text.from_markup("\n".join(lines) if lines else "Waiting for packets…")

    badges = []
    if streaming:  badges.append("[green]● stream[/green]")
    if saving:     badges.append(f"[yellow]● save → {fp}[/yellow]")
    if lv_fwd:     badges.append("[cyan]● labview[/cyan]")
    title = f"Stream  ch{active}  " + "  ".join(badges) if active is not None else "Stream"
    return Panel(body, title=title,
                 border_style="green" if streaming else "default", height=16)

def build_help() -> Panel:
    cmds = ("[dim][1-4][/dim] select   [dim][s][/dim] start   [dim][x][/dim] stop   "
            "[dim][w][/dim] save   [dim][l][/dim] labview fwd   [dim][q][/dim] quit")
    return Panel(Text.from_markup(cmds, justify="center"), box=box.MINIMAL)

def build_layout() -> Layout:
    lo = Layout()
    lo.split_column(
        Layout(build_header(),   size=3),
        Layout(build_overview(), size=len(CH_IDS)+6),
        Layout(build_stream(),   size=16),
        Layout(build_help(),     size=3),
    )
    return lo

# ── Input loop ────────────────────────────────────────────────────────────────

def input_loop(live: Live):
    num_map = {str(i+1): ch for i, ch in enumerate(CH_IDS)}

    while not state._shutdown:
        try: cmd = input().strip().lower()
        except EOFError: break

        with state.lock:
            active = state.active_ch

        if cmd == "q":
            state._shutdown = True; break

        elif cmd == "s":
            with state.lock:
                if state.active_ch is None:
                    state.add_log("[yellow]Select a channel first[/yellow]")
                else:
                    state.streaming = True
                    state.stream_log.clear()
                    state.add_log(f"[green]Streaming ch{state.active_ch}[/green]")

        elif cmd == "x":
            with state.lock:
                state.streaming = False
                state.add_log("[dim]Stopped.[/dim]")

        elif cmd == "w":
            with state.lock:
                if state.active_ch is None:
                    state.add_log("[yellow]Select a channel first[/yellow]")
                elif not state.saving:
                    state.start_save(state.active_ch)
                else:
                    state.stop_save(state.active_ch)

        elif cmd == "l":
            with state.lock:
                state.lv_forward = not state.lv_forward
                status = "enabled" if state.lv_forward else "disabled"
                state.add_log(f"[cyan]LabVIEW forward {status}[/cyan]")

        elif cmd in num_map:
            new_ch = num_map[cmd]
            with state.lock:
                if state.saving and state.active_ch is not None:
                    state.stop_save(state.active_ch)
                state.active_ch = new_ch
                state.streaming = False
                state.stream_log.clear()
                cs = state.channels[new_ch]
                state.add_log(
                    f"[dim]ch{new_ch} — {cs.name}  "
                    f"last={cs.last_value} {cs.unit}[/dim]")

        live.refresh()

# ── Shutdown ──────────────────────────────────────────────────────────────────

def _sig(sig, frame):
    log.info(f"Signal {sig}")
    state._shutdown = True

signal.signal(signal.SIGINT,  _sig)
signal.signal(signal.SIGTERM, _sig)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Subscriber starting")
    client = build_mqtt()

    lv.start()

    mqtt_t = threading.Thread(target=mqtt_thread, args=(client,), daemon=True)
    mqtt_t.start()

    with Live(build_layout(), console=console, refresh_per_second=2,
              screen=True) as live:
        inp_t = threading.Thread(target=input_loop, args=(live,), daemon=True)
        inp_t.start()
        while not state._shutdown:
            live.update(build_layout())
            time.sleep(0.5)

    with state.lock:
        for ch_id, csv_w in state.csvs.items():
            if csv_w._f: csv_w.close()

    client.disconnect()
    log.info("Subscriber stopped")
    console.print("\n[dim]Bye.[/dim]")

if __name__ == "__main__":
    main()