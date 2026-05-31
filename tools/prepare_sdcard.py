#!/usr/bin/env python3
"""
Prepare a Casanode SD card image, update configuration, and generate helper assets.

This script merges the previous install-sdcard and QR code helpers into a single tool.
- Copies an input image to the chosen destination (file or block device).
- Mounts the image and patches Wi-Fi, regulatory, and API configuration.
- Creates Wi-Fi and browser QR codes, plus a JSON file with the generated secrets.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import qrcode
from qrcode.constants import ERROR_CORRECT_Q


DEFAULT_COUNTRY = "FR"
DEFAULT_IP = "192.168.50.1"
DEFAULT_PORT = 14045
DEFAULT_SYSTEM_USER = "sentinel"
IMAGE_COPY_BUFFER = 4 * 1024 * 1024


class PreparationError(RuntimeError):
    """Raised when the SD card preparation fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare Casanode SD card image and helper assets.")
    parser.add_argument("input_image", type=Path, help="Source image to clone (e.g. deploy/2025-09-07-casanode-os.img)")
    parser.add_argument(
        "--output-image",
        type=Path,
        help="Destination image file or block device. Defaults to sdcard/<ssid>/<source-name>.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("sdcard"),
        help="Root directory that will receive QR codes and metadata (defaults to ./sdcard).",
    )
    parser.add_argument("--country", default=DEFAULT_COUNTRY, help="Two-letter regulatory country code.")
    parser.add_argument("--ssid", help="Custom Wi-Fi SSID. Defaults to Casanode-<random>.")
    parser.add_argument("--password", help="Custom Wi-Fi passphrase (8-63 chars). Defaults to 16 random characters.")
    parser.add_argument("--system-user", default=DEFAULT_SYSTEM_USER, help="Local admin username to provision. Defaults to sentinel.")
    parser.add_argument("--system-password", help="Local admin password (8-63 chars). Defaults to 16 random characters.")
    parser.add_argument("--auth-token", help="Custom API auth token. Defaults to a random UUID4.")
    parser.add_argument(
        "--url-template",
        default="http://{ip}/connect?ip={ip}&port={port}&key={token}",
        help="Template used to build the browser QR URL.",
    )
    parser.add_argument("--ip", default=DEFAULT_IP, help="IP embedded in the browser QR URL.")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Port embedded in the browser QR URL.")
    parser.add_argument(
        "--enable-ssh-eth0",
        action="store_true",
        help="Create enable-ssh-eth0 marker on the boot partition.",
    )
    parser.add_argument(
        "--enable-ssh-wlan0",
        action="store_true",
        help="Create enable-ssh-wlan0 marker on the boot partition to allow SSH from Wi-Fi.",
    )
    parser.add_argument(
        "--skip-copy",
        action="store_true",
        help="Skip the image copy step and only patch the destination (must already exist).",
    )
    return parser.parse_args()


def ensure_root_required(operation_label: str) -> None:
    if os.geteuid() != 0:
        raise PreparationError(f"{operation_label} requires root privileges. Please re-run with sudo.")


def generate_random_ssid() -> str:
    return f"Casanode-{random.randint(1000, 9999)}"


def generate_random_password(length: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def require_commands(*commands: str) -> None:
    missing = [cmd for cmd in commands if shutil.which(cmd) is None]
    if missing:
        raise PreparationError(f"Missing required command(s): {', '.join(missing)}")


def is_block_device(path: Path) -> bool:
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISBLK(mode)


def copy_image(src: Path, dst: Path) -> None:
    if not src.exists():
        raise PreparationError(f"Input image not found: {src}")
    if src.resolve() == dst.resolve():
        raise PreparationError("Source and destination image are identical. Use --skip-copy if you intend to patch in-place.")

    if is_block_device(dst):
        ensure_root_required("Writing to block device")

    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"Copying image from {src} to {dst} ...")
    with src.open("rb") as handle_in, open(dst, "wb") as handle_out:
        shutil.copyfileobj(handle_in, handle_out, length=IMAGE_COPY_BUFFER)
        handle_out.flush()
        os.fsync(handle_out.fileno())
    if is_block_device(dst):
        os.sync()
    print("Image copy complete.")


def compute_partitions(target: Path) -> Tuple[str, str]:
    device = str(target)
    suffix = "p" if re.search(r"(mmcblk|nvme|loop)", device) else ""
    return f"{device}{suffix}1", f"{device}{suffix}2"


