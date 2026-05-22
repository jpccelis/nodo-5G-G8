"""
test_publicar.py — Prueba rapida de TODA la cadena, sin el bucle infinito.
Lee el sensor UNA vez y publica el resultado por MQTT. Ideal para el viernes:
si ves el mensaje llegar al broker, ya tienes sensor + MQTT + 5G funcionando.

Uso (desde la carpeta del proyecto):
    python3 scripts/test_publicar.py
"""
import sys, os, json
from datetime import datetime, timezone
import configparser

# Permitir importar los modulos que estan en src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from sensor_aht10 import AHT10
from mqtt_client import MqttPublisher

cfg = configparser.ConfigParser()
cfg.read("config.ini")

sensor = AHT10(bus_number=1)
t, h = sensor.read()
sensor.close()
print(f"Sensor -> {t} C, {h} %")

paquete = {
    "node_id": cfg.get("nodo", "node_id"),
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "temperatura_c": t,
    "humedad_pct": h,
}

mqtt = MqttPublisher(cfg.get("mqtt", "broker"),
                     cfg.getint("mqtt", "port"),
                     cfg.get("mqtt", "topic"),
                     client_id=cfg.get("nodo", "node_id"))
if mqtt.connect():
    mqtt.publish(paquete)
    mqtt.disconnect()
    print("Listo. Revisa el broker para ver el mensaje.")
else:
    print("No se pudo conectar al broker. Revisa internet/5G.")
