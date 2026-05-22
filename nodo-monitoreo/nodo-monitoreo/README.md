# Nodo de Monitoreo Ambiental — Grupo P8 / VitiScience (IEE3112)

Firmware del nodo de monitoreo microclimático con comunicación 5G.
Corre sobre **Raspberry Pi 4** + **Teltonika Calyx EBD050 (5G)** + sensor **AHT10**.

## ¿Qué hace?
Mide temperatura y humedad cada cierto intervalo, arma un paquete de datos y lo
transmite por **MQTT sobre 5G**. Si no hay conexión, guarda las mediciones en un
buffer local y las reenvía cuando la red vuelve (*store-and-forward*).

## Estructura
```
nodo-monitoreo/
├── src/
│   ├── sensor_aht10.py   # driver del sensor AHT10 por I2C
│   ├── mqtt_client.py    # publicación por MQTT (paho-mqtt)
│   └── main.py           # máquina de estados (FSM) principal
├── scripts/
│   └── test_publicar.py  # prueba rápida: 1 lectura + 1 publicación
├── systemd/
│   └── nodo-monitoreo.service  # para que arranque solo (opcional)
├── config.example.ini    # plantilla de configuración (copiar a config.ini)
├── requirements.txt      # dependencias de Python
└── .gitignore
```

## Puesta en marcha rápida
```bash
git clone <url-del-repo>
cd nodo-monitoreo
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.ini config.ini   # editar broker/topic
python3 scripts/test_publicar.py    # prueba de extremo a extremo
python3 src/main.py config.ini      # bucle continuo
```

## Máquina de estados
`BOOT → CHECK_CONNECTIVITY → MEDIR → PROCESAR → TRANSMITIR → SLEEP` (y `ERROR` ante fallos).

> Hardware confirmado: arquitectura 5G integrada de nodo único. La guía de puesta
> en marcha del módem está en `GUIA_VIERNES.md`.