def attach_loop_device(image: Path) -> str:
    ensure_root_required("Loop device setup")
    cmd = ["losetup", "--show", "-fP", str(image)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    loop_device = result.stdout.strip()
    if not loop_device:
        raise PreparationError("Failed to attach loop device.")

    part1, part2 = compute_partitions(Path(loop_device))
    for _ in range(10):
        if Path(part1).exists() and Path(part2).exists():
            break
        time.sleep(0.2)
    else:
        subprocess.run(["losetup", "-d", loop_device], check=True)
        raise PreparationError("Loop partitions did not appear in time.")
    return loop_device


def run_mount(partition: str, mount_point: Path) -> None:
    mount_point.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", partition, str(mount_point)], check=True)


def run_umount(mount_point: Path) -> None:
    if mount_point.is_dir():
        subprocess.run(["umount", str(mount_point)], check=True)


@contextmanager
def mount_partitions(target: Path) -> Iterator[Tuple[Path, Path]]:
    boot_mount = Path(tempfile.mkdtemp(prefix="casanode-boot-"))
    root_mount = Path(tempfile.mkdtemp(prefix="casanode-root-"))
    loop_device = None
    part1: Optional[str] = None
    part2: Optional[str] = None
    try:
        if is_block_device(target):
            ensure_root_required("Mounting block device partitions")
            part1, part2 = compute_partitions(target)
        else:
            loop_device = attach_loop_device(target)
            part1, part2 = compute_partitions(Path(loop_device))

        run_mount(part1, boot_mount)
        run_mount(part2, root_mount)
        yield boot_mount, root_mount
    finally:
        for mount_point in (boot_mount, root_mount):
            if mount_point.exists():
                try:
                    run_umount(mount_point)
                except subprocess.CalledProcessError:
                    pass
                mount_point.rmdir()
        if loop_device:
            subprocess.run(["losetup", "-d", loop_device], check=True)


def write_device_json(
    boot_path: Path,
    ssid: str,
    password: str,
    country: str,
    system_user: str,
    system_password: str,
    enable_ssh_wlan0: bool,
) -> None:
    casanode_dir = boot_path / "casanode"
    casanode_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ssid": ssid,
        "password": password,
        "country": country,
        "system_user": system_user,
        "system_password": system_password,
        "enable_ssh_wlan0": enable_ssh_wlan0,
    }
    (casanode_dir / "device.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (boot_path / "ssh").touch(exist_ok=True)


def set_boot_marker(boot_path: Path, marker_name: str, enabled: bool) -> None:
    marker_path = boot_path / marker_name
    if enabled:
        marker_path.touch(exist_ok=True)
    else:
        marker_path.unlink(missing_ok=True)


def update_wpa_supplicant(root_path: Path, country: str) -> None:
    conf_path = root_path / "etc/wpa_supplicant/wpa_supplicant.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    if conf_path.exists():
        content = conf_path.read_text(encoding="utf-8").splitlines()
    else:
        content = []

    def ensure_line(lines: List[str], key: str, value: str) -> None:
        prefix = f"{key}="
        for idx, line in enumerate(lines):
            if line.startswith(prefix):
                lines[idx] = f"{prefix}{value}"
                return
        lines.append(f"{prefix}{value}")

    content = [line for line in content if not line.startswith("country=")]
    ensure_line(content, "ctrl_interface", "DIR=/var/run/wpa_supplicant GROUP=netdev")
    ensure_line(content, "update_config", "1")
    content.append(f"country={country}")
    conf_path.write_text("\n".join(content) + "\n", encoding="utf-8")
    conf_path.chmod(0o600)


def update_cfg80211(root_path: Path, country: str) -> None:
    target = root_path / "etc/modprobe.d/cfg80211.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"options cfg80211 ieee80211_regdom={country}\n", encoding="utf-8")


def update_hostapd(root_path: Path, ssid: str, password: str, country: str) -> None:
    conf_path = root_path / "etc/hostapd/hostapd.conf"
    if not conf_path.exists():
        return
    lines = conf_path.read_text(encoding="utf-8").splitlines()

    def replace_or_append(lines_ref: List[str], key: str, value: str) -> None:
        prefix = f"{key}="
        for idx, line in enumerate(lines_ref):
            if line.startswith(prefix):
                lines_ref[idx] = f"{prefix}{value}"
                return
        lines_ref.append(f"{prefix}{value}")

    replace_or_append(lines, "country_code", country)
    replace_or_append(lines, "ssid", ssid)
    replace_or_append(lines, "wpa_passphrase", password)
    replace_or_append(lines, "ap_isolate", "1")
    conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_unblock_script(root_path: Path, country: str) -> None:
    script_path = root_path / "usr/local/sbin/casanode-unblock-wifi.sh"
    if not script_path.exists():
        return
    lines = script_path.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("COUNTRY="):
            lines[idx] = f'COUNTRY="{country}"'
            break
    else:
        lines.append(f'COUNTRY="{country}"')
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_casanode_conf(root_path: Path, auth_token: str) -> None:
    conf_path = root_path / "etc/casanode.conf"
    if conf_path.exists():
        lines = conf_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    prefix = "API_AUTH="
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            lines[idx] = f"{prefix}{auth_token}"
            break
    else:
        lines.append(f"{prefix}{auth_token}")
    conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reset_firstboot_flag(root_path: Path) -> None:
    (root_path / "etc/casanode_ap_configured").unlink(missing_ok=True)


def configure_image(
    target: Path,
    ssid: str,
    password: str,
    country: str,
    system_user: str,
    system_password: str,
    auth_token: str,
    enable_ssh_eth0: bool,
    enable_ssh_wlan0: bool,
) -> None:
    ensure_root_required("Configuring image partitions")
    with mount_partitions(target) as (boot_path, root_path):
        print(f"Mount points: boot -> {boot_path}, root -> {root_path}")
        write_device_json(boot_path, ssid, password, country, system_user, system_password, enable_ssh_wlan0)
        set_boot_marker(boot_path, "enable-ssh-eth0", enable_ssh_eth0)
        set_boot_marker(boot_path, "enable-ssh-wlan0", enable_ssh_wlan0)
        update_cfg80211(root_path, country)
        update_wpa_supplicant(root_path, country)
        update_hostapd(root_path, ssid, password, country)
        update_unblock_script(root_path, country)
        update_casanode_conf(root_path, auth_token)
        reset_firstboot_flag(root_path)
        os.sync()
    print("Configuration applied to image.")


def expand_rootfs(target: Path) -> None:
    if not is_block_device(target):
        raise PreparationError("--expand-rootfs requires that the destination is a block device (e.g. /dev/sdX).")
    require_commands("parted", "e2fsck", "resize2fs")
    ensure_root_required("Root filesystem expansion")

    device = str(target)
    _, root_part = compute_partitions(target)
    print(f"Expanding root filesystem on {root_part} ...")
    try:
        subprocess.run(["parted", "-s", device, "resizepart", "2", "100%"], check=True)
        if shutil.which("partprobe"):
            subprocess.run(["partprobe", device], check=False)
        subprocess.run(["e2fsck", "-f", "-y", root_part], check=True)
        subprocess.run(["resize2fs", root_part], check=True)
    except subprocess.CalledProcessError as exc:
        raise PreparationError("Failed to expand root filesystem.") from exc
    print("Root filesystem expansion complete.")


def make_url_qr(url: str, output: Path) -> None:
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_Q, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    with output.open("wb") as handle:
        img.save(handle)


def build_wifi_payload(ssid: str, password: str, security: str = "WPA", hidden: bool = False) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace(":", r"\:")

    parts = ["WIFI:"]
    security_norm = security.upper()
    if security_norm == "NOPASS":
        parts.append("T:nopass;")
    else:
        parts.append(f"T:{escape(security_norm)};")
    parts.append(f"S:{escape(ssid)};")
    if security_norm != "NOPASS":
        parts.append(f"P:{escape(password)};")
    if hidden:
        parts.append("H:true;")
    parts.append(";")
    return "".join(parts)


def make_wifi_qr(ssid: str, password: str, output: Path, security: str = "WPA", hidden: bool = False) -> None:
    payload = build_wifi_payload(ssid, password, security=security, hidden=hidden)
    img = qrcode.make(payload)
    with output.open("wb") as handle:
        img.save(handle)


def main() -> int:
    args = parse_args()

    ssid = args.ssid or generate_random_ssid()
    password = args.password or generate_random_password()
    if not (8 <= len(password) <= 63):
        raise PreparationError("Wi-Fi password must be between 8 and 63 characters.")
    system_user = args.system_user.strip()
    if not system_user:
        raise PreparationError("System user must not be empty.")
    system_password = args.system_password or generate_random_password()
    if not (8 <= len(system_password) <= 63):
        raise PreparationError("System password must be between 8 and 63 characters.")
    auth_token = args.auth_token or str(uuid.uuid4())
    country = args.country.upper()

    output_dir = args.output_root / slugify(ssid)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_image = args.output_image
    if output_image is None:
        output_image = output_dir / args.input_image.name

    if not args.skip_copy:
        copy_image(args.input_image, output_image)
    else:
        if not output_image.exists() and not is_block_device(output_image):
            raise PreparationError(f"Destination image not found: {output_image}")

    configure_image(
        output_image,
        ssid,
        password,
        country,
        system_user,
        system_password,
        auth_token,
        args.enable_ssh_eth0,
        args.enable_ssh_wlan0,
    )

    if is_block_device(output_image):
        expand_rootfs(output_image)

    url = args.url_template.format(ip=args.ip, port=args.port, token=auth_token)

    wifi_qr_path = output_dir / "wifi-qr.png"
    browser_qr_path = output_dir / "browser-qr.png"
    make_wifi_qr(ssid, password, wifi_qr_path)
    make_url_qr(url, browser_qr_path)

    metadata = {
        "ssid": ssid,
        "password": password,
        "country": country,
        "system_user": system_user,
        "system_password": system_password,
        "auth_token": auth_token,
        "api_url": url,
        "wifi_qr": wifi_qr_path.name,
        "browser_qr": browser_qr_path.name,
        "image": str(output_image),
        "source_image": str(args.input_image),
        "enable_ssh_eth0": args.enable_ssh_eth0,
        "enable_ssh_wlan0": args.enable_ssh_wlan0,
        "ip": args.ip,
        "port": args.port,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    (output_dir / "device.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print("Assets generated:")
    print(f"  Wi-Fi QR: {wifi_qr_path}")
    print(f"  Browser QR: {browser_qr_path}")
    print(f"  Metadata: {output_dir / 'device.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreparationError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
