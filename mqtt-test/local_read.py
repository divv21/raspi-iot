import os
import fcntl
import struct
import tkinter as tk
from tkinter import ttk
from collections import deque

I2C_SLAVE = 0x0703
ADS_ADDR  = 0x48

# MUX config byte for each single-ended channel (AINx vs GND)
# Bit layout of config high byte: [OS=1][MUX=1xx][PGA=001][MODE=1]
CHANNEL_CONFIG = {
    0: 0xC3,   # MUX = 100 → AIN0
    1: 0xD3,   # MUX = 101 → AIN1
    2: 0xE3,   # MUX = 110 → AIN2
    3: 0xF3,   # MUX = 111 → AIN3
}

def get_voltage(channel: int) -> float:
    config_high = CHANNEL_CONFIG.get(channel, 0xC3)

    fd = os.open("/dev/i2c-1", os.O_RDWR)
    try:
        fcntl.ioctl(fd, I2C_SLAVE, ADS_ADDR)

        # Write config register: point to register 0x01, send config bytes
        os.write(fd, bytes([0x01, config_high, 0x83]))

        import time
        time.sleep(0.01)  # wait for single-shot conversion (~8ms at 128SPS)

        # Point to conversion register and read 2 bytes
        os.write(fd, bytes([0x00]))
        res  = os.read(fd, 2)
        raw  = struct.unpack(">h", res)[0]   # signed 16-bit big-endian

        return (raw * 4.096) / 32767
    except Exception as e:
        print(f"I2C error: {e}")
        return 0.0
    finally:
        os.close(fd)


class ChannelSelectScreen(tk.Frame):
    """Initial screen — asks the user which channel to monitor."""

    def __init__(self, master, on_select):
        super().__init__(master, bg="black")
        self.pack(fill="both", expand=True)
        self.on_select = on_select

        tk.Label(self, text="ADS1115 Oscilloscope",
                 fg="cyan", bg="black",
                 font=("Arial", 48, "bold")).pack(pady=(80, 10))

        tk.Label(self, text="Select input channel:",
                 fg="white", bg="black",
                 font=("Arial", 24)).pack(pady=20)

        btn_frame = tk.Frame(self, bg="black")
        btn_frame.pack(pady=10)

        channel_labels = {
            0: "AIN0  (Channel 0)",
            1: "AIN1  (Channel 1)",
            2: "AIN2  (Channel 2)",
            3: "AIN3  (Channel 3)",
        }

        for ch, label in channel_labels.items():
            tk.Button(
                btn_frame,
                text=label,
                font=("Arial", 22),
                width=22,
                bg="#1a1a2e",
                fg="cyan",
                activebackground="#16213e",
                activeforeground="white",
                relief="flat",
                cursor="hand2",
                command=lambda c=ch: self.on_select(c),
            ).pack(pady=8)

        tk.Label(self, text="Press Esc to quit at any time",
                 fg="#555555", bg="black",
                 font=("Arial", 14)).pack(side="bottom", pady=20)


class ADCGraphApp(tk.Frame):
    """Live oscilloscope screen for a selected channel."""

    def __init__(self, master, channel: int, on_back):
        super().__init__(master, bg="black")
        self.pack(fill="both", expand=True)

        self.channel  = channel
        self.on_back  = on_back
        self.width    = master.winfo_screenwidth()
        self.data     = deque([0.0] * self.width, maxlen=self.width)

        # ── top bar ──────────────────────────────────────────────
        top = tk.Frame(self, bg="black")
        top.pack(fill="x", padx=20, pady=(10, 0))

        tk.Button(top, text="◀ Back", font=("Arial", 16),
                  bg="#1a1a2e", fg="cyan", relief="flat",
                  cursor="hand2", command=self.go_back).pack(side="left")

        tk.Label(top, text=f"Channel AIN{channel}",
                 fg="#888888", bg="black",
                 font=("Arial", 18)).pack(side="right")

        # ── voltage label ─────────────────────────────────────────
        self.label = tk.Label(self, text="0.000 V",
                              fg="cyan", bg="black",
                              font=("Arial", 60))
        self.label.pack(pady=10)

        # ── canvas ────────────────────────────────────────────────
        self.canvas = tk.Canvas(self, bg="black",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self._running = True
        self.update_plot()

    def update_plot(self):
        if not self._running:
            return

        voltage = get_voltage(self.channel)
        self.data.append(voltage)

        self.canvas.delete("all")
        self.label.config(text=f"{voltage:.3f} V")

        h = self.canvas.winfo_height() or 400

        # draw 0V and 4V grid lines
        for ref_v, color in [(0.0, "#1a1a1a"), (2.048, "#1a1a1a"), (4.096, "#1a1a1a")]:
            gy = h - (ref_v / 4.096 * (h - 50)) - 25
            self.canvas.create_line(0, gy, self.width, gy, fill=color, dash=(4, 6))

        # waveform
        points = []
        for x, v in enumerate(self.data):
            y = h - (v / 4.096 * (h - 50)) - 25
            points.append((x, max(0, min(h, y))))

        if len(points) > 1:
            self.canvas.create_line(points, fill="cyan", width=2)

        self.after(50, self.update_plot)

    def go_back(self):
        self._running = False
        self.destroy()
        self.on_back()


class App:
    """Root controller — switches between channel-select and graph screens."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ADS1115 Oscilloscope")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.show_selector()

    def show_selector(self):
        ChannelSelectScreen(self.root, on_select=self.show_graph)

    def show_graph(self, channel: int):
        # destroy selector, launch graph
        for widget in self.root.winfo_children():
            widget.destroy()
        ADCGraphApp(self.root, channel=channel, on_back=self.show_selector)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()