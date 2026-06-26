#!/usr/bin/env python3
"""
Analiza el log de journalctl de nodo-monitoreo y resume:
- Tecnologia celular usada (LTE/5GNR) y conteos
- Salud de la FSM (errores, lecturas invalidas)
- Eventos de buffer (fallos de conexion -> guardado local)
- Tasa de transmision exitosa
- Patron de reuso del indice de modem (para detectar el bug del bearer)

Uso:
    python3 analizar_log.py archivo.log
    journalctl -u nodo-monitoreo --since "10 hours ago" --no-pager | python3 analizar_log.py
"""
import re
import sys
from collections import Counter
from datetime import datetime


def leer_input():
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", errors="replace") as f:
            return f.read()
    return sys.stdin.read()


def parse_ts(ts_str, anio=None):
    """Parsea timestamp estilo journalctl: 'Jun 23 13:48:38'."""
    if anio is None:
        anio = datetime.now().year
    try:
        return datetime.strptime(f"{anio} {ts_str}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None


def main():
    texto = leer_input()
    lineas_totales = texto.count("\n")

    print("=" * 60)
    print(f"ANALISIS DE LOG - {lineas_totales} lineas procesadas")
    print("=" * 60)

    # --- 1) Tecnologia celular ---
    techs = re.findall(r"Tecnologia: ([^.\n]+)\.", texto)
    tech_counts = Counter(techs)
    print("\n[1] TECNOLOGIA CELULAR (mmcli)")
    if tech_counts:
        for tech, n in tech_counts.most_common():
            print(f"    {tech:20s} {n} veces")
    else:
        print("    (no se encontraron lineas 'Tecnologia: ...')")

    # --- 2) Salud de la FSM ---
    n_error = texto.count("Estado de error")
    n_invalida = texto.count("Lectura INVALIDA")
    n_fallo_sensor = texto.count("Fallo leyendo sensor")
    n_medir = texto.count("] Lectura cruda ->")
    print("\n[2] SALUD DE LA MAQUINA DE ESTADOS (FSM)")
    print(f"    Ciclos MEDIR completados:        {n_medir}")
    print(f"    Entradas a estado ERROR:         {n_error}")
    print(f"    Lecturas invalidas (rango/NaN):  {n_invalida}")
    print(f"    Fallos leyendo sensor (excepcion): {n_fallo_sensor}")

    # --- 3) Eventos de buffer ---
    n_buffer_save = texto.count("Guardo en buffer")
    n_no_modem = texto.count("No pude encontrar modem despues del escaneo")
    flush_matches = re.findall(r"Enviando (\d+) agregados del buffer", texto)
    n_flush_events = len(flush_matches)
    n_flush_total = sum(int(x) for x in flush_matches)
    print("\n[3] EVENTOS DE BUFFER (store-and-forward)")
    print(f"    Veces que guardo en buffer (sin internet): {n_buffer_save}")
    print(f"    Veces que NO encontro modem tras escaneo:  {n_no_modem}")
    print(f"    Eventos de vaciado de buffer:               {n_flush_events}")
    print(f"    Total de agregados recuperados del buffer:  {n_flush_total}")
    if n_buffer_save != n_flush_total:
        print(f"    *** ALERTA: guardados ({n_buffer_save}) != recuperados ({n_flush_total})")
        print(f"        Puede haber agregados pendientes sin enviar al final del log.")
    else:
        print(f"    OK: todo lo guardado en buffer fue recuperado (0% perdida de datos)")

    # --- 4) Tasa de transmision ---
    n_toca_transmitir = texto.count("Toca transmitir")
    n_tx_ok = texto.count("Agregado transmitido OK")
    n_tx_fail = texto.count("No se pudo transmitir")
    print("\n[4] TASA DE TRANSMISION")
    print(f"    Ciclos que activaron transmision:     {n_toca_transmitir}")
    print(f"    Transmisiones exitosas (al toque):    {n_tx_ok}")
    print(f"    Fallos de transmision MQTT (broker):  {n_tx_fail}")
    if n_toca_transmitir:
        pct_directo = 100 * (n_toca_transmitir - n_buffer_save) / n_toca_transmitir
        print(f"    % de ciclos con internet al primer intento: {pct_directo:.1f}%")

    # --- 5) Patron de reuso del indice de modem ---
    modem_events = re.findall(
        r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}).*?\[modem_net\] Modem activo: (\d+)",
        texto,
    )
    print("\n[5] PATRON DE REUSO DEL MODEM (diagnostico del bug del bearer)")
    if modem_events:
        runs = []
        actual_id = None
        racha = 0
        for ts, mid in modem_events:
            if mid == actual_id:
                racha += 1
            else:
                if actual_id is not None:
                    runs.append((actual_id, racha))
                actual_id = mid
                racha = 1
        runs.append((actual_id, racha))

        racha_lengths = Counter(r[1] for r in runs[:-1])
        print(f"    Total de indices de modem distintos usados: {len(runs)}")
        print(f"    Distribucion de 'usos antes de cambiar de indice':")
        for largo, cant in sorted(racha_lengths.items()):
            marca = "  <-- patron del bug (era 3 antes del fix)" if largo == 3 else ""
            print(f"        {largo} usos seguidos: {cant} veces{marca}")
        print(f"    (Racha final, aun en curso: modem {runs[-1][0]} con {runs[-1][1]} usos)")

        if racha_lengths and 3 in racha_lengths:
            print("    *** El patron de fallar cada 3-4 usos SIGUE presente.")
            print("        El fix de bearer-disconnect no resolvio el problema solo.")
        elif not n_buffer_save:
            print("    OK: no hubo cambios de indice de modem en todo el periodo")
            print("        (el modem se mantuvo estable, sin re-escaneos).")
    else:
        print("    (no se encontraron lineas '[modem_net] Modem activo: N')")

    # --- 6) Gaps entre eventos de buffer-fail (periodicidad) ---
    fail_ts_raw = re.findall(
        r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}).*?HAT encendido pero SIN internet",
        texto,
    )
    if len(fail_ts_raw) >= 2:
        fechas = [parse_ts(t) for t in fail_ts_raw]
        fechas = [f for f in fechas if f is not None]
        gaps = [(fechas[i+1] - fechas[i]).total_seconds() for i in range(len(fechas)-1)]
        print("\n[6] INTERVALOS ENTRE FALLOS DE CONEXION (segundos)")
        print(f"    Cantidad de fallos: {len(fechas)}")
        print(f"    Gaps: {[round(g) for g in gaps]}")
        if gaps:
            print(f"    Promedio: {sum(gaps)/len(gaps):.0f}s  |  Min: {min(gaps):.0f}s  |  Max: {max(gaps):.0f}s")

    print("\n" + "=" * 60)
    print("FIN DEL ANALISIS")
    print("=" * 60)


if __name__ == "__main__":
    main()
