# Nodo de Monitoreo Ambiental — Grupo P8 / VitiScience (IEE3112)

Firmware del nodo de monitoreo microclimático con comunicación 5G.
Corre sobre **Raspberry Pi 4** + **Teltonika Calyx EBD050 (5G NR, Quectel RG520N-EB)** + sensor **AHT10**.

## ¿Qué hace?
Mide temperatura y humedad cada cierto intervalo, arma un paquete de telemetría con
estadísticas de la ventana de muestras y lo transmite por **MQTT sobre 5G/4G**. Si no hay
conexión, guarda las mediciones en un buffer local y las reenvía cuando la red vuelve
(*store-and-forward*). Para ahorrar energía, el módem 5G solo se enciende en el momento de
transmitir y se apaga el resto del tiempo (*duty cycling*).

## Arquitectura de sensado
El AHT10 **no** se conecta por I2C directo a la Raspberry Pi. El HAT Calyx, al montarse sobre
los 40 pines GPIO, no deja longitud de pin expuesta para cablear el sensor de forma fiable. Por
eso el sensor se lee desde una **ESP32 externa** que expone los valores por **Bluetooth Low
Energy (BLE)**, y la Raspberry los lee con la librería `bleak`:

```
AHT10  --I2C-->  ESP32  --BLE-->  Raspberry Pi 4  --USB/5G-->  Broker MQTT
```

## Estructura del repositorio
```
nodo-monitoreo/
├── src/
│   ├── main.py           # FSM principal y lazo de control (núcleo del firmware)
│   ├── sensor_ble.py     # driver de sensado VIGENTE: lee el AHT10 vía ESP32 + BLE (bleak)
│   ├── sensor_aht10.py   # driver I2C heredado/simulado — NO se usa (ver nota abajo)
│   ├── mqtt_client.py    # publicación por MQTT con paho-mqtt (QoS 1)
│   ├── modem_net.py      # conexión del módem 5G y configuración de wwan0
│   └── buffer.jsonl      # buffer local de store-and-forward (se crea en runtime)
├── scripts/
│   ├── test_publicar.py  # prueba rápida: 1 lectura + 1 publicación
│   └── analizar_log.py   # análisis automático de logs (tecnología, FSM, buffer, TX)
├── systemd/
│   └── nodo-monitoreo.service  # unidad systemd para auto-arranque
├── config.example.ini    # plantilla de configuración (copiar a config.ini)
├── config.ini            # configuración real del nodo (no debería versionarse)
└── requirements.txt      # dependencias de Python
```

> **Nota sobre `sensor_aht10.py`:** quedó como **código heredado** de la arquitectura
> original (lectura I2C directa, hoy en versión simulada con `random`). La arquitectura final
> usa BLE, por lo que `main.py` importa `sensor_ble.py` con `from sensor_ble import SensorBLE as
> AHT10`. El driver I2C se conserva solo como referencia y **no se ejecuta**.

## Cómo funciona el firmware

### Máquina de estados finita (FSM)
El núcleo es una FSM en `main.py` que materializa el ciclo de operación energética. Cada estado
es un método de la clase `Nodo`; el lazo principal (`run()`) ejecuta el método del estado actual,
y cada método decide cuál es el siguiente estado.

```
BOOT → CHECK_CONNECTIVITY → MEDIR → VALIDAR → PROCESAR ─┬─ (toca TX) → HAT_ON → TRANSMITIR → HAT_OFF → SLEEP
  ↑                                                      └─ (no toca) ─────────────────────────────────→ SLEEP
  └──────────────────────────── SLEEP ──────────────────────────────────────────────────────────────────┘
                                  (lectura o validación inválida) → ERROR → CHECK_CONNECTIVITY
```

| Estado | Qué hace |
|---|---|
| `BOOT` | Inicializa el driver de sensor (BLE), el cliente MQTT y el controlador del HAT. Arranca el reloj de transmisión. |
| `CHECK_CONNECTIVITY` | Verifica si hay IP en `wwan0` y registra la tecnología celular observada (`mmcli`). Mide igual aunque no haya red. |
| `MEDIR` | Lee temperatura y humedad desde la ESP32 por BLE. Si falla, pasa a `ERROR`. |
| `VALIDAR` | Descarta lecturas `None`, no numéricas, `NaN`, infinitas o fuera de rango (−40 a 85 °C; 0 a 100 % HR). Si pasa, agrega la muestra a la ventana deslizante. |
| `PROCESAR` | Calcula promedio y desviación estándar de la ventana, arma el JSON con *timestamp* UTC e ID de nodo. Decide si toca transmitir según el tiempo transcurrido. |
| `HAT_ON` | Enciende el módem según `config.ini` (GPIO o mmcli) y espera conectividad. Si no logra internet, guarda el agregado en el buffer y va a dormir. |
| `TRANSMITIR` | Publica por MQTT (QoS 1). Primero reenvía lo pendiente del buffer en orden, luego el agregado actual, y vacía el buffer al confirmar. |
| `HAT_OFF` | Apaga el módem y limpia la ruta `default` por `wwan0` para no dejar enrutamiento huérfano entre ciclos. |
| `SLEEP` | Duerme `t_medir_segundos` y vuelve a `MEDIR`. |
| `ERROR` | Intenta un `soft_reset` del sensor, espera 30 s y reintenta desde `CHECK_CONNECTIVITY`. |

