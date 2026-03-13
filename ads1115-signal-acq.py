# used this code to acquire signal data from the ads1115 for testing purposes. 
# we used a potentiometer connected to the A0 input of the ads1115 to generate a varying voltage signal.
# it reads the raw adc value and converts it to voltage, printing both values to the console every 0.5 seconds. 
# the code runs indefinitely until interrupted by the user (e.g. by pressing ctrl+c).

import smbus2
import time

# ads1115 default 12c address
ADDR = 0x48

#register addresses
POINTER_CONVERSION = 0x00
POINTER_CONFIG = 0x01

#config: single-ended A0, 4.096 V range, continuous mode
#0xC383 is a standard config for reading A0

bus = smbus.SMBus(1)
config = [0xC3, 0x83]
bus.write_i2c_block_data(ADDR, POINTER_CONFIG, config)

def read_adc():
    data = bus.read_i2c_block_data(ADDR, POINTER_CONVERSION, 2)
    value = (data[0] << 8) | data[1]
    if value > 32767:
        value -= 65536
    return value

try:
    while True:
        raw_value = read_adc()
        voltage = raw_value * (4.096 / 32768)
        print(f"raw value: {raw_value}, voltage: {voltage:.2f} V")
        time.sleep(0.5)
except KeyboardInterrupt:
    print("exiting...")


# to ensure hardware connections are correct, you can use the following command 
# to check if the ads1115 is detected on the i2c bus:
# i2cdetect -y 1