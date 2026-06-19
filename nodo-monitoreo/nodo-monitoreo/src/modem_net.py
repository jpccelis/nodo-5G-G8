import subprocess
import time


def _run(cmd, timeout=10, check=False):
    return subprocess.run(
        cmd,
        timeout=timeout,
        check=check,
        capture_output=True,
        text=True,
    )


def _extract_field(mmcli_output, field):
    for line in mmcli_output.splitlines():
        if f"{field}:" in line:
            return line.split(":", 1)[1].strip()
    return None


def _find_modem_id():
    """
    Encuentra automaticamente el ID actual del modem en ModemManager.
    Ejemplo: si mmcli -L muestra /Modem/1, devuelve "1".
    """
    r = _run(["mmcli", "-L"], timeout=10, check=False)
    salida = (r.stdout or "") + "\n" + (r.stderr or "")

    for token in salida.replace("[", " ").replace("]", " ").split():
        if "/Modem/" in token:
            return token.split("/Modem/")[-1].strip()

    return None


def configure_wwan_from_bearer(modem_id="auto", apn="bam.entelpcs.cl", metric="50"):
    """
    Conecta el modem y configura wwan0 usando la IP/gateway reales
    entregados por ModemManager en el bearer activo.
    """
    if modem_id == "auto":
        modem_id = _find_modem_id()

    if not modem_id:
        print("[modem_net] No hay modem visible. Forzando escaneo...")
        _run(["sudo", "mmcli", "--scan-modems"], timeout=10, check=False)
        time.sleep(15)
        modem_id = _find_modem_id()

    if not modem_id:
        print("[modem_net] No pude encontrar modem despues del escaneo.")
        return False

    print(f"[modem_net] Modem activo: {modem_id}")
    # A veces ModemManager ve /dev/cdc-wdm0, pero aun no registra el modem.
    # Forzamos un escaneo antes de habilitar.
    _run(["sudo", "mmcli", "--scan-modems"], timeout=10, check=False)
    time.sleep(12)

    # Habilitar modem. Puede demorarse; por eso damos un timeout mas amplio.
    _run(["sudo", "mmcli", "-m", modem_id, "--enable"], timeout=35, check=False)
    time.sleep(3)

    r = _run(
        ["sudo", "mmcli", "-m", modem_id, f"--simple-connect=apn={apn}"],
        timeout=40,
        check=False,
    )

    output = (r.stdout or "") + "\n" + (r.stderr or "")
    if output.strip():
        print(output.strip())

    time.sleep(5)

    bearer_id = None

    for token in output.replace("'", " ").replace(",", " ").split():
        if "/Bearer/" in token:
            bearer_id = token.split("/Bearer/")[-1].strip().strip(".")
            break

    if not bearer_id:
        m = _run(["mmcli", "-m", modem_id], timeout=10, check=False)
        for line in (m.stdout or "").splitlines():
            if "/Bearer/" in line:
                bearer_id = line.split("/Bearer/")[-1].split()[0].strip()

    if not bearer_id:
        print("[modem_net] No pude detectar bearer activo.")
        return False

    print(f"[modem_net] Bearer activo: {bearer_id}")

    b = _run(["mmcli", "-b", bearer_id], timeout=10, check=False)
    info = b.stdout or ""

    iface = _extract_field(info, "interface") or "wwan0"
    address = _extract_field(info, "address")
    prefix = _extract_field(info, "prefix")
    gateway = _extract_field(info, "gateway")
    mtu = _extract_field(info, "mtu") or "1500"

    if not address or not prefix or not gateway:
        print("[modem_net] Faltan datos IP en el bearer:")
        print(info)
        return False

    print(f"[modem_net] Configurando {iface}: {address}/{prefix}, gateway {gateway}, mtu {mtu}")

    _run(["sudo", "ip", "link", "set", iface, "up"], timeout=5, check=False)
    _run(["sudo", "ip", "addr", "replace", f"{address}/{prefix}", "dev", iface], timeout=5, check=False)
    _run(
        ["sudo", "ip", "route", "replace", "default", "via", gateway, "dev", iface, "metric", metric, "mtu", mtu],
        timeout=5,
        check=False,
    )

    time.sleep(2)
    return True
