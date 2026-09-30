#!/usr/bin/env python3
import json
import os
import platform
import re
import configparser
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


def run_command(args, timeout=6, env=None):
    try:
        return subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env=env,
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


PACKAGE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+.-]*(?::[a-zA-Z0-9]+)?$")


def package_base_name(name):
    return name.split(":", 1)[0]


def get_upgradable_packages():
    proc = run_command(["apt", "list", "--upgradable"], timeout=30)
    upgrades = {}
    if not proc or proc.returncode != 0:
        return upgrades

    pattern = re.compile(r"^(?P<name>[^/]+)/\S+\s+(?P<version>\S+)")
    for line in proc.stdout.splitlines():
        match = pattern.match(line.strip())
        if match:
            upgrades[package_base_name(match.group("name"))] = match.group("version")
    return upgrades


def get_manual_packages():
    proc = run_command(["apt-mark", "showmanual"], timeout=30)
    if not proc or proc.returncode != 0:
        return set()
    return {package_base_name(line.strip()) for line in proc.stdout.splitlines() if line.strip()}


def get_ubuntu_packages():
    if SYSTEM != "linux":
        raise RuntimeError("Package management is currently available on Ubuntu/Linux only")

    query_format = "${binary:Package}\\t${Version}\\t${Architecture}\\t${Installed-Size}\\t${Section}\\t${db:Status-Abbrev}\\t${binary:Summary}\\n"
    proc = run_command(["dpkg-query", "-W", f"-f={query_format}"], timeout=30)
    if not proc:
        raise RuntimeError("Could not find dpkg-query. This feature requires Ubuntu or Debian")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Could not read installed packages")

    manual = get_manual_packages()
    upgrades = get_upgradable_packages()
    packages = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 6)
        if len(parts) != 7 or not parts[5].startswith("ii"):
            continue
        name, version, architecture, size, section, _status, summary = parts
        base_name = package_base_name(name)
        is_library = section.startswith("libs") or (base_name.startswith("lib") and not base_name.startswith("libreoffice"))
        packages.append(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "installedSizeKb": int(size) if size.isdigit() else 0,
                "section": section or "unknown",
                "summary": summary,
                "kind": "library" if is_library else "app",
                "manual": base_name in manual,
                "upgradeVersion": upgrades.get(base_name, ""),
            }
        )

    packages.sort(key=lambda item: item["name"].lower())
    can_manage = hasattr(os, "geteuid") and os.geteuid() == 0
    return {
        "packages": packages,
        "applications": get_installed_applications(packages),
        "platform": platform.platform(),
        "canManage": can_manage,
        "packageManager": "apt/dpkg",
    }


def get_desktop_package_owners():
    proc = run_command(["dpkg-query", "-S", "/usr/share/applications/*.desktop"], timeout=30)
    owners = {}
    if not proc:
        return owners
    for line in proc.stdout.splitlines():
        package, separator, path = line.partition(": ")
        if separator and PACKAGE_NAME_PATTERN.fullmatch(package):
            owners[os.path.realpath(path)] = package
    return owners


def get_flatpak_apps():
    proc = run_command(
        ["flatpak", "list", "--app", "--columns=application,name,version,installation"],
        timeout=30,
    )
    apps = {}
    if not proc or proc.returncode != 0:
        return apps
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            apps[parts[0]] = {"name": parts[1], "version": parts[2], "installation": parts[3]}
    return apps


def get_user_data_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd

            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".local" / "share"
        except (ImportError, KeyError):
            pass
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def get_installed_applications(packages):
    if SYSTEM != "linux":
        return []

    user_data = get_user_data_home()
    search_dirs = [
        (Path("/usr/share/applications"), "apt"),
        (Path("/usr/local/share/applications"), "manual"),
        (Path("/var/lib/snapd/desktop/applications"), "snap"),
        (Path("/var/lib/flatpak/exports/share/applications"), "flatpak"),
        (user_data / "applications", "manual"),
        (user_data / "flatpak/exports/share/applications", "flatpak"),
    ]
    package_versions = {item["name"]: item["version"] for item in packages}
    owners = get_desktop_package_owners()
    flatpak_apps = get_flatpak_apps()
    applications = {}

    for directory, default_source in search_dirs:
        if not directory.is_dir():
            continue
        for desktop_file in sorted(directory.glob("*.desktop")):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            try:
                parser.read(desktop_file, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
                continue
            try:
                hidden = entry.getboolean("Hidden", fallback=False) or entry.getboolean("NoDisplay", fallback=False)
            except ValueError:
                hidden = False
            if entry.get("Type", "Application") != "Application" or hidden:
                continue

            app_id = desktop_file.stem
            package = owners.get(os.path.realpath(desktop_file), "")
            source = default_source
            version = package_versions.get(package, "")
            if default_source == "flatpak":
                flatpak = flatpak_apps.get(app_id, {})
                version = flatpak.get("version", "")
                package = app_id
            elif default_source == "snap":
                package = entry.get("X-SnapInstanceName", app_id.split("_", 1)[0])

            name = entry.get("Name", "").strip()
            if not name:
                continue
            applications[app_id] = {
                "id": app_id,
                "name": name,
                "comment": entry.get("Comment", "").strip(),
                "exec": entry.get("Exec", "").strip(),
                "icon": entry.get("Icon", "").strip(),
                "categories": [value for value in entry.get("Categories", "").split(";") if value],
                "source": source,
                "package": package,
                "version": version,
                "removable": source == "apt" and bool(package),
            }

    return sorted(applications.values(), key=lambda item: item["name"].lower())


def validate_package_name(name):
    if not isinstance(name, str) or not PACKAGE_NAME_PATTERN.fullmatch(name):
        raise ValueError("Invalid package name")
    return name


def preview_package_removal(name):
    name = validate_package_name(name)
    proc = run_command(["apt-get", "--simulate", "remove", name], timeout=30)
    if not proc:
        raise RuntimeError("Could not find apt-get")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "Could not preview package removal")

    removed = []
    for line in proc.stdout.splitlines():
        match = re.match(r"^Remv\s+(\S+)", line)
        if match:
            removed.append(match.group(1))
    return {"package": name, "removedPackages": removed}


def remove_package(name):
    name = validate_package_name(name)
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        raise PermissionError("Run Port Killer with sudo to remove installed packages")

    command_env = os.environ.copy()
    command_env["DEBIAN_FRONTEND"] = "noninteractive"
    proc = run_command(["apt-get", "--assume-yes", "remove", name], timeout=180, env=command_env)
    if not proc:
        raise RuntimeError("Could not find apt-get")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "Package removal failed")
    return {"ok": True, "package": name}


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

        if parsed.path == "/api/packages":
            try:
                self.send_json(200, get_ubuntu_packages())
            except RuntimeError as exc:
                self.send_json(501, {"error": str(exc)})
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

        if parsed.path == "/api/packages/remove-preview":
            try:
                result = preview_package_removal(payload.get("name"))
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            else:
                self.send_json(200, result)
            return

        if parsed.path == "/api/packages/remove":
            try:
                result = remove_package(payload.get("name"))
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except PermissionError as exc:
                self.send_json(403, {"error": str(exc)})
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            else:
                self.send_json(200, result)
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
