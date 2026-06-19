"""
main.py — Firmware del Nodo de Monitoreo Ambiental (Grupo P8 / VitiScience)

Maquina de estados (FSM) del nodo. Version con sensado y transmision
DESACOPLADOS, telemetria estadistica y duty cycling del HAT 5G.

BOOT -> CHECK_CONNECTIVITY -> MEDIR -> VALIDAR -> PROCESAR -> (decide)
  ^                                                    |
  |                              .--------------------|
  |                              |-- toca TX --> HAT_ON -> TRANSMITIR -> HAT_OFF -> SLEEP
  |                              |-- no toca --> SLEEP
  +-------------- SLEEP ---------+
  |
  (lectura/validacion mala) v
  ERROR

Duty cycling del HAT:
  - Durante la fase de sensado el HAT esta APAGADO.
  - Se enciende solo cuando toca transmitir (HAT_ON).
  - Se apaga inmediatamente despues de transmitir (HAT_OFF).

Control del HAT (configurable en config.ini via hat_control):
  gpio  -> Pulsa el pin PWRKEY del modulo (mayor ahorro energetico).
           Requiere RPi.GPIO y conocer el pin BCM del Calyx (coordinar con Vicente).
  mmcli -> mmcli --enable / --disable (apaga el radio, no la alimentacion).
           Funciona sin GPIO, util como fallback o mientras se confirma el pin.
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

#from sensor_aht10 import AHT10
from mqtt_client import MqttPublisher
from sensor_ble import SensorBLE as AHT10

# ---------- 1) Estados de la FSM ----------

class Estado(enum.Enum):
    BOOT              = "BOOT"
    CHECK_CONNECTIVITY = "CHECK_CONNECTIVITY"
    MEDIR             = "MEDIR"
    VALIDAR           = "VALIDAR"
    PROCESAR          = "PROCESAR"
    HAT_ON            = "HAT_ON"
    TRANSMITIR        = "TRANSMITIR"
    HAT_OFF           = "HAT_OFF"
    SLEEP             = "SLEEP"
    ERROR             = "ERROR"

# ---------- 2) Limites de validacion ----------

TEMP_MIN_C = -40.0
TEMP_MAX_C = 85.0
RH_MIN_PCT = 0.0
RH_MAX_PCT = 100.0

# ---------- 3) Utilidades ----------

def log(estado, mensaje):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora}] [{estado.value:18}] {mensaje}", flush=True)

def cargar_config(ruta):
    if not os.path.exists(ruta):
        print(f"ERROR: no encuentro el archivo de configuracion: {ruta}")
        print("Copia config.example.ini a config.ini y editalo.")
        sys.exit(1)
    cfg = configparser.ConfigParser(inline_comment_prefixes="#")
    cfg.read(ruta)
    return cfg

def obtener_tech_celular():
    """Detecta automaticamente el modem visible y devuelve su tecnologia celular."""
    try:
        lista = subprocess.run(
            ["mmcli", "-L"],
            capture_output=True, text=True, timeout=5
        )

        modem_id = None
        for linea in lista.stdout.splitlines():
            if "/Modem/" in linea:
                modem_id = linea.split("/Modem/")[-1].split()[0].strip()
                break

        if modem_id is None:
            return "DESCONOCIDA"

        resultado = subprocess.run(
            ["mmcli", "-m", modem_id],
            capture_output=True, text=True, timeout=5
        )

        for linea in resultado.stdout.splitlines():
            if "access tech" in linea.lower():
                tech = linea.split(":", 1)[-1].strip()
                return tech.upper()

    except Exception:
        pass

    return "DESCONOCIDA"

def hay_internet(host="1.1.1.1", port=80, timeout=4, interfaz="wwan0"):
    try:
        resultado = subprocess.run(
            ["ip", "addr", "show", interfaz],
            capture_output=True, text=True, timeout=3
        )
        if "inet " not in resultado.stdout:
            return False
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
    if t is None or h is None:
        return False, "lectura None (sensor sin datos)"
    try:
        t = float(t)
        h = float(h)
    except (TypeError, ValueError):
        return False, f"lectura no numerica (t={t!r}, h={h!r})"
    if t != t or h != h:
        return False, "lectura NaN"
    if t in (float("inf"), float("-inf")) or h in (float("inf"), float("-inf")):
        return False, "lectura infinita"
    if not (TEMP_MIN_C <= t <= TEMP_MAX_C):
        return False, f"temperatura fuera de rango: {t} C"
    if not (RH_MIN_PCT <= h <= RH_MAX_PCT):
        return False, f"humedad fuera de rango: {h} %"
    return True, ""

# ---------- 4) Buffer local (store-and-forward) ----------

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

# ---------- 5) Control del HAT (duty cycling) ----------

class HatController:
    """
    Controla el encendido/apagado del HAT 5G (Teltonika Calyx / Quectel RG520N-EB).

    Modo 'gpio':
        Pulsa el pin PWRKEY del modulo para encender o apagar.
        Es el modo mas eficiente: corta la alimentacion del modulo
        durante la fase de sensado.
        Requiere: RPi.GPIO instalado y el numero de pin BCM correcto
                  (consultar esquematico del Calyx con Vicente).

    Modo 'mmcli':
        Usa mmcli --enable / --disable para apagar el radio.
        No corta la alimentacion fisica pero es mas simple y no necesita GPIO.
        Es el fallback automatico si RPi.GPIO no esta disponible.
    """

    def __init__(self, modo="mmcli", gpio_pin=4, pwrkey_ms=500, boot_wait_s=15):
        self.modo       = modo.lower()
        self.gpio_pin   = gpio_pin
        self.pwrkey_ms  = pwrkey_ms
        self.boot_wait_s = boot_wait_s
        self._gpio_ok   = False

        if self.modo == "gpio":
            self._init_gpio()

    def _init_gpio(self):
        """Inicializa RPi.GPIO. Si falla, cae automaticamente a modo mmcli."""
        try:
            import RPi.GPIO as GPIO
            self._GPIO = GPIO
            GPIO.setmode(GPIO.BCM)
            # PWRKEY normalmente esta en reposo HIGH; un pulso LOW enciende/apaga.
            GPIO.setup(self.gpio_pin, GPIO.OUT, initial=GPIO.LOW)
            self._gpio_ok = True
            print(f"[HatController] GPIO iniciado en pin BCM {self.gpio_pin}.")
        except ImportError:
            print("[HatController] RPi.GPIO no disponible. Usando mmcli como fallback.")
            self.modo = "mmcli"
        except Exception as e:
            print(f"[HatController] Error GPIO: {e}. Usando mmcli.")
            self.modo = "mmcli"

    def _pulso_pwrkey(self):
        """
        Genera un pulso LOW en PWRKEY por pwrkey_ms milisegundos.
        El RG520N-EB interpreta este pulso como orden de encendido o apagado.
        """
        self._GPIO.output(self.gpio_pin, self._GPIO.HIGH)
        time.sleep(self.pwrkey_ms / 1000.0)
        self._GPIO.output(self.gpio_pin, self._GPIO.LOW)

    def encender(self):
        """
        Enciende el HAT y espera a que el modem se registre en la red.
        Retorna True si quedo con internet, False si no.
        """
        if self.modo == "gpio" and self._gpio_ok:
            print(f"[HatController] Pulsando PWRKEY para ENCENDER (pin BCM {self.gpio_pin})...")
            self._pulso_pwrkey()
            print(f"[HatController] Esperando {self.boot_wait_s}s para boot del modem...")
            time.sleep(self.boot_wait_s)
            return self._reconectar_modem()
        else:
            # En modo mmcli, no basta con --enable: despues de --disable
            # hay que volver a conectar APN y restaurar la ruta por wwan0.
            return self._reconectar_modem()

    def apagar(self):
        """Apaga el HAT."""
        if self.modo == "gpio" and self._gpio_ok:
            print(f"[HatController] Pulsando PWRKEY para APAGAR (pin BCM {self.gpio_pin})...")
            self._pulso_pwrkey()
            time.sleep(3)  # espera a que el modulo termine su secuencia de apagado
        else:
            self._mmcli_disable()

    def cleanup(self):
        """Libera pines GPIO al terminar el programa."""
        if self.modo == "gpio" and self._gpio_ok:
            try:
                self._GPIO.cleanup()
            except Exception:
                pass

    # ---- helpers internos ----

    def _reconectar_modem(self):
        """
        Reconecta el modem 5G y configura wwan0 dinamicamente
        usando src/modem_net.py.
        """
        try:
            from modem_net import configure_wwan_from_bearer

            ok = configure_wwan_from_bearer(
                modem_id="auto",
                apn="bam.entelpcs.cl",
                metric="50"
            )

            if not ok:
                return False

            return hay_internet()

        except Exception as e:
            print(f"[HatController] Error al reconectar modem: {e}")
            return False

    def _mmcli_enable(self):
        """Habilita el radio via mmcli y espera conectividad."""
        try:
            subprocess.run(["sudo", "mmcli", "-m", "0", "--enable"],
                           timeout=10, check=True)
            time.sleep(5)
            return hay_internet()
        except Exception as e:
            print(f"[HatController] Error al habilitar modem: {e}")
            return False

    def _mmcli_disable(self):
        """
        Deshabilita el radio via mmcli usando el modem_id actual.

        ModemManager puede cambiar el numero del modem:
        a veces es /Modem/0, otras /Modem/1.
        Por eso aqui tambien lo detectamos automaticamente.
        """
        try:
            from modem_net import _find_modem_id

            modem_id = _find_modem_id()
            if not modem_id:
                print("[HatController] Aviso: no hay modem visible para deshabilitar.")
                return

            subprocess.run(["sudo", "mmcli", "-m", modem_id, "--disable"],
                           timeout=35, check=False)

        except Exception as e:
            print(f"[HatController] Aviso al deshabilitar modem: {e}")


class Nodo:

    def __init__(self, cfg):
        self.cfg    = cfg
        self.estado = Estado.BOOT

        # Parametros de tiempo
        self.t_medir      = cfg.getint("nodo", "t_medir_segundos",     fallback=10)
        self.t_transmitir = cfg.getint("nodo", "t_transmitir_segundos", fallback=40)

        ventana_default = max(1, self.t_transmitir // self.t_medir)
        self.ventana_n  = cfg.getint("nodo", "ventana_muestras", fallback=ventana_default)

        self.node_id     = cfg.get("nodo", "node_id",     fallback="nodo1")
        self.buffer_path = cfg.get("nodo", "buffer_path", fallback="buffer.jsonl")

        self.broker = cfg.get("mqtt", "broker")
        self.port   = cfg.getint("mqtt", "port", fallback=1883)
        self.topic  = cfg.get("mqtt", "topic")

        # Parametros del HAT
        hat_modo      = cfg.get("nodo",  "hat_control",    fallback="mmcli")
        hat_pin       = cfg.getint("nodo", "hat_gpio_pin",   fallback=4)
        hat_pwrkey_ms = cfg.getint("nodo", "hat_pwrkey_ms",  fallback=500)
        hat_boot_wait = cfg.getint("nodo", "hat_boot_wait_s", fallback=15)

        self.hat = HatController(
            modo=hat_modo,
            gpio_pin=hat_pin,
            pwrkey_ms=hat_pwrkey_ms,
            boot_wait_s=hat_boot_wait,
        )

        self.muestras        = deque(maxlen=self.ventana_n)
        self.ultima_tx       = None
        self.agregado_actual = None
        self.sensor          = None
        self.mqtt            = None

    # ---- transiciones ----

    def boot(self):
        log(self.estado, "Inicializando sensor y cliente MQTT...")
        # --- OPCIONES DE SENSOR ---
        # 1. Sensor Local I2C (o Simulado/Random si se modifico sensor_aht10.py)
        #self.sensor = AHT10(bus_number=1)

        # 2. Sensor BLE (Descomentar para usar ESP32 externa y comentar la linea anterior)
        # Importante: requiere configurar la mac_address en config.ini mas adelante
        self.sensor = AHT10(mac_address=self.cfg.get("nodo", "esp32_mac", fallback="XX:XX:XX:XX:XX:XX"))
        # --------------------------

        self.mqtt   = MqttPublisher(self.broker, self.port, self.topic,
                                    client_id=self.node_id)
        self.ultima_tx = time.monotonic()
        log(self.estado,
            f"Config: T_MEDIR={self.t_medir}s, T_TRANSMITIR={self.t_transmitir}s, "
            f"ventana={self.ventana_n} muestras, HAT={self.hat.modo.upper()}.")
        self.estado = Estado.CHECK_CONNECTIVITY

    def check_connectivity(self):
        tech = obtener_tech_celular()
        if hay_internet():
            log(self.estado, f"Hay conexion a internet. Tecnologia: {tech}.")
        else:
            log(self.estado, f"SIN conexion ({tech}). Igual mido y acumulo.")
        self.estado = Estado.MEDIR

    def medir(self):
        try:
            t, h = self.sensor.read()
            self._lectura_cruda = (t, h)
            log(self.estado, f"Lectura cruda -> {t} C, {h} %")
            self.estado = Estado.VALIDAR
        except Exception as e:
            log(self.estado, f"Fallo leyendo sensor: {e}")
            self.estado = Estado.ERROR

    def validar(self):
        t, h = self._lectura_cruda
        ok, motivo = lectura_valida(t, h)
        if not ok:
            log(self.estado, f"Lectura INVALIDA: {motivo}. Paso a ERROR.")
            self.estado = Estado.ERROR
            return
        muestra = {
            "t":  float(t),
            "h":  float(h),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.muestras.append(muestra)
        log(self.estado,
            f"Lectura valida. Muestras en ventana: {len(self.muestras)}/{self.ventana_n}")
        self.estado = Estado.PROCESAR

    def procesar(self):
        temps = [m["t"] for m in self.muestras]
        hums  = [m["h"] for m in self.muestras]
        n     = len(self.muestras)

        temp_std = statistics.stdev(temps) if n >= 2 else 0.0
        rh_std   = statistics.stdev(hums)  if n >= 2 else 0.0

        self.agregado_actual = {
            "node_id":   self.node_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temp_avg":  round(statistics.mean(temps), 2),
            "temp_std":  round(temp_std, 3),
            "rh_avg":    round(statistics.mean(hums), 2),
            "rh_std":    round(rh_std, 3),
            "n_samples": n,
            "samples": [
                {"timestamp": m["ts"], "temperatura_c": m["t"], "humedad_pct": m["h"]}
                for m in self.muestras
            ],
        }

        log(self.estado,
            f"Agregado: temp_avg={self.agregado_actual['temp_avg']} C, "
            f"rh_avg={self.agregado_actual['rh_avg']} %, n_samples={n}.")

        transcurrido = time.monotonic() - self.ultima_tx
        if transcurrido >= self.t_transmitir:
            log(self.estado,
                f"Toca transmitir ({transcurrido:.0f}s >= {self.t_transmitir}s).")
            self.estado = Estado.HAT_ON
        else:
            log(self.estado,
                f"Aun no toca TX ({transcurrido:.0f}s < {self.t_transmitir}s). A dormir.")
            self.estado = Estado.SLEEP

    def hat_on(self):
        """Enciende el HAT y espera conectividad antes de transmitir."""
        log(self.estado, f"Encendiendo HAT (modo={self.hat.modo})...")
        ok = self.hat.encender()
        if ok:
            tech = obtener_tech_celular()
            log(self.estado, f"HAT encendido y conectado. Tecnologia: {tech}.")
            self.estado = Estado.TRANSMITIR
        else:
            log(self.estado, "HAT encendido pero SIN internet. Guardo en buffer.")
            guardar_en_buffer(self.buffer_path, self.agregado_actual)
            self.ultima_tx = time.monotonic()
            self.hat.apagar()
            self.estado = Estado.SLEEP

    def transmitir(self):
        try:
            conectado = self.mqtt.connect()
            if not conectado:
                raise ConnectionError("no se pudo conectar al broker")

            pendientes = leer_buffer(self.buffer_path)
            if pendientes:
                log(self.estado, f"Enviando {len(pendientes)} agregados del buffer...")
                for p in pendientes:
                    self.mqtt.publish(p)
                vaciar_buffer(self.buffer_path)

            self.mqtt.publish(self.agregado_actual)
            self.mqtt.disconnect()
            log(self.estado, "Agregado transmitido OK.")

        except Exception as e:
            log(self.estado, f"No se pudo transmitir: {e}. Guardo en buffer.")
            guardar_en_buffer(self.buffer_path, self.agregado_actual)

        finally:
            self.ultima_tx = time.monotonic()
            self.estado = Estado.HAT_OFF  # siempre apagar despues de TX

    def hat_off(self):
        """Apaga el HAT despues de transmitir y limpia la ruta celular."""
        log(self.estado, f"Apagando HAT (modo={self.hat.modo})...")
        self.hat.apagar()

        # Al deshabilitar el modem con mmcli, Linux puede conservar una ruta vieja por wwan0.
        # La eliminamos para que, entre ciclos, la Raspberry vuelva a usar wlan0/eth0 si existen.
        try:
            subprocess.run(
                ["sudo", "ip", "route", "del", "default", "dev", "wwan0"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log(self.estado, "Ruta default por wwan0 limpiada.")
        except Exception as e:
            log(self.estado, f"No se pudo limpiar ruta wwan0: {e}")

        log(self.estado, "HAT apagado.")
        self.estado = Estado.SLEEP

    def sleep(self):
        log(self.estado, f"Durmiendo {self.t_medir}s hasta la proxima medicion.")
        time.sleep(self.t_medir)
        self.estado = Estado.MEDIR

    def error(self):
        log(self.estado, "Estado de error. Reinicio sensor y reintento en 30s.")
        try:
            if self.sensor:
                self.sensor.soft_reset()
        except Exception:
            pass
        time.sleep(30)
        self.estado = Estado.CHECK_CONNECTIVITY

    # ---- bucle principal ----

    def run(self):
        acciones = {
            Estado.BOOT:               self.boot,
            Estado.CHECK_CONNECTIVITY: self.check_connectivity,
            Estado.MEDIR:              self.medir,
            Estado.VALIDAR:            self.validar,
            Estado.PROCESAR:           self.procesar,
            Estado.HAT_ON:             self.hat_on,
            Estado.TRANSMITIR:         self.transmitir,
            Estado.HAT_OFF:            self.hat_off,
            Estado.SLEEP:              self.sleep,
            Estado.ERROR:              self.error,
        }
        log(self.estado, f"Nodo '{self.node_id}' arrancando. Ctrl+C para detener.")
        while True:
            acciones[self.estado]()

    def shutdown(self):
        """Asegura que el HAT quede apagado al salir."""
        log(self.estado, "Shutdown: apagando HAT...")
        self.hat.apagar()
        self.hat.cleanup()


if __name__ == "__main__":
    ruta_cfg = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    config = cargar_config(ruta_cfg)
    nodo = Nodo(config)
    try:
        nodo.run()
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
        nodo.shutdown()
        print("Hasta luego!")
