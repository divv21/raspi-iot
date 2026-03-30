import os
import fcntl
import struct
import tkinter as tk
from collections import deque

I2C_SLAVE = 0x0703
ADS_ADDR = 0x48

def get_voltage():
    fd = os.open("/dev/i2c-1", os.O_RDWR)
    try:
        fcntl.ioctl(fd, I2C_SLAVE, ADS_ADDR)

        os.write(fd, bytes([0x01, 0xC3, 0x83]))

        import time
        time.sleep(0.01)
        os.write(fd, bytes([0x00]))
        res = os.read(fd, 2)
        raw = struct.unpack(">h", res)[0]

        return (raw * 4.096) / 32767
    except:
        return 0.0
    finally:
        os.close(fd)


class ADCGraphApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ADS1115")
        self.root.attributes('-fullscreen', True)
        self.root.configure(bg='black')

        # screen dimensions
        self.width = self.root.winfo_screenwidth()
        self.height = self.root.winfo_screenheight()

        # data storage for scrolling graph
        self.data = deque([0] * self.width, maxlen=self.width)

        #label for digital reading
        self.label = tk.Label(root, text="0.000V", fg="cyan", bg="black", font=("Arial", 60))
        self.label.pack(pady=20)

        # Canvas
        self.canvas = tk.Canvas(root, width=self.width, height=400, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.update_plot()

    def update_plot(self):
        voltage = get_voltage()
        self.data.append(voltage)

        self.canvas.delete("all")
        self.label.config(text=f"{voltage:.3f} V")

        #line graph
        points = []
        for x, v in enumerate(self.data):
            #scale 0-4V to canvas height 
            y = 400 - (v / 4.096 * 350) 
            points.append((x, y))

        if len(points) > 1:
            self.canvas.create_line(points, fill="deep purple", width=2)

        self.root.after(50, self.update_plot)

if __name__ == "__main__":
    root = tk.Tk()
    app = ADCGraphApp(root)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