### Telemetría con ventana deslizante
La ventana es un `deque(maxlen=ventana_muestras)`: siempre conserva las últimas N muestras.
Cada agregado transmitido incluye `temp_avg`, `temp_std`, `rh_avg`, `rh_std`, `n_samples` y la
lista de muestras crudas con sus *timestamps*. Sensado y transmisión están **desacoplados**: se
mide en cada ciclo (`t_medir_segundos`) pero solo se transmite cuando se cumple
`t_transmitir_segundos`.

### Duty cycling del HAT 5G
Para ahorrar energía, el módem está apagado durante el sensado y solo se enciende para
transmitir. El control es configurable vía `hat_control` en `config.ini`:

- **`gpio`** — pulsa el pin PWR_KEY del Calyx (BCM 23 / pin físico 16, según datasheet
  EBD050; pulso activo-HIGH ≥500 ms). Es el modo de mayor ahorro porque hace un *power-cycle*
  real del módulo. Requiere `RPi.GPIO`.
- **`mmcli`** — usa `mmcli --disable` para apagar el radio (no la alimentación). Más simple,
  no requiere GPIO. Es el **fallback automático** si `RPi.GPIO` no está disponible.

### Conexión del módem (`modem_net.py`)
ModemManager reasigna el índice del módem tras cada power-cycle, por lo que `modem_net.py`
**descubre el módem dinámicamente** (`_find_modem_id()` sobre `mmcli -L`, nunca `-m 0`
hardcodeado), fuerza un `--scan-modems` si no lo ve, hace `--enable` + `--simple-connect` con
el APN, y configura `wwan0` con la IP/gateway/MTU **reales** del bearer activo (no valores
fijos).

### Transmisión MQTT (`mqtt_client.py`)
Envoltorio sobre `paho-mqtt`. Publica el agregado como JSON con QoS 1 (*at-least-once*, con
PUBACK). Abre la conexión al transmitir y la cierra después, coherente con el duty cycling.

## Configuración (`config.ini`)
Parámetros principales (copiar desde `config.example.ini`):

```ini
[nodo]
node_id               = nodo1
t_medir_segundos      = 900          # intervalo de medición
t_transmitir_segundos = 3600         # intervalo de transmisión
ventana_muestras      = 4
buffer_path           = buffer.jsonl
esp32_mac             = XX:XX:XX:XX:XX:XX   # MAC de la ESP32 del sensor BLE
hat_control           = gpio         # "gpio" o "mmcli"
hat_gpio_pin          = 23           # BCM 23 = pin físico 16 (PWR_KEY del EBD050)
hat_pwrkey_ms         = 500
hat_boot_wait_s       = 15

[mqtt]
broker = broker.emqx.io
port   = 1883
topic  = vitiscience/nodo1/clima
```

## Puesta en marcha rápida
```bash
git clone https://github.com/jpccelis/nodo-5G-G8.git
cd nodo-5G-G8/nodo-monitoreo/nodo-monitoreo
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.ini config.ini    # editar broker, topic, esp32_mac, HAT
python3 scripts/test_publicar.py     # prueba de extremo a extremo
sudo venv/bin/python src/main.py config.ini   # bucle continuo
```

## Ejecución como servicio
```bash
sudo cp systemd/nodo-monitoreo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nodo-monitoreo
journalctl -u nodo-monitoreo -f      # ver el log en vivo
```

## Análisis de logs
Para resumir una corrida (tecnología celular, salud de la FSM, eventos de buffer, tasa de
transmisión y patrón de reuso del módem):
```bash
sudo journalctl -u nodo-monitoreo --since "10 hours ago" --no-pager > log.txt
python3 scripts/analizar_log.py log.txt
```

## Dependencias
- `bleak` — cliente BLE para leer el sensor desde la ESP32
- `paho-mqtt` — cliente MQTT
- `RPi.GPIO` — control del pin PWR_KEY (modo `gpio`)
- `smbus2` — solo para el driver I2C heredado (`sensor_aht10.py`)
