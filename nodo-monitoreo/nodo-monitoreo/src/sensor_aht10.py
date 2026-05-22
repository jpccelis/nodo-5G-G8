"""
sensor_aht10.py
================
Driver mínimo para el sensor de temperatura y humedad AHT10 vía I2C.

¿Por qué escribimos nuestro propio driver en vez de usar una librería gigante?
- El AHT10 es un sensor MUY simple: se le manda un comando, se espera ~80 ms,
  y se leen 6 bytes. Escribirlo nosotros nos enseña exactamente qué pasa.
- Solo dependemos de 'smbus2', que es la librería estándar para hablar I2C
  en Linux / Raspberry Pi.

I2C en una frase: es un "bus" de 2 cables (SDA = datos, SCL = reloj) por el
que la Raspberry Pi (maestro) le hace preguntas a uno o varios sensores
(esclavos). Cada esclavo tiene una "dirección" única. La del AHT10 es 0x38.
"""

import time
from smbus2 import SMBus, i2c_msg

# Dirección I2C del AHT10 (fija de fábrica). En hexadecimal 0x38 = 56 decimal.
AHT10_ADDR = 0x38

# Comandos que entiende el AHT10 (vienen en su datasheet):
CMD_INIT = [0xE1, 0x08, 0x00]      # Inicializa/calibra el sensor
CMD_MEASURE = [0xAC, 0x33, 0x00]   # "Dispara" una medición
CMD_SOFT_RESET = 0xBA              # Reinicio por software


class AHT10:
    """Representa un sensor AHT10 conectado a un bus I2C concreto."""

    def __init__(self, bus_number: int = 1, address: int = AHT10_ADDR):
        # En la Raspberry Pi 4, el bus I2C de los pines GPIO es el número 1.
        self.bus_number = bus_number
        self.address = address
        self.bus = SMBus(bus_number)
        time.sleep(0.05)  # pequeña pausa tras abrir el bus
        self._init_sensor()

    def _init_sensor(self):
        """Envía la secuencia de calibración inicial."""
        self.bus.write_i2c_block_data(self.address, CMD_INIT[0], CMD_INIT[1:])
        time.sleep(0.05)  # el sensor necesita un momento para calibrarse

    def soft_reset(self):
        """Reinicia el sensor sin cortarle la energía (útil si se 'cuelga')."""
        self.bus.write_byte(self.address, CMD_SOFT_RESET)
        time.sleep(0.02)
        self._init_sensor()

    def read(self):
        """
        Dispara una medición y devuelve (temperatura_C, humedad_%).

        Pasos:
        1. Mandamos el comando de medición.
        2. Esperamos ~80 ms (el sensor está midiendo).
        3. Leemos 6 bytes crudos.
        4. Convertimos esos bits a °C y %HR usando las fórmulas del datasheet.
        """
        # 1) disparar medición
        self.bus.write_i2c_block_data(self.address, CMD_MEASURE[0], CMD_MEASURE[1:])
        # 2) esperar a que termine
        time.sleep(0.08)

        # 3) leer 6 bytes crudos
        read = i2c_msg.read(self.address, 6)
        self.bus.i2c_rdwr(read)
        data = list(read)

        # El primer byte es el "estado". El bit 7 (0x80) indica si el sensor
        # todavía está ocupado. Si lo está, esperamos un poco más y reintentamos.
        if data[0] & 0x80:
            time.sleep(0.08)
            read = i2c_msg.read(self.address, 6)
            self.bus.i2c_rdwr(read)
            data = list(read)

        # 4) reconstruir los valores. La humedad ocupa 20 bits y la
        # temperatura otros 20 bits, "empaquetados" en los bytes 1..5.
        raw_hum = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
        raw_temp = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]

        # Fórmulas oficiales del datasheet del AHT10:
        humidity = (raw_hum / 2**20) * 100.0
        temperature = (raw_temp / 2**20) * 200.0 - 50.0

        return round(temperature, 2), round(humidity, 2)

    def close(self):
        self.bus.close()


# Si ejecutas este archivo directamente (python3 sensor_aht10.py),
# hace una lectura de prueba. Así puedes probar SOLO el sensor.
if __name__ == "__main__":
    sensor = AHT10()
    try:
        t, h = sensor.read()
        print(f"Temperatura: {t} °C   |   Humedad: {h} %")
    finally:
        sensor.close()
