# LabVIEW VI Build Specification
# Pi ADC Binary MQTT Monitor
# ============================================================
# File: labview_spec/LABVIEW_BUILD_SPEC.md
#
# This document tells you exactly which LabVIEW functions to place,
# how to wire them, and what constants to set.
# Build order: packet parser first, then TCP receiver, then top-level VI.
# ============================================================


## 1. Packet structure reference (copy this onto a free diagram space)

HEADER (28 bytes, big-endian):
  [0..3]   U32   MAGIC       = 0xADC1ADC1
  [4]      U8    channel_id
  [5]      U8    n_samples   = N  (10–20)
  [6]      U8    gain_code
  [7]      U8    version     = 1
  [8..15]  DBL   t_epoch     (Unix UTC seconds of sample[0])
  [16..19] U32   dt_us       (inter-sample interval, microseconds)
  [20..23] U32   seq_packet
  [24..27] U32   seq_sample0

PAYLOAD (N × 20 bytes):
  [28 .. 28+N×8-1]     DBL[N]  values[]   (physical, scaled)
  [28+N×8 .. 28+2N×8-1] DBL[N]  raw_v[]
  [28+2N×8 .. 28+3N×4-1] U32[N]  seq[]

TRAILER (4 bytes):
  last 4 bytes: U32 CRC32 (zlib, big-endian)

Total bytes = 28 + N×20 + 4  →  N=16 gives 352 bytes


## 2. Sub-VI: "Parse Packet.vi"

### Connector pane
  Input:   raw bytes[]  (U8 array)
  Outputs: channel_id (U8), n_samples (U8), gain_code (U8),
           t_epoch (DBL), dt_us (U32), seq_packet (U32),
           values[] (DBL array), raw_v[] (DBL array), seq[] (U32 array),
           crc_ok (Boolean), error out

### Block diagram — wire in this order:

#### Step 1: CRC check
  1. Array Size  ← raw bytes[]
  2. Subtract    ← [Array Size output] - 4        → body_len
  3. Array Subset← raw bytes[], offset=0, length=body_len   → body[]
  4. Array Subset← raw bytes[], offset=body_len, length=4   → crc_rx_bytes[]
  5. Type Cast   ← crc_rx_bytes[], type=U32 (big-endian)    → crc_rx
  6. CRC-32      ← body[]  (Functions → Data Communication → Protocols → CRC)
                   Set polynomial to 0xEDB88320 (standard CRC32)
                   → crc_calc
  7. Equal?      ← crc_rx == crc_calc    → crc_ok

#### Step 2: Extract header scalars
  Use "Index Array" + "Type Cast" for each field.
  Shortcut: use ONE "Flatten To String" / "Unflatten From String" block.

  Place: Functions → Programming → String → "Unflatten From String"
    Wire: string input = raw bytes[] (cast U8[]→String via "Byte Array To String")
    Wire: type input   = a cluster constant with fields:
            U32 magic
            U8  channel_id
            U8  n_samples
            U8  gain_code
            U8  version
            DBL t_epoch
            U32 dt_us
            U32 seq_packet
            U32 seq_sample0
    Set byte order: Big-endian (right-click → Properties → Byte Order → Big-endian)
    Output: unbundle the cluster with "Unbundle By Name"

  Wire each cluster field to its output terminal.

#### Step 3: Extract payload arrays
  n = n_samples (from header cluster)

  values[]:
    1. Array Subset ← raw bytes[], offset=28, length=n×8   → vbytes[]
    2. Byte Array To String ← vbytes[]                     → vstr
    3. Unflatten From String ← vstr, type=DBL[0..0] (1-element DBL array)
       Set array size to n using a For Loop or reshape:
       Better approach:
         a. Split vbytes[] into chunks of 8 bytes (For Loop i=0 to n-1)
         b. Inside loop: Array Subset(vbytes[], i×8, 8) → chunk[]
         c. Byte Array To String → chunk_str
         d. Type Cast(chunk_str, DBL) → value_i
         e. Tunnel out → auto-index → values[]

  raw_v[]:  same as values[], offset = 28 + n×8

  seq[]:    same pattern but chunks of 4, Type Cast to U32
            offset = 28 + 2×n×8

#### Step 4: Reconstruct absolute timestamps
  Build a DBL array t_abs[] where:
    t_abs[i] = t_epoch + i × dt_us / 1,000,000

  For Loop (count = n_samples):
    i  = loop iteration terminal
    t_abs_i = t_epoch + (DBL(i) × DBL(dt_us) / 1,000,000.0)
    Auto-index tunnel → t_abs[]

  Add t_abs[] to output connector.

  Note: LabVIEW timestamp (LV epoch = 1 Jan 1904).
  To convert Unix epoch → LabVIEW timestamp:
    LV_ts = Unix_ts - 2,082,844,800   (seconds between 1904 and 1970)
  Use "Seconds To Date/Time" after subtracting the offset constant.


## 3. Sub-VI: "TCP Receive Packet.vi"

### Purpose
  Receives length-prefixed binary packets from subscriber.py TCP bridge.
  subscriber.py sends: [4 bytes big-endian length][N bytes raw packet]

### Inputs:  connection ID (from TCP Open Connection)
### Outputs: raw bytes[] (U8 array), error out

### Block diagram:
  1. TCP Read ← connection ID, bytes to read = 4, timeout = 5000ms  → len_bytes[]
  2. Type Cast← len_bytes[] → U32 (big-endian)                      → pkt_len
  3. TCP Read ← connection ID, bytes to read = pkt_len, timeout = 5000ms → raw bytes[]
  4. Wire raw bytes[] to output

  Wrap in error-handling: if error, close connection, return empty array.


## 4. Top-level VI: "ADC Monitor.vi"

### Front panel controls and indicators

  Controls:
    - "Subscriber IP"   String control    default "127.0.0.1"
    - "Subscriber Port" I32 control       default 9876
    - "Connect"         Boolean button    (latch action)
    - "Disconnect"      Boolean button
    - "Channel"         Ring/Enum         items: ch0 ch1 ch2 ch3
    - "Stop"            Boolean button    (STOP button, red)

  Indicators:
    - "Connection Status" LED (green=connected)
    - "Channel ID"        U8 indicator
    - "Packets Received"  U32 indicator
    - "Last Value"        DBL indicator   (with unit label)
    - "CRC Errors"        U32 indicator
    - "Waveform Chart"    Waveform Chart
        X-axis: absolute time (use t_abs[], see step 4c below)
        Y-axis: physical value
        Plot 1: values[] (scaled)
        Plot 2: raw_v[]  (optional overlay)
    - "Packet Info" Cluster indicator:
        seq_packet (U32), dt_us (U32), n_samples (U8), gain_code (U8)

### Block diagram — top-level loop structure

#### Outer sequence (Init → Run → Cleanup):

  FRAME 0: Init
    TCP Open Connection ← Subscriber IP, Subscriber Port → conn_id
    Wire conn_id to shift register

  FRAME 1: Main While Loop
    Loop condition: Stop button OR error

    Inside loop:
      a. Call "TCP Receive Packet.vi" ← conn_id → raw[]
      b. Check if raw[] is empty (Array Size = 0) → skip iteration
      c. Call "Parse Packet.vi" ← raw[] →
           channel_id, n_samples, t_epoch, dt_us, seq_packet,
           values[], raw_v[], t_abs[], crc_ok
      d. Check crc_ok:
           False → increment CRC Errors counter, continue
           True  →
             i.  Write channel_id → Channel ID indicator
             ii. Index Last element of values[] → Last Value indicator
             iii.Increment Packets Received
             iv. Build waveform for chart:

                 Build Waveform (Functions → Programming → Waveform → Build Waveform):
                   .t0  = t_abs[0] converted to LV timestamp
                        = Seconds To Date/Time(t_abs[0] - 2082844800)
                   .dt  = dt_us / 1,000,000.0  (DBL, seconds)
                   .Y   = values[]
                 Wire waveform → Waveform Chart (append mode)

             v.  Write seq_packet, dt_us, n_samples, gain_code to Packet Info

    Loop timing: no Wait inside — TCP Read blocks naturally.

  FRAME 2: Cleanup
    TCP Close Connection ← conn_id

### Waveform Chart configuration (right-click chart → Properties):
  - Update mode: Sweep (shows time scrolling)
  - X-axis: Absolute Time checked = YES
    This makes LabVIEW use the .t0 and .dt from the waveform,
    so the X-axis shows real clock time from the Pi timestamp —
    independent of when the packet arrived on the PC.
  - Y-axis: auto-scale OR set fixed range matching sensor unit
  - History length: 1000 points minimum

### Multiple channel display (optional enhancement):
  Replace single Waveform Chart with Mixed Signal Graph or
  add a Tab Control with one chart per channel.
  Use a Case Structure on channel_id to route packets to the
  correct chart's data queue (Enqueue Element).


## 5. Gain code → full-scale voltage lookup (for display)

  Use a Case Structure on gain_code:
    0 → 6.144 V
    1 → 4.096 V
    2 → 2.048 V
    3 → 1.024 V
    4 → 0.512 V
    5 → 0.256 V
  Display as "FSV: X.XXX V" string indicator.


## 6. File logging from LabVIEW (alternative to Python CSV)

  Inside the main While Loop, add a parallel branch:
    1. "Open/Create/Replace File" (once, in Init frame): path = channel_{timestamp}.csv
    2. Build string:
         Format Into String: "%s,%.6f,%.5f,%u\n"
           arguments: t_abs_i (formatted), values[i], raw_v[i], seq[i]
         Use a For Loop over n_samples to write each sample
    3. "Write to Text File" ← formatted string
    4. "Close File" in Cleanup frame


## 7. Testing without hardware

  Run subscriber.py on the PC with the Pi publisher running in mock mode.
  subscriber.py bridges packets to TCP 127.0.0.1:9876.
  In LabVIEW, connect to 127.0.0.1:9876.
  You should see waveform data immediately.

  To test the parser alone: use "Parse Packet.vi" test harness:
    1. Generate a test packet in Python: python3 -c "
         from protocol import *
         import struct, time
         pkt = ADCPacket(0,16,1,1,time.time(),7812,0,0,
                         [float(i) for i in range(16)],
                         [i/10000 for i in range(16)],
                         list(range(16)))
         b = pack(pkt)
         print(list(b))
       "
    2. Paste the byte list into a U8 array constant in LabVIEW
    3. Wire to "Parse Packet.vi" and probe all outputs


## 8. Checklist before running live

  [ ] subscriber.py running, LabVIEW forward enabled ([l] key)
  [ ] LabVIEW: Subscriber IP = 127.0.0.1, Port = 9876
  [ ] TCP Open Connection succeeds (Connection Status LED green)
  [ ] First packet received: probe raw[] size = 28 + N×20 + 4
  [ ] CRC Errors = 0
  [ ] Waveform chart X-axis shows clock time (not sample index)
  [ ] t0 on chart matches Pi system clock (within ~1s)