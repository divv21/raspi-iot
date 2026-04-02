import paho.mqtt.client as mqtt


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect('10.1.7.143', 1884)

print("select channel")
channel = input("enter channel number: c0/c1/c2/c3")
client.subscribe(f'ads1115/{channel}')
def on_message(client, userdata, msg):
    print(msg.topic+" "+str(msg.payload))
client.on_message = on_message
client.loop_forever()

