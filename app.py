#!/usr/bin/env python3
import json
import os
import platform
import re
import calendar
import configparser
import plistlib
import signal
import socket
import errno
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
SYSTEM = platform.system().lower()


def run_command(args, timeout=6, env=None, encoding=None):
    try:
        return subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env=env,
            encoding=encoding,
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


def parse_etime(text):
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text)
    parts = [int(part) for part in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def get_process_start_times(pids):
    pids = sorted({pid for pid in pids if pid})
    if not pids:
        return {}

    starts = {}
    if SYSTEM == "windows":
        script = (
            "Get-Process -Id %s -ErrorAction SilentlyContinue | ForEach-Object "
            "{ '{0} {1}' -f $_.Id, ([DateTimeOffset]$_.StartTime).ToUnixTimeSeconds() }"
        ) % ",".join(str(pid) for pid in pids)
        proc = run_command(["powershell", "-NoProfile", "-Command", script], timeout=10)
        if proc and proc.returncode == 0:
            for line in proc.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
                    starts[int(fields[0])] = int(fields[1])
        return starts

    proc = run_command(["ps", "-o", "pid=,etime=", "-p", ",".join(str(pid) for pid in pids)])
    if proc and proc.stdout:
        now = int(time.time())
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0].isdigit():
                try:
                    starts[int(fields[0])] = now - parse_etime(fields[1])
                except ValueError:
                    continue
    return starts


def get_container_start_times(ids):
    if not ids:
        return {}
    proc = run_command(["docker", "inspect", "--format", "{{.Id}} {{.State.StartedAt}}", *ids])
    starts = {}
    if not proc or not proc.stdout:
        return starts
    for line in proc.stdout.splitlines():
        container_id, _, started = line.partition(" ")
        match = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?", started)
        if not match:
            continue
        stamp = calendar.timegm(tuple(int(part) for part in match.groups()[:6]) + (0, 0, 0))
        stamp += float(match.group(7) or 0)
        starts[container_id[:12]] = stamp
    return starts


def build_snapshot():
    listeners = get_listeners()
    start_times = get_process_start_times(item.get("pid") for item in listeners)
    for listener in listeners:
        listener["startedAt"] = start_times.get(listener.get("pid"))
    containers = get_docker_ports()
    containers_by_port = {}
    for container in containers:
        for port in container["ports"]:
            containers_by_port.setdefault(port["publicPort"], []).append(container)

    container_starts = get_container_start_times([c["id"] for c in containers])
    for listener in listeners:
        listener["containers"] = containers_by_port.get(listener["port"], [])
        # A published container port is "opened" when the container starts.
        started = [container_starts[c["id"]] for c in listener["containers"] if c["id"] in container_starts]
        if started:
            listener["startedAt"] = max(started)

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
        "platformName": "Linux",
        "canManage": can_manage,
        "supportsPackages": True,
        "packageManager": "apt/dpkg",
        "manageHint": "Browsing is available. To remove APT packages, restart this app with sudo.",
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


def get_user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and SYSTEM != "windows":
        try:
            import pwd

            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (ImportError, KeyError):
            pass
    return Path.home()


