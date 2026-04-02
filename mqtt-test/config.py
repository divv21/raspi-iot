# config.py — edit this file to match your setup

# ── MQTT broker ───────────────────────────────────────────────────────────────
BROKER    = "localhost"   # IP or hostname of your MQTT broker
PORT      = 1883          # 1883 = plain,  8883 = TLS
USERNAME  = ""            # leave empty if no auth
PASSWORD  = ""

# ── Topic ─────────────────────────────────────────────────────────────────────
TOPIC_ROOT = "adc"        # messages go to  adc/ch0 … adc/ch3

# ── ADS1115 hardware ──────────────────────────────────────────────────────────
ADC_BUS     = 1           # /dev/i2c-1
ADC_ADDR    = 0x48
ADC_GAIN    = 1           # 1 = ±4.096 V  (see protocol.py GAIN_TABLE)
ADC_RATE    = 128         # samples per second: 8 16 32 64 128 250 475 860

# ── Burst settings ────────────────────────────────────────────────────────────
N_SAMPLES      = 16       # ADC readings packed into one MQTT message
BURST_INTERVAL = 0.5      # seconds between bursts (per channel)

# ── Channels to publish ───────────────────────────────────────────────────────
# Add / remove channel IDs (0–3).  Each entry needs a name, unit, and a
# scale function that converts raw ADC volts → physical value.
CHANNELS = {
    0: {"name": "sensor_0", "unit": "V",  "scale": lambda v: v},
    1: {"name": "sensor_1", "unit": "V",  "scale": lambda v: v},
    # 2: {"name": "temp",   "unit": "°C", "scale": lambda v: (v - 0.5) * 100},
    # 3: {"name": "pressure","unit": "Pa","scale": lambda v: v * 1000},
}

# ── CSV output (subscriber) ───────────────────────────────────────────────────
DATA_DIR = "data"         # folder where CSV files are saved