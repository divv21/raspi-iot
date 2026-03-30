"""
ads1115.py  —  bare-metal ADS1115 driver

Uses only the Linux kernel I2C character device (/dev/i2c-1) via
fcntl.ioctl — zero external dependencies beyond the Python stdlib.

ADS1115 register map (all 16-bit, big-endian):
  0x00  Conversion register  (read-only)
  0x01  Config register      (read/write)
  0x02  Lo-thresh register
  0x03  Hi-thresh register

Config register bits [15:0]:
  [15]    OS      1 = start single conversion (write); 1 = idle (read)
  [14:12] MUX     input mux
  [11:9]  PGA     programmable gain amplifier
  [8]     MODE    0 = continuous, 1 = single-shot
  [7:5]   DR      data rate
  [4]     COMP_MODE
  [3]     COMP_POL
  [2]     COMP_LAT
  [1:0]   COMP_QUE  11 = disable comparator

MUX codes (single-ended vs GND):
  AIN0: 0b100   AIN1: 0b101   AIN2: 0b110   AIN3: 0b111

PGA codes (full-scale range):
  0b000 = ±6.144 V    0b001 = ±4.096 V    0b010 = ±2.048 V
  0b011 = ±1.024 V    0b100 = ±0.512 V    0b101 = ±0.256 V

Data rate codes (samples/sec):
  0b000=8  0b001=16  0b010=32  0b011=64  0b100=128
  0b101=250 0b110=475 0b111=860
"""

import fcntl
import os
import struct
import time

# ── I2C ioctl constants ───────────────────────────────────────────────────────

I2C_SLAVE       = 0x0703
I2C_SMBUS       = 0x0720
I2C_SMBUS_READ  = 1
I2C_SMBUS_WRITE = 0
I2C_SMBUS_WORD_DATA = 2

# ── ADS1115 register addresses ────────────────────────────────────────────────

REG_CONVERSION = 0x00
REG_CONFIG     = 0x01

# ── MUX codes for single-ended measurements (vs GND) ─────────────────────────

MUX = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}

# ── PGA (gain) codes and corresponding full-scale voltage ────────────────────

PGA_CODE = {
    2/3: (0b000, 6.144),
    1:   (0b001, 4.096),
    2:   (0b010, 2.048),
    4:   (0b011, 1.024),
    8:   (0b100, 0.512),
   16:   (0b101, 0.256),
}

# ── Data rate codes and actual rates (samples/sec) ───────────────────────────

DR_CODE = {
    8:   0b000,
   16:   0b001,
   32:   0b010,
   64:   0b011,
  128:   0b100,
  250:   0b101,
  475:   0b110,
  860:   0b111,
}


class ADS1115:
    """
    Bare-metal ADS1115 driver over /dev/i2c-N.

    Usage:
        adc = ADS1115(bus=1, address=0x48, gain=1, data_rate=128)
        voltage = adc.read_voltage(channel=0)
        adc.close()

    Or as context manager:
        with ADS1115() as adc:
            v = adc.read_voltage(0)
    """

    def __init__(
        self,
        bus:       int   = 1,
        address:   int   = 0x48,
        gain:      float = 1,
        data_rate: int   = 128,
    ):
        self._bus_path = f"/dev/i2c-{bus}"
        self._addr     = address
        self._gain     = gain
        self._dr       = data_rate

        if gain not in PGA_CODE:
            raise ValueError(f"Invalid gain {gain}. Choose from {list(PGA_CODE)}")
        if data_rate not in DR_CODE:
            raise ValueError(f"Invalid data_rate {data_rate}. Choose from {list(DR_CODE)}")

        self._pga_bits, self._fsv = PGA_CODE[gain]
        self._dr_bits  = DR_CODE[data_rate]
        self._lsb      = self._fsv / 32768.0   # ADS1115 is 16-bit signed

        # Open I2C device and bind to slave address
        self._fd = os.open(self._bus_path, os.O_RDWR)
        fcntl.ioctl(self._fd, I2C_SLAVE, self._addr)

    # ── Low-level register I/O ────────────────────────────────────────────────

    def _write_reg(self, reg: int, value: int):
        """Write a 16-bit value to a register (big-endian, MSB first)."""
        hi = (value >> 8) & 0xFF
        lo =  value       & 0xFF
        os.write(self._fd, bytes([reg, hi, lo]))

    def _read_reg(self, reg: int) -> int:
        """Read a 16-bit register (big-endian)."""
        os.write(self._fd, bytes([reg]))
        raw = os.read(self._fd, 2)
        return struct.unpack(">H", raw)[0]

    # ── Single-shot conversion ────────────────────────────────────────────────

    def _build_config(self, channel: int) -> int:
        """
        Build the 16-bit config register value for a single-shot read
        on the given channel.
        """
        os_bit    = 1        # start conversion
        mux_bits  = MUX[channel]
        mode_bit  = 1        # single-shot
        comp_que  = 0b11     # disable comparator

        cfg = (
            (os_bit   << 15) |
            (mux_bits << 12) |
            (self._pga_bits << 9) |
            (mode_bit <<  8) |
            (self._dr_bits << 5) |
            comp_que
        )
        return cfg

    def _conversion_ready(self) -> bool:
        """OS bit goes high when conversion is complete."""
        return bool(self._read_reg(REG_CONFIG) & 0x8000)

    def _wait_conversion(self):
        """
        Poll OS bit until ready.
        Timeout = 2 × theoretical conversion time (1/data_rate seconds).
        """
        timeout = 2.0 / self._dr + 0.005     # generous upper bound
        deadline = time.monotonic() + timeout
        while not self._conversion_ready():
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"ADS1115 conversion timeout after {timeout*1000:.0f} ms"
                )
            time.sleep(0.0005)   # 0.5 ms poll interval

    def read_raw(self, channel: int) -> int:
        """
        Trigger a single-shot conversion and return the raw 16-bit
        signed integer from the conversion register.
        """
        if channel not in MUX:
            raise ValueError(f"Invalid channel {channel}. Must be 0–3.")
        cfg = self._build_config(channel)
        self._write_reg(REG_CONFIG, cfg)
        self._wait_conversion()
        raw_u = self._read_reg(REG_CONVERSION)
        # Convert unsigned 16-bit to signed (two's complement)
        return raw_u if raw_u < 32768 else raw_u - 65536

    def read_voltage(self, channel: int) -> float:
        """Return the calibrated voltage on a channel (float, volts)."""
        return self.read_raw(channel) * self._lsb

    @property
    def full_scale_voltage(self) -> float:
        return self._fsv

    @property
    def gain_code_byte(self) -> int:
        """Gain code to embed in the binary packet header."""
        from protocol import GAIN_TO_CODE
        return GAIN_TO_CODE.get(int(self._gain), 0)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Mock ADC (no hardware) ────────────────────────────────────────────────────

class MockADS1115:
    """
    Drop-in replacement for ADS1115 when running without hardware.
    Generates realistic slowly-drifting sine + noise signals.
    """
    import math as _math
    import random as _random

    def __init__(self, gain=1, data_rate=128, **kwargs):
        from protocol import GAIN_TO_CODE
        self._gain      = gain
        self._dr        = data_rate
        self._fsv       = 4.096 / gain if gain >= 1 else 6.144
        self._lsb       = self._fsv / 32768.0
        self.gain_code_byte = GAIN_TO_CODE.get(int(gain), 0)
        self.full_scale_voltage = self._fsv
        import math, random
        self._math   = math
        self._random = random

    def read_raw(self, channel: int) -> int:
        import math, random, time
        t     = time.time()
        base  = 0.5 + 0.3 * math.sin(t / 12 + channel * 1.5)
        noise = random.gauss(0, 0.005)
        v     = max(-self._fsv, min(self._fsv, base + noise))
        return int(v / self._lsb)

    def read_voltage(self, channel: int) -> float:
        return self.read_raw(channel) * self._lsb

    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def open_adc(bus=1, address=0x48, gain=1, data_rate=128) -> "ADS1115 | MockADS1115":
    """
    Try to open the real ADS1115. Fall back to MockADS1115 transparently.
    Returns an object with the same interface in both cases.
    """
    try:
        adc = ADS1115(bus=bus, address=address, gain=gain, data_rate=data_rate)
        # Quick sanity check — read channel 0 to confirm device responds
        adc.read_raw(0)
        return adc
    except Exception as e:
        import sys
        print(f"[ADS1115] Hardware not available ({e}) — using mock", file=sys.stderr)
        return MockADS1115(gain=gain, data_rate=data_rate)