"""
subscriber.py  —  PC / any machine with Python
Connects to MQTT broker, subscribes to one channel topic, prints live data.
Optionally saves every sample to a CSV file.

Usage:
    python subscriber.py          # prompts you to pick a channel
    python subscriber.py --ch 2   # subscribe to adc/ch2 directly
    python subscriber.py --ch 0 --save   # also save to CSV

Controls while running:
    Ctrl-C  →  stop
"""

import argparse, csv, os, signal, time, logging
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from protocol import unpack, CODE_TO_FSV
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s"
)
log = logging.getLogger("subscriber")


# ── CSV writer ────────────────────────────────────────────────────────────────

class CSVWriter:
    def __init__(self, channel_id: int):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        path     = Path(config.DATA_DIR) / f"ch{channel_id}_{ts}.csv"
        self._f  = open(path, "w", newline="")
        self._w  = csv.writer(self._f)
        self._w.writerow(["pi_timestamp_utc", "sample_idx", "channel",
                          "value", "raw_v", "seq_global", "seq_packet", "dt_us"])
        self.path  = path
        self.rows  = 0
        log.info(f"CSV → {path}")

    def write(self, pkt):
        for i, (val, raw, seq) in enumerate(zip(pkt.values, pkt.raw_v, pkt.seq)):
            ts = datetime.fromtimestamp(
                pkt.t_epoch + i * pkt.dt_us / 1e6, tz=timezone.utc
            ).isoformat()
            self._w.writerow([ts, i, f"ch{pkt.channel_id}",
                              val, raw, seq, pkt.seq_packet, pkt.dt_us])
            self.rows += 1
        if self.rows % 100 == 0:          # flush every 100 samples
            self._f.flush()

    def close(self):
        self._f.flush()
        self._f.close()
        log.info(f"CSV closed — {self.rows} rows saved to {self.path}")


# ── MQTT callbacks ────────────────────────────────────────────────────────────

csv_writer = None   # set in main() if --save was passed

def on_connect(client, userdata, flags, rc):
    if rc != 0:
        log.error(f"Connect failed rc={rc}")
        return
    channel = userdata["channel"]
    topic   = f"{config.TOPIC_ROOT}/ch{channel}"
    client.subscribe(topic, qos=1)
    log.info(f"Connected — subscribed to  {topic}")

def on_message(client, userdata, msg):
    try:
        pkt = unpack(msg.payload)
    except Exception as e:
        log.warning(f"Bad packet: {e}")
        return

    # last value in the burst
    last   = pkt.values[-1]
    mn, mx = min(pkt.values), max(pkt.values)
    fsv    = CODE_TO_FSV.get(pkt.gain_code, 4.096)
    ts     = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    print(
        f"[{ts}]  ch{pkt.channel_id}  "
        f"pkt#{pkt.seq_packet:>5}  "
        f"last={last:+.4f} V  "
        f"min={mn:+.4f}  max={mx:+.4f}  "
        f"n={pkt.n_samples}  dt={pkt.dt_us}µs  FSV=±{fsv}V"
    )

    if csv_writer:
        csv_writer.write(pkt)


def on_disconnect(client, userdata, rc):
    if rc != 0:
        log.warning(f"Unexpected disconnect rc={rc} — will reconnect")


# ── Channel prompt ────────────────────────────────────────────────────────────

def ask_channel() -> int:
    print("\nAvailable channels:")
    for ch_id, cfg in config.CHANNELS.items():
        print(f"  [{ch_id}]  AIN{ch_id}  —  {cfg['name']}  ({cfg['unit']})")
    while True:
        try:
            ch = int(input("\nEnter channel number: ").strip())
            if ch in config.CHANNELS:
                return ch
            print(f"  ✗  Choose from {list(config.CHANNELS.keys())}")
        except ValueError:
            print("  ✗  Enter a number")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global csv_writer

    parser = argparse.ArgumentParser(description="ADS1115 MQTT subscriber")
    parser.add_argument("--ch",   type=int, default=None, help="Channel 0–3")
    parser.add_argument("--save", action="store_true",    help="Save to CSV")
    args = parser.parse_args()

    channel = args.ch if args.ch is not None else ask_channel()
    if channel not in config.CHANNELS:
        print(f"Channel {channel} not in config.CHANNELS — check config.py")
        return

    if args.save:
        csv_writer = CSVWriter(channel)

    print(f"\nSubscribing to  {config.TOPIC_ROOT}/ch{channel}  "
          f"({config.CHANNELS[channel]['name']})  "
          f"— press Ctrl-C to stop\n")

    client = mqtt.Client(userdata={"channel": channel}, clean_session=True)
    if config.USERNAME:
        client.username_pw_set(config.USERNAME, config.PASSWORD)

    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    # graceful Ctrl-C
    def _stop(sig, frame):
        log.info("Stopping…")
        client.loop_stop()
        client.disconnect()
        if csv_writer:
            csv_writer.close()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    client.connect(config.BROKER, config.PORT, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()