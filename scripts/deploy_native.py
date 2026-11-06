"""Deploy the current Axis revision to Debian using uv, a user service and Tailscale.

Run from the local checkout: .venv/bin/python scripts/deploy_native.py
The configured model credential is transferred over SSH stdin, never in arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener, urlopen


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def output(args: list[str]) -> str:
    return run(args, capture_output=True, text=True).stdout.strip()


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        handle.write(text)
    os.replace(temporary, path)


def verify(base: str, secret: str, websocket_url: str, *, seconds: int) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + seconds
    while True:
        try:
            with opener.open(base + "/", timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError("La WebUI no responde correctamente.")
            try:
                with opener.open(base + "/webui/bootstrap", timeout=10):
                    raise RuntimeError("La WebUI permite acceso sin contraseña.")
            except HTTPError as exc:
                if exc.code != 401:
                    raise
            request = Request(base + "/webui/bootstrap", headers={"X-Nanobot-Auth": secret})
            with opener.open(request, timeout=10) as response:
                data = json.load(response)
            if data.get("ws_url") != websocket_url:
                raise RuntimeError("El gateway no anuncia el WebSocket de la tailnet.")
            return
        except HTTPError:
            raise
        except (URLError, TimeoutError, ConnectionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def deployment_config(existing: dict, payload: dict, workspace: Path, address: str) -> dict:
    from nanobot.channels.websocket.runtime import WebSocketConfig
    from nanobot.config.schema import Config

    existing.setdefault("agents", {}).setdefault("defaults", {}).update({
        "modelPreset": payload["preset_name"], "workspace": str(workspace),
        "timezone": payload["timezone"], "timezoneMode": "manual",
    })
    existing.setdefault("modelPresets", {})[payload["preset_name"]] = payload["preset"]
    existing.setdefault("providers", {})[payload["provider"]] = payload["provider_config"]
    existing.setdefault("tools", {})["restrictToWorkspace"] = False
    existing["tools"].setdefault("exec", {})["sandbox"] = ""
    existing["tools"]["web"] = payload["web"]
    channels = existing.setdefault("channels", {})
    websocket = WebSocketConfig.model_validate(channels.get("websocket") or {})
    if websocket.ssl_certfile or websocket.ssl_keyfile or websocket.unix_socket_path:
        raise RuntimeError("La WebUI existente usa TLS o un socket Unix; requiere revisar el proxy.")
    websocket.enabled = True
    websocket.host = "127.0.0.1"
    websocket.port = 8765
    websocket.token_issue_secret = websocket.token_issue_secret.strip() or secrets.token_urlsafe(32)
    websocket.websocket_requires_token = True
    websocket.public_ws_url = f"wss://{address}{websocket.path}"
    channels["websocket"] = websocket.model_dump(by_alias=True, exclude_none=True)
    existing.setdefault("gateway", {}).update({"host": "127.0.0.1", "port": 18790})
    Config.model_validate(existing)
    return existing


def remote_main(payload: dict) -> None:
    if os.geteuid() == 0:
        raise RuntimeError("El despliegue debe ejecutarse como roavru, no como root.")
    for name in ["git", "python3", "systemctl", "loginctl", "tailscale"]:
        if not shutil.which(name):
            raise RuntimeError(f"Falta el comando {name} en el servidor.")
    run(["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL)
    status = json.loads(output(["tailscale", "status", "--json"]))
    hostname = (status.get("Self", {}).get("DNSName") or "").rstrip(".")
    if status.get("BackendState") != "Running" or not hostname.endswith(".ts.net"):
        raise RuntimeError("El servidor necesita estar conectado a Tailscale con MagicDNS.")
    serve = json.loads(output(["tailscale", "serve", "status", "--json"])) or {}
    target = "http://127.0.0.1:8765"
    address = f"{hostname}:8443"
    if serve.get("AllowFunnel", {}).get(address):
        raise RuntimeError("El puerto 8443 tiene Funnel habilitado; hay que elegir un puerto privado.")
    if "8443" in serve.get("TCP", {}):
        handlers = serve.get("Web", {}).get(address, {}).get("Handlers", {})
        if handlers != {"/": {"Proxy": target}}:
            raise RuntimeError("El puerto 8443 ya pertenece a otro servicio de Tailscale.")

    home = Path.home()
    source = home / "axis"
    state = home / ".local" / "share" / "axis"
    config_path = state / "config.json"
    unit = home / ".config" / "systemd" / "user" / "axis.service"
    if unit.exists() and str(config_path) not in unit.read_text():
        raise RuntimeError("Ya existe otro axis.service; se conserva sin modificarlo.")
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "axis.service"]).returncode == 0
    if not active:
        for port in [8765, 18790]:
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"El puerto local {port} ya está ocupado por otro proceso.")
    if source.exists():
        if not (source / ".git").is_dir():
            raise RuntimeError(f"{source} ya existe y no es el repositorio de Axis.")
        origin = output(["git", "-C", str(source), "remote", "get-url", "origin"])
        if origin.removesuffix(".git") != payload["repository"].removesuffix(".git"):
            raise RuntimeError("El repositorio existente tiene otro origin.")
        if output(["git", "-C", str(source), "status", "--porcelain"]):
            raise RuntimeError("Axis tiene cambios locales en el servidor; se conservan sin actualizarlo.")

    username = pwd.getpwuid(os.getuid()).pw_name
    if output(["loginctl", "show-user", username, "-p", "Linger", "--value"]) != "yes":
        result = subprocess.run(["loginctl", "enable-linger", username], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(
                f"Para arrancar sin una sesión abierta, ejecuta en Debian: sudo loginctl enable-linger {username}. "
                "Después repite este despliegue."
            )
    if not source.exists():
        run(["git", "clone", payload["repository"], str(source)])
    different_revision = output(["git", "-C", str(source), "rev-parse", "HEAD"]) != payload["commit"]
    if different_revision:
        run(["git", "-C", str(source), "fetch", "origin", payload["commit"]])
    if active:
        run(["systemctl", "--user", "stop", "axis.service"])
    if different_revision:
        run(["git", "-C", str(source), "checkout", "--detach", payload["commit"]])

    uv = shutil.which("uv") or str(home / ".local" / "bin" / "uv")
    if not Path(uv).is_file():
        print("Instalando uv para este usuario…", flush=True)
        with urlopen("https://astral.sh/uv/install.sh", timeout=60) as response:
            installer = response.read()
        run(["sh"], input=installer, env={**os.environ, "UV_UNMANAGED_INSTALL": str(home / ".local" / "bin")})
    environment = source / ".venv"
    if not environment.exists():
        run([uv, "venv", "--python", "3.12", str(environment)])
    python = environment / "bin" / "python"
    run([uv, "pip", "install", "--python", str(python), "-e", str(source)])
    run([str(python), "-c", "from nanobot.bun import ensure_bun; from nanobot.webui.build import ensure_webui_bundle; ensure_webui_bundle(mode='auto', runner=ensure_bun(), output=print)"], cwd=source)

    # Configuration imports need nanobot's installed dependencies, not system Python.
    run([
        str(python), "-c",
        "import json, sys; from scripts.deploy_native import activate_server; activate_server(json.load(sys.stdin))",
    ], input=json.dumps({**payload, "address": address}), text=True, cwd=source)


def activate_server(payload: dict) -> None:
    home = Path.home()
    source = home / "axis"
    state = home / ".local" / "share" / "axis"
    config_path = state / "config.json"
    unit = home / ".config" / "systemd" / "user" / "axis.service"
    environment = source / ".venv"
    python = environment / "bin" / "python"
    address = payload["address"]
    target = "http://127.0.0.1:8765"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = state / "workspace"
    workspace.mkdir(exist_ok=True, mode=0o700)
    existing = json.loads(config_path.read_text()) if config_path.exists() else {}
    existing = deployment_config(existing, payload, workspace, address)
    websocket = existing["channels"]["websocket"]
    secret = websocket["tokenIssueSecret"]
    if config_path.exists():
        private_write(state / f"config.before-deploy-{time.time_ns()}.json", config_path.read_text())
    private_write(config_path, json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    run([str(python), "-c", "import sys; from pathlib import Path; from nanobot.config.loader import load_config; load_config(Path(sys.argv[1])); print('Configuración válida.')", str(config_path)])

    service = "\n".join([
        "[Unit]", "Description=Axis personal assistant", "After=network-online.target", "",
        "[Service]", "Type=simple", f"WorkingDirectory={workspace}",
        f"ExecStart={environment}/bin/nanobot gateway --foreground --config {config_path}",
        "Restart=on-failure", "RestartSec=5", "Environment=PYTHONUNBUFFERED=1",
        f"Environment=TZ={payload['timezone']}",
        f"Environment=PATH={environment}/bin:{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "UMask=0077", "",
        "[Install]", "WantedBy=default.target", "",
    ])
    private_write(unit, service)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "axis.service"])
    websocket_url = websocket["publicWsUrl"]
    verify(target, secret, websocket_url, seconds=60)
    run(["tailscale", "serve", "--bg", "--https=8443", target])
    print("Esperando HTTPS; el primer certificado puede tardar hasta dos minutos…", flush=True)
    verify(f"https://{address}", secret, websocket_url, seconds=120)
    access_file = state / "webui-access-url.txt"
    private_write(access_file, f"https://{address}/#/?bootstrapSecret={quote(secret, safe='')}\n")
    print(f"\nAxis desplegado en {source}; servicio axis.service activo.")
    print(f"WebUI privada: https://{address}/")
    print(f"Enlace privado de acceso guardado en: {access_file}")
    print("Estado: systemctl --user status axis.service")
    print("Logs: journalctl --user -u axis.service -f")


def local_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="roavru@debian")
    parser.add_argument("--config", type=Path, default=Path.home() / ".nanobot" / "config.json")
    args = parser.parse_args()
    from nanobot.config.loader import load_config, resolve_config_env_vars

    root = Path(__file__).resolve().parents[1]
    if output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"]):
        raise RuntimeError("Hay cambios sin commit; guarda y sube la revisión antes de desplegar desde GitHub.")
    config = resolve_config_env_vars(load_config(args.config), config_path=args.config).model_dump(mode="json", by_alias=True)
    name = config["agents"]["defaults"].get("modelPreset")
    preset = config.get("modelPresets", {}).get(name)
    if not preset or preset.get("provider") in {None, "auto"}:
        raise RuntimeError("Selecciona un preset con proveedor explícito antes de desplegar.")
    provider = preset["provider"]
    credential = config.get("providers", {}).get(provider)
    if not credential or not credential.get("apiKey"):
        raise RuntimeError("Este despliegue transfiere un proveedor con API key; no transfiere sesiones OAuth.")
    payload = {
        "repository": output(["git", "-C", str(root), "remote", "get-url", "origin"]),
        "commit": output(["git", "-C", str(root), "rev-parse", "HEAD"]),
        "preset_name": name, "preset": preset, "provider": provider, "provider_config": credential,
        "timezone": config["agents"]["defaults"]["timezone"], "web": config["tools"]["web"],
    }
    remote_command = shlex.join(["python3", "-c", Path(__file__).read_text(), "--remote-worker"])
    run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.host, remote_command],
        input=json.dumps(payload), text=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--remote-worker"]:
            remote_main(json.load(sys.stdin))
        else:
            local_main()
    except subprocess.CalledProcessError as exc:
        print(f"Falló el comando de despliegue (código {exc.returncode}).", file=sys.stderr)
        raise SystemExit(exc.returncode) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