def get_homebrew_inventory():
    brew = "brew"
    for candidate in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")):
        if candidate.is_file():
            brew = str(candidate)
            break
    proc = run_command([brew, "info", "--json=v2", "--installed"], timeout=45)
    if not proc or proc.returncode != 0:
        return [], {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [], {}

    packages = []
    for formula in data.get("formulae", []):
        installed = formula.get("installed") or []
        version = installed[-1].get("version", "") if installed else ""
        name = formula.get("name", "")
        if not name:
            continue
        description = formula.get("desc", "") or ""
        is_library = name.startswith("lib") or " library" in description.lower()
        packages.append(
            {
                "name": name,
                "version": version,
                "architecture": platform.machine(),
                "installedSizeKb": 0,
                "section": "homebrew",
                "summary": description,
                "kind": "library" if is_library else "app",
                "manual": bool(installed and installed[-1].get("installed_on_request", True)),
                "upgradeVersion": "",
            }
        )

    cask_apps = {}
    for cask in data.get("casks", []):
        token = cask.get("token", "")
        version = cask.get("installed") or cask.get("version", "")
        for artifact in cask.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            for app_name in artifact.get("app", []):
                cask_apps[app_name] = {"token": token, "version": version}
    return sorted(packages, key=lambda item: item["name"].lower()), cask_apps


def iter_macos_app_bundles(root):
    if not root.is_dir():
        return
    for current, directories, _files in os.walk(root):
        app_directories = [name for name in directories if name.lower().endswith(".app")]
        for name in app_directories:
            yield Path(current) / name
        directories[:] = [name for name in directories if name not in app_directories]


def get_macos_inventory():
    packages, cask_apps = get_homebrew_inventory()
    roots = [
        Path("/System/Library/CoreServices/Applications"),
        Path("/System/Applications"),
        Path("/Applications"),
        get_user_home() / "Applications",
    ]
    applications = {}
    for root in roots:
        for app_bundle in iter_macos_app_bundles(root):
            try:
                with (app_bundle / "Contents/Info.plist").open("rb") as plist_file:
                    info = plistlib.load(plist_file)
            except (OSError, plistlib.InvalidFileException):
                continue
            app_id = str(info.get("CFBundleIdentifier") or app_bundle.stem)
            name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or app_bundle.stem)
            version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "")
            cask = cask_apps.get(app_bundle.name, {})
            if cask:
                source = "homebrew"
                package = cask.get("token", "")
                version = str(cask.get("version") or version)
            elif str(app_bundle).startswith("/System/"):
                source = "system"
                package = app_id
            elif (app_bundle / "Contents/_MASReceipt/receipt").exists():
                source = "app-store"
                package = app_id
            else:
                source = "manual"
                package = app_id
            applications[app_id] = {
                "id": app_id,
                "name": name,
                "comment": str(info.get("NSHumanReadableCopyright") or ""),
                "exec": str(app_bundle),
                "icon": str(info.get("CFBundleIconFile") or ""),
                "categories": ["macOS Application"],
                "source": source,
                "package": package,
                "version": version,
                "removable": False,
            }
    return {
        "packages": packages,
        "applications": sorted(applications.values(), key=lambda item: item["name"].lower()),
        "platform": platform.platform(),
        "platformName": "macOS",
        "canManage": False,
        "supportsPackages": bool(packages),
        "packageManager": "Applications/Homebrew",
        "manageHint": "Application inventory is available. Removal from macOS is not enabled yet.",
    }


def get_windows_registry_apps():
    try:
        import winreg
    except ImportError:
        return []

    uninstall_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    locations = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "64-bit"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "32-bit"),
        (winreg.HKEY_CURRENT_USER, 0, "Current user"),
    ]
    applications = {}
    for hive, view_flag, scope in locations:
        try:
            root = winreg.OpenKey(hive, uninstall_path, 0, winreg.KEY_READ | view_flag)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    key_name = winreg.EnumKey(root, index)
                    entry = winreg.OpenKey(root, key_name)
                except OSError:
                    continue
                with entry:
                    def value(name, default=""):
                        try:
                            return winreg.QueryValueEx(entry, name)[0]
                        except OSError:
                            return default

                    display_name = str(value("DisplayName")).strip()
                    release_type = str(value("ReleaseType")).lower()
                    if (
                        not display_name
                        or value("SystemComponent", 0) == 1
                        or value("ParentKeyName")
                        or release_type in ("update", "hotfix", "security update")
                    ):
                        continue
                    publisher = str(value("Publisher")).strip()
                    display_version = str(value("DisplayVersion")).strip()
                    app_id = f"{scope}:{key_name}"
                    source = "msi" if value("WindowsInstaller", 0) == 1 else "registry"
                    identity = (display_name.lower(), display_version.lower(), publisher.lower())
                    applications[identity] = {
                        "id": key_name,
                        "name": display_name,
                        "comment": publisher,
                        "exec": str(value("InstallLocation")).strip(),
                        "icon": str(value("DisplayIcon")).strip(),
                        "categories": [publisher or scope],
                        "source": source,
                        "package": key_name,
                        "version": display_version,
                        "removable": False,
                    }
    return sorted(applications.values(), key=lambda item: item["name"].lower())


def get_windows_store_apps():
    command = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        "$packages = @(Get-AppxPackage | Where-Object { -not $_.IsFramework -and -not $_.IsResourcePackage } | "
        "Select-Object Name,PackageFullName,PackageFamilyName,Version,Publisher,InstallLocation,NonRemovable); "
        "$start = @(Get-StartApps); "
        "[PSCustomObject]@{ Packages = $packages; Start = $start } | ConvertTo-Json -Depth 4 -Compress"
    )
    proc = run_command(["powershell.exe", "-NoProfile", "-Command", command], timeout=45, encoding="utf-8")
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, dict):
        return []
    start_rows = rows.get("Start", [])
    package_rows = rows.get("Packages", [])
    if isinstance(start_rows, dict):
        start_rows = [start_rows]
    if isinstance(package_rows, dict):
        package_rows = [package_rows]
    start_names = {}
    for start_app in start_rows:
        app_user_model_id = str(start_app.get("AppID") or "")
        family = app_user_model_id.split("!", 1)[0]
        if family:
            start_names.setdefault(family, []).append(str(start_app.get("Name") or "").strip())
    applications = []
    for row in package_rows:
        family = str(row.get("PackageFamilyName") or "").strip()
        display_names = [name for name in start_names.get(family, []) if name]
        if not display_names:
            continue
        package = str(row.get("PackageFullName") or row.get("Name") or family).strip()
        for name in display_names:
            applications.append(
                {
                    "id": f"{package}:{name}",
                    "name": name,
                    "comment": str(row.get("Publisher") or "").strip(),
                    "exec": str(row.get("InstallLocation") or "").strip(),
                    "icon": "",
                    "categories": ["Microsoft Store"],
                    "source": "microsoft-store",
                    "package": package,
                    "version": str(row.get("Version") or "").strip(),
                    "removable": False,
                }
            )
    return applications


