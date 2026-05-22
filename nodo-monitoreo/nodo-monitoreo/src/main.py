"""
main.py  —  Firmware del Nodo de Monitoreo Ambiental (Grupo P8 / VitiScience)
============================================================================

Este es el "cerebro" del nodo. Implementa la maquina de estados (FSM) que
acordamos en el SRS:

   BOOT -> CHECK_CONNECTIVITY -> MEDIR -> PROCESAR -> TRANSMITIR -> SLEEP
                                                          |
                                                          v
                                                        ERROR

Idea general:
  - Cada cierto intervalo (ej: 15 min) el nodo despierta, mide el sensor,
    arma un paquete de datos, y lo transmite por MQTT (sobre 5G).
  - Si no hay conexion, GUARDA la medicion en un buffer local (un archivo).
    Cuando la conexion vuelve, manda primero lo guardado (store-and-forward).
  - Si algo falla, pasa al estado ERROR, registra el problema y reintenta.

Como es la primera version (POC del viernes), el "SLEEP" es un simple
time.sleep(). Mas adelante se puede optimizar el consumo de energia de verdad.
"""

import os
import sys
import json
import time
import enum
import socket
import configparser
from datetime import datetime, timezone

# Importamos nuestros propios modulos (los otros archivos del proyecto).
from sensor_aht10 import AHT10
from mqtt_client import MqttPublisher


# ---------- 1) Definicion de los estados de la FSM ----------
class Estado(enum.Enum):
    BOOT = "BOOT"
    CHECK_CONNECTIVITY = "CHECK_CONNECTIVITY"
    MEDIR = "MEDIR"
    PROCESAR = "PROCESAR"
    TRANSMITIR = "TRANSMITIR"
    SLEEP = "SLEEP"
    ERROR = "ERROR"


# ---------- 2) Utilidades ----------
def log(estado, mensaje):
    """Imprime con marca de tiempo y estado actual. Asi sabemos que pasa."""
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora}] [{estado.value:18}] {mensaje}", flush=True)


