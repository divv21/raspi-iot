"""
config.py  —  shared configuration

All values overridable via environment variables.
"""

import os

# ── Broker ────────────────────────────────────────────────────────────────────

BROKER    = os.getenv("MQTT_BROKER",   "broker.emqx.io")
PORT      = int(os.getenv("MQTT_PORT", "1883"))
USERNAME  = os.getenv("MQTT_USER",     "")
PASSWORD  = os.getenv("MQTT_PASS",     "")
USE_TLS   = os.getenv("MQTT_TLS",      "false").lower() == "true"
CA_CERT   = os.getenv("MQTT_CA_CERT",  "")
KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "30"))
QOS       = int(os.getenv("MQTT_QOS",        "1"))

TOPIC_ROOT = os.getenv("TOPIC_ROOT", "pi/adc")

# ── ADC hardware ──────────────────────────────────────────────────────────────

ADC_BUS     = int(os.getenv("ADC_BUS",          "1"))      # /dev/i2c-1
ADC_ADDR    = int(os.getenv("ADC_ADDR",         "0x48"), 16)
ADC_GAIN    = float(os.getenv("ADC_GAIN",       "1"))      # 1 = ±4.096V range
ADC_RATE    = int(os.getenv("ADC_RATE",         "128"))    # samples/sec

# ── Burst / buffer settings ───────────────────────────────────────────────────

N_SAMPLES       = int(os.getenv("N_SAMPLES",       "16"))   # samples per packet
BURST_INTERVAL  = float(os.getenv("BURST_INTERVAL","0.5"))  # min seconds between packets

# ── Channel definitions ───────────────────────────────────────────────────────
# scale_fn : callable(raw_voltage: float) -> physical_value: float
# alert_hi / alert_lo : optional thresholds for subscriber badge

def _passthrough(v): return round(v, 5)
def _lm35(v):        return round(v * 100, 2)           # LM35  10mV/°C
def _pct(v):         return round((v / 4.096) * 100, 1) # 0–4.096V → 0–100%
def _kpa(v):         return round((v - 0.5) * 62.5, 2)  # 0.5–4.5V → 0–250kPa

CHANNELS = {
    0: {"name": "Temperature", "unit": "°C",  "scale": _lm35,        "alert_hi": 40.0, "alert_lo": None},
    1: {"name": "Humidity",    "unit": "%",   "scale": _pct,         "alert_hi": 85.0, "alert_lo": 20.0},
    2: {"name": "Pressure",    "unit": "kPa", "scale": _kpa,         "alert_hi": 200.0,"alert_lo": None},
    3: {"name": "Raw voltage", "unit": "V",   "scale": _passthrough, "alert_hi": 3.5,  "alert_lo": None},
}

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR   = os.getenv("LOG_DIR",   "logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Subscriber / LabVIEW bridge ───────────────────────────────────────────────

LV_HOST      = os.getenv("LV_HOST",      "127.0.0.1")  # LabVIEW TCP listener
LV_PORT      = int(os.getenv("LV_PORT",  "9876"))
STALE_AFTER  = float(os.getenv("STALE_AFTER", "5.0"))
SPARKLINE_LEN= int(os.getenv("SPARKLINE_LEN", "60"))
DATA_DIR     = os.getenv("DATA_DIR",  "data")
SAVE_FLUSH   = int(os.getenv("SAVE_FLUSH", "10"))