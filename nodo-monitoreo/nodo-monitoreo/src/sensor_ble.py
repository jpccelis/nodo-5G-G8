"""
sensor_ble.py
=============
Reemplazo de sensor_aht10.py para cuando el AHT10 está en una ESP32 externa
que transmite los datos por Bluetooth Low Energy (BLE).

Interfaz IDÉNTICA a sensor_aht10.py:
    sensor = SensorBLE(mac_address="XX:XX:XX:XX:XX:XX")
    t, h = sensor.read()      # devuelve (temperatura_C, humedad_%)
    sensor.soft_reset()       # intenta reconectar (equivale al reset del AHT10)
    sensor.close()            # cierra limpiamente

¿Por qué la misma interfaz?
    main.py no necesita cambiar nada de su lógica. Solo cambia el import:
        from sensor_ble import SensorBLE as AHT10   # una línea distinta
    El resto de la FSM (MEDIR, VALIDAR, ERROR) funciona igual que antes.

Estrategia de conexión (diseñada para bajo consumo energético):
    - La Raspberry NO mantiene una conexión BLE permanente.
    - Cada llamada a read() abre la conexión, lee, y la cierra.
    - Esto deja el radio BLE activo solo ~2-4 segundos por ciclo de medición,
      en vez de todo el tiempo.
    - Si la ESP32 está caída (sin batería, reiniciando), read() reintenta
      SCAN_RETRIES veces antes de lanzar excepción. La FSM de main.py capta
      esa excepción y pasa al estado ERROR, que espera 30 s y reintenta.
      Cuando la ESP32 vuelve, la Raspberry la encuentra en el siguiente scan.

Dependencia:
    pip install bleak
    (agregar "bleak==0.21.1" a requirements.txt)

UUIDs del servicio BLE (deben coincidir con el firmware de la ESP32):
    Servicio  : 0000181A-0000-1000-8000-00805f9b34fb  (Environmental Sensing)
    Temp      : 00002A6E-0000-1000-8000-00805f9b34fb  (Temperature, IEEE 11073)
    Humedad   : 00002A6F-0000-1000-8000-00805f9b34fb  (Humidity, IEEE 11073)

Formato de los valores (debe coincidir con la ESP32):
    Temperatura : entero de 2 bytes, little-endian, en centésimas de °C
                  Ej: 2534 → 25.34 °C
    Humedad     : entero de 2 bytes, little-endian, en centésimas de %
                  Ej: 6012 → 60.12 %
"""

import asyncio
import struct
import time

from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

# ---------- UUIDs (deben coincidir con el firmware de la ESP32) ----------
UUID_SERVICIO  = "0000181A-0000-1000-8000-00805f9b34fb"
UUID_TEMP      = "00002A6E-0000-1000-8000-00805f9b34fb"
UUID_HUMEDAD   = "00002A6F-0000-1000-8000-00805f9b34fb"

# ---------- Parámetros de conexión ----------
SCAN_TIMEOUT_S  = 5.0   # segundos buscando la ESP32 en el aire
CONN_TIMEOUT_S  = 8.0   # segundos esperando que la conexión se establezca
SCAN_RETRIES    = 3     # intentos de scan antes de rendirse y lanzar excepción
RETRY_DELAY_S   = 2.0   # segundos entre reintentos de scan


class SensorBLE:
    """
    Representa el sensor AHT10 remoto corriendo en una ESP32.
    La interfaz es idéntica a la clase AHT10 de sensor_aht10.py.
    """

    def __init__(self, mac_address: str):
        """
        mac_address: dirección MAC de la ESP32, en formato "XX:XX:XX:XX:XX:XX".
                     Se obtiene una sola vez desde el firmware de la ESP32
                     (está impresa en el monitor serial al arrancar).
        """
        self.mac_address = mac_address.upper()

    # ---- API pública (idéntica a sensor_aht10.AHT10) ----

    def read(self):
        """
        Conecta a la ESP32 por BLE, lee temperatura y humedad, desconecta.
        Devuelve (temperatura_C, humedad_%) como floats redondeados a 2 dec.
        Lanza excepción si no puede conectar tras SCAN_RETRIES intentos.
        """
        return asyncio.run(self._leer_con_reintentos())

    def soft_reset(self):
        """
        En el sensor I2C esto reiniciaba el chip. Aquí equivale a intentar
        reconectar. Si la ESP32 está arriba, la próxima llamada a read()
        funcionará. No hay nada que resetear en la Raspberry.
        """
        # No hace nada activo: la reconexión ocurre naturalmente en read().
        # Se mantiene para que main.py no explote al llamar sensor.soft_reset().
        pass

    def close(self):
        """
        En el sensor I2C cerraba el bus. Aquí no hay conexión permanente,
        así que no hay nada que cerrar. Se mantiene por compatibilidad.
        """
        pass

    # ---- Lógica BLE interna (async) ----

    async def _leer_con_reintentos(self):
        """
        Intenta escanear + conectar + leer SCAN_RETRIES veces.
        Si todos los intentos fallan, lanza RuntimeError.
        """
        ultimo_error = None

        for intento in range(1, SCAN_RETRIES + 1):
            try:
                return await self._leer_una_vez()
            except (BleakError, asyncio.TimeoutError, RuntimeError) as e:
                ultimo_error = e
                print(
                    f"[BLE] Intento {intento}/{SCAN_RETRIES} fallido: {e}. "
                    f"{'Reintentando...' if intento < SCAN_RETRIES else 'Sin mas intentos.'}"
                )
                if intento < SCAN_RETRIES:
                    await asyncio.sleep(RETRY_DELAY_S)

        raise RuntimeError(
            f"No se pudo leer el sensor BLE tras {SCAN_RETRIES} intentos. "
            f"Ultimo error: {ultimo_error}"
        )

    async def _leer_una_vez(self):
        """
        Un ciclo completo: scan → connect → read → disconnect.
        """
        # 1) Verificar que la ESP32 está en el aire (advertising)
        #    Esto evita intentar conectar a algo que no existe, lo cual
        #    cuelga durante CONN_TIMEOUT_S sin dar feedback útil.
        dispositivo = await BleakScanner.find_device_by_address(
            self.mac_address,
            timeout=SCAN_TIMEOUT_S
        )
        if dispositivo is None:
            raise RuntimeError(
                f"ESP32 ({self.mac_address}) no encontrada en el scan BLE. "
                f"¿Está encendida y haciendo advertising?"
            )

        # 2) Conectar y leer
        async with BleakClient(dispositivo, timeout=CONN_TIMEOUT_S) as cliente:
            if not cliente.is_connected:
                raise RuntimeError(
                    f"No se pudo establecer conexión con {self.mac_address}."
                )

            # Leer característica de temperatura (2 bytes, little-endian)
            raw_temp = await cliente.read_gatt_char(UUID_TEMP)
            # Leer característica de humedad (2 bytes, little-endian)
            raw_hum  = await cliente.read_gatt_char(UUID_HUMEDAD)

        # 3) Decodificar
        #    La ESP32 envía enteros de 16 bits sin signo en centésimas de unidad.
        #    struct.unpack("<H", ...) = little-endian, unsigned short (2 bytes).
        temp_centesimas = struct.unpack("<H", bytes(raw_temp))[0]
        hum_centesimas  = struct.unpack("<H", bytes(raw_hum))[0]

        temperatura = round(temp_centesimas / 100.0, 2)
        humedad     = round(hum_centesimas  / 100.0, 2)

        return temperatura, humedad


# ---------- Prueba directa ----------
# Si ejecutas: python3 sensor_ble.py XX:XX:XX:XX:XX:XX
# Hace una lectura de prueba, igual que sensor_aht10.py.
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python3 sensor_ble.py <MAC_ADDRESS>")
        print("Ejemplo: python3 sensor_ble.py A4:CF:12:3B:7E:01")
        sys.exit(1)

    mac = sys.argv[1]
    sensor = SensorBLE(mac_address=mac)
    try:
        t, h = sensor.read()
        print(f"Temperatura: {t} °C   |   Humedad: {h} %")
    except RuntimeError as e:
        print(f"Error: {e}")
    finally:
        sensor.close()