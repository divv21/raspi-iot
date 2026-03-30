"""
publisher.py  —  Raspberry Pi 4

Reads ADS1115 over bare-metal I2C, collects N samples per channel,
packs a binary burst packet (protocol.py), publishes over MQTT.

Topic: {TOPIC_ROOT}/ch{N}   e.g.  pi/adc/ch0

One packet per burst = N_SAMPLES ADC readings per MQTT message.
Timestamp of sample[0] embedded in packet header — LabVIEW uses this
for accurate X-axis plotting regardless of network latency.

No external ADC library — uses /dev/i2c-1 directly via ioctl.

Run:
    python publisher.py
    N_SAMPLES=20 ADC_RATE=860 python publisher.py
"""

import json
import logging
import logging.handlers
import os
import signal
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

import config
from ads1115 import open_adc
from protocol import ADCPacket, pack, GAIN_TO_CODE

# ── Logger ────────────────────────────────────────────────────────────────────

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
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(fh); log.addHandler(sh)
    return log

log = _make_log("publisher")

# ── Burst sampler ─────────────────────────────────────────────────────────────

class BurstSampler:
    """
    Collects exactly N_SAMPLES readings from one ADC channel,
    then returns a packed binary blob ready to publish.

    Timing:
      - Records wall-clock time of first sample (t0) as float64 UTC epoch.
      - Records monotonic time between each sample → dt_us (microseconds).
      - Both are embedded in the packet so LabVIEW can reconstruct
        the absolute time of every sample:
            t[i] = t_epoch + i * dt_us / 1e6
    """

    def __init__(self, adc, channel_id: int, n: int):
        self._adc     = adc
        self._ch      = channel_id
        self._n       = n
        self._cfg     = config.CHANNELS[channel_id]
        self._scale   = self._cfg["scale"]
        self._pkt_seq = 0
        self._smp_seq = 0

    def collect(self) -> bytes:
        values, raw_vs, seqs = [], [], []
        t0_wall = None
        t_prev  = None
        dt_us   = int(1_000_000 / config.ADC_RATE)   # nominal, updated below

        for i in range(self._n):
            t_mono = time.monotonic()
            t_wall = time.time()

            raw_v  = self._adc.read_voltage(self._ch)
            phys   = self._scale(raw_v)

            if i == 0:
                t0_wall = t_wall
            if t_prev is not None and i == 1:
                dt_us = max(1, int((t_mono - t_prev) * 1_000_000))

            values.append(phys)
            raw_vs.append(raw_v)
            seqs.append(self._smp_seq)
            self._smp_seq += 1
            t_prev = t_mono

            # Pace to ADC data rate (skip for last sample)
            if i < self._n - 1:
                elapsed = time.monotonic() - t_mono
                sleep   = max(0.0, (1.0 / config.ADC_RATE) - elapsed)
                if sleep > 0:
                    time.sleep(sleep)

        pkt = ADCPacket(
            channel_id  = self._ch,
            n_samples   = self._n,
            gain_code   = self._adc.gain_code_byte,
            version     = 1,
            t_epoch     = t0_wall,
            dt_us       = dt_us,
            seq_packet  = self._pkt_seq,
            seq_sample0 = seqs[0],
            values      = values,
            raw_v       = raw_vs,
            seq         = seqs,
        )
        self._pkt_seq += 1
        return pack(pkt)

# ── MQTT client ───────────────────────────────────────────────────────────────

_shutdown = False

def _on_connect(c, u, f, rc):
    if rc == 0:
        log.info(f"Broker connected {config.BROKER}:{config.PORT}")
        will = json.dumps({"status": "online",
                           "channels": list(config.CHANNELS.keys()),
                           "n_samples": config.N_SAMPLES,
                           "ts": datetime.now(timezone.utc).isoformat()})
        c.publish(f"{config.TOPIC_ROOT}/status", will, qos=1, retain=True)
    else:
        log.error(f"Connect failed rc={rc}")

def _on_disconnect(c, u, rc):
    if rc != 0:
        log.warning(f"Unexpected disconnect rc={rc}")

def _on_publish(c, u, mid):
    log.debug(f"ACK mid={mid}")

def _build_client() -> mqtt.Client:
    cid    = f"pi-pub-{uuid.uuid4().hex[:8]}"
    client = mqtt.Client(client_id=cid, clean_session=True)
    if config.USERNAME:
        client.username_pw_set(config.USERNAME, config.PASSWORD)
    if config.USE_TLS:
        ctx = ssl.create_default_context()
        if config.CA_CERT:
            ctx.load_verify_locations(config.CA_CERT)
        client.tls_set_context(ctx)
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_publish    = _on_publish
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    will = json.dumps({"status": "offline", "ts": datetime.now(timezone.utc).isoformat()})
    client.will_set(f"{config.TOPIC_ROOT}/status", will, qos=1, retain=True)
    return client

# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    global _shutdown

    adc = open_adc(
        bus       = config.ADC_BUS,
        address   = config.ADC_ADDR,
        gain      = config.ADC_GAIN,
        data_rate = config.ADC_RATE,
    )
    log.info(f"ADC ready — gain={config.ADC_GAIN}  rate={config.ADC_RATE} sps  "
             f"FSV=±{adc.full_scale_voltage:.3f}V")

    samplers = {
        ch: BurstSampler(adc, ch, config.N_SAMPLES)
        for ch in config.CHANNELS
    }

    client = _build_client()
    backoff = 1
    while not _shutdown:
        try:
            client.connect(config.BROKER, config.PORT, keepalive=config.KEEPALIVE)
            break
        except Exception as e:
            log.error(f"Connect failed: {e} — retry {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 60)

    client.loop_start()

    log.info(f"Publishing {config.N_SAMPLES} samples/burst on "
             f"{len(config.CHANNELS)} channels — topic root: {config.TOPIC_ROOT}")

    try:
        while not _shutdown:
            t_round = time.monotonic()

            for ch_id, sampler in samplers.items():
                if _shutdown:
                    break
                try:
                    blob  = sampler.collect()
                    topic = f"{config.TOPIC_ROOT}/ch{ch_id}"
                    info  = client.publish(topic, blob, qos=config.QOS)
                    info.wait_for_publish(timeout=5)
                    if info.is_published():
                        log.debug(f"ch{ch_id} → {len(blob)} bytes published")
                    else:
                        log.warning(f"ch{ch_id} publish not ACKed")
                except Exception as e:
                    log.error(f"ch{ch_id} error: {e}")

            elapsed = time.monotonic() - t_round
            gap     = max(0.0, config.BURST_INTERVAL - elapsed)
            if gap > 0:
                time.sleep(gap)

    finally:
        offline = json.dumps({"status": "offline",
                              "ts": datetime.now(timezone.utc).isoformat()})
        if client.is_connected():
            client.publish(f"{config.TOPIC_ROOT}/status", offline, qos=1, retain=True)
            time.sleep(0.3)
        client.loop_stop()
        client.disconnect()
        adc.close()
        log.info("Publisher stopped cleanly")

# ── Signal handling ───────────────────────────────────────────────────────────

def _sig(sig, frame):
    global _shutdown
    log.info(f"Signal {sig}")
    _shutdown = True

signal.signal(signal.SIGINT,  _sig)
signal.signal(signal.SIGTERM, _sig)

if __name__ == "__main__":
    log.info(f"Pi Binary Publisher starting — N={config.N_SAMPLES} "
             f"rate={config.ADC_RATE}sps broker={config.BROKER}")
    run()