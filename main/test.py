import paho.mqtt.client as paho

def on_connect(client, userdata, flags, rc):
    print('CONNACK received with code %d.' % (rc))

client = paho.Client(paho.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.connect('10.1.7.143', 1884)