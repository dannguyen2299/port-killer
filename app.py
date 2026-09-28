#!/usr/bin/env python3
import json
import os
import platform
import re
import signal
import socket
import errno
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
SYSTEM = platform.system().lower()


def run_command(args):
    try:
        return subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=6,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, exc.stdout or "", exc.stderr or "Command timed out")


def parse_address(address):
    address = address.strip()
    if address.startswith("["):
        match = re.match(r"^\[(?P<host>.*)\]:(?P<port>\d+)$", address)
        if match:
            return match.group("host"), int(match.group("port"))
    if ":" in address:
        host, port = address.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return address, None


def parse_users(users):
    result = []
    for name, pid in re.findall(r'\("([^"]+)",pid=(\d+)', users):
        result.append({"name": name, "pid": int(pid)})
    return result


def get_linux_listeners():
    proc = run_command(["ss", "-H", "-ltnup"])
    if not proc or proc.returncode != 0:
        return []

    listeners = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue

        protocol = parts[0].lower()
        local_address = parts[4]
        host, port = parse_address(local_address)
        if port is None:
            continue

        users = parse_users(parts[5] if len(parts) > 5 else "")
        if not users:
            listeners.append(
                {
                    "protocol": protocol,
                    "host": host,
                    "port": port,
                    "process": "",
                    "pid": None,
                    "canKill": False,
                }
            )
            continue

        for user in users:
            listeners.append(
                {
                    "protocol": protocol,
                    "host": host,
                    "port": port,
                    "process": user["name"],
                    "pid": user["pid"],
                    "canKill": user["pid"] != os.getpid(),
                }
            )
    return sorted(listeners, key=lambda item: (item["port"], item["protocol"], item.get("pid") or 0))


