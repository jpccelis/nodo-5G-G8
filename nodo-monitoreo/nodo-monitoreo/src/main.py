"""
main.py  —  Firmware del Nodo de Monitoreo Ambiental (Grupo P8 / VitiScience)


Maquina de estados (FSM) del nodo. Version con sensado y transmision
DESACOPLADOS y telemetria estadistica:

   BOOT -> CHECK_CONNECTIVITY -> MEDIR -> VALIDAR -> PROCESAR -> (decide)
                                   ^                                |
                                   |                                |-- toca TX --> TRANSMITIR -> SLEEP
                                   |                                |-- no toca --> SLEEP
                                   +-------------- SLEEP -----------+
                                                                    |
                                          (lectura/validacion mala) v
                                                                  ERROR

Idea general:
  - El nodo MIDE cada T_MEDIR segundos (default 10 s) y acumula las muestras
    en una ventana deslizante de las ultimas N lecturas.
  - Cada T_TRANSMITIR segundos (default 40 s) transmite por MQTT un mensaje
    con las muestras crudas (FIFO) Y el agregado estadistico (avg, std).
  - Si no hay conexion, GUARDA el agregado en un buffer local (store-and-forward).
    Cuando la conexion vuelve, manda primero lo guardado (orden FIFO).
  - Cada lectura pasa por un paso de VALIDACION (rango fisico + sanidad).
    Si una lectura es invalida, la FSM pasa al estado ERROR.
  - CHECK_CONNECTIVITY reporta la tecnologia de acceso celular (LTE, 5GNR, etc).

El "SLEEP" sigue siendo un time.sleep() (POC). El control de duty cycling
por GPIO del HAT se integra mas adelante.
"""

import os
import sys
import json
import time
import enum
import socket
import subprocess
import statistics
import configparser
from collections import deque
from datetime import datetime, timezone

# Importamos nuestros propios modulos (los otros archivos del proyecto).
from sensor_aht10 import AHT10
from mqtt_client import MqttPublisher


# ---------- 1) Definicion de los estados de la FSM ----------
class Estado(enum.Enum):
    BOOT = "BOOT"
    CHECK_CONNECTIVITY = "CHECK_CONNECTIVITY"
    MEDIR = "MEDIR"
    VALIDAR = "VALIDAR"
    PROCESAR = "PROCESAR"
    TRANSMITIR = "TRANSMITIR"
    SLEEP = "SLEEP"
    ERROR = "ERROR"


# ---------- 2) Limites de validacion (rango fisico del AHT10) ----------
# El AHT10 mide temperatura de -40 a +85 C y humedad de 0 a 100 %.
# Para el contexto agricola exterior acotamos a un rango sensato; cualquier
# lectura fuera de estos limites se considera invalida (sensor o I2C con fallo).
TEMP_MIN_C = -40.0
TEMP_MAX_C = 85.0
RH_MIN_PCT = 0.0
RH_MAX_PCT = 100.0


# ---------- 3) Utilidades ----------
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


def obtener_tech_celular():
    """
    Consulta mmcli para obtener la tecnologia de acceso celular actual.
    Retorna un string como 'LTE', '5GNR', 'UMTS', o 'DESCONOCIDA'.
    """
    try:
        resultado = subprocess.run(
            ["mmcli", "-m", "0"],
            capture_output=True, text=True, timeout=5
        )
        for linea in resultado.stdout.splitlines():
            if "access tech" in linea:
                tech = linea.split(":")[-1].strip()
                return tech.upper()
    except Exception:
        pass
    return "DESCONOCIDA"


def hay_internet(host="1.1.1.1", port=80, timeout=4, interfaz="wwan0"):
    """
    Prueba de conectividad forzando salida por la interfaz 5G/LTE (wwan0).
    Si wwan0 no existe o no tiene IP, retorna False inmediatamente.
    """
    try:
        resultado = subprocess.run(
            ["ip", "addr", "show", interfaz],
            capture_output=True, text=True, timeout=3
        )
        if "inet " not in resultado.stdout:
            return False

        # abrir socket vinculado a la interfaz especifica
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        interfaz.encode())
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        return True

    except OSError:
        return False