def get_windows_inventory():
    applications = get_windows_registry_apps()
    known = {(item["name"].lower(), item["version"].lower()) for item in applications}
    for app in get_windows_store_apps():
        if (app["name"].lower(), app["version"].lower()) not in known:
            applications.append(app)
    return {
        "packages": [],
        "applications": sorted(applications, key=lambda item: item["name"].lower()),
        "platform": platform.platform(),
        "platformName": "Windows",
        "canManage": False,
        "supportsPackages": False,
        "packageManager": "Windows Registry/Microsoft Store",
        "manageHint": "Application inventory is available. Windows uninstall commands are not enabled yet.",
    }


def get_software_inventory():
    if SYSTEM == "darwin":
        return get_macos_inventory()
    if SYSTEM == "windows":
        return get_windows_inventory()
    return get_ubuntu_packages()


def validate_package_name(name):
    if not isinstance(name, str) or not PACKAGE_NAME_PATTERN.fullmatch(name):
        raise ValueError("Invalid package name")
    return name


def preview_package_removal(name):
    if SYSTEM != "linux":
        raise RuntimeError("Package removal is currently available for APT on Linux only")
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
    if SYSTEM != "linux":
        raise RuntimeError("Package removal is currently available for APT on Linux only")
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


CONTAINER_ID_PATTERN = re.compile(r"^[a-fA-F0-9]{6,64}$")
COMPOSE_PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CONTAINER_ACTIONS = {"start", "stop", "restart", "remove"}
PROJECT_ACTIONS = {"up", "start", "stop", "restart", "down"}
PUBLISHED_PORT_PATTERN = re.compile(r"(?:(?P<host>[\d.]+|\[::\]):)?(?P<public>\d+)->(?P<private>\d+)/(?P<proto>tcp|udp)")
# Go template so compose labels arrive as separate fields; the plain Labels
# string is comma-joined and label values may contain commas themselves.
CONTAINER_LIST_FORMAT = (
    '{"id":{{json .ID}},"name":{{json .Names}},"image":{{json .Image}},'
    '"state":{{json .State}},"status":{{json .Status}},"ports":{{json .Ports}},'
    '"createdAt":{{json .CreatedAt}},"command":{{json .Command}},'
    '"project":{{json (.Label "com.docker.compose.project")}},'
    '"service":{{json (.Label "com.docker.compose.service")}},'
    '"workingDir":{{json (.Label "com.docker.compose.project.working_dir")}},'
    '"configFiles":{{json (.Label "com.docker.compose.project.config_files")}},'
    '"envFile":{{json (.Label "com.docker.compose.project.environment_file")}}}'
)


def docker_error(proc, fallback):
    if not proc:
        return "Could not find the docker command"
    return proc.stderr.strip() or proc.stdout.strip() or fallback


def list_docker_containers(project=None):
    args = ["docker", "ps", "--all", "--format", CONTAINER_LIST_FORMAT]
    if project:
        args[3:3] = ["--filter", f"label=com.docker.compose.project={project}"]
    proc = run_command(args, timeout=15)
    if not proc or proc.returncode != 0:
        raise RuntimeError(docker_error(proc, "docker ps failed"))

    containers = []
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        seen = set()
        published = []
        for match in PUBLISHED_PORT_PATTERN.finditer(row.get("ports", "")):
            key = (int(match.group("public")), int(match.group("private")), match.group("proto"))
            if key in seen:
                continue  # Same mapping is listed once for IPv4 and once for IPv6.
            seen.add(key)
            published.append({"publicPort": key[0], "privatePort": key[1], "protocol": key[2]})
        row["publishedPorts"] = published
        containers.append(row)
    return containers


def get_container_overview():
    containers = list_docker_containers()
    projects = {}
    for container in containers:
        name = container.get("project")
        if not name:
            continue
        project = projects.setdefault(
            name,
            {
                "name": name,
                "workingDir": container.get("workingDir", ""),
                "configFiles": [f for f in container.get("configFiles", "").split(",") if f],
                "containers": [],
            },
        )
        project["containers"].append(container["id"])

    for project in projects.values():
        project["composeFileFound"] = bool(project["configFiles"]) and all(Path(f).is_file() for f in project["configFiles"])

    compose = run_command(["docker", "compose", "version", "--short"])
    return {
        "containers": containers,
        "projects": sorted(projects.values(), key=lambda p: p["name"]),
        "composeAvailable": bool(compose and compose.returncode == 0),
    }