def get_macos_listeners():
    proc = run_command(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-iUDP"])
    if not proc or proc.returncode != 0:
        return []

    listeners = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue

        command, pid, protocol, name = parts[0], parts[1], parts[7].lower(), parts[8]
        if not pid.isdigit():
            continue
        host, port = parse_address(name.split("->", 1)[0])
        if port is None:
            continue

        listeners.append(
            {
                "protocol": protocol,
                "host": host,
                "port": port,
                "process": command,
                "pid": int(pid),
                "canKill": int(pid) != os.getpid(),
            }
        )
    return sorted(listeners, key=lambda item: (item["port"], item["protocol"], item.get("pid") or 0))


def get_windows_task_names():
    proc = run_command(["tasklist", "/FO", "CSV", "/NH"])
    names = {}
    if not proc or proc.returncode != 0:
        return names

    for line in proc.stdout.splitlines():
        fields = re.findall(r'"([^"]*)"', line)
        if len(fields) >= 2 and fields[1].isdigit():
            names[int(fields[1])] = fields[0]
    return names


def get_windows_listeners():
    proc = run_command(["netstat", "-ano"])
    if not proc or proc.returncode != 0:
        return []

    task_names = get_windows_task_names()
    listeners = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue

        protocol = parts[0].lower()
        if protocol not in ("tcp", "udp"):
            continue

        if protocol == "tcp":
            if len(parts) < 5 or parts[3].upper() != "LISTENING":
                continue
            local_address, pid_text = parts[1], parts[4]
        else:
            local_address, pid_text = parts[1], parts[-1]

        if not pid_text.isdigit():
            continue
        host, port = parse_address(local_address)
        if port is None:
            continue

        pid = int(pid_text)
        listeners.append(
            {
                "protocol": protocol,
                "host": host,
                "port": port,
                "process": task_names.get(pid, ""),
                "pid": pid,
                "canKill": pid != os.getpid(),
            }
        )
    return sorted(listeners, key=lambda item: (item["port"], item["protocol"], item.get("pid") or 0))


def get_listeners():
    if SYSTEM == "darwin":
        return get_macos_listeners()
    if SYSTEM == "windows":
        return get_windows_listeners()
    return get_linux_listeners()


def get_docker_ports():
    proc = run_command(["docker", "ps", "--format", "{{json .}}"])
    containers = []
    if not proc or proc.returncode != 0:
        return containers

    port_pattern = re.compile(r"(?:(?P<host>[\d.]+|\[::\]):)?(?P<public>\d+)->(?P<private>\d+)/(tcp|udp)")
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        ports = []
        for match in port_pattern.finditer(row.get("Ports", "")):
            ports.append(
                {
                    "host": match.group("host") or "",
                    "publicPort": int(match.group("public")),
                    "privatePort": int(match.group("private")),
                }
            )

        containers.append(
            {
                "id": row.get("ID", ""),
                "image": row.get("Image", ""),
                "name": row.get("Names", ""),
                "status": row.get("Status", ""),
                "ports": ports,
            }
        )
    return containers


def build_snapshot():
    listeners = get_listeners()
    containers = get_docker_ports()
    containers_by_port = {}
    for container in containers:
        for port in container["ports"]:
            containers_by_port.setdefault(port["publicPort"], []).append(container)

    for listener in listeners:
        listener["containers"] = containers_by_port.get(listener["port"], [])

    return {"listeners": listeners, "containers": containers}


def kill_process(pid, sig):
    if SYSTEM == "windows":
        args = ["taskkill", "/PID", str(pid), "/T"]
        if sig == signal.SIGKILL:
            args.append("/F")
        proc = run_command(args)
        if not proc:
            raise RuntimeError("Could not find the taskkill command")
        if proc.returncode != 0:
            message = proc.stderr.strip() or proc.stdout.strip() or "taskkill failed"
            if "not found" in message.lower():
                raise ProcessLookupError(message)
            raise PermissionError(message)
        return

    os.kill(pid, sig)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            return

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if parsed.path == "/api/listeners":
            self.send_json(200, build_snapshot())
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json(400, {"error": "Invalid JSON"})
            return

        if parsed.path == "/api/kill":
            pid = payload.get("pid")
            sig = signal.SIGTERM if payload.get("signal") != "SIGKILL" else signal.SIGKILL
            if not isinstance(pid, int) or pid <= 0:
                self.send_json(400, {"error": "Invalid PID"})
                return
            if pid == os.getpid():
                self.send_json(400, {"error": "Cannot kill this app's own process"})
                return
            try:
                kill_process(pid, sig)
            except ProcessLookupError:
                self.send_json(404, {"error": "Process no longer exists"})
            except PermissionError as exc:
                self.send_json(403, {"error": str(exc) or "Permission denied. Run the app with sudo or Administrator privileges to kill processes owned by another user."})
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            else:
                self.send_json(200, {"ok": True, "pid": pid, "signal": sig.name})
            return

        if parsed.path == "/api/docker-stop":
            container_id = payload.get("id", "")
            if not isinstance(container_id, str) or not re.match(r"^[a-fA-F0-9]{6,64}$", container_id):
                self.send_json(400, {"error": "Invalid container ID"})
                return
            proc = run_command(["docker", "stop", container_id])
            if not proc:
                self.send_json(500, {"error": "Could not find the docker command"})
            elif proc.returncode != 0:
                self.send_json(500, {"error": proc.stderr.strip() or proc.stdout.strip() or "docker stop failed"})
            else:
                self.send_json(200, {"ok": True, "id": container_id})
            return

        self.send_json(404, {"error": "Not found"})


def create_server():
    for port in range(PORT, PORT + 50):
        try:
            return ThreadingHTTPServer((HOST, port), Handler), port
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    raise OSError(f"Could not find an available port from {PORT} to {PORT + 49}")


def open_browser(url):
    if os.environ.get("PORT_KILLER_NO_BROWSER") == "1":
        return
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()


if __name__ == "__main__":
    server, active_port = create_server()
    url = f"http://{HOST}:{active_port}"
    print(f"Port Killer is running at {url}")
    print("Press Ctrl+C to stop.")
    open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPort Killer stopped.")