def cargar_config(ruta):
    """Lee config.ini. Si no existe, avisa y termina."""
    if not os.path.exists(ruta):
        print(f"ERROR: no encuentro el archivo de configuracion: {ruta}")
        print("Copia config.example.ini a config.ini y editalo.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(ruta)
    return cfg


def hay_internet(host="1.1.1.1", port=53, timeout=4):
    """
    Prueba simple de conectividad: intenta abrir un socket a un DNS publico.
    Si funciona, asumimos que hay salida a internet (via 5G en este proyecto).
    Devuelve True/False sin lanzar excepciones.
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except OSError:
        return False


# ---------- 3) Buffer local (store-and-forward) ----------
# Guardamos mediciones no enviadas en un archivo, una por linea (formato JSON).
def guardar_en_buffer(ruta_buffer, paquete):
    with open(ruta_buffer, "a") as f:
        f.write(json.dumps(paquete) + "\n")


def leer_buffer(ruta_buffer):
    if not os.path.exists(ruta_buffer):
        return []
    with open(ruta_buffer) as f:
        return [json.loads(linea) for linea in f if linea.strip()]


def vaciar_buffer(ruta_buffer):
    if os.path.exists(ruta_buffer):
        os.remove(ruta_buffer)


# ---------- 4) El nodo (la FSM en si) ----------
class Nodo:
    def __init__(self, cfg):
        self.cfg = cfg
        self.estado = Estado.BOOT

        # Parametros leidos del config.ini
        self.intervalo = cfg.getint("nodo", "intervalo_segundos", fallback=900)
        self.node_id = cfg.get("nodo", "node_id", fallback="nodo1")
        self.buffer_path = cfg.get("nodo", "buffer_path", fallback="buffer.jsonl")

        self.broker = cfg.get("mqtt", "broker")
        self.port = cfg.getint("mqtt", "port", fallback=1883)
        self.topic = cfg.get("mqtt", "topic")

        # Objetos que se crean en el BOOT
        self.sensor = None
        self.mqtt = None
        self.medicion_actual = None

    # ---- transiciones ----
    def boot(self):
        log(self.estado, "Inicializando sensor y cliente MQTT...")
        self.sensor = AHT10(bus_number=1)
        self.mqtt = MqttPublisher(self.broker, self.port, self.topic,
                                  client_id=self.node_id)
        self.estado = Estado.CHECK_CONNECTIVITY

    def check_connectivity(self):
        if hay_internet():
            log(self.estado, "Hay conexion a internet (5G OK).")
        else:
            log(self.estado, "SIN conexion. Igual mido y guardo en buffer.")
        # Pase lo que pase, siempre medimos. La decision de transmitir o
        # guardar se toma mas adelante.
        self.estado = Estado.MEDIR

    def medir(self):
        try:
            t, h = self.sensor.read()
            self.medicion_actual = {"temperatura_c": t, "humedad_pct": h}
            log(self.estado, f"Lectura -> {t} C, {h} %")
            self.estado = Estado.PROCESAR
        except Exception as e:
            log(self.estado, f"Fallo leyendo sensor: {e}")
            self.estado = Estado.ERROR

    def procesar(self):
        # Aqui se podrian agregar validaciones, promedios, calibraciones, etc.
        # Por ahora solo le agregamos metadatos (id del nodo y la hora UTC).
        paquete = {
            "node_id": self.node_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **self.medicion_actual,
        }
        self.medicion_actual = paquete
        log(self.estado, f"Paquete armado: {paquete}")
        self.estado = Estado.TRANSMITIR

    def transmitir(self):
        try:
            conectado = self.mqtt.connect()
            if not conectado:
                raise ConnectionError("no se pudo conectar al broker")

            # Primero mandamos lo que quedo guardado en el buffer (orden FIFO).
            pendientes = leer_buffer(self.buffer_path)
            if pendientes:
                log(self.estado, f"Enviando {len(pendientes)} mediciones del buffer...")
                for p in pendientes:
                    self.mqtt.publish(p)
                vaciar_buffer(self.buffer_path)

            # Luego mandamos la medicion actual.
            self.mqtt.publish(self.medicion_actual)
            self.mqtt.disconnect()
            self.estado = Estado.SLEEP
        except Exception as e:
            log(self.estado, f"No se pudo transmitir: {e}. Guardo en buffer.")
            guardar_en_buffer(self.buffer_path, self.medicion_actual)
            self.estado = Estado.SLEEP  # no es fatal: reintentaremos despues

    def sleep(self):
        log(self.estado, f"Durmiendo {self.intervalo} s hasta la proxima medicion.")
        time.sleep(self.intervalo)
        self.estado = Estado.CHECK_CONNECTIVITY  # vuelve al ciclo

    def error(self):
        log(self.estado, "Estado de error. Reinicio el sensor y reintento en 30 s.")
        try:
            if self.sensor:
                self.sensor.soft_reset()
        except Exception:
            pass
        time.sleep(30)
        self.estado = Estado.CHECK_CONNECTIVITY

    # ---- bucle principal ----
    def run(self):
        # Diccionario que mapea cada estado a la funcion que lo maneja.
        acciones = {
            Estado.BOOT: self.boot,
            Estado.CHECK_CONNECTIVITY: self.check_connectivity,
            Estado.MEDIR: self.medir,
            Estado.PROCESAR: self.procesar,
            Estado.TRANSMITIR: self.transmitir,
            Estado.SLEEP: self.sleep,
            Estado.ERROR: self.error,
        }
        log(self.estado, f"Nodo '{self.node_id}' arrancando. Ctrl+C para detener.")
        while True:
            acciones[self.estado]()


if __name__ == "__main__":
    # La ruta del config se puede pasar como argumento; por defecto config.ini
    ruta_cfg = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    config = cargar_config(ruta_cfg)
    nodo = Nodo(config)
    try:
        nodo.run()
    except KeyboardInterrupt:
        print("\nDetenido por el usuario. Hasta luego!")