def get_container_stats():
    proc = run_command(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=20)
    if not proc or proc.returncode != 0:
        raise RuntimeError(docker_error(proc, "docker stats failed"))
    stats = {}
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        stats[row.get("ID", "")[:12]] = {
            "cpu": row.get("CPUPerc", ""),
            "memory": row.get("MemUsage", ""),
            "memoryPercent": row.get("MemPerc", ""),
            "netIO": row.get("NetIO", ""),
            "blockIO": row.get("BlockIO", ""),
        }
    return stats


def validate_container_id(container_id):
    if not isinstance(container_id, str) or not CONTAINER_ID_PATTERN.fullmatch(container_id):
        raise ValueError("Invalid container ID")
    return container_id


def run_container_action(container_id, action):
    container_id = validate_container_id(container_id)
    if action not in CONTAINER_ACTIONS:
        raise ValueError("Unsupported container action")
    args = ["docker", "rm", "--force", container_id] if action == "remove" else ["docker", action, container_id]
    proc = run_command(args, timeout=60)
    if not proc or proc.returncode != 0:
        raise RuntimeError(docker_error(proc, f"docker {action} failed"))
    return {"ok": True, "id": container_id, "action": action}


def get_container_logs(container_id, tail):
    container_id = validate_container_id(container_id)
    tail = max(10, min(int(tail), 5000))
    proc = run_command(["docker", "logs", "--timestamps", "--tail", str(tail), container_id], timeout=15, encoding="utf-8")
    if not proc or proc.returncode != 0:
        raise RuntimeError(docker_error(proc, "docker logs failed"))
    # docker logs writes the container's stderr stream to our stderr; merge
    # both and order by the leading RFC3339 timestamp.
    lines = proc.stdout.splitlines() + proc.stderr.splitlines()
    lines.sort(key=lambda line: line.split(" ", 1)[0])
    return {"id": container_id, "lines": lines[-tail:]}


def run_project_action(name, action):
    if not isinstance(name, str) or not COMPOSE_PROJECT_PATTERN.fullmatch(name):
        raise ValueError("Invalid compose project name")
    if action not in PROJECT_ACTIONS:
        raise ValueError("Unsupported project action")

    # Rebuild the compose invocation from the containers' own labels instead
    # of trusting paths sent by the browser.
    containers = list_docker_containers(project=name)
    if not containers:
        raise ValueError(f"No containers found for project {name}")
    first = containers[0]
    config_files = [f for f in first.get("configFiles", "").split(",") if f]
    files_found = bool(config_files) and all(Path(f).is_file() for f in config_files)

    if not files_found:
        if action == "up":
            raise RuntimeError("Compose file for this project was not found on disk, so it cannot be brought up again")
        if action == "down":
            # Compose v2 can tear down a project from its labels alone.
            args = ["docker", "compose", "--project-name", name, "down"]
        else:
            args = ["docker", action, *[c["id"] for c in containers]]
        proc = run_command(args, timeout=300)
    else:
        args = ["docker", "compose", "--project-name", name]
        working_dir = first.get("workingDir") or str(Path(config_files[0]).parent)
        args += ["--project-directory", working_dir]
        for config_file in config_files:
            args += ["--file", config_file]
        for env_file in first.get("envFile", "").split(","):
            if env_file and Path(env_file).is_file():
                args += ["--env-file", env_file]
        args += ["up", "--detach"] if action == "up" else [action]
        proc = run_command(args, timeout=600)

    if not proc or proc.returncode != 0:
        raise RuntimeError(docker_error(proc, f"docker compose {action} failed"))
    return {"ok": True, "project": name, "action": action, "output": (proc.stderr + proc.stdout).strip()[-4000:]}


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

        if parsed.path == "/api/containers":
            try:
                self.send_json(200, get_container_overview())
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/containers/stats":
            try:
                self.send_json(200, {"stats": get_container_stats()})
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/containers/logs":
            query = parse_qs(parsed.query)
            try:
                tail = int(query.get("tail", ["300"])[0])
                self.send_json(200, get_container_logs(query.get("id", [""])[0], tail))
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/packages":
            try:
                self.send_json(200, get_software_inventory())
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

        if parsed.path in ("/api/containers/action", "/api/containers/project-action"):
            try:
                if parsed.path == "/api/containers/action":
                    result = run_container_action(payload.get("id"), payload.get("action"))
                else:
                    result = run_project_action(payload.get("project"), payload.get("action"))
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.send_json(500, {"error": str(exc)})
            else:
                self.send_json(200, result)
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
