"""
mqtt_client.py
==============
Pequeño envoltorio sobre 'paho-mqtt' para publicar mediciones por MQTT.

¿Qué es MQTT? Es un protocolo de mensajería liviano, pensado para IoT.
Funciona como un sistema de "diario mural" (broker):
  - El NODO publica mensajes en un "tema" (topic), ej: "vitiscience/nodo1/clima".
  - Cualquier interesado (un servidor, un dashboard) se SUSCRIBE a ese topic
    y recibe los mensajes.
El nodo y el suscriptor nunca se hablan directo: el broker hace de intermediario.
Esto es ideal para 5G porque tolera cortes de conexión y reconexiones.
"""

import json
import time
import paho.mqtt.client as mqtt


class MqttPublisher:
    def __init__(self, broker: str, port: int, topic: str,
                 client_id: str = "nodo-vitiscience", keepalive: int = 60):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.keepalive = keepalive
        # client_id debe ser único por dispositivo en el broker.
        self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        self._connected = False

        # Callbacks: funciones que paho llama automáticamente cuando ocurre algo.
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        # rc == 0 significa conexión exitosa.
        self._connected = (rc == 0)
        estado = "OK" if rc == 0 else f"ERROR (codigo {rc})"
        print(f"[MQTT] Conexion a {self.broker}: {estado}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print("[MQTT] Desconectado del broker")

    def connect(self):
        """Abre la conexion con el broker y arranca el hilo de red de fondo."""
        self.client.connect(self.broker, self.port, self.keepalive)
        self.client.loop_start()  # maneja reconexiones automaticamente
        # Esperamos hasta 5 s a que confirme la conexion.
        for _ in range(50):
            if self._connected:
                break
            time.sleep(0.1)
        return self._connected

    def publish(self, payload: dict):
        """
        Publica un diccionario Python como JSON en el topic configurado.
        QoS=1 => 'al menos una vez': el broker confirma que recibio el mensaje.
        """
        mensaje = json.dumps(payload)
        result = self.client.publish(self.topic, mensaje, qos=1)
        result.wait_for_publish(timeout=5)
        ok = result.is_published()
        print(f"[MQTT] Publicado en '{self.topic}': {mensaje}  -> {'OK' if ok else 'FALLO'}")
        return ok

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()