def lectura_valida(t, h):
    """
    Valida una lectura cruda del sensor.
    Retorna (True, "") si es valida, o (False, motivo) si no lo es.
    """
    # 1) Que no sean None
    if t is None or h is None:
        return False, "lectura None (sensor sin datos)"
    # 2) Que sean numeros reales (no NaN/inf)
    try:
        t = float(t)
        h = float(h)
    except (TypeError, ValueError):
        return False, f"lectura no numerica (t={t!r}, h={h!r})"
    if t != t or h != h:  # NaN != NaN es True
        return False, "lectura NaN"
    if t in (float("inf"), float("-inf")) or h in (float("inf"), float("-inf")):
        return False, "lectura infinita"
    # 3) Que esten dentro del rango fisico
    if not (TEMP_MIN_C <= t <= TEMP_MAX_C):
        return False, f"temperatura fuera de rango: {t} C"
    if not (RH_MIN_PCT <= h <= RH_MAX_PCT):
        return False, f"humedad fuera de rango: {h} %"
    return True, ""


# ---------- 4) Buffer local (store-and-forward) ----------
# Guardamos agregados no enviados en un archivo, uno por linea (formato JSON).
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


# ---------- 5) El nodo (la FSM en si) ----------
class Nodo:
    def __init__(self, cfg):
        self.cfg = cfg
        self.estado = Estado.BOOT

        # --- Parametros de tiempo (desacoplados) ---
        # T_MEDIR: cada cuanto se toma una muestra del sensor.
        # T_TRANSMITIR: cada cuanto se envia un agregado por MQTT.
        self.t_medir = cfg.getint("nodo", "t_medir_segundos", fallback=10)
        self.t_transmitir = cfg.getint("nodo", "t_transmitir_segundos", fallback=40)

        # Tamano de la ventana deslizante de muestras. Por defecto, suficientes
        # muestras para cubrir un periodo de transmision; se puede sobrescribir.
        ventana_default = max(1, self.t_transmitir // self.t_medir)
        self.ventana_n = cfg.getint("nodo", "ventana_muestras",
                                    fallback=ventana_default)

        self.node_id = cfg.get("nodo", "node_id", fallback="nodo1")
        self.buffer_path = cfg.get("nodo", "buffer_path", fallback="buffer.jsonl")

        self.broker = cfg.get("mqtt", "broker")
        self.port = cfg.getint("mqtt", "port", fallback=1883)
        self.topic = cfg.get("mqtt", "topic")

        # --- Ventana deslizante de muestras crudas ---
        # Cada elemento es un dict {"t": temp, "h": hum, "ts": iso8601}.
        # deque con maxlen descarta automaticamente la muestra mas vieja.
        self.muestras = deque(maxlen=self.ventana_n)

        # Marca de tiempo (monotonica) de la ultima transmision.
        self.ultima_tx = None

        # Agregado calculado en PROCESAR, listo para transmitir.
        self.agregado_actual = None

        # Objetos que se crean en el BOOT
        self.sensor = None
        self.mqtt = None

    # ---- transiciones ----
    def boot(self):
        log(self.estado, "Inicializando sensor y cliente MQTT...")
        self.sensor = AHT10(bus_number=1)
        self.mqtt = MqttPublisher(self.broker, self.port, self.topic,
                                  client_id=self.node_id)
        # Inicializamos el reloj de transmision: la primera TX ocurre
        # cuando se cumpla t_transmitir desde ahora.
        self.ultima_tx = time.monotonic()
        log(self.estado,
            f"Config: T_MEDIR={self.t_medir}s, T_TRANSMITIR={self.t_transmitir}s, "
            f"ventana={self.ventana_n} muestras.")
        self.estado = Estado.CHECK_CONNECTIVITY

    def check_connectivity(self):
        # Detectar tecnologia de acceso celular actual
        tech = obtener_tech_celular()

        if hay_internet():
            log(self.estado, f"Hay conexion a internet. Tecnologia: {tech}.")
        else:
            log(self.estado, f"SIN conexion ({tech}). Igual mido y acumulo.")
        self.estado = Estado.MEDIR

    def medir(self):
        """Toma UNA muestra del sensor y la deja en self._lectura_cruda."""
        try:
            t, h = self.sensor.read()
            self._lectura_cruda = (t, h)
            log(self.estado, f"Lectura cruda -> {t} C, {h} %")
            self.estado = Estado.VALIDAR
        except Exception as e:
            log(self.estado, f"Fallo leyendo sensor: {e}")
            self.estado = Estado.ERROR

    def validar(self):
        """Valida la lectura cruda. Si es invalida, va a ERROR."""
        t, h = self._lectura_cruda
        ok, motivo = lectura_valida(t, h)
        if not ok:
            log(self.estado, f"Lectura INVALIDA: {motivo}. Paso a ERROR.")
            self.estado = Estado.ERROR
            return

        # Lectura valida: la agregamos a la ventana deslizante.
        muestra = {
            "t": float(t),
            "h": float(h),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.muestras.append(muestra)
        log(self.estado,
            f"Lectura valida. Muestras en ventana: {len(self.muestras)}/{self.ventana_n}")
        self.estado = Estado.PROCESAR

    def procesar(self):
        """
        Calcula estadisticas de la ventana deslizante y decide si toca
        transmitir o seguir midiendo.
        El payload incluye las muestras crudas (FIFO) Y el agregado estadistico.
        """
        temps = [m["t"] for m in self.muestras]
        hums = [m["h"] for m in self.muestras]
        n = len(self.muestras)

        # statistics.stdev necesita al menos 2 datos; con 1 dato std = 0.
        temp_std = statistics.stdev(temps) if n >= 2 else 0.0
        rh_std = statistics.stdev(hums) if n >= 2 else 0.0

        self.agregado_actual = {
            "node_id": self.node_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # --- Agregado estadistico (promedio movil + desviacion estandar) ---
            "temp_avg": round(statistics.mean(temps), 2),
            "temp_std": round(temp_std, 3),
            "rh_avg": round(statistics.mean(hums), 2),
            "rh_std": round(rh_std, 3),
            "n_samples": n,
            # --- Muestras crudas en orden cronologico (FIFO) ---
            "samples": [
                {
                    "timestamp": m["ts"],
                    "temperatura_c": m["t"],
                    "humedad_pct": m["h"],
                }
                for m in self.muestras
            ],
        }
        log(self.estado,
            f"Agregado: temp_avg={self.agregado_actual['temp_avg']} C, "
            f"rh_avg={self.agregado_actual['rh_avg']} %, "
            f"n_samples={n}, samples incluidos.")

        # Decidir: ya paso T_TRANSMITIR desde la ultima transmision?
        transcurrido = time.monotonic() - self.ultima_tx
        if transcurrido >= self.t_transmitir:
            log(self.estado,
                f"Toca transmitir ({transcurrido:.0f}s >= {self.t_transmitir}s).")
            self.estado = Estado.TRANSMITIR
        else:
            log(self.estado,
                f"Aun no toca TX ({transcurrido:.0f}s < {self.t_transmitir}s). A dormir.")
            self.estado = Estado.SLEEP

    def transmitir(self):
        try:
            conectado = self.mqtt.connect()
            if not conectado:
                raise ConnectionError("no se pudo conectar al broker")

            # Primero mandamos lo que quedo guardado en el buffer (orden FIFO).
            pendientes = leer_buffer(self.buffer_path)
            if pendientes:
                log(self.estado, f"Enviando {len(pendientes)} agregados del buffer...")
                for p in pendientes:
                    self.mqtt.publish(p)
                vaciar_buffer(self.buffer_path)

            # Luego mandamos el agregado actual.
            self.mqtt.publish(self.agregado_actual)
            self.mqtt.disconnect()
            log(self.estado, "Agregado transmitido OK.")
        except Exception as e:
            log(self.estado, f"No se pudo transmitir: {e}. Guardo en buffer.")
            guardar_en_buffer(self.buffer_path, self.agregado_actual)
        finally:
            # Reiniciamos el reloj de transmision pase lo que pase, para no
            # intentar transmitir en cada ciclo si el broker esta caido.
            self.ultima_tx = time.monotonic()
            self.estado = Estado.SLEEP

    def sleep(self):
        log(self.estado, f"Durmiendo {self.t_medir}s hasta la proxima medicion.")
        time.sleep(self.t_medir)
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
            Estado.VALIDAR: self.validar,
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
