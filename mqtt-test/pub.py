"""
publisher.py  —  Raspberry Pi
Reads ADS1115 over I2C, packs N samples per channel, publishes over MQTT.

Topic per channel:  adc/ch0   adc/ch1   adc/ch2   adc/ch3

Usage:
    python publisher.py
"""

import os, fcntl, struct, time, signal, logging
import paho.mqtt.client as mqtt
from protocol import ADCPacket, pack, GAIN_TO_CODE, CODE_TO_FSV
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s"
)
log = logging.getLogger("publisher")

# ── ADS1115 constants ─────────────────────────────────────────────────────────

I2C_SLAVE  = 0x0703
DR_CODES   = {8:0, 16:1, 32:2, 64:3, 128:4, 250:5, 475:6, 860:7}

# Config high byte = OS(1) | MUX(AINx vs GND) | PGA | MODE(single-shot)
# MUX bits for single-ended: AIN0=100, AIN1=101, AIN2=110, AIN3=111
def _cfg_bytes(channel: int, gain: int, data_rate: int) -> tuple[int, int]:
    mux      = 0b100 | (channel & 0x3)           # 4..7
    pga      = GAIN_TO_CODE.get(gain, 1)          # 3-bit PGA field
    dr       = DR_CODES.get(data_rate, 4)         # 3-bit data-rate field
    high = (1 << 7) | (mux << 4) | (pga << 1) | 1   # OS=1, MODE=single
    low  = (dr << 5) | 0x03                           # DR, COMP_QUE=disabled
    return high, low


# ── ADC driver ────────────────────────────────────────────────────────────────

class ADS1115:
    def __init__(self, bus=1, address=0x48, gain=1, data_rate=128):
        self.address        = address
        self.gain           = gain
        self.data_rate      = data_rate
        self.full_scale_v   = CODE_TO_FSV[GAIN_TO_CODE.get(gain, 1)]
        self.gain_code_byte = GAIN_TO_CODE.get(gain, 1)
        self._dev           = f"/dev/i2c-{bus}"

    def read_voltage(self, channel: int) -> float:
        hi, lo = _cfg_bytes(channel, self.gain, self.data_rate)
        fd = os.open(self._dev, os.O_RDWR)
        try:
            fcntl.ioctl(fd, I2C_SLAVE, self.address)
            os.write(fd, bytes([0x01, hi, lo]))           # write config register
            time.sleep(1.0 / self.data_rate + 0.002)      # wait for conversion
            os.write(fd, bytes([0x00]))                    # point at conversion reg
            raw = struct.unpack(">h", os.read(fd, 2))[0]  # signed 16-bit
            return (raw * self.full_scale_v) / 32767.0
        finally:
            os.close(fd)

    def close(self): pass   # nothing to clean up


# ── Burst sampler ─────────────────────────────────────────────────────────────

def collect_burst(adc: ADS1115, channel_id: int, pkt_seq: int, smp_seq: int) -> bytes:
    """Read N_SAMPLES from one channel, return a packed binary blob."""
    cfg        = config.CHANNELS[channel_id]
    scale      = cfg["scale"]
    n          = config.N_SAMPLES
    values, raw_vs, seqs = [], [], []

    t0_wall = None
    t_prev  = None
    dt_us   = int(1_000_000 / config.ADC_RATE)

    for i in range(n):
        t_mono = time.monotonic()
        if i == 0:
            t0_wall = time.time()
        if t_prev is not None and i == 1:
            dt_us = max(1, int((t_mono - t_prev) * 1_000_000))

        raw_v = adc.read_voltage(channel_id)
        values.append(scale(raw_v))
        raw_vs.append(raw_v)
        seqs.append(smp_seq + i)
        t_prev = t_mono

        # pace to data rate
        if i < n - 1:
            sleep = max(0.0, (1.0 / config.ADC_RATE) - (time.monotonic() - t_mono))
            if sleep: time.sleep(sleep)

    pkt = ADCPacket(
        channel_id=channel_id, n_samples=n,
        gain_code=adc.gain_code_byte, version=1,
        t_epoch=t0_wall, dt_us=dt_us,
        seq_packet=pkt_seq, seq_sample0=seqs[0],
        values=values, raw_v=raw_vs, seq=seqs,
    )
    return pack(pkt)


# ── MQTT setup ────────────────────────────────────────────────────────────────

def build_client() -> mqtt.Client:
    client = mqtt.Client(clean_session=True)
    if config.USERNAME:
        client.username_pw_set(config.USERNAME, config.PASSWORD)

    client.on_connect    = lambda c, u, f, rc: (
        log.info(f"Broker connected") if rc == 0
        else log.error(f"Connect failed rc={rc}")
    )
    client.on_disconnect = lambda c, u, rc: (
        log.warning(f"Disconnected rc={rc}") if rc != 0 else None
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


# ── Main loop ─────────────────────────────────────────────────────────────────

running = True

def _stop(sig, frame):
    global running
    log.info("Stopping…")
    running = False

signal.signal(signal.SIGINT,  _stop)
signal.signal(signal.SIGTERM, _stop)


def main():
    adc    = ADS1115(config.ADC_BUS, config.ADC_ADDR, config.ADC_GAIN, config.ADC_RATE)
    client = build_client()

    log.info(f"Connecting to {config.BROKER}:{config.PORT} …")
    client.connect(config.BROKER, config.PORT, keepalive=60)
    client.loop_start()

    # per-channel packet and sample counters
    pkt_seqs = {ch: 0 for ch in config.CHANNELS}
    smp_seqs = {ch: 0 for ch in config.CHANNELS}

    log.info(f"Publishing channels {list(config.CHANNELS.keys())} "
             f"— {config.N_SAMPLES} samples/burst  rate={config.ADC_RATE} sps")

    while running:
        t_round = time.monotonic()

        for ch_id in config.CHANNELS:
            if not running:
                break
            try:
                blob  = collect_burst(adc, ch_id, pkt_seqs[ch_id], smp_seqs[ch_id])
                topic = f"{config.TOPIC_ROOT}/ch{ch_id}"
                client.publish(topic, blob, qos=1)
                log.info(f"ch{ch_id} → {len(blob)} B  pkt#{pkt_seqs[ch_id]}")
                pkt_seqs[ch_id] += 1
                smp_seqs[ch_id] += config.N_SAMPLES
            except Exception as e:
                log.error(f"ch{ch_id} error: {e}")

        # sleep remainder of burst interval
        gap = max(0.0, config.BURST_INTERVAL - (time.monotonic() - t_round))
        if gap:
            time.sleep(gap)

    client.disconnect()
    log.info("Publisher stopped")


if __name__ == "__main__":
    main()