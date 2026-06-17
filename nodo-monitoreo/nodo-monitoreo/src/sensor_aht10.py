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
import random

# Simulación de sensor AHT10 (sin I2C para pruebas)


class AHT10:
    """Simulación de sensor AHT10 conectado a un bus I2C concreto."""

    def __init__(self, bus_number: int = 1, address: int = 0x38):
        # En la Raspberry Pi 4, el bus I2C de los pines GPIO es el número 1.
        self.bus_number = bus_number
        self.address = address
        time.sleep(0.05)  # pequeña pausa tras abrir el bus

    def _init_sensor(self):
        """Simulación de secuencia de calibración."""
        pass

    def soft_reset(self):
        """Simulación de reinicio."""
        pass

    def read(self):
        """
        Genera una medición aleatoria de (temperatura_C, humedad_%).
        """
        # Rangos razonables y mutuamente excluyentes en sus valores numéricos:
        # Temperatura entre 10 y 30.0
        temperature = random.uniform(10.0, 30.0)
        # Humedad entre 50.0 y 90.0
        humidity = random.uniform(50.0, 90.0)

        return round(temperature, 2), round(humidity, 2)

    def close(self):
        """Simulación de cierre de bus."""
        pass


# Si ejecutas este archivo directamente (python3 sensor_aht10.py),
# hace una lectura de prueba. Así puedes probar SOLO el sensor.
if __name__ == "__main__":
    sensor = AHT10()
    try:
        t, h = sensor.read()
        print(f"Temperatura: {t} °C   |   Humedad: {h} %")
    finally:
        sensor.close()
