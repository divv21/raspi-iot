"""
protocol.py  —  shared binary packet definition

Imported by both publisher (Pi) and subscriber / LabVIEW bridge (PC).

Packet layout (all values big-endian to match LabVIEW defaults):
┌──────────┬──────┬───────────────────────────────────────────────┐
│ Offset   │ Size │ Field                                          │
├──────────┼──────┼───────────────────────────────────────────────┤
│  0       │  4 B │ MAGIC       0xADC1ADC1  (sync / frame marker) │
│  4       │  1 B │ channel_id  0–3                               │
│  5       │  1 B │ n_samples   number of samples in this packet  │
│  6       │  1 B │ gain_code   ADC gain setting used             │
│  7       │  1 B │ version     packet format version = 1         │
│  8       │  8 B │ t_epoch     float64  Unix UTC of sample[0]    │
│ 16       │  4 B │ dt_us       uint32   inter-sample µs          │
│ 20       │  4 B │ seq_packet  uint32   packet counter           │
│ 24       │  4 B │ seq_sample0 uint32   global sample counter    │
│          │      │             at sample[0]                      │
│ 28       │  n*8 │ values[]    float64  scaled physical values   │
│ 28+n*8   │  n*8 │ raw_v[]     float64  raw ADC voltages         │
│ 28+2n*8  │  n*4 │ seq[]       uint32   per-sample seq numbers   │
│ end-4    │  4 B │ crc32       CRC32 of all bytes before it      │
└──────────┴──────┴───────────────────────────────────────────────┘

Fixed header = 28 bytes.
Payload per sample = 8 + 8 + 4 = 20 bytes.
Total = 28 + n*20 + 4  →  for n=16: 28 + 320 + 4 = 352 bytes/packet.

LabVIEW Type Cast offset reference:
  [I32 @0]  magic       read as U32 big-endian
  [U8  @4]  channel_id
  [U8  @5]  n_samples
  [U8  @6]  gain_code
  [U8  @7]  version
  [DBL @8]  t_epoch     (LabVIEW: DBL = IEEE 754 float64)
  [U32 @16] dt_us
  [U32 @20] seq_packet
  [U32 @24] seq_sample0
  [DBL @28] values[0] … values[n-1]        (n doubles)
  [DBL @28+n*8] raw_v[0] … raw_v[n-1]     (n doubles)
  [U32 @28+2n*8] seq[0] … seq[n-1]        (n U32s)
  [U32 @ end-4] crc32
"""

import struct
import zlib
from dataclasses import dataclass
from typing import List

MAGIC   = 0xADC1_ADC1
VERSION = 1

# Gain code → (full-scale voltage, ADS1115 config bits PGA[2:0])
GAIN_TABLE = {
    0: (6.144, 0b000),   # ±6.144 V  (code 0)
    1: (4.096, 0b001),   # ±4.096 V  (code 1)
    2: (2.048, 0b010),   # ±2.048 V  (code 2)
    4: (1.024, 0b011),   # ±1.024 V  (code 3 → stored as code 3 below)
    8: (0.512, 0b100),   # ±0.512 V
   16: (0.256, 0b101),   # ±0.256 V
}
# Map gain multiplier → gain_code byte stored in packet
GAIN_TO_CODE = {1: 1, 2: 2, 4: 3, 8: 4, 16: 5, 0: 0}
CODE_TO_FSV  = {0: 6.144, 1: 4.096, 2: 2.048, 3: 1.024, 4: 0.512, 5: 0.256}

HEADER_FMT = ">IBBBB dIII"     # big-endian: 4+1+1+1+1 + 8 + 4+4+4 = 28 bytes
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 28
CRC_SIZE    = 4


@dataclass
class ADCPacket:
    channel_id:  int
    n_samples:   int
    gain_code:   int
    version:     int
    t_epoch:     float          # Unix UTC timestamp of sample[0]
    dt_us:       int            # inter-sample interval in microseconds
    seq_packet:  int            # packet counter (per channel)
    seq_sample0: int            # global sample counter at sample[0]
    values:      List[float]    # scaled physical values
    raw_v:       List[float]    # raw ADC voltages
    seq:         List[int]      # per-sample sequence numbers


def pack(pkt: ADCPacket) -> bytes:
    """Serialise an ADCPacket to bytes. Appends CRC32."""
    n = pkt.n_samples
    assert len(pkt.values) == n and len(pkt.raw_v) == n and len(pkt.seq) == n

    header = struct.pack(
        HEADER_FMT,
        MAGIC,
        pkt.channel_id,
        n,
        pkt.gain_code,
        pkt.version,
        pkt.t_epoch,
        pkt.dt_us,
        pkt.seq_packet,
        pkt.seq_sample0,
    )
    values_bytes = struct.pack(f">{n}d", *pkt.values)
    rawv_bytes   = struct.pack(f">{n}d", *pkt.raw_v)
    seq_bytes    = struct.pack(f">{n}I", *pkt.seq)

    payload = header + values_bytes + rawv_bytes + seq_bytes
    crc     = zlib.crc32(payload) & 0xFFFF_FFFF
    return payload + struct.pack(">I", crc)


def unpack(data: bytes) -> ADCPacket:
    """Deserialise bytes → ADCPacket. Validates magic and CRC32."""
    if len(data) < HEADER_SIZE + CRC_SIZE:
        raise ValueError(f"Packet too short: {len(data)} bytes")

    # CRC check
    body       = data[:-CRC_SIZE]
    crc_rx     = struct.unpack(">I", data[-CRC_SIZE:])[0]
    crc_calc   = zlib.crc32(body) & 0xFFFF_FFFF
    if crc_rx != crc_calc:
        raise ValueError(f"CRC mismatch: got {crc_rx:#010x}, expected {crc_calc:#010x}")

    # Header
    (magic, channel_id, n, gain_code, version,
     t_epoch, dt_us, seq_packet, seq_sample0) = struct.unpack_from(HEADER_FMT, data, 0)

    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic:#010x}")

    # Payload arrays
    off_v   = HEADER_SIZE
    off_r   = off_v + n * 8
    off_s   = off_r + n * 8

    values  = list(struct.unpack_from(f">{n}d", data, off_v))
    raw_v   = list(struct.unpack_from(f">{n}d", data, off_r))
    seq     = list(struct.unpack_from(f">{n}I", data, off_s))

    return ADCPacket(
        channel_id=channel_id, n_samples=n, gain_code=gain_code,
        version=version, t_epoch=t_epoch, dt_us=dt_us,
        seq_packet=seq_packet, seq_sample0=seq_sample0,
        values=values, raw_v=raw_v, seq=seq,
    )


def expected_size(n_samples: int) -> int:
    return HEADER_SIZE + n_samples * 20 + CRC_SIZE