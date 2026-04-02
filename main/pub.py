import os
import fcntl
import struct
import json
import time
from collections import deque
import paho.mqtt.client as mqtt

MQTT_BROKER = "10.1.7.143"
MQTT_PORT = 1884
MQTT_TOPIC = [
    "ads1115/c0",
    "ads1115/c1",
    "ads1115/c2",
    "ads1115/c3"
]
PUBLISH_INTERVAL = .5

I2C_SLAVE = 0x0703
ADS_ADDR = 0x48
CHANNEL_LIST = [0xC3, 0xD3, 0xE3, 0xF3]

def READ_CHANx(x: int):
    fd = os.open("/dev/i2c-1",os.O_RDWR)
    try:
        fcntl.ioctl(fd, I2C_SLAVE,ADS_ADDR)
        os.write(fd,bytes([0x01,CHANNEL_LIST[x],0x83]))
        import time
        time.sleep(0.01)
        os.write(fd, bytes([0x00]))
        res=os.read(fd,2)
        raw=struct.unpack(">h",res)[0]
        return (raw*4.096)/32767
    except:
        return 0.0
    finally:
        os.close(fd)

def on_connect(client, userdata, flags, rc):
    print("Connected to MQTT broker")

print("Starting ADS1115 publisher...")
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.connect(MQTT_BROKER, MQTT_PORT, 60)

try:
    while True:
        for i in range(3):
            data = READ_CHANx(i)
            payload = json.dumps({MQTT_TOPIC[i]: data})
            client.publish(MQTT_TOPIC[i], payload)
            time.sleep(PUBLISH_INTERVAL)
        
except KeyboardInterrupt:
    print("Exiting...")
finally:    client.disconnect()
