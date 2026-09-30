#!/usr/bin/env python3
"""
Pi Hotspot Injector & Inquisitor
================================
Required Notice: Copyright Matthew Armstrong (https://github.com/The-Dorkknight)
Licensed under the PolyForm Noncommercial License 1.0.0. No warranty.
(Full text embedded below as LICENSE_TEXT, and in LICENSE.md. Free for
non-commercial use, AS IS, with NO WARRANTY and NO LIABILITY.)
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

A point-and-click Linux tool for preparing Raspberry Pi SD cards (Raspberry Pi
OS / MainsailOS, NetworkManager-based images, i.e. Bookworm and newer) so the
Pi broadcasts its own Wi-Fi hotspot on first boot - handy for Klipper /
Mainsail 3D-printer setups with no Wi-Fi network around.

What it can do:

  1. Write an OS image (.img / .img.xz / .img.gz / .img.bz2 / .zip / .zst)
     to an SD card or USB stick. For safety it only offers removable drives
     sold as 8-32 GB (+/-1 GB), and asks twice before erasing anything.

  2. Build a Klipper card: install Klipper + Moonraker + Mainsail (and
     optionally Crowsnest) onto a clean Raspberry Pi OS Lite card from this
     PC. On a 32-bit card everything is compiled for the oldest Pi (Pi 1 /
     Zero / Zero W) inside a chroot that emulates its CPU, so the one card
     works in every Pi up to the Pi 5.

     (Also: install hostapd + dnsmasq onto the offline card on its own.)

  3. Inject a hotspot configuration. Two AP backends:
       * "hostapd"        - hostapd + dnsmasq, wlan0 marked unmanaged in
                            NetworkManager. Recommended: NetworkManager's
                            own AP mode is unreliable on the Pi Zero W's
                            BCM43430 chip on trixie-based images.
       * "networkmanager" - a NetworkManager AP-mode keyfile. No extra
                            packages needed, but see above.

  4. 'Card Inquisitor' - installs a one-shot boot diagnostics collector that
     writes XXXXX_DIAGNOSTICS.txt to the card's boot partition.

  5. Pi Zero / Zero W / Pi 1 fix - finds software on the card that was built
     for a newer ARM CPU (it crashes those ARMv6 boards with "Illegal
     instruction", e.g. Moonraker on MainsailOS 3.x) and rebuilds it from
     source for ARMv6 in a chroot that emulates the Pi Zero's CPU.

Run it by double-clicking PiInjector.desktop (keep it in the same folder as
this file), or from a terminal:   python3 pi_injector.py
It asks for your password itself (via pkexec) because it needs root to
write to disks.

Log file: pi_injector.log in your home folder (passwords are never logged).
"""

import sys

if sys.version_info < (3, 6):
    sys.stderr.write("Pi Hotspot Injector needs Python 3.6 or newer.\n")
    sys.exit(1)

import atexit
import glob
import gzip
import hashlib
import json
import locale
import logging
import os
import pwd
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
import zlib

try:  # missing from some self-compiled / pyenv Pythons - only needed for .xz / .bz2 images
    import lzma
except ImportError:
    lzma = None
try:
    import bz2
except ImportError:
    bz2 = None


def _desktop_error(title, message):
    """Show an error when tkinter itself isn't usable. When the tool is
    double-clicked there's no terminal, so a plain print() would vanish -
    try the desktop's own dialog/notification tools first."""
    sys.stderr.write("%s: %s\n" % (title, message))
    for cmd in (
        ["zenity", "--error", "--title", title, "--text", message],
        ["kdialog", "--title", title, "--error", message],
        ["xmessage", "-center", "%s\n\n%s" % (title, message)],
        ["notify-send", "-u", "critical", title, message],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, timeout=600)
                return
            except Exception:
                continue


try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError:
    _desktop_error(
        "Pi Hotspot Injector",
        "Python's Tkinter GUI library isn't installed.\n\n"
        "On Ubuntu / Linux Mint / Debian / Raspberry Pi OS run:\n"
        "    sudo apt install python3-tk\n"
        "On Fedora:  sudo dnf install python3-tkinter\n"
        "On Arch:    sudo pacman -S tk\n\n"
        "then start this tool again.",
    )
    sys.exit(1)

APP_TITLE = "Pi Hotspot Injector & Inquisitor"
APP_VERSION = "2.3.0"
COPYRIGHT_HOLDER = "Matthew Armstrong (https://github.com/The-Dorkknight)"
PROJECT_URL = "https://github.com/The-Dorkknight/pi-hotspot-injector"


def real_user_info():
    """(uid, gid, home) of the human who started the tool - not root.

    Under pkexec/sudo, HOME points at /root, so the log file and the image
    file picker would otherwise land in /root where a normal user can't
    even look. pkexec sets PKEXEC_UID, sudo sets SUDO_UID."""
    for var in ("PKEXEC_UID", "SUDO_UID"):
        value = os.environ.get(var)
        if value and value.isdigit():
            try:
                pw = pwd.getpwuid(int(value))
                return pw.pw_uid, pw.pw_gid, pw.pw_dir
            except KeyError:
                pass
    return os.getuid(), os.getgid(), os.path.expanduser("~")


REAL_UID, REAL_GID, REAL_HOME = real_user_info()
LOG_FILE = os.path.join(REAL_HOME, "pi_injector.log")

# Image writer safety window: only whole drives sold as 8 GB to 32 GB are ever
# offered as write targets, with 1 GB of leeway either side, because the real
# capacity never matches the label exactly (an "8 GB" card is typically
# 7.4-7.9 billion bytes, a "32 GB" one 30-32 billion). Anything bigger - a
# 64 GB+ stick, an external hard drive or SSD - or smaller is refused.
WRITE_TARGET_LABEL = "8-32 GB"
MIN_WRITE_TARGET_BYTES = 7 * 1000 ** 3   # 8 GB - 1 GB
MAX_WRITE_TARGET_BYTES = 33 * 1000 ** 3  # 32 GB + 1 GB
WRITE_CHUNK = 4 * 1024 * 1024

CONN_NAME = "Hotspot"
CONN_FILE = "Hotspot.nmconnection"
DEFAULT_SUBNET_OCTET = 50  # 192.168.<this>.0/24 - changeable per card in the GUI so
                           # two hotspots running at once don't collide on the same IPs
SERVICE_NAME = "pi-hotspot-ensure.service"
STATIC_IP_SERVICE = "pi-hotspot-static-ip.service"
DIAG_SERVICE_NAME = "pi-boot-diag.service"

REQUIRED_TOOLS = ("lsblk", "findmnt", "mount", "umount", "sync", "mountpoint")

# Two private ranges people's own networks use far more than any other:
# almost every home router hands out 192.168.0.x or 192.168.1.x. A Pi
# hotspot on the same range as the LAN its Pi (or your laptop) is also
# plugged into by cable breaks routing in confusing ways.
RESERVED_SUBNET_OCTETS = (0, 1)

FONT_HEADER = ("Sans", 16, "bold")
FONT_LABEL = ("Sans", 11)
FONT_SMALL = ("Sans", 9)
FONT_BTN = ("Sans", 11, "bold")

log = logging.getLogger("pi_injector")

ACTIVE_SESSIONS = []


# --------------------------------------------------------------------------- #
# Helpers & System Checks
# --------------------------------------------------------------------------- #
class InjectError(Exception):
    """A problem we can explain to the user in plain words."""


class MissingPackagesError(InjectError):
    """The hostapd backend was chosen but the card lacks hostapd/dnsmasq -
    the GUI catches this specifically and offers to install them."""


class CancelledError(InjectError):
    """The user pressed Cancel during a long operation."""


def missing_required_tools():
    return [t for t in REQUIRED_TOOLS if shutil.which(t) is None]


def human_size(num_bytes):
    """Decimal units, matching what's printed on the card's packaging."""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1000.0


def default_country_code():
    """Best guess at the Wi-Fi regulatory country from this PC's locale
    (en_GB.UTF-8 -> GB). Returns '' when it can't tell, so the user is made
    to pick one rather than silently getting the wrong country's rules."""
    candidates = [os.environ.get(v, "") for v in ("LC_ALL", "LC_CTYPE", "LANG")]
    try:
        candidates.append(locale.getlocale()[0] or "")
    except (ValueError, TypeError):
        pass
    for value in candidates:
        m = re.match(r"^[a-z]{2,3}_([A-Z]{2})\b", value or "")
        if m:
            return m.group(1)
    return ""


def random_wifi_password(length=12):
    """A fresh password per launch: a hard-coded default published on GitHub
    would let anyone who's read the source join every hotspot left on it.
    Ambiguous characters (0/O, 1/l/I) are left out so it's easy to type
    on a phone."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def run(cmd, input_text=None, check=True, timeout=90, quiet=False):
    if not quiet:
        log.info("run: %s", " ".join(cmd))
    try:
        res = subprocess.run(
            cmd,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "%s did not finish within %ds - the device (or its reader/cable) may be "
            "unresponsive. Try a different USB port/cable or reader." % (cmd[0], timeout)
        )
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise RuntimeError("%s failed: %s" % (cmd[0], detail))
    return res


def truthy(value):
    return value in (True, 1, "1", "true", "True")


def load_country_codes():
    path = "/usr/share/zoneinfo/iso3166.tab"
    codes = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                codes.add(line.split("\t", 1)[0].strip().upper())
    except OSError:
        return None
    return codes or None


def subnet_addresses(octet):
    """Derive the AP's gateway/CIDR and DHCP range from a single 0-255
    octet, so each card can be given its own 192.168.<octet>.0/24 - two
    of these hotspots running at once (or a client that's talked to more
    than one over time) won't collide on the same gateway IP."""
    base = "192.168.%d" % octet
    return {
        "ap_address": "%s.1/24" % base,
        "ap_gateway": "%s.1" % base,
        "dhcp_start": "%s.10" % base,
        "dhcp_end": "%s.100" % base,
    }


def describe_cards(cards):
    return "\n".join("  - %s" % c["desc"] for c in cards)


def validate_inputs(ssid, password, country, countries, want_user, username, user_pw, subnet_str):
    if not subnet_str.strip().isdigit() or not (0 <= int(subnet_str.strip()) <= 254):
        return "Hotspot subnet must be a number from 2 to 254 (the 'X' in 192.168.X.1)."
    if int(subnet_str.strip()) in RESERVED_SUBNET_OCTETS:
        return (
            "Please don't use 192.168.%s.x for the hotspot - nearly every home router "
            "already uses 192.168.0.x or 192.168.1.x, and clashing with it causes "
            "confusing connection problems. Pick any number from 2 to 254 "
            "(or press 'Random')." % subnet_str.strip()
        )
    if not ssid.strip():
        return "SSID cannot be empty."
    if ssid != ssid.strip():
        return "SSID has leading or trailing spaces. Remove them."
    if len(ssid.encode("utf-8")) > 32:
        return "SSID is too long (maximum 32 bytes)."
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        return "SSID contains control characters."
    if "#" in ssid:
        return "SSID may not contain '#' (it breaks the hostapd config file)."
    if not 8 <= len(password) <= 63:
        return "Wi-Fi password must be 8-63 characters."
    if not all(32 <= ord(c) < 127 for c in password):
        return "Wi-Fi password may only contain standard ASCII characters."
    if "#" in password:
        return "Wi-Fi password may not contain '#' (it breaks the hostapd config file)."
    if not country:
        return (
            "Please enter your 2-letter country code (e.g. GB, US, DE, FR, AU).\n\n"
            "It's the law in most places: it selects which Wi-Fi channels and power "
            "levels the Pi may legally use - and some Pi Wi-Fi chips stay switched "
            "off without it."
        )
    if len(country) != 2 or not country.isalpha():
        return "Country code must be exactly 2 letters (e.g. GB, US, DE)."
    if countries is not None and country not in countries:
        return "'%s' is not a valid ISO country code (UK users: use GB)." % country
    if password.strip() != password:
        return "Wi-Fi password has leading or trailing spaces - phones make those very hard to type. Remove them."
    if want_user:
        if not re.match(r"^[a-z_][a-z0-9_-]{0,30}$", username):
            return "Username must be lowercase letters/digits/-/_ and start with a letter."
        if username == "root":
            return "Username can't be 'root'. Pick a normal name such as 'pi'."
        if len(user_pw) < 8:
            return "Login password must be at least 8 characters."
        if "\n" in user_pw or "\r" in user_pw:
            return "Login password cannot contain line breaks."
    return None


SYSTEM_MOUNT_ROOTS = (
    "/home", "/usr", "/var", "/etc", "/opt", "/root", "/srv", "/boot", "/snap",
    # live-USB boot media: the stick the running system booted from
    "/cdrom", "/run/live", "/run/initramfs", "/lib/live",
)


def is_critical_mount(mountpoint):
    """True if something mounted here is part of the running system (or its
    users' data) - anything at or below a system folder, not just the folder
    itself: a stick mounted at /home/alice or /var/lib/docker counts."""
    if mountpoint in ("/", "[SWAP]"):
        return True
    return any(mountpoint == root or mountpoint.startswith(root + "/") for root in SYSTEM_MOUNT_ROOTS)


def node_mountpoints(node):
    mps = node.get("mountpoints")
    if mps is None:
        mps = [node.get("mountpoint")]
    return [m for m in mps if m]


def walk(node):
    yield node
    for child in node.get("children") or []:
        for sub in walk(child):
            yield sub


def lsblk_devices(quiet=False):
    attempts = [
        "NAME,TYPE,FSTYPE,LABEL,MOUNTPOINTS,RM,HOTPLUG,TRAN,SIZE,MODEL,RO,SERIAL,VENDOR",
        "NAME,TYPE,FSTYPE,LABEL,MOUNTPOINT,RM,HOTPLUG,TRAN,SIZE,MODEL,RO,SERIAL,VENDOR",
        "NAME,TYPE,FSTYPE,LABEL,MOUNTPOINT,RM,TRAN,SIZE,MODEL,RO",
    ]
    last_error = None
    for cols in attempts:
        try:
            # -b: sizes in exact bytes, so the 8-32 GB write window is precise.
            out = run(["lsblk", "-J", "-p", "-b", "-o", cols], quiet=quiet).stdout
            return json.loads(out).get("blockdevices", [])
        except (RuntimeError, ValueError) as exc:
            last_error = exc
    raise InjectError("Could not read block devices with lsblk: %s" % last_error)


INTERNAL_TRANSPORTS = (
    "sata", "ata", "pata", "nvme", "sas", "scsi", "fc", "iscsi", "virtio", "spi", "ieee1394", "ubd",
)


def is_external_disk(dev):
    """USB stick, USB card reader, or SD card in a built-in slot - never an
    internal drive. HOTPLUG alone is deliberately NOT trusted: many desktop
    BIOSes flag every internal SATA port as hot-pluggable."""
    name = dev.get("name", "")
    base = os.path.basename(name)
    tran = str(dev.get("tran") or "").lower()
    if base.startswith("mmcblk"):
        return not is_emmc(name)
    if tran in ("usb", "mmc"):
        return True
    return truthy(dev.get("rm")) and tran in ("", "none")


def find_cards():
    cards = []
    for dev in lsblk_devices():
        if dev.get("type") != "disk":
            continue
        name = dev.get("name", "")
        if not is_external_disk(dev):
            continue

        if any(
            is_critical_mount(mp)
            for node in walk(dev)
            for mp in node_mountpoints(node)
        ):
            continue

        parts = [c for c in (dev.get("children") or []) if c.get("type") == "part"]
        ext4 = [p for p in parts if p.get("fstype") == "ext4"]
        vfat = sorted(
            (p for p in parts if p.get("fstype") == "vfat"), key=lambda p: p["name"]
        )
        if len(ext4) != 1 or not vfat:
            continue

        cards.append(
            {
                "disk": name,
                "boot": vfat[0]["name"],
                "root": ext4[0]["name"],
                "readonly": truthy(dev.get("ro")) or any(truthy(p.get("ro")) for p in parts),
                "desc": "%s, %s (%s)" % (device_model(dev), human_size(dev.get("size")), name),
            }
        )
    return cards


def device_model(dev):
    vendor = (dev.get("vendor") or "").strip()
    model = (dev.get("model") or "").strip()
    text = ("%s %s" % (vendor, model)).strip() if vendor and vendor not in model else model
    if not text and "mmcblk" in (dev.get("name") or ""):
        return "SD card"
    return text or "Unknown device"


def device_size_bytes(dev):
    try:
        return int(dev.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def verify_pi_card(card, boot_mp, root_mp):
    """Called while both partitions are still mounted READ-ONLY, before
    anything is changed: refuse anything that isn't a Raspberry Pi OS /
    MainsailOS card (e.g. a USB backup drive that happens to have a FAT and
    an ext4 partition)."""
    if not os.path.isfile(os.path.join(boot_mp, "cmdline.txt")):
        raise InjectError(
            "%s doesn't look like a Raspberry Pi OS / MainsailOS card: its boot "
            "partition has no cmdline.txt.\n\nWrite a Raspberry Pi OS or MainsailOS "
            "image to it first (the 'Write Image' tab can do that). Nothing was changed."
            % card["desc"]
        )
    return detect_target_arch(root_mp)  # raises a plain-English error if not ARM


def ensure_writable_card(card):
    """Clear message for the single most common 'why won't it write' cause."""
    if card.get("readonly"):
        raise InjectError(
            "%s is write-protected.\n\nIf it's a full-size SD card (or a microSD "
            "in an SD adapter), slide the little LOCK switch on its side up "
            "towards the contacts, re-insert it, and try again." % card["desc"]
        )


def unmount_existing(dev):
    """Unmount dev, retrying on a transient failure (e.g. 'target is busy'
    right after the desktop's own automounter grabs a freshly inserted SD
    card - very common, and previously NOT actually retried despite the
    loop below: a single failed umount used to raise immediately)."""
    last_error = None
    for _ in range(6):
        res = run(["findmnt", "-rn", "-S", dev, "-o", "TARGET"], check=False)
        targets = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        if not targets:
            return
        for target in targets:
            if is_critical_mount(target):
                raise InjectError(
                    "%s is mounted at %s, which looks like part of this computer. Refusing to continue." % (dev, target)
                )
        try:
            run(["umount", dev])
        except RuntimeError as exc:
            last_error = exc
            time.sleep(1)
            continue
    raise InjectError(
        "Could not fully unmount %s after several attempts%s."
        % (dev, (" (last error: %s)" % last_error) if last_error else "")
    )


class MountSession:
    def __init__(self):
        self.mounts = []
        ACTIVE_SESSIONS.append(self)

    def mount(self, dev, options="ro,nosuid,nodev"):
        mountpoint = tempfile.mkdtemp(prefix="pi_inject_")
        try:
            run(["mount", "-o", options, dev, mountpoint])
        except RuntimeError as exc:
            os.rmdir(mountpoint)
            if "wrong fs type" in str(exc) or "bad superblock" in str(exc):
                raise InjectError(
                    "Couldn't open %s - its filesystem looks damaged or unfinished (was the "
                    "card removed while it was being written?). Re-write the OS image to "
                    "the card and try again.\n\nDetails: %s" % (dev, exc)
                )
            raise
        self.mounts.append((dev, mountpoint, True))
        return mountpoint

    def mount_at(self, dev, target, options="ro,nosuid,nodev"):
        """Mount dev onto an existing directory (e.g. the card's own
        /boot/firmware). Tracked like a bind mount: unmounted before the
        filesystem it sits in, and the directory itself is left in place."""
        run(["mount", "-o", options, dev, target])
        self.mounts.append((dev, target, False))

    def bind(self, source, target):
        """Bind-mount source onto target, tracked so cleanup (and the atexit
        safety net, if the app is force-quit) unmounts it before the
        filesystem it sits inside. remove_dir=False: target is a real
        directory on the card (e.g. its /dev) and must be left in place."""
        run(["mount", "--bind", source, target])
        self.mounts.append((source, target, False))

    def remount_rw(self, mountpoint):
        run(["mount", "-o", "remount,rw", mountpoint])

    def cleanup(self):
        problems = []
        subprocess.run(["sync"])
        for entry in reversed(list(self.mounts)):
            dev, mountpoint, remove_dir = entry
            unmounted = False
            for _ in range(5):
                try:
                    res = subprocess.run(
                        ["umount", mountpoint],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        universal_newlines=True,
                        timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    time.sleep(1)
                    continue
                if res.returncode == 0:
                    unmounted = True
                    break
                time.sleep(1)
            if unmounted:
                if remove_dir:
                    try:
                        os.rmdir(mountpoint)
                    except OSError:
                        pass
                self.mounts.remove(entry)
            else:
                problems.append("%s is still mounted at %s" % (dev, mountpoint))
        if not self.mounts and self in ACTIVE_SESSIONS:
            ACTIVE_SESSIONS.remove(self)
        return problems


@atexit.register
def global_cleanup_safety_net():
    for session in list(ACTIVE_SESSIONS):
        try:
            session.cleanup()
        except Exception:
            pass


def write_file(path, data, mode=0o600, posix=True):
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if posix:
            os.chown(tmp, 0, 0)
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def kf_escape(text):
    return text.replace("\\", "\\\\").replace(" ", "\\s")


def build_nm_keyfile(ssid, password, channel, ap_address):
    ssid_bytes = "".join("%d;" % b for b in ssid.encode("utf-8"))
    lines = [
        "# Generated by Pi Hotspot Injector",
        "",
        "[connection]",
        "id=%s" % CONN_NAME,
        "uuid=%s" % uuid.uuid4(),
        "type=wifi",
        "autoconnect=true",
        "autoconnect-priority=100",
        "",
        "[wifi]",
        "mode=ap",
        "ssid=%s" % ssid_bytes,
        "band=bg",
        "channel=%d" % channel,
        "powersave=2",  # 2 = Disable powersave in NetworkManager
        "",
        "[wifi-security]",
        "key-mgmt=wpa-psk",
        "psk=%s" % kf_escape(password),
        "",
        "[ipv4]",
        "address1=%s" % ap_address,
        "method=shared",
        "",
        "[ipv6]",
        "addr-gen-mode=default",
        "method=ignore",
        "",
    ]
    return "\n".join(lines)


def build_hostapd_conf(settings):
    lines = [
        "# Generated by Pi Hotspot Injector",
        "interface=wlan0",
        "driver=nl80211",
        "ssid=%s" % settings["ssid"],
        "hw_mode=g",
        "channel=%d" % settings["channel"],
        "country_code=%s" % settings["country"],
        "ieee80211d=1",
        "ieee80211n=1",
        "wmm_enabled=1",
        "macaddr_acl=0",
        "auth_algs=1",
        "ignore_broadcast_ssid=0",
        "wpa=2",
        "wpa_passphrase=%s" % settings["password"],
        "wpa_key_mgmt=WPA-PSK",
        "wpa_pairwise=CCMP",
        "rsn_pairwise=CCMP",
        "",
    ]
    return "\n".join(lines)


def build_dnsmasq_conf(ap_gateway, dhcp_start, dhcp_end):
    lines = [
        "# Generated by Pi Hotspot Injector",
        "interface=wlan0",
        "bind-interfaces",
        "except-interface=lo",
        "dhcp-range=%s,%s,255.255.255.0,12h" % (dhcp_start, dhcp_end),
        "dhcp-option=option:router,%s" % ap_gateway,
        "dhcp-option=option:dns-server,%s" % ap_gateway,
        "",
    ]
    return "\n".join(lines)


def build_static_ip_service(ap_address):
    return """[Unit]
Description=Assign static IP to wlan0 for the hotspot (hostapd/dnsmasq backend)
Before=hostapd.service dnsmasq.service
After=sys-subsystem-net-devices-wlan0.device
Requires=sys-subsystem-net-devices-wlan0.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '\\
    rfkill unblock wifi || true; \\
    ip addr flush dev wlan0 || true; \\
    ip addr add %s dev wlan0; \\
    ip link set wlan0 up'

[Install]
WantedBy=multi-user.target
""" % ap_address


CMDLINE_DROP = (
    "cfg80211.ieee80211_regdom=",
    "rfkill.default_state=",
    "systemd.restore_state=",
)


def build_cmdline(original, country):
    lines = [ln for ln in original.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise InjectError("cmdline.txt has %d lines (expected 1)." % len(lines))
    tokens = [t for t in lines[0].split() if not t.startswith(CMDLINE_DROP)]
    tokens += [
        "cfg80211.ieee80211_regdom=%s" % country,
        "rfkill.default_state=1",
        "systemd.restore_state=0",
    ]
    return " ".join(tokens) + "\n"


def build_service_unit(country):
    return """[Unit]
Description=Make sure the Wi-Fi hotspot is up
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
TimeoutStartSec=180
ExecStart=/bin/sh -c '\\
    rm -f /boot/headless_nm.txt /boot/firmware/headless_nm.txt || true; \\
    iw reg set %s || true; \\
    rfkill unblock wifi || true; \\
    nmcli device set wlan0 managed yes || true; \\
    nmcli connection delete "preconfigured" 2>/dev/null || true; \\
    nmcli radio wifi on || true; \\
    sleep 5; \\
    for i in 1 2 3 4 5; do \\
        nmcli -t -f NAME connection show --active | grep -qx Hotspot && exit 0; \\
        nmcli connection up Hotspot && exit 0; \\
        sleep 5; \\
    done; \\
    exit 0'

[Install]
WantedBy=multi-user.target
""" % country


DIAG_SCRIPT_CONTENT = """#!/bin/bash
DIAG_FILENAME="XXXXX_DIAGNOSTICS.txt"
# The boot partition is /boot/firmware on Bookworm/trixie-based Raspberry Pi
# OS images (like this one), or plain /boot on older ones. Check which one
# is an actual separate mount rather than just an existing directory - a
# bare directory can exist on the root filesystem even when it isn't where
# the real boot partition lives, which would silently write the file
# somewhere you'd never see it.
if mountpoint -q /boot/firmware 2>/dev/null; then
    LOG_FILE="/boot/firmware/$DIAG_FILENAME"
elif mountpoint -q /boot 2>/dev/null; then
    LOG_FILE="/boot/$DIAG_FILENAME"
elif [ -d /boot/firmware ]; then
    LOG_FILE="/boot/firmware/$DIAG_FILENAME"
else
    LOG_FILE="/boot/$DIAG_FILENAME"
fi

exec > "$LOG_FILE" 2>&1

echo "=========================================="
echo "      PI ZERO W BOOT DIAGNOSTICS          "
echo "=========================================="
echo "Timestamp: $(date)  (uptime: $(uptime -p 2>/dev/null || uptime))"
echo "Note: system clock may not be synced (no RTC) - trust 'uptime' over 'date' for ordering events."
echo ""
echo "--- 1. OS & KERNEL INFO ---"
cat /etc/os-release | grep PRETTY_NAME
uname -a
echo ""
echo "--- 2. HARDWARE TREE ---"
cat /proc/device-tree/model 2>/dev/null || echo "Model tree unavailable"
echo ""
echo "--- 3. RFKILL & REGULATORY ---"
rfkill list all
iw reg get 2>/dev/null || echo "iw command unavailable"
echo ""
echo "--- 4. NETWORK INTERFACES ---"
ip link show
ip addr show wlan0 2>/dev/null
echo ""
echo "--- 5. NETWORKMANAGER STATUS ---"
systemctl status NetworkManager --no-pager
nmcli device status 2>/dev/null
nmcli connection show 2>/dev/null
echo ""
echo "--- 6. KERNEL LOGS (Wi-Fi / Broadcom Driver) ---"
dmesg | grep -E "brcm|wlan|cfg80211|ieee80211" | tail -n 50
echo ""
echo "--- 7. JOURNAL LOGS (NetworkManager) ---"
journalctl -u NetworkManager -n 50 --no-pager
echo ""
echo "--- 8. HOTSPOT BACKEND STATUS (hostapd/dnsmasq, if used) ---"
systemctl status pi-hotspot-static-ip.service --no-pager 2>/dev/null
systemctl status hostapd --no-pager 2>/dev/null
systemctl status dnsmasq --no-pager 2>/dev/null
journalctl -u pi-hotspot-static-ip -u hostapd -u dnsmasq -n 80 --no-pager 2>/dev/null
echo ""
echo "--- 9. MOONRAKER / MAINSAIL STATUS ---"
systemctl status moonraker --no-pager 2>/dev/null
systemctl status klipper --no-pager 2>/dev/null
systemctl status nginx --no-pager 2>/dev/null
echo "--- moonraker API probe (localhost) ---"
curl -sS -m 5 http://127.0.0.1:7125/server/info 2>&1
echo ""
echo "--- moonraker.conf [authorization] section ---"
for f in /home/*/printer_data/config/moonraker.conf /home/*/klipper_config/moonraker.conf; do
    if [ -f "$f" ]; then
        echo "Found: $f"
        sed -n '/^\\[authorization\\]/,/^\\[/p' "$f"
    fi
done
echo "--- journalctl moonraker (last 60, includes systemd's own exit/kill reason) ---"
journalctl -u moonraker -n 60 --no-pager 2>/dev/null
echo "--- journalctl klipper (last 40) ---"
journalctl -u klipper -n 40 --no-pager 2>/dev/null
echo ""
echo "--- 10. MEMORY / OOM CHECK (a crash-loop with no error in moonraker.log often means this) ---"
free -h
echo "--- swap ---"
swapon --show 2>/dev/null || echo "No swap configured"
echo "--- top memory consumers ---"
ps aux --sort=-%mem | head -n 15
echo "--- dmesg: OOM killer / killed processes ---"
dmesg | grep -iE "oom|killed process|out of memory" | tail -n 30
echo "--- moonraker.log tail (its own file, in case journald rotated/is volatile) ---"
for f in /home/*/printer_data/logs/moonraker.log; do
    if [ -f "$f" ]; then
        echo "Found: $f"
        tail -n 150 "$f"
    fi
done
echo "--- journalctl: watchdog/oom/killed messages, last hour ---"
journalctl --since "-1 hour" --no-pager | grep -iE "watchdog|oom|killed|out of memory" | tail -n 40
echo ""
echo "--- 11. CPU COMPATIBILITY ('Illegal instruction' / status=4/ILL crashes) ---"
grep -m1 -E "^model name|^Processor" /proc/cpuinfo
grep -m1 "^CPU architecture" /proc/cpuinfo
echo "uname -m: $(uname -m)   (armv6l = Pi Zero / Zero W / Pi 1)"
echo "--- program files built for a newer CPU than ARMv6 (these crash on Pi Zero / Zero W / Pi 1) ---"
if command -v readelf >/dev/null 2>&1; then
    FOUND=0
    for f in $(find /home/*/*-env /home/*/klipper /usr/local -type f -name "*.so*" 2>/dev/null); do
        ARCH=$(readelf -A "$f" 2>/dev/null | sed -n 's/.*Tag_CPU_arch: *//p' | head -n 1)
        case "$ARCH" in
            v6T2|v7*|v8*|v9*) echo "  $ARCH  $f"; FOUND=1 ;;
        esac
    done
    [ "$FOUND" = 0 ] && echo "  none found"
else
    echo "  (readelf not installed - skipped)"
fi
echo "--- one Moonraker start with crash tracing (up to 90 s) ---"
MOON_USER=$(systemctl show -p User --value moonraker 2>/dev/null)
MOON_HOME=$(getent passwd "$MOON_USER" | cut -d: -f6)
if [ -n "$MOON_USER" ] && [ -x "$MOON_HOME/moonraker-env/bin/python" ] && [ -f "$MOON_HOME/moonraker/moonraker/__main__.py" ]; then
    systemctl stop moonraker 2>/dev/null
    timeout 90 runuser -u "$MOON_USER" -- env PYTHONFAULTHANDLER=1 "$MOON_HOME/moonraker-env/bin/python" "$MOON_HOME/moonraker/moonraker/__main__.py" -d "$MOON_HOME/printer_data" > /tmp/moontrace.txt 2>&1
    RC=$?
    grep -v "^$" /tmp/moontrace.txt | grep -iE -A 25 "fatal python error|illegal|traceback|error" | head -n 60
    case "$RC" in
        124) echo "Result: Moonraker was still running fine after 90 s (no crash)." ;;
        132) echo "Result: CRASHED with 'Illegal instruction' - the 'File ...' lines above show which module." ;;
        *)   echo "Result: exited with code $RC"; tail -n 15 /tmp/moontrace.txt ;;
    esac
    systemctl start moonraker 2>/dev/null
else
    echo "  (Moonraker not found in the usual place - skipped)"
fi
sync
"""

DIAG_SERVICE_UNIT = """[Unit]
Description=Collect Boot Diagnostics
After=multi-user.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
# Deliberately wait before collecting: this unit is reached very early in
# boot (right after multi-user.target), well before a crash-looping
# service like Moonraker has had time to fail more than once or twice.
# Without this, the snapshot below can look deceptively clean/sparse even
# when there's an ongoing crash loop, and "wait 2 minutes before removing
# the card" would do nothing since the file was already written and
# closed long before that.
ExecStartPre=-/bin/sleep 100
ExecStart=/bin/bash /usr/local/bin/pi_diag.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def hash_password(password):
    if shutil.which("openssl") is None:
        raise InjectError("openssl is required to create the login user.")
    res = run(["openssl", "passwd", "-6", "-stdin"], input_text=password + "\n")
    return res.stdout.strip()


# --------------------------------------------------------------------------- #
# systemd unit helpers (for enabling package-provided units like hostapd.service)
# --------------------------------------------------------------------------- #
UNIT_SEARCH_DIRS = ("usr/lib/systemd/system", "lib/systemd/system", "etc/systemd/system")


def find_unit_path(root_mp, unit_name):
    """Return the unit file's absolute path *inside the target OS* (not the
    mount point), or None if the package that ships it isn't installed."""
    for d in UNIT_SEARCH_DIRS:
        candidate = os.path.join(root_mp, d, unit_name)
        if os.path.isfile(candidate):
            return "/" + d + "/" + unit_name
    return None


def enable_unit(root_mp, unit_name):
    """Enable a package-provided systemd unit (removing any distro mask),
    mirroring what `systemctl enable` would do, without running systemd."""
    unit_target = find_unit_path(root_mp, unit_name)
    if unit_target is None:
        return False

    mask_path = os.path.join(root_mp, "etc", "systemd", "system", unit_name)
    if os.path.islink(mask_path):
        try:
            if os.readlink(mask_path) == "/dev/null":
                os.remove(mask_path)
        except OSError:
            pass

    wants_dir = os.path.join(root_mp, "etc", "systemd", "system", "multi-user.target.wants")
    os.makedirs(wants_dir, exist_ok=True)
    link = os.path.join(wants_dir, unit_name)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(unit_target, link)
    return True


def hostapd_dnsmasq_available(root_mp):
    hostapd_present = any(
        os.path.isfile(os.path.join(root_mp, d, "hostapd")) for d in ("usr/sbin", "sbin")
    )
    dnsmasq_present = any(
        os.path.isfile(os.path.join(root_mp, d, "dnsmasq")) for d in ("usr/sbin", "sbin")
    )
    return hostapd_present and dnsmasq_present


# --------------------------------------------------------------------------- #
# Host-side chroot package installer (installs hostapd+dnsmasq onto the card)
# --------------------------------------------------------------------------- #
CHROOT_BIND_MOUNTS = ("dev", "proc", "sys")


def detect_target_arch(root_mp):
    """Inspect an ELF binary already on the card to work out its CPU
    architecture, so we can look for the matching qemu-user interpreter."""
    probe = None
    for candidate in ("bin/bash", "bin/dash", "bin/ls", "usr/bin/dpkg"):
        p = os.path.join(root_mp, candidate)
        if os.path.islink(p):
            target = os.readlink(p)
            p = target if os.path.isabs(target) else os.path.normpath(os.path.join(os.path.dirname(p), target))
            p = os.path.join(root_mp, p.lstrip("/")) if not p.startswith(root_mp) else p
        if os.path.isfile(p):
            probe = p
            break
    if probe is None:
        raise InjectError("Could not find a binary on the card to detect its CPU architecture.")
    # Read the ELF header directly rather than depending on the 'file'
    # command being installed on this PC. e_machine is a 16-bit field at
    # offset 18; EI_DATA (offset 5) says whether it's little/big endian.
    try:
        with open(probe, "rb") as f:
            header = f.read(20)
    except OSError as exc:
        raise InjectError("Could not read %s on the card: %s" % (probe, exc))
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise InjectError("%s on the card isn't a Linux program - is this really a Pi OS image?" % probe)
    byteorder = "little" if header[5] == 1 else "big"
    machine = int.from_bytes(header[18:20], byteorder)
    if machine == 0xB7:
        return "arm64", "qemu-aarch64"
    if machine == 0x28:
        return "armhf", "qemu-arm"
    raise InjectError(
        "This card's OS isn't built for a Raspberry Pi's ARM processor (ELF machine "
        "type 0x%x). Did you flash the right image?" % machine
    )


def find_qemu_candidates(binfmt_prefix):
    """Return (static_path_or_None, dynamic_path_or_None) for this arch."""
    static_path = "/usr/bin/%s-static" % binfmt_prefix
    plain_path = "/usr/bin/%s" % binfmt_prefix
    static = static_path if os.path.isfile(static_path) else None
    plain = plain_path if os.path.isfile(plain_path) else None
    return static, plain


def binfmt_f_flag_ready(prefix):
    """True only if an ENABLED binfmt_misc entry for this prefix also has
    the 'F' (fix binary) flag. F matters specifically for a *dynamically
    linked* interpreter: it makes the kernel hold an fd to the interpreter
    in the HOST's own namespace at registration time, so it keeps working
    under chroot. Without F, the kernel re-resolves the interpreter (and
    its shared libraries) relative to the new root, which fails - a copied
    dynamically-linked binary has no matching libs inside an ARM chroot.
    A genuinely static interpreter doesn't need any of this."""
    d = "/proc/sys/fs/binfmt_misc"
    if not os.path.isdir(d):
        return False
    for name in os.listdir(d):
        if not name.startswith(prefix):
            continue
        try:
            lines = open(os.path.join(d, name)).read().splitlines()
        except OSError:
            continue
        if not lines or lines[0].strip() != "enabled":
            continue
        flags_line = next((ln for ln in lines if ln.startswith("flags:")), "")
        if "F" in flags_line:
            return True
    return False


def resolve_host_qemu(binfmt_prefix):
    """Pick which qemu-user interpreter to use for the chroot.

    A genuinely STATIC interpreter is strongly preferred: we invoke it
    explicitly as the chroot's own command (chroot <root> /usr/bin/qemu-arm-static
    /usr/bin/env ...). qemu-user then keeps handling every same-architecture
    exec a process inside the chroot makes afterwards on its own (apt-get
    spawning dpkg, dpkg running maintainer scripts, etc), so this works
    regardless of this machine's binfmt_misc setup - which matters because
    this tool runs across several different Linux PCs with inconsistent
    binfmt configurations.

    A dynamically-linked interpreter only works if the kernel's own
    binfmt_misc handles the exec, and only correctly with the 'F' flag (see
    binfmt_f_flag_ready). We fall back to it only when that is confirmed.

    Returns (path_or_None, is_static).
    """
    static_path, plain_path = find_qemu_candidates(binfmt_prefix)
    if static_path:
        return static_path, True
    if plain_path and binfmt_f_flag_ready(binfmt_prefix):
        return plain_path, False
    return None, False


def ensure_host_qemu_tools(say):
    """Try to auto-install qemu-user tooling on THIS machine (not the card)
    via apt. Prefers qemu-user-static (a genuinely static interpreter we
    can invoke explicitly - see resolve_host_qemu) and only falls back to
    the newer qemu-user/qemu-user-binfmt split where -static has no
    installation candidate (seen on newer Ubuntu). Used across multiple
    Linux PCs, so we don't want to require knowing which name applies on
    which distro/release."""
    if shutil.which("apt-get") is None:
        raise InjectError(
            "This machine doesn't appear to use apt, so qemu-user tooling can't be "
            "auto-installed. Install qemu-user-static (or qemu-user + qemu-user-binfmt) "
            "with your distro's package manager and try again."
        )

    say("Installing qemu-user tooling on this machine (apt-get update)...")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    update_res = subprocess.run(
        ["apt-get", "update"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=env
    )
    log.info("host apt-get update:\n%s", update_res.stdout)

    attempts = [
        ["qemu-user-static", "binfmt-support"],
        ["qemu-user", "qemu-user-binfmt", "binfmt-support"],
    ]
    last_output = ""
    for pkgs in attempts:
        say("Installing %s..." % " ".join(pkgs))
        res = subprocess.run(
            ["apt-get", "install", "-y"] + pkgs,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=env,
        )
        last_output = res.stdout
        log.info("host apt-get install %s:\n%s", " ".join(pkgs), res.stdout)
        if res.returncode == 0:
            subprocess.run(["update-binfmts", "--enable"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return
    raise InjectError(
        "Couldn't auto-install qemu-user tooling on this machine:\n\n%s%s"
        % (last_output[-1500:], explain_apt_failure(last_output))
    )


def check_internet():
    """Fail fast with a plain-English message instead of a wall of apt
    'Temporary failure resolving...' errors several minutes in."""
    import socket

    hosts = ("deb.debian.org", "archive.raspberrypi.com", "raspbian.raspberrypi.com")
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(8)
    try:
        for host in hosts:
            try:
                socket.getaddrinfo(host, 80)
                return
            except OSError:
                continue
    finally:
        socket.setdefaulttimeout(old_timeout)
    raise InjectError(
        "This PC doesn't seem to be online (couldn't look up %s).\n\n"
        "Installing packages downloads them from the internet on THIS computer. "
        "Connect it to the internet - note: not to the Pi's hotspot, which has no "
        "internet - and try again." % hosts[0]
    )


def explain_apt_failure(output):
    """Translate the handful of apt errors people actually hit."""
    text = output or ""
    if "Could not get lock" in text or "Unable to acquire the dpkg frontend lock" in text:
        return (
            "\n\nIn plain English: another program (probably this PC's automatic "
            "updates or Software Updater) is using apt right now. Wait a few minutes "
            "for it to finish, then try again."
        )
    if "Temporary failure resolving" in text or "Could not resolve" in text:
        return "\n\nIn plain English: no internet connection (DNS lookup failed)."
    if "No space left on device" in text:
        return "\n\nIn plain English: the card (or this PC) ran out of disk space."
    return ""


# Long-running child processes (the chroot's apt-get) are tracked so that
# Cancel, closing the window, or a logout/SIGTERM can stop them - which
# lets the installer's finally: block restore the card before unmounting.
ACTIVE_CHILDREN = []
CHILDREN_LOCK = threading.Lock()
CANCEL = threading.Event()        # set when the user cancels or quits
CURRENT_WORKER = [None]           # the background thread doing the current job
POLICY_RC_D = "#!/bin/sh\n# written by Pi Hotspot Injector during package install\nexit 101\n"


def run_tracked(cmd, timeout, env=None):
    """Run cmd in its own process group; returns (returncode, output)."""
    log.info("run: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        start_new_session=True, env=env,
    )
    with CHILDREN_LOCK:
        ACTIVE_CHILDREN.append(proc)
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            proc.communicate()
            raise
    finally:
        with CHILDREN_LOCK:
            if proc in ACTIVE_CHILDREN:
                ACTIVE_CHILDREN.remove(proc)
    return proc.returncode, out or ""


def kill_process_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def kill_active_children():
    with CHILDREN_LOCK:
        procs = list(ACTIVE_CHILDREN)
    for proc in procs:
        kill_process_group(proc)


def stop_background_work(timeout=30):
    """Ask the current job to stop, and wait (up to timeout) for its own
    clean-up code to finish - used when quitting mid-job."""
    CANCEL.set()
    kill_active_children()
    worker = CURRENT_WORKER[0]
    if worker is not None and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout)


def run_in_chroot(chroot_prefix, args, say_prefix, timeout=1800, env=None, env_args=()):
    full_cmd = chroot_prefix + ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive"] + list(env_args) + args
    if CANCEL.is_set():
        raise CancelledError("Cancelled.")
    try:
        rc, out = run_tracked(full_cmd, timeout, env=env)
    except subprocess.TimeoutExpired:
        raise InjectError("%s timed out after %d minutes." % (say_prefix, timeout // 60))
    log.info("%s output:\n%s", say_prefix, out)
    if CANCEL.is_set():
        raise CancelledError(
            "Cancelled. The card has been tidied up, but whatever was being installed may be "
            "only partly done - run it again before using the card."
        )
    if rc != 0:
        tail = out[-2000:] if out else "(no output captured)"
        raise InjectError("%s failed:\n%s%s" % (say_prefix, tail, explain_apt_failure(out)))
    return out


def chroot_environment():
    """A clean environment for programs run inside the card's system.

    This PC's own environment must not leak in: things like PYTHONPATH,
    VIRTUAL_ENV, LD_LIBRARY_PATH, PIP_* or SSL_CERT_FILE point at paths on
    THIS computer and break pip/git/apt inside the card in baffling ways.
    Only proxy settings are passed through - they're genuinely needed to
    reach the internet from behind a proxy."""
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
    }
    for var in ("http_proxy", "https_proxy", "ftp_proxy", "no_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    return env


class CardChroot:
    """Prepare a card's own Linux system to run programs from this PC (via a
    qemu-user chroot) - and put every temporary change back afterwards, even
    if something fails or the user cancels. Use as a context manager.

    pi_zero_cpu=True emulates the Pi Zero / Pi 1's ARM1176 CPU instead of a
    modern one: programs then see "armv6l" (so pip won't pick up packages
    built for ARMv7) and ARMv7-only code crashes here exactly like it would
    on the real Pi."""

    def __init__(self, card, say, min_free_mb=250, pi_zero_cpu=False, need_internet=True):
        self.card, self.say = card, say
        self.min_free_mb = min_free_mb
        self.pi_zero_cpu = pi_zero_cpu
        self.need_internet = need_internet
        self.session = MountSession()
        self.root_mp = None
        self.arch = None
        self.qemu_name = None
        self.is_static = False
        self.env = None
        self._qemu_copied_path = None
        self._resolv_path = None
        self._resolv_backup = None
        self._resolv_had_original = False
        self._resolv_written = False
        self._policy_path = None
        self._policy_written = False

    # -- setup ------------------------------------------------------------ #
    def __enter__(self):
        try:
            self._setup()
        except BaseException:
            self._teardown()
            raise
        return self

    def _setup(self):
        say, card = self.say, self.card
        ensure_writable_card(card)
        if self.need_internet:
            say("Checking this PC's internet connection...")
            check_internet()
        root_dev = card["root"]
        say("Unmounting existing mounts...")
        unmount_existing(root_dev)
        # Read-only until we're sure this really is a Raspberry Pi OS card.
        self.root_mp = root_mp = self.session.mount(root_dev)

        say("Checking this is a Raspberry Pi OS card...")
        self.arch, binfmt_prefix = detect_target_arch(root_mp)
        if self.pi_zero_cpu == "auto":
            # 32-bit card: build for the oldest Pi, so it works in every Pi.
            self.pi_zero_cpu = self.arch == "armhf"
        if self.pi_zero_cpu and self.arch != "armhf":
            raise InjectError(
                "This card has a 64-bit system, which can't run on a Pi Zero, Zero W or Pi 1 "
                "at all - those only run 32-bit Raspberry Pi OS. Use the 32-bit image, or a "
                "Pi Zero 2 W / Pi 3 / 4 / 5."
            )
        if not os.path.isfile(os.path.join(root_mp, "usr", "bin", "apt-get")):
            raise InjectError("This card's system has no apt-get, so packages can't be installed onto it.")

        free_bytes = shutil.disk_usage(root_mp).free
        if free_bytes < self.min_free_mb * 1024 * 1024:
            raise InjectError(
                "Only %.0f MB free on the card's root partition - at least %d MB is needed for "
                "this.\n\nThis is common on a freshly flashed image: the root partition stays at "
                "its small original size until the Pi's own first real boot expands it to fill "
                "the card. Boot the Pi once first (then power it off after ~3 minutes), then try "
                "again." % (free_bytes / 1024 / 1024, self.min_free_mb)
            )
        self.session.remount_rw(root_mp)

        # Self-heal leftovers from an earlier run that was killed part-way:
        # our policy-rc.d would otherwise stop the Pi starting services for
        # ever, and a stranded resolv.conf backup is the card's real one.
        self._policy_path = os.path.join(root_mp, "usr", "sbin", "policy-rc.d")
        try:
            if os.path.isfile(self._policy_path) and open(self._policy_path).read() == POLICY_RC_D:
                os.remove(self._policy_path)
        except OSError:
            pass
        # A hard-killed Klipper build could leave these behind.
        for leftover in (BUILD_PW_PATH, BUILD_SCRIPT_PATH):
            try:
                os.remove(os.path.join(root_mp, leftover.lstrip("/")))
            except OSError:
                pass

        # Mount the boot partition where the card's system expects it, so
        # package scripts that update the kernel/initramfs/firmware write to
        # the real boot partition (as MainsailOS's build does).
        boot_dev = self.card.get("boot")
        if boot_dev:
            for rel in ("boot/firmware", "boot"):
                target = os.path.join(root_mp, rel)
                if os.path.isdir(target):
                    unmount_existing(boot_dev)
                    self.session.mount_at(boot_dev, target, "rw,nosuid,nodev")
                    break

        self._resolv_path = os.path.join(root_mp, "etc", "resolv.conf")
        self._resolv_backup = self._resolv_path + ".pi_injector_bak"
        if os.path.lexists(self._resolv_backup):
            if os.path.lexists(self._resolv_path):
                os.remove(self._resolv_path)
            shutil.move(self._resolv_backup, self._resolv_path)

        host_qemu, self.is_static = resolve_host_qemu(binfmt_prefix)
        if host_qemu is None:
            ensure_host_qemu_tools(say)
            host_qemu, self.is_static = resolve_host_qemu(binfmt_prefix)
        if host_qemu is None:
            raise InjectError(
                "Couldn't get a working %s interpreter on this machine. A static "
                "qemu-user-static build wasn't found, and the dynamically-linked "
                "qemu-user one (if present) isn't registered in binfmt_misc with the "
                "'F' flag, which chroot needs to use it safely. Try:\n\n"
                "  sudo apt install --reinstall qemu-user-static\n\n"
                "or, if apt says that package has no installation candidate:\n\n"
                "  sudo apt install --reinstall qemu-user-binfmt qemu-user\n"
                "  sudo update-binfmts --enable\n\n"
                "then try again." % binfmt_prefix
            )
        if CANCEL.is_set():
            raise CancelledError("Cancelled before anything was changed.")

        self.qemu_name = os.path.basename(host_qemu)
        say("Copying %s into the card..." % self.qemu_name)
        target_qemu_path = os.path.join(root_mp, "usr", "bin", self.qemu_name)
        os.makedirs(os.path.dirname(target_qemu_path), exist_ok=True)
        if not os.path.exists(target_qemu_path):
            shutil.copy2(host_qemu, target_qemu_path)
            os.chmod(target_qemu_path, 0o755)
            self._qemu_copied_path = target_qemu_path

        self.env = chroot_environment()
        if self.pi_zero_cpu:
            self.env["QEMU_CPU"] = "arm1176"  # qemu-user reads this at start-up

        say("Testing the chroot interpreter...")
        rc, smoke_out = run_tracked(self.prefix() + ["/bin/true"], timeout=120, env=self.env)
        if rc != 0:
            if self.pi_zero_cpu and rc in (-4, 132):
                raise InjectError(
                    "This card's own core system is built for a newer processor than the Pi "
                    "Zero / Zero W / Pi 1 have - even basic commands crash on that CPU. It can't "
                    "be fixed here: use an image made for these older Pis, or a Pi Zero 2 W / "
                    "Pi 3 / 4 / 5."
                )
            raise InjectError(
                "Couldn't run a basic command inside the chroot even after copying in %s:\n\n%s\n\n"
                "A fresh qemu-user-static install sometimes needs this machine rebooted "
                "before binfmt/chroot works. Try rebooting and running this again."
                % (self.qemu_name, smoke_out.strip() or "(no output)")
            )

        # Stop postinst scripts from trying to actually start daemons in the chroot.
        if not os.path.exists(self._policy_path):
            write_file(self._policy_path, POLICY_RC_D, 0o755)
            self._policy_written = True

        # apt / pip need working DNS inside the chroot.
        self._resolv_had_original = os.path.lexists(self._resolv_path)
        if self._resolv_had_original:
            shutil.move(self._resolv_path, self._resolv_backup)
        self._resolv_written = True
        if os.path.isfile("/etc/resolv.conf"):
            shutil.copy2("/etc/resolv.conf", self._resolv_path)
        else:
            write_file(self._resolv_path, "nameserver 1.1.1.1\nnameserver 8.8.8.8\n", 0o644)

        say("Binding /dev, /proc, /sys into the chroot...")
        for name in CHROOT_BIND_MOUNTS:
            target = os.path.join(root_mp, name)
            os.makedirs(target, exist_ok=True)
            self.session.bind("/%s" % name, target)

    # -- running things --------------------------------------------------- #
    def prefix(self, user=None):
        """The command prefix that runs a program inside the card's system.
        If we have a static interpreter it's invoked explicitly as the
        chroot command - this works regardless of this machine's binfmt_misc
        setup ("chroot: failed to run command '/usr/bin/env': No such file
        or directory" means the kernel had no handler for ARM programs)."""
        cmd = ["chroot"]
        if user is not None:
            cmd.append("--userspec=%d:%d" % user)
        cmd.append(self.root_mp)
        if self.is_static:
            cmd.append("/usr/bin/" + self.qemu_name)
        return cmd

    def run(self, args, desc, timeout=1800, user=None, env_args=()):
        return run_in_chroot(self.prefix(user), args, desc, timeout=timeout, env=self.env, env_args=env_args)

    def run_rc(self, args, timeout=600, user=None, env_args=()):
        """Like run(), but returns (returncode, output) instead of raising."""
        cmd = self.prefix(user) + ["/usr/bin/env"] + list(env_args) + args
        return run_tracked(cmd, timeout, env=self.env)

    # -- teardown --------------------------------------------------------- #
    def __exit__(self, exc_type, exc, tb):
        problems = self._teardown()
        if problems and exc_type is None:
            raise InjectError(
                "The work finished, but the card couldn't be released:\n\n%s\n\n"
                "Don't remove it yet - close any windows showing its files and try again, or "
                "restart this PC before removing the card." % "\n".join(problems)
            )
        return False

    def _teardown(self):
        kill_active_children()  # nothing may still be running inside the chroot

        # Bind mounts first, while the card's root is still mounted.
        for entry in reversed(list(self.session.mounts)):
            if not entry[2]:
                res = subprocess.run(["umount", entry[1]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if res.returncode != 0:
                    subprocess.run(["umount", "-l", entry[1]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.session.mounts.remove(entry)

        if self._resolv_backup and os.path.lexists(self._resolv_backup):
            try:
                if os.path.lexists(self._resolv_path):
                    os.remove(self._resolv_path)
                shutil.move(self._resolv_backup, self._resolv_path)
            except OSError:
                log.exception("restoring resolv.conf")
        elif self._resolv_written and not self._resolv_had_original:
            try:
                os.remove(self._resolv_path)  # we created it - remove it
            except OSError:
                pass

        if self._policy_written:
            try:
                os.remove(self._policy_path)
            except OSError:
                pass

        if self._qemu_copied_path:
            try:
                os.remove(self._qemu_copied_path)
            except OSError:
                pass

        return self.session.cleanup()


def install_ap_packages_via_chroot(card, say):
    """apt-get install hostapd + dnsmasq onto the card from this PC, so the
    hostapd backend becomes usable without ever booting the Pi."""
    with CardChroot(card, say, min_free_mb=250) as chroot:
        say("Running apt-get update on the card (this can take a few minutes)...")
        chroot.run(["apt-get", "update"], "apt-get update")

        # Finish any install an earlier, interrupted run left half-done;
        # otherwise apt refuses with "dpkg was interrupted".
        chroot.run(["dpkg", "--configure", "-a"], "dpkg --configure -a")

        say("Installing hostapd + dnsmasq on the card (several minutes)...")
        chroot.run(
            ["apt-get", "install", "-y", "--no-install-recommends", "hostapd", "dnsmasq"],
            "apt-get install hostapd dnsmasq",
        )

        say("Cleaning apt cache to save SD card space...")
        chroot.run_rc(["apt-get", "clean"], timeout=300)

        # Apply our desired enable/mask state regardless of what the
        # packages' own postinst scripts did.
        for unit in ("hostapd.service", "dnsmasq.service"):
            enable_unit(chroot.root_mp, unit)
    return True


# --------------------------------------------------------------------------- #
# Pi Zero / Zero W / Pi 1 (ARMv6) compatibility: find and rebuild software
# on the card that was compiled for a newer ARM CPU
# --------------------------------------------------------------------------- #
# The Pi Zero, Zero W and Pi 1 have an ARM1176 processor (ARMv6). Everything
# on a Raspberry Pi OS 32-bit card normally runs on it - but an image whose
# extra software (e.g. Moonraker's Python packages) was built on an ARMv7
# machine can contain code the ARMv6 chip doesn't understand. The first time
# that code runs, the program dies with "Illegal instruction" (SIGILL,
# systemd shows status=4/ILL) - which is exactly how Moonraker crash-loops.
#
# Every ARM program/library records the CPU it was built for in its
# .ARM.attributes section, so this can be checked from the PC without
# booting the Pi.

ELF_TAG_CPU_ARCH = 6
ELF_TAG_THUMB_ISA_USE = 9
ELF_TAG_ADVANCED_SIMD_ARCH = 12
ARM_CPU_ARCH_NAMES = {
    0: "pre-v4", 1: "v4", 2: "v4T", 3: "v5T", 4: "v5TE", 5: "v5TEJ", 6: "v6", 7: "v6KZ", 8: "v6T2",
    9: "v6K", 10: "v7", 11: "v6-M", 12: "v6S-M", 13: "v7E-M", 14: "v8", 15: "v8-R", 16: "v8-M.base",
    17: "v8-M.main", 18: "v8.1-A", 19: "v8.2-A", 20: "v8.3-A", 21: "v8.1-M.main", 22: "v9",
}
ARMV6_OK_CPU_ARCHS = (0, 1, 2, 3, 4, 5, 6, 7, 9)  # the Pi Zero's ARM1176 is ARMv6KZ
ARMV6_SCAN_DIRS = ("home", "usr/local", "opt")
ARMV6_QUICK_SCAN_GLOBS = ("home/*/moonraker-env", "home/*/klippy-env", "home/*/klipper/klippy/chelper")


def _uleb128(buf, i):
    value = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return value, i


def arm_elf_info(path):
    """What an ELF file says about the CPU it needs, or None if it isn't an
    ARM ELF file. Returns a dict: bits, cpu_arch, thumb, neon."""
    try:
        with open(path, "rb") as f:
            head = f.read(52)
            if len(head) < 52 or head[:4] != b"\x7fELF":
                return None
            if head[4] == 2:  # 64-bit
                if int.from_bytes(head[18:20], "little") == 0xB7:
                    return {"bits": 64, "cpu_arch": None, "thumb": None, "neon": None}
                return None
            if head[4] != 1 or head[5] != 1 or int.from_bytes(head[18:20], "little") != 40:
                return None  # not 32-bit little-endian ARM
            shoff = int.from_bytes(head[32:36], "little")
            shentsize = int.from_bytes(head[46:48], "little")
            shnum = int.from_bytes(head[48:50], "little")
            info = {"bits": 32, "cpu_arch": None, "thumb": None, "neon": None}
            if not shoff or shentsize < 40 or not 0 < shnum < 4096:
                return info
            f.seek(shoff)
            table = f.read(shentsize * shnum)
            for n in range(shnum):
                sh = table[n * shentsize:(n + 1) * shentsize]
                if len(sh) < 24 or int.from_bytes(sh[4:8], "little") != 0x70000003:  # SHT_ARM_ATTRIBUTES
                    continue
                offset = int.from_bytes(sh[16:20], "little")
                size = int.from_bytes(sh[20:24], "little")
                if not 0 < size < 1 << 20:
                    break
                f.seek(offset)
                _parse_arm_attributes(f.read(size), info)
                break
            return info
    except (OSError, ValueError):
        return None


def _parse_arm_attributes(buf, info):
    if not buf or buf[0] != 0x41:  # format version 'A'
        return
    i = 1
    while i + 4 <= len(buf):
        sub_len = int.from_bytes(buf[i:i + 4], "little")
        if sub_len < 5:
            return
        sub = buf[i + 4:i + sub_len]
        i += sub_len
        nul = sub.find(b"\0")
        if nul < 0 or sub[:nul] != b"aeabi":
            continue
        j = nul + 1
        while j + 5 <= len(sub):
            tag = sub[j]
            size = int.from_bytes(sub[j + 1:j + 5], "little")
            if size < 5:
                return
            body = sub[j + 5:j + size]
            j += size
            if tag != 1:  # only file-wide attributes matter here
                continue
            k = 0
            while k < len(body):
                attr, k = _uleb128(body, k)
                if attr in (4, 5, 67) or (attr > 32 and attr % 2 == 1):
                    end = body.find(b"\0", k)
                    k = len(body) if end < 0 else end + 1
                    continue
                if attr == 32:  # Tag_compatibility: uleb128 flag + string
                    _, k = _uleb128(body, k)
                    end = body.find(b"\0", k)
                    k = len(body) if end < 0 else end + 1
                    continue
                value, k = _uleb128(body, k)
                if attr == ELF_TAG_CPU_ARCH:
                    info["cpu_arch"] = value
                elif attr == ELF_TAG_THUMB_ISA_USE:
                    info["thumb"] = value
                elif attr == ELF_TAG_ADVANCED_SIMD_ARCH:
                    info["neon"] = value


def armv6_problem(path):
    """Why this file can't run on a Pi Zero / Zero W / Pi 1, or None."""
    info = arm_elf_info(path)
    if info is None:
        return None
    if info["bits"] == 64:
        return "64-bit ARM code"
    reasons = []
    arch = info["cpu_arch"]
    if arch is not None and arch not in ARMV6_OK_CPU_ARCHS:
        reasons.append("built for ARM%s" % ARM_CPU_ARCH_NAMES.get(arch, "?"))
    if info["neon"]:
        reasons.append("uses NEON")
    if info["thumb"] == 2 and not reasons:
        reasons.append("uses Thumb-2")
    return ", ".join(reasons) or None


def _site_packages_owners(site_dir, cache):
    """Map files in a Python site-packages folder to the pip package that
    installed them (from each *.dist-info/RECORD)."""
    if site_dir in cache:
        return cache[site_dir]
    owners = {}
    try:
        entries = os.listdir(site_dir)
    except OSError:
        entries = []
    for entry in entries:
        if not entry.endswith(".dist-info"):
            continue
        meta = {}
        try:
            with open(os.path.join(site_dir, entry, "METADATA"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        break
                    key, _, value = line.partition(":")
                    if key in ("Name", "Version") and key not in meta:
                        meta[key] = value.strip()
            with open(os.path.join(site_dir, entry, "RECORD"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    rel = line.split(",", 1)[0].strip()
                    if rel:
                        owners[os.path.normpath(os.path.join(site_dir, rel))] = (meta.get("Name"), meta.get("Version"))
        except OSError:
            continue
    cache[site_dir] = owners
    return owners


def _candidate_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        # never wander into caches, git history or other people's mounts
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".cache", "node_modules")
                       and not os.path.ismount(os.path.join(dirpath, d))]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if ".so" in name:
                yield path
                continue
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mode & 0o111 and st.st_size > 52:
                yield path


def scan_card_for_armv6_problems(root_mp, say=None, quick=False):
    """List software on a mounted card that can't run on an ARMv6 Pi.
    Each problem: {path (on the card), reason, venv, package, version}."""
    import glob

    if quick:
        bases = sorted(set(p for g in ARMV6_QUICK_SCAN_GLOBS for p in glob.glob(os.path.join(root_mp, g))))
    else:
        bases = [os.path.join(root_mp, d) for d in ARMV6_SCAN_DIRS if os.path.isdir(os.path.join(root_mp, d))]
    problems, owners_cache, checked = [], {}, 0
    for base in bases:
        for path in _candidate_files(base):
            checked += 1
            if say and checked % 500 == 0:
                say("Checking the card's programs for Pi Zero compatibility... (%d files)" % checked)
            reason = armv6_problem(path)
            if not reason:
                continue
            rel = "/" + os.path.relpath(path, root_mp)
            venv = site = package = version = None
            m = re.match(r"^(/.+?)/lib/python3[^/]*/site-packages/", rel)
            if m:
                venv = m.group(1)
                site = os.path.join(root_mp, rel[1:m.end() - 1])
                package, version = _site_packages_owners(site, owners_cache).get(os.path.normpath(path), (None, None))
            problems.append({"path": rel, "reason": reason, "venv": venv, "package": package, "version": version})
    log.info("ARMv6 scan (%s): %d files checked, %d problems", "quick" if quick else "full", checked, len(problems))
    return problems


def summarize_armv6_problems(problems):
    """Group problems into readable lines: one per pip package, plus other files."""
    lines, by_pkg, other = [], {}, []
    for p in problems:
        if p["package"]:
            by_pkg.setdefault((p["venv"], p["package"], p["version"]), []).append(p)
        else:
            other.append(p)
    for (venv, pkg, ver), items in sorted(by_pkg.items()):
        lines.append("• %s %s  (in %s) - %d file%s, %s" % (
            pkg, ver or "", venv, len(items), "" if len(items) == 1 else "s", items[0]["reason"]))
    for p in other[:15]:
        lines.append("• %s - %s" % (p["path"], p["reason"]))
    if len(other) > 15:
        lines.append("  ...and %d more files" % (len(other) - 15))
    return lines

# Extra -dev packages some Python packages need to build from source.
BUILD_DEPS_BY_PACKAGE = {
    "pillow": ["libjpeg-dev", "zlib1g-dev"],
    "cffi": ["libffi-dev"],
    "lxml": ["libxml2-dev", "libxslt1-dev"],
    "uvloop": ["autoconf", "automake", "libtool"],
    "cryptography": ["libssl-dev"],
}
BASE_BUILD_DEPS = ["python3-dev", "build-essential", "pkg-config", "libffi-dev"]
SILL_EXIT_CODES = (-4, 132)  # killed by SIGILL, directly or as reported by a shell


def check_card_armv6(card, say):
    """Read-only check: which programs on the card would crash on a Pi Zero /
    Zero W / Pi 1. Returns {"arch": ..., "problems": [...]}."""
    session = MountSession()
    error, result = None, None
    try:
        say("Unmounting existing mounts...")
        unmount_existing(card["root"])
        root_mp = session.mount(card["root"])  # read-only: nothing is changed
        arch, _prefix = detect_target_arch(root_mp)
        problems = []
        if arch == "armhf":
            say("Checking the card's programs for Pi Zero compatibility (a minute or two)...")
            problems = scan_card_for_armv6_problems(root_mp, say)
        result = {"arch": arch, "problems": problems}
    except BaseException as exc:
        error = exc
    cleanup_problems = session.cleanup()
    if error:
        raise error
    if cleanup_problems:
        raise InjectError("Cleanup failed:\n" + "\n".join(cleanup_problems))
    return result


def _module_name_for(path):
    """'/home/pi/x-env/lib/python3.13/site-packages/msgspec/_core.cpython-313-arm-linux-gnueabihf.so'
    -> 'msgspec._core' (None if it isn't an importable Python module)."""
    m = re.search(r"/site-packages/(.+)$", path)
    if not m:
        return None
    parts = m.group(1).split("/")
    parts[-1] = parts[-1].split(".", 1)[0]
    if not all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", p) for p in parts):
        return None  # e.g. vendored libraries in 'foo.libs/'
    return ".".join(parts)


def _card_user_home(root_mp, uid):
    try:
        with open(os.path.join(root_mp, "etc", "passwd")) as f:
            for line in f:
                fields = line.strip().split(":")
                if len(fields) >= 6 and fields[2] == str(uid):
                    return fields[5]
    except OSError:
        pass
    return "/tmp"


def rebuild_incompatible_in_chroot(chroot, say):
    """Inside an open CardChroot (pi_zero_cpu=True): rebuild, from source and
    for the Pi Zero's CPU, every pip package on the card that contains code
    built for a newer ARM processor; then prove the result by loading each
    rebuilt module on the emulated Pi Zero CPU."""
    result = {"rebuilt": [], "failed": [], "deleted": [], "unfixable": [], "still_crashing": [],
              "remaining": [], "nothing_to_do": False}
    root_mp = chroot.root_mp
    say("Finding programs built for a newer CPU than the Pi Zero's...")
    problems = scan_card_for_armv6_problems(root_mp, say)
    if not problems:
        result["nothing_to_do"] = True
        return result

    packages, deletable = {}, []
    for p in problems:
        if p["package"] and p["venv"]:
            packages.setdefault((p["venv"], p["package"], p["version"]), []).append(p)
        elif "/klippy/chelper/" in p["path"] and p["path"].endswith(".so"):
            deletable.append(p)  # Klipper rebuilds its C helper on the Pi by itself
        else:
            result["unfixable"].append(p)

    for p in deletable:
        try:
            os.remove(os.path.join(root_mp, p["path"].lstrip("/")))
            result["deleted"].append(p["path"])
        except OSError:
            result["unfixable"].append(p)

    if packages:
        extras = sorted({d for (_v, name, _ver) in packages for d in BUILD_DEPS_BY_PACKAGE.get(name.lower(), [])})
        say("Installing build tools on the card (apt, a few minutes)...")
        chroot.run(["apt-get", "update"], "apt-get update")
        chroot.run(["dpkg", "--configure", "-a"], "dpkg --configure -a")
        chroot.run(
            ["apt-get", "install", "-y", "--no-install-recommends"] + BASE_BUILD_DEPS + extras,
            "Installing build tools",
        )

    total = len(packages)
    for n, ((venv, name, version), files) in enumerate(sorted(packages.items()), 1):
        if CANCEL.is_set():
            raise CancelledError(
                "Repair cancelled. The packages rebuilt so far are fine; run the repair "
                "again to finish the rest."
            )
        venv_on_card = os.path.join(root_mp, venv.lstrip("/"))
        python = venv + "/bin/python"
        if not os.path.exists(os.path.join(root_mp, python.lstrip("/"))):
            result["failed"].append((name, "no Python found in %s" % venv))
            continue
        st = os.stat(venv_on_card)
        user = (st.st_uid, st.st_gid)  # build as the venv's owner, not root
        home = _card_user_home(root_mp, st.st_uid)
        spec = "%s==%s" % (name, version) if version else name
        say("Rebuilding %s %s for the Pi Zero's CPU (%d of %d - can take 5-30 minutes)..."
            % (name, version or "", n, total))
        try:
            chroot.run(
                [python, "-m", "pip", "install", "--no-cache-dir", "--force-reinstall", "--no-deps",
                 "--no-binary", name, "--index-url", "https://pypi.org/simple", spec],
                "Rebuilding %s" % name,
                timeout=5400,
                user=user,
                # Ignore the card's own pip settings (e.g. piwheels), so
                # nothing prebuilt can sneak back in: compile from source,
                # with the card's compiler, for the Pi Zero's CPU.
                env_args=["HOME=" + home, "PIP_CONFIG_FILE=/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK=1",
                          "PIP_NO_INPUT=1"],
            )
            result["rebuilt"].append((venv, name, version, files))
        except CancelledError:
            raise
        except InjectError as exc:
            log.warning("rebuilding %s failed: %s", name, exc)
            result["failed"].append((name, str(exc).splitlines()[-1][:200] if str(exc) else "build failed"))

    # Proof, not hope: load every rebuilt module on the emulated Pi Zero CPU.
    say("Testing the rebuilt packages on an emulated Pi Zero CPU...")
    for venv, name, version, files in result["rebuilt"]:
        st = os.stat(os.path.join(root_mp, venv.lstrip("/")))
        for mod in sorted({m for m in (_module_name_for(f["path"]) for f in files) if m}):
            rc, out = chroot.run_rc([venv + "/bin/python", "-c", "import %s" % mod], timeout=600,
                                    user=(st.st_uid, st.st_gid), env_args=["HOME=/tmp"])
            if rc in SILL_EXIT_CODES or "Illegal instruction" in out:
                result["still_crashing"].append(mod)
            elif rc != 0:
                log.info("import %s exited %s (not SIGILL): %s", mod, rc, out[-300:])

    result["remaining"] = [p for p in scan_card_for_armv6_problems(root_mp, say)
                           if not ("/klippy/chelper/" in p["path"])]
    return result


def repair_card_for_armv6(card, say):
    """Extras -> 'Fix for Pi Zero / Pi 1' on an existing card."""
    with CardChroot(card, say, min_free_mb=400, pi_zero_cpu=True) as chroot:
        return rebuild_incompatible_in_chroot(chroot, say)


# --------------------------------------------------------------------------- #
# Build a Klipper card: Klipper + Moonraker + Mainsail (+ Crowsnest) installed
# onto a clean Raspberry Pi OS Lite card, from this PC
# --------------------------------------------------------------------------- #
# Everything is installed inside the card's own system through the same
# qemu chroot as the package installer. On a 32-bit card the chroot emulates
# the Pi Zero / Pi 1's ARM1176 CPU, so everything that gets compiled is built
# for the oldest Pi - and older-Pi code runs natively on every newer Pi
# (Zero 2 W, 2, 3, 4, 400, 5). One card works in any Pi.
#
# The install steps follow MainsailOS's own chroot build modules and the
# Klipper / Moonraker / Crowsnest install scripts, minus the parts that need
# sudo as a normal user (which can't work under qemu): those are done here
# as root, dropping to the Klipper user with runuser.

KLIPPER_REPOS = (
    ("klipper", "https://github.com/Klipper3d/klipper.git"),
    ("moonraker", "https://github.com/Arksine/moonraker.git"),
    ("mainsail-config", "https://github.com/mainsail-crew/mainsail-config.git"),
)
CROWSNEST_REPO = ("crowsnest", "https://github.com/mainsail-crew/crowsnest.git")
MAINSAIL_ZIP_URL = "https://github.com/mainsail-crew/mainsail/releases/latest/download/mainsail.zip"
BUILD_MARKER = "etc/pi-hotspot-injector-build"
BUILD_SCRIPT_PATH = "/root/.pi-injector-build.sh"
BUILD_PW_PATH = "/root/.pi-injector-pw"

BUILD_SCRIPT = r'''#!/bin/bash
# Pi Hotspot Injector - Klipper stack build steps.
# Runs INSIDE the card's own system (chroot), as root, one step at a time.
set -eo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
U="${BUILD_USER:?}"
H="$(getent passwd "${U}" | cut -d: -f6 || true)"
H="${H:-/home/${U}}"
SKIPPED_FILE=/root/.pi-injector-skipped

as_user() { runuser -u "${U}" -- env HOME="${H}" USER="${U}" LOGNAME="${U}" "$@"; }

# Print the packages from "$@" that this system's apt actually has (package
# names drift between Raspberry Pi OS releases; a missing optional one must
# not abort the whole install).
available() {
    local p
    for p in "$@"; do
        if apt-cache show "${p}" >/dev/null 2>&1; then
            echo "${p}"
        else
            echo "  (not available on this system, skipped: ${p})" >&2
        fi
    done
}

step_user() {
    local hash old g
    hash="$(cat "${BUILD_PW_FILE:?}")"
    if id -u "${U}" >/dev/null 2>&1; then
        echo "User ${U} already exists"
    elif old="$(getent passwd 1000 | cut -d: -f1)" && [ -n "${old}" ]; then
        echo "Renaming the image's default user '${old}' to '${U}'"
        if [ -e "/home/${U}" ] && [ "$(getent passwd "${old}" | cut -d: -f6)" != "/home/${U}" ]; then
            echo "ERROR: /home/${U} already exists on the card"; exit 1
        fi
        usermod -l "${U}" "${old}"
        if getent group "${old}" >/dev/null; then groupmod -n "${U}" "${old}"; fi
        usermod -d "/home/${U}" -m "${U}"
    else
        useradd -m -u 1000 -U -s /bin/bash "${U}"
    fi
    H="$(getent passwd "${U}" | cut -d: -f6)"
    # via stdin, so the hash never appears in a process list
    printf '%s:%s\n' "${U}" "${hash}" | chpasswd -e
    for g in sudo adm dialout tty video plugdev input render netdev gpio i2c spi users audio; do
        if getent group "${g}" >/dev/null; then usermod -a -G "${g}" "${U}"; fi
    done
    mkdir -p "${H}"
    chown "${U}:$(id -gn "${U}")" "${H}"

    # Stop Raspberry Pi OS's first-boot wizard asking for a new user (it
    # would rename this one and break every Klipper path), and cloud-init
    # doing the same from the boot partition's user-data.
    ln -sf /dev/null /etc/systemd/system/userconfig.service
    if [ -d /etc/cloud ]; then touch /etc/cloud/cloud-init.disabled; fi

    # SSH on, with this card's own host keys.
    ssh-keygen -A
    if [ -f /usr/lib/systemd/system/ssh.service ]; then
        mkdir -p /etc/systemd/system/multi-user.target.wants
        ln -sf /usr/lib/systemd/system/ssh.service /etc/systemd/system/multi-user.target.wants/ssh.service
    fi
    rm -f /etc/ssh/sshd_config.d/rename_user.conf  # Pi OS "please rename" SSH banner, if present

    echo "${BUILD_HOSTNAME:?}" > /etc/hostname
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${BUILD_HOSTNAME}/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "${BUILD_HOSTNAME}" >> /etc/hosts
    fi
}

step_apt() {
    # man-db re-indexes after every package - painfully slow under emulation.
    echo "man-db man-db/auto-update boolean false" | debconf-set-selections || true
    rm -f /var/lib/man-db/auto-update

    # finish anything an interrupted earlier run left half-done
    dpkg --configure -a
    apt-get -f install -y
    # retry downloads if the connection wobbles
    echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-pi-injector-retries
    apt-get update
    local core optional prebuilt fw pkgs opt p
    # Must install, or the build stops:
    core=(git virtualenv python3-venv python3-dev libffi-dev build-essential pkg-config
          libncurses-dev libusb-1.0-0-dev libusb-1.0-0 nginx unzip curl
          libopenjp2-7 libsodium-dev zlib1g-dev libjpeg-dev hostapd dnsmasq)
    # Wanted, but names/availability vary between Raspberry Pi OS releases:
    optional=(python3-virtualenv libusb-dev packagekit polkitd pkexec wireless-tools iw
              libopenblas-dev python3-numpy python3-libcamera)
    # Python libraries with compiled parts, prebuilt by the distribution for
    # THIS CPU. The venvs can see them, so pip needn't compile them (slow
    # under emulation) - pip only builds what isn't here in a suitable version.
    prebuilt=(python3-pil python3-tornado python3-zeroconf python3-dbus-fast python3-msgspec python3-uvloop
              python3-greenlet python3-cffi python3-markupsafe python3-jinja2 python3-serial)
    fw=(avrdude gcc-avr binutils-avr avr-libc stm32flash dfu-util
        libnewlib-arm-none-eabi gcc-arm-none-eabi binutils-arm-none-eabi)
    mapfile -t pkgs < <(available "${core[@]}")
    apt-get install -y --no-install-recommends "${pkgs[@]}"
    for tool in git virtualenv gcc nginx; do
        command -v "${tool}" >/dev/null || { echo "ERROR: ${tool} did not install"; exit 1; }
    done

    # Best effort: one unusable optional package mustn't stop the build.
    : > "${SKIPPED_FILE}"
    opt=("${optional[@]}" "${prebuilt[@]}")
    if [ "${BUILD_FW_TOOLS:-0}" = 1 ]; then opt+=("${fw[@]}"); fi
    # A missing "prebuilt" package is only a speed-up lost: pip installs that
    # library into the venv anyway, so it isn't worth warning the user about.
    skipped() {
        case " ${prebuilt[*]} " in
            *" $1 "*) echo "  (no prebuilt $1 - pip will install it instead)" ;;
            *) echo "$1" >> "${SKIPPED_FILE}" ;;
        esac
    }
    for p in "${opt[@]}"; do
        if ! apt-cache show "${p}" >/dev/null 2>&1; then skipped "${p}"; fi
    done
    mapfile -t pkgs < <(available "${opt[@]}" 2>/dev/null)
    if ! apt-get install -y --no-install-recommends "${pkgs[@]}"; then
        for p in "${pkgs[@]}"; do
            apt-get install -y --no-install-recommends "${p}" || skipped "${p}"
        done
    fi
    command -v pkaction >/dev/null || echo "polkit (Moonraker can't reboot/restart services without it)" >> "${SKIPPED_FILE}"
    apt-get clean
}

step_clone() {  # only used when this PC has no git of its own
    local name="$1" url="$2"
    if as_user git -C "${H}/${name}" rev-parse --verify -q HEAD >/dev/null 2>&1; then
        echo "${name} already cloned"; return
    fi
    rm -rf "${H:?}/${name}"
    as_user git clone "${url}" "${H}/${name}"
}

step_klipper() {
    local d
    for d in config logs gcodes systemd comms; do mkdir -p "${H}/printer_data/${d}"; done
    chown -R "${U}:$(id -gn "${U}")" "${H}/printer_data"
    usermod -a -G tty,dialout "${U}"
    if [ ! -x "${H}/klippy-env/bin/python" ]; then
        as_user virtualenv -p python3 --system-site-packages "${H}/klippy-env"
    fi
    as_user "${H}/klippy-env/bin/pip" install -r "${H}/klipper/scripts/klippy-requirements.txt"
    # Build Klipper's C helper now, with the card's own compiler, instead of
    # on the Pi's first start (minutes on a Pi Zero).
    as_user bash -c "cd '${H}/klipper/klippy' && '${H}/klippy-env/bin/python' -c 'import chelper; chelper.get_ffi()'"

    cat > "${H}/printer_data/systemd/klipper.env" <<EOF
KLIPPER_ARGS="${H}/klipper/klippy/klippy.py ${H}/printer_data/config/printer.cfg -l ${H}/printer_data/logs/klippy.log -I ${H}/printer_data/comms/klippy.serial -a ${H}/printer_data/comms/klippy.sock"
EOF
    cat > /etc/systemd/system/klipper.service <<EOF
[Unit]
Description=Klipper 3D Printer Firmware SV1
Documentation=https://www.klipper3d.org/
After=network-online.target
Before=moonraker.service
Wants=udev.target

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=${U}
RemainAfterExit=yes
WorkingDirectory=${H}/klipper
EnvironmentFile=${H}/printer_data/systemd/klipper.env
ExecStart=${H}/klippy-env/bin/python \$KLIPPER_ARGS
Restart=always
RestartSec=10
EOF
    chown "${U}:$(id -gn "${U}")" "${H}/printer_data/systemd/klipper.env"
}

step_moonraker() {
    if [ ! -x "${H}/moonraker-env/bin/python" ]; then
        as_user virtualenv -p python3 --system-site-packages "${H}/moonraker-env"
    fi
    # run from scripts/ so the bundled pure-Python wheels (--find-links) are found
    as_user bash -c "cd '${H}/moonraker/scripts' && '${H}/moonraker-env/bin/pip' install -r moonraker-requirements.txt"
    if [ -f "${H}/moonraker/scripts/moonraker-speedups.txt" ]; then
        as_user bash -c "cd '${H}/moonraker/scripts' && '${H}/moonraker-env/bin/pip' install -r moonraker-speedups.txt" \
            || echo "WARNING: optional Moonraker speedups could not be installed - Moonraker works without them"
    fi

    # Only the Moonraker SERVICE gets this group (SupplementaryGroups below),
    # exactly as Moonraker's installer does: giving it to the login user
    # would let every shell and macro reboot/manage services/install
    # packages through polkit without a password.
    groupadd -f moonraker-admin
    cat > "${H}/printer_data/systemd/moonraker.env" <<EOF
MOONRAKER_DATA_PATH="${H}/printer_data"
MOONRAKER_ARGS="-m moonraker"
PYTHONPATH="${H}/moonraker"
EOF
    chown "${U}:$(id -gn "${U}")" "${H}/printer_data/systemd/moonraker.env"
    cat > /etc/systemd/system/moonraker.service <<EOF
# systemd service file for moonraker
[Unit]
Description=API Server for Klipper SV1
Requires=network-online.target
After=network-online.target

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=${U}
SupplementaryGroups=moonraker-admin
RemainAfterExit=yes
EnvironmentFile=${H}/printer_data/systemd/moonraker.env
ExecStart=${H}/moonraker-env/bin/python \$MOONRAKER_ARGS
Restart=always
RestartSec=10
EOF

    # Let Moonraker (only when running with the moonraker-admin group)
    # restart services, reboot/shut down and run system updates - what
    # Moonraker's own set-policykit-rules.sh installs.
    local gid rules_dir
    gid="$(getent group moonraker-admin | cut -d: -f3)"
    rules_dir=/usr/share/polkit-1/rules.d
    [ -d "${rules_dir}" ] || rules_dir=/etc/polkit-1/rules.d
    mkdir -p "${rules_dir}"
    cat > "${rules_dir}/moonraker.rules" <<EOF
// Allow Moonraker User to manage systemd units, reboot and shutdown
// the system
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.systemd1.manage-units" ||
         action.id == "org.freedesktop.login1.power-off" ||
         action.id == "org.freedesktop.login1.power-off-multiple-sessions" ||
         action.id == "org.freedesktop.login1.reboot" ||
         action.id == "org.freedesktop.login1.reboot-multiple-sessions" ||
         action.id == "org.freedesktop.login1.halt" ||
         action.id == "org.freedesktop.login1.halt-multiple-sessions" ||
         action.id.startsWith("org.freedesktop.packagekit.")) &&
        subject.user == "${U}") {
        // Only allow processes with the "moonraker-admin" supplementary group
        // access
        var regex = "^Groups:.+?\\\\s${gid}[\\\\s\\\\0]";
        var cmdpath = "/proc/" + subject.pid.toString() + "/status";
        try {
            polkit.spawn(["grep", "-Po", regex, cmdpath]);
            return polkit.Result.YES;
        } catch (error) {
            return polkit.Result.NOT_HANDLED;
        }
    }
});
EOF
}

step_mainsail() {
    rm -f /etc/nginx/sites-enabled/default
    cat > /etc/nginx/conf.d/common_vars.conf <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
EOF
    cat > /etc/nginx/conf.d/upstreams.conf <<'EOF'
upstream apiserver {
    ip_hash;
    server 127.0.0.1:7125;
}

upstream mjpgstreamer1 {
    ip_hash;
    server 127.0.0.1:8080;
}

upstream mjpgstreamer2 {
    ip_hash;
    server 127.0.0.1:8081;
}

upstream mjpgstreamer3 {
    ip_hash;
    server 127.0.0.1:8082;
}

upstream mjpgstreamer4 {
    ip_hash;
    server 127.0.0.1:8083;
}
EOF
    cat > /etc/nginx/sites-available/mainsail <<'EOF'
server {
    listen 80 default_server;
    # uncomment the next line to activate IPv6
    # listen [::]:80 default_server;

    access_log /var/log/nginx/mainsail-access.log;
    error_log /var/log/nginx/mainsail-error.log;

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_proxied expired no-cache no-store private auth;
    gzip_comp_level 4;
    gzip_buffers 16 8k;
    gzip_http_version 1.1;
    gzip_types text/plain text/css text/xml text/javascript application/javascript application/x-javascript application/json application/xml;

    root @MAINSAIL_ROOT@;

    index index.html;
    server_name _;

    client_max_body_size 0;
    proxy_request_buffering off;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location = /index.html {
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    location /websocket {
        proxy_pass http://apiserver/websocket;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }

    location ~ ^/(printer|api|access|machine|server)/ {
        proxy_pass http://apiserver$request_uri;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme $scheme;
    }

    location /webcam/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer1/;
    }

    location /webcam2/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer2/;
    }

    location /webcam3/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer3/;
    }

    location /webcam4/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer4/;
    }
}
EOF
    sed -i "s#@MAINSAIL_ROOT@#${H}/mainsail#" /etc/nginx/sites-available/mainsail
    ln -sf /etc/nginx/sites-available/mainsail /etc/nginx/sites-enabled/mainsail
    if [ -f /etc/logrotate.d/nginx ]; then sed -i 's/rotate 14/rotate 2/' /etc/logrotate.d/nginx; fi

    # nginx (www-data) must be able to reach ~/mainsail
    usermod -a -G "$(id -gn "${U}")" www-data
    chmod g+x "${H}"

    local g
    g="$(id -gn "${U}")"
    ln -sf /var/log/nginx/mainsail-access.log "${H}/printer_data/logs/mainsail-access.log"
    ln -sf /var/log/nginx/mainsail-error.log "${H}/printer_data/logs/mainsail-error.log"
    ln -sf "${H}/mainsail-config/mainsail.cfg" "${H}/printer_data/config/mainsail.cfg"
    chown -h "${U}:${g}" "${H}/printer_data/logs/"mainsail-*.log "${H}/printer_data/config/mainsail.cfg"

    if [ ! -f "${H}/printer_data/config/moonraker.conf" ]; then
        cat > "${H}/printer_data/config/moonraker.conf" <<'EOF'
[server]
host: 0.0.0.0
port: 7125
# The maximum size allowed for a file upload (in MiB).  Default 1024 MiB
max_upload_size: 1024
# Path to klippy Unix Domain Socket
klippy_uds_address: ~/printer_data/comms/klippy.sock

[file_manager]
# post processing for object cancel. Not recommended for low resource SBCs such as a Pi Zero. Default False
enable_object_processing: False

[authorization]
cors_domains:
    https://my.mainsail.xyz
    http://my.mainsail.xyz
    http://*.local
    http://*.lan
trusted_clients:
    10.0.0.0/8
    127.0.0.0/8
    169.254.0.0/16
    172.16.0.0/12
    192.168.0.0/16
    FE80::/10
    ::1/128

# enables partial support of Octoprint API
[octoprint_compat]

# enables moonraker to track and store print history.
[history]

# this enables moonraker announcements for mainsail
[announcements]
subscriptions:
    mainsail

# this enables moonraker's update manager
[update_manager]
refresh_interval: 168
enable_auto_refresh: True

[update_manager mainsail]
type: web
channel: stable
repo: mainsail-crew/mainsail
path: ~/mainsail

[update_manager mainsail-config]
type: git_repo
primary_branch: master
path: ~/mainsail-config
origin: https://github.com/mainsail-crew/mainsail-config.git
managed_services: klipper
EOF
    fi
    if [ ! -f "${H}/printer_data/config/printer.cfg" ]; then
        cat > "${H}/printer_data/config/printer.cfg" <<'EOF'
# ---------------------------------------------------------------------------
# Starter printer.cfg - replace this with your printer's configuration.
#   * Klipper's example configs are in ~/klipper/config/ (printer-*.cfg)
#   * Find your printer board's serial port by running:  ls /dev/serial/by-id/*
# Until then, Mainsail will show a Klipper error - that's expected.
# ---------------------------------------------------------------------------
[include mainsail.cfg]

[mcu]
serial: /dev/serial/by-id/REPLACE-ME

[printer]
kinematics: none
max_velocity: 100
max_accel: 100
EOF
    fi
    chown "${U}:${g}" "${H}/printer_data/config/moonraker.conf" "${H}/printer_data/config/printer.cfg"
    nginx -t
}

step_crowsnest() {
    cd "${H}/crowsnest"
    BASE_USER="${U}" CROWSNEST_UNATTENDED=1 CROWSNEST_ADD_CROWSNEST_MOONRAKER=1 \
        CROWSNEST_SKIP_REBOOT_PROMPT=1 make install
}

# Replace Crowsnest's prebuilt streamer packages (built for a newer CPU)
# with ustreamer compiled here - Crowsnest's own fallback method.
step_crowsnest_source() {
    local p
    dpkg --configure -a
    for p in mainsail-ustreamer mainsail-camera-streamer-raspi mainsail-camera-streamer-generic mainsail-spyglass; do
        if dpkg -s "${p}" >/dev/null 2>&1; then apt-get purge -y "${p}"; fi
    done
    rm -f /etc/apt/sources.list.d/mainsail.sources /etc/apt/sources.list.d/mainsail.list \
          /etc/apt/keyrings/mainsail.asc /etc/apt/trusted.gpg.d/mainsail.asc
    apt-get install -y --no-install-recommends git build-essential libevent-dev libjpeg-dev libbsd-dev pkg-config
    cd "${H}/crowsnest"
    rm -rf bin/ustreamer
    as_user git clone --depth=1 --single-branch -b master https://github.com/pikvm/ustreamer.git bin/ustreamer
    as_user make -C bin/ustreamer -j2
    test -x bin/ustreamer/ustreamer
}

step_finish() {
    rm -f /etc/apt/apt.conf.d/80-pi-injector-retries
    # hostapd + dnsmasq are pre-installed for the hotspot (step 3 turns them
    # on). Until then dnsmasq shouldn't run a DNS server on the Pi.
    systemctl disable dnsmasq.service hostapd.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/multi-user.target.wants/dnsmasq.service \
          /etc/systemd/system/multi-user.target.wants/hostapd.service
    apt-get clean
    rm -rf "${H}/.cache/pip" /root/.cache/pip
}

case "${1:-}" in
    user) step_user ;;
    apt) step_apt ;;
    clone) step_clone "$2" "$3" ;;
    klipper) step_klipper ;;
    moonraker) step_moonraker ;;
    mainsail) step_mainsail ;;
    crowsnest) step_crowsnest ;;
    crowsnest_source) step_crowsnest_source ;;
    finish) step_finish ;;
    *) echo "unknown step: ${1:-}"; exit 2 ;;
esac
'''


def validate_build_inputs(username, password, hostname):
    if not re.match(r"^[a-z_][a-z0-9_-]{0,30}$", username or ""):
        return "Username must be lowercase letters/digits/-/_ and start with a letter (e.g. 'pi')."
    if username == "root":
        return "Username can't be 'root'."
    if len(password or "") < 8:
        return "Please choose a login password of at least 8 characters."
    if any(c in password for c in "\r\n"):
        return "The login password can't contain line breaks."
    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", hostname or ""):
        return "Hostname may only use letters, digits and '-' (e.g. 'klipper'), and can't start or end with '-'."
    return None


def _dev_bytes(dev):
    return int(run(["blockdev", "--getsize64", dev], timeout=30).stdout.strip())


def _ext_fs_bytes(dev):
    """Size of the ext2/3/4 filesystem on dev (not of the partition)."""
    res = run(["dumpe2fs", "-h", dev], check=False, timeout=60)
    count = size = None
    for line in res.stdout.splitlines():
        if line.startswith("Block count:"):
            count = int(line.split()[-1])
        elif line.startswith("Block size:"):
            size = int(line.split()[-1])
    return count * size if count and size else None


def e2fsck_precheck(root):
    """Read-only filesystem check BEFORE anything on the card is changed.
    Also catches a PC whose e2fsprogs is too old for the card's ext4
    features (Raspberry Pi OS trixie uses features e2fsprogs < 1.47 lacks)
    - which would otherwise fail half-way through, after the partition
    table had already been changed."""
    res = run(["e2fsck", "-n", "-f", root], check=False, timeout=1800)
    text = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 0:
        return
    low = text.lower()
    if "unsupported feature" in low or "newer version of e2fsck" in low or "get a newer version" in low:
        raise InjectError(
            "This PC's filesystem tools (e2fsprogs) are too old for this card - newer Raspberry Pi OS "
            "uses ext4 features they don't know. NOTHING was changed.\n\nUse a newer Linux on this PC "
            "(e.g. Ubuntu 24.04 / Linux Mint 22 or later) or install a newer e2fsprogs, then try again."
            "\n\nDetails: %s" % text.strip()[-400:]
        )
    raise InjectError(
        "The card's system partition has filesystem errors (e2fsck exit %d). NOTHING was changed.\n\n"
        "Write the Raspberry Pi OS image to the card again (tab 1) and retry.\n\n%s"
        % (res.returncode, text.strip()[-600:])
    )


def grow_root_partition(card, say):
    """Make the card's root (last) partition, and its filesystem, fill the card.

    A freshly written Raspberry Pi OS image is only as big as the image
    itself - the Pi grows it on its first boot. The Klipper build needs that
    space now, so do the same here (sfdisk + resize2fs); the Pi's own
    first-boot resize then has nothing to do. Also finishes a half-done
    earlier attempt (partition grown but filesystem not). Only call this
    after preflight_build_card() has confirmed it's a fresh Pi OS card and
    e2fsck_precheck() passed. Returns True if anything grew."""
    for tool in ("sfdisk", "e2fsck", "resize2fs", "blockdev", "dumpe2fs"):
        if shutil.which(tool) is None:
            raise InjectError("This PC is missing the '%s' tool (packages util-linux / e2fsprogs)." % tool)
    disk, root = card["disk"], card["root"]
    m = re.search(r"(\d+)$", root)
    if not m:
        raise InjectError("Couldn't work out the partition number of %s." % root)
    partno = m.group(1)

    table = json.loads(run(["sfdisk", "-J", disk]).stdout)["partitiontable"]
    sector = int(table.get("sectorsize", 512))
    parts = table.get("partitions", [])
    if not parts:
        raise InjectError("No partitions found on %s." % disk)
    last = max(parts, key=lambda p: int(p["start"]))
    if last.get("node") != root:
        # Not the usual Pi OS layout - leave it alone; CardChroot's free
        # space check will explain if there isn't enough room.
        log.info("root %s is not the last partition (%s); not growing", root, last.get("node"))
        return False
    disk_bytes = _dev_bytes(disk)
    end_bytes = (int(last["start"]) + int(last["size"])) * sector
    grew = False

    for p in parts:
        unmount_existing(p["node"])
    run(["sync"], check=False)

    if disk_bytes - end_bytes >= 256 * 1024 * 1024:
        say("Making the card's system partition use the whole card...")
        old_bytes = _dev_bytes(root)
        run(["sfdisk", "--no-reread", "-N", partno, disk], input_text=",+\n", timeout=120)
        run(["blockdev", "--rereadpt", disk], check=False, timeout=60)
        if shutil.which("partx"):
            run(["partx", "-u", disk], check=False, timeout=60)
        if shutil.which("udevadm"):
            run(["udevadm", "settle", "--timeout=15"], check=False, timeout=30)
        for p in parts:  # the desktop may have auto-mounted the partitions again
            unmount_existing(p["node"])
        if _dev_bytes(root) < old_bytes + 128 * 1024 * 1024:
            raise InjectError(
                "The card's partition was enlarged, but this PC hasn't picked up the new size yet.\n\n"
                "Unplug the card, plug it back in and start the build again - it carries on from here."
            )
        grew = True

    part_bytes = _dev_bytes(root)
    fs_bytes = _ext_fs_bytes(root)
    if fs_bytes and part_bytes - fs_bytes > 64 * 1024 * 1024:
        say("Checking the card's filesystem before enlarging it...")
        res = run(["e2fsck", "-f", "-y", root], check=False, timeout=1800)
        if res.returncode >= 4:
            raise InjectError(
                "The card's filesystem has errors that couldn't be repaired (e2fsck exit %d). Write the "
                "Raspberry Pi OS image to the card again and retry.\n\n%s"
                % (res.returncode, (res.stdout or res.stderr)[-800:])
            )
        say("Enlarging the card's filesystem...")
        run(["resize2fs", root], timeout=1800)
        grew = True
    run(["sync"], check=False)
    return grew


def _looks_like_our_stopped_build(root_mp):
    """Fingerprint of this tool's own 'user' build step (first-boot wizard
    masked + cloud-init disabled), which MainsailOS and plain Pi OS lack."""
    link = os.path.join(root_mp, "etc", "systemd", "system", "userconfig.service")
    try:
        masked = os.path.islink(link) and os.readlink(link) == "/dev/null"
    except OSError:
        masked = False
    return masked and os.path.exists(os.path.join(root_mp, "etc", "cloud", "cloud-init.disabled"))


def preflight_build_card(card, username, say):
    """Read-only checks BEFORE the Klipper build changes anything: it must be
    a Raspberry Pi OS card, never started yet (or one this tool already
    built, for the same user), and the user name mustn't clash with a
    system account. Then a read-only filesystem check."""
    import glob

    session = MountSession()
    error, info = None, {}
    try:
        say("Checking the card (read-only)...")
        unmount_existing(card["root"])
        unmount_existing(card["boot"])
        root_mp = session.mount(card["root"])
        boot_mp = session.mount(card["boot"])
        arch, _prefix = verify_pi_card(card, boot_mp, root_mp)

        marker = None
        marker_path = os.path.join(root_mp, BUILD_MARKER)
        if os.path.isfile(marker_path):
            try:
                with open(marker_path) as f:
                    marker = json.load(f)
            except (OSError, ValueError):
                marker = {}
        elif _looks_like_our_stopped_build(root_mp):
            # A build by v2.2.0-2.2.2 that stopped part-way (they only wrote
            # the marker at the very end): let it carry on.
            marker = {}
        if marker is not None and marker.get("user") not in (None, username):
            raise InjectError(
                "This card was already built for the user '%s'. Use that same user name to redo or "
                "finish the build - or write a fresh Raspberry Pi OS image first (tab 1). Nothing was "
                "changed." % marker.get("user")
            )
        if marker is None:
            try:
                with open(os.path.join(root_mp, "etc", "machine-id")) as f:
                    machine_id = f.read().strip()
            except OSError:
                machine_id = ""
            if machine_id not in ("", "uninitialized"):
                raise InjectError(
                    "This card has already been started in a Pi, so it isn't a clean Raspberry Pi OS "
                    "card any more. The Klipper build needs a freshly written Raspberry Pi OS Lite card "
                    "- write one in tab 1 first. Nothing was changed."
                )
            if glob.glob(os.path.join(root_mp, "home", "*", "klipper")):
                raise InjectError(
                    "This card already has Klipper on it (a MainsailOS card?). The build is for plain "
                    "Raspberry Pi OS Lite - write that in tab 1 first. Nothing was changed."
                )

        # The user name mustn't be a system account or group on the card.
        users, groups = {}, set()
        try:
            with open(os.path.join(root_mp, "etc", "passwd")) as f:
                for line in f:
                    fields = line.strip().split(":")
                    if len(fields) >= 4:
                        users[fields[0]] = (int(fields[2]), int(fields[3]))
            with open(os.path.join(root_mp, "etc", "group")) as f:
                gid_names = {}
                for line in f:
                    fields = line.strip().split(":")
                    if len(fields) >= 3:
                        groups.add(fields[0])
                        gid_names[int(fields[2])] = fields[0]
        except (OSError, ValueError):
            gid_names = {}
        own_groups = {gid_names.get(gid) for uid, gid in users.values() if uid == 1000}
        if username in users and users[username][0] != 1000:
            raise InjectError("'%s' is a system account on the card - please pick another user name "
                              "(e.g. 'pi')." % username)
        if username in groups and username not in own_groups:
            raise InjectError("'%s' is a system group on the card - please pick another user name "
                              "(e.g. 'pi')." % username)
        info = {"arch": arch, "rebuild": marker is not None}
    except BaseException as exc:
        error = exc
    problems = session.cleanup()
    if error:
        raise error
    if problems:
        raise InjectError("Cleanup failed:\n" + "\n".join(problems))
    say("Checking the card's filesystem (read-only)...")
    e2fsck_precheck(card["root"])
    return info


def _card_passwd_entry(root_mp, username):
    try:
        with open(os.path.join(root_mp, "etc", "passwd")) as f:
            for line in f:
                fields = line.rstrip("\n").split(":")
                if len(fields) >= 7 and fields[0] == username:
                    return int(fields[2]), int(fields[3]), fields[5]
    except (OSError, ValueError):
        pass
    return None


def _chown_tree(path, uid, gid):
    for dirpath, dirnames, filenames in os.walk(path):
        os.lchown(dirpath, uid, gid)
        for name in dirnames + filenames:
            os.lchown(os.path.join(dirpath, name), uid, gid)


NETWORK_ERROR_HINTS = ("could not resolve host", "temporary failure in name resolution", "name or service not known",
                       "network is unreachable", "connection timed out", "timed out", "connection reset",
                       "failed to connect", "unable to access", "urlopen error", "getaddrinfo failed")


def network_error(what, detail):
    """A plain-English error for a lost internet connection mid-build."""
    detail = (detail or "").strip()
    if any(h in detail.lower() for h in NETWORK_ERROR_HINTS):
        return InjectError(
            "This PC lost its internet connection while %s (tried 4 times over about 3 minutes).\n\n"
            "Check the Wi-Fi or cable, then press 'Build Klipper Card' again - it carries on where it "
            "stopped; everything already done is kept.\n\nDetails: %s" % (what, detail[-400:])
        )
    return InjectError("Something went wrong while %s:\n\n%s" % (what, detail[-1500:]))


def _host_git_clone(url, dest, say):
    """Clone with this PC's own git (native speed). Returns False if the PC
    has no git, so the caller can clone inside the card instead."""
    if shutil.which("git") is None:
        return False
    if os.path.isdir(os.path.join(dest, ".git")):
        rc, _out = run_tracked(["git", "-c", "safe.directory=*", "-C", dest, "rev-parse", "--verify", "-q", "HEAD"],
                               timeout=60)
        if rc == 0:
            return True  # complete clone from an earlier run
        log.info("incomplete clone at %s - downloading again", dest)
    out = ""
    for attempt, pause in enumerate((0, 15, 45, 90)):
        if pause:
            say("Internet connection problem - trying again in %d seconds (attempt %d of 4)..." % (pause, attempt + 1))
            for _ in range(pause):
                if CANCEL.is_set():
                    raise CancelledError("Build cancelled.")
                time.sleep(1)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        try:
            rc, out = run_tracked(["git", "clone", "--quiet", url, dest], timeout=1800)
        except subprocess.TimeoutExpired:
            rc, out = 1, "timed out after 30 minutes"
        if rc == 0:
            return True
        if CANCEL.is_set():
            raise CancelledError("Build cancelled.")
        log.warning("git clone %s failed (attempt %d): %s", url, attempt + 1, out[-300:])
    raise network_error("downloading %s" % url, out)


def _download_mainsail(dest, say):
    """Download the latest Mainsail release zip (on this PC) into dest."""
    import io

    cache_dir = os.path.join(tempfile.gettempdir(), "pi-injector-downloads")
    os.makedirs(cache_dir, exist_ok=True)
    zip_path = os.path.join(cache_dir, "mainsail.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)  # always fetch the current release
    say("Downloading Mainsail...")
    try:
        shown = set()

        def report(frac, text):  # the zip is small: only mention dropouts, once each
            if text.startswith("Connection dropped"):
                key = text.split(". Resuming")[0]
                if key not in shown:
                    shown.add(key)
                    say(key + " - retrying...")

        resumable_download(MAINSAIL_ZIP_URL, zip_path, say, report, CANCEL, label="Mainsail")
    except DownloadPaused as exc:
        raise InjectError("Couldn't download Mainsail: %s\n\nPress 'Build Klipper Card' again - it carries on "
                          "where it stopped." % exc)
    with open(zip_path, "rb") as f:
        data = f.read()
    os.remove(zip_path)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InjectError("The Mainsail download was damaged - please try again.")
    if "index.html" not in zf.namelist():
        raise InjectError("The Mainsail download doesn't look right (no index.html in it).")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    for member in zf.infolist():  # refuse any path that escapes dest
        target = os.path.realpath(os.path.join(dest, member.filename))
        if not target.startswith(os.path.realpath(dest) + os.sep) and target != os.path.realpath(dest):
            raise InjectError("The Mainsail download contains an unsafe path - not using it.")
    zf.extractall(dest)


def _crowsnest_packages_armv6_problems(root_mp):
    """Files installed by Crowsnest's prebuilt 'mainsail-*' apt packages that
    can't run on an ARMv6 Pi."""
    import glob

    problems = []
    for listing in glob.glob(os.path.join(root_mp, "var/lib/dpkg/info/mainsail-*.list")):
        try:
            with open(listing) as f:
                paths = [line.strip() for line in f if line.strip()]
        except OSError:
            continue
        for p in paths:
            full = os.path.join(root_mp, p.lstrip("/"))
            if os.path.isfile(full) and not os.path.islink(full):
                reason = armv6_problem(full)
                if reason:
                    problems.append((p, reason))
    return problems


def _crowsnest_has_streamer(root_mp, home):
    candidates = ["usr/bin/ustreamer", "usr/bin/ustreamer.bin", "usr/local/bin/ustreamer",
                  home.lstrip("/") + "/crowsnest/bin/ustreamer/ustreamer",
                  home.lstrip("/") + "/crowsnest/bin/ustreamer/ustreamer.bin"]
    return any(os.path.isfile(os.path.join(root_mp, c)) for c in candidates)


def disable_desktop_at_boot(root_mp):
    """Raspberry Pi OS *with desktop*: start in text mode instead. A printer
    controller doesn't need a graphical desktop, and it would use a big
    share of a Pi Zero's 512 MB. Reversible with 'sudo raspi-config'
    (System Options -> Boot). Returns True if it changed anything."""
    desktop_bins = ("usr/sbin/lightdm", "usr/bin/labwc", "usr/bin/wayfire", "usr/sbin/gdm3", "usr/sbin/sddm")
    if not any(os.path.exists(os.path.join(root_mp, b)) for b in desktop_bins):
        return False
    target = find_unit_path(root_mp, "multi-user.target")
    if target is None:
        return False
    link = os.path.join(root_mp, "etc", "systemd", "system", "default.target")
    try:
        if os.path.islink(link) and os.readlink(link).endswith("/multi-user.target"):
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)
    log.info("Desktop image: default boot target set to multi-user (text mode)")
    return True


def _smoke_test_moonraker(chroot, user, home, say, wait=240):
    """Start Moonraker once inside the card (on the emulated CPU), bound to
    this PC's loopback only, and wait for its API to answer. This is the
    exact thing that crash-looped on MainsailOS - proving it starts here
    beats hoping. Returns (ok, detail)."""
    import socket
    import urllib.error
    import urllib.request

    # Talk to Moonraker directly, never through a proxy this PC may have set.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    s = socket.socket()
    try:
        if s.connect_ex(("127.0.0.1", 7125)) == 0:
            return None, "skipped - something on this PC already uses port 7125"
    finally:
        s.close()

    test_dir = "/tmp/pi-injector-moonraker-test"
    host_dir = os.path.join(chroot.root_mp, test_dir.lstrip("/"))
    shutil.rmtree(host_dir, ignore_errors=True)
    os.makedirs(os.path.join(host_dir, "config"))
    with open(os.path.join(host_dir, "config", "moonraker.conf"), "w") as f:
        f.write("[server]\nhost: 127.0.0.1\nport: 7125\nklippy_uds_address: %s/klippy.sock\n\n"
                "[machine]\nprovider: none\n\n[authorization]\ntrusted_clients:\n    127.0.0.0/8\n" % test_dir)
    uid_gid = _card_passwd_entry(chroot.root_mp, user)
    _chown_tree(host_dir, uid_gid[0], uid_gid[1])

    cmd = chroot.prefix(uid_gid[:2]) + [
        "/usr/bin/env", "HOME=" + home, "PYTHONPATH=%s/moonraker" % home,
        home + "/moonraker-env/bin/python", "-m", "moonraker", "-d", test_dir, "-n",
    ]
    log.info("run: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, env=chroot.env)
    with CHILDREN_LOCK:
        ACTIVE_CHILDREN.append(proc)
    ok, detail = False, "Moonraker didn't answer within %d s" % wait
    try:
        deadline = time.time() + wait
        while time.time() < deadline and not CANCEL.is_set():
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                sig = " (Illegal instruction!)" if proc.returncode in SILL_EXIT_CODES else ""
                detail = "Moonraker exited with code %s%s:\n%s" % (proc.returncode, sig, out[-1200:])
                break
            try:
                with opener.open("http://127.0.0.1:7125/server/info", timeout=5) as r:
                    info = json.loads(r.read().decode("utf-8"))
                ok = True
                detail = "Moonraker %s started and answered" % info.get("result", {}).get("moonraker_version", "")
                break
            except urllib.error.HTTPError as exc:  # any HTTP answer = the server is up
                ok, detail = True, "Moonraker started and answered (HTTP %s)" % exc.code
                break
            except Exception:
                time.sleep(3)
    finally:
        kill_process_group(proc)
        with CHILDREN_LOCK:
            if proc in ACTIVE_CHILDREN:
                ACTIVE_CHILDREN.remove(proc)
        shutil.rmtree(host_dir, ignore_errors=True)
    log.info("Moonraker smoke test: ok=%s %s", ok, detail)
    return ok, detail


def build_klipper_card(card, opts, say):
    """Install Klipper + Moonraker + Mainsail (+ optional Crowsnest) onto a
    clean Raspberry Pi OS Lite card. opts: username, password, hostname,
    crowsnest (bool), fw_tools (bool). Returns a result dict."""
    user, hostname = opts["username"], opts["hostname"]
    result = {"grew": False, "pi_zero_build": False, "crowsnest": opts["crowsnest"],
              "crowsnest_source_build": False, "repair": None, "smoke": (None, ""), "warnings": [],
              "skipped": [], "desktop_disabled": False}
    ensure_writable_card(card)
    say("Checking this PC's internet connection...")
    check_internet()
    # Read-only: is this a fresh Pi OS card, and does its filesystem check
    # clean with this PC's tools? Nothing is changed until this passes.
    preflight_build_card(card, user, say)
    password_hash = hash_password(opts["password"])  # never logged

    result["grew"] = grow_root_partition(card, say)

    need_mb = 2500 + (1200 if opts["fw_tools"] else 0) + (400 if opts["crowsnest"] else 0)
    with CardChroot(card, say, min_free_mb=need_mb, pi_zero_cpu="auto") as chroot:
        root_mp = chroot.root_mp
        result["pi_zero_build"] = chroot.pi_zero_cpu
        if not os.path.isfile(os.path.join(root_mp, "etc", "rpi-issue")) and \
                not os.path.isdir(os.path.join(root_mp, "etc", "rpi")):
            result["warnings"].append("This doesn't look like Raspberry Pi OS - the build may not work as expected.")

        # Mark the card as "being built by this tool" straight away, so a
        # build that stops part-way (lost internet, cancel, power cut) can
        # simply be started again - see preflight_build_card().
        marker_path = os.path.join(root_mp, BUILD_MARKER)
        if not os.path.isfile(marker_path):
            write_file(marker_path, json.dumps({"version": APP_VERSION, "user": user, "hostname": hostname,
                                                "status": "in-progress"}) + "\n", 0o644)

        script_on_host = os.path.join(root_mp, BUILD_SCRIPT_PATH.lstrip("/"))
        pw_on_host = os.path.join(root_mp, BUILD_PW_PATH.lstrip("/"))
        os.makedirs(os.path.dirname(script_on_host), exist_ok=True)
        write_file(script_on_host, BUILD_SCRIPT, 0o700)
        env_args = ["BUILD_USER=" + user, "BUILD_HOSTNAME=" + hostname,
                    "BUILD_FW_TOOLS=" + ("1" if opts["fw_tools"] else "0"), "BUILD_PW_FILE=" + BUILD_PW_PATH]

        def step(name, message, timeout=3600, extra=()):
            if CANCEL.is_set():
                raise CancelledError("Build cancelled. The card can be built again later - finished steps are kept.")
            say(message)
            chroot.run(["/bin/bash", BUILD_SCRIPT_PATH, name] + list(extra),
                       "Build step '%s'" % name, timeout=timeout, env_args=env_args)

        try:
            write_file(pw_on_host, password_hash + "\n", 0o600)
            try:
                step("user", "Setting up the login user, SSH and hostname...")
            finally:
                os.remove(pw_on_host)

            step("apt", "Installing system packages - 15-40 minutes, the longest part...", timeout=7200)
            skipped_file = os.path.join(root_mp, "root", ".pi-injector-skipped")
            try:
                with open(skipped_file) as f:
                    result["skipped"] = [line.strip() for line in f if line.strip()]
                os.remove(skipped_file)
            except OSError:
                pass

            entry = _card_passwd_entry(root_mp, user)
            if not entry:
                raise InjectError("The user '%s' wasn't created on the card." % user)
            uid, gid, home = entry
            home_on_host = os.path.join(root_mp, home.lstrip("/"))

            repos = list(KLIPPER_REPOS) + ([CROWSNEST_REPO] if opts["crowsnest"] else [])
            for name, url in repos:
                if CANCEL.is_set():
                    raise CancelledError("Build cancelled.")
                say("Downloading %s..." % name)
                dest = os.path.join(home_on_host, name)
                if _host_git_clone(url, dest, say):
                    _chown_tree(dest, uid, gid)
                else:
                    step("clone", "Downloading %s (inside the card)..." % name, timeout=3600, extra=(name, url))

            step("klipper", "Setting up Klipper (compiles parts of it for the Pi) - 10-30 minutes...", timeout=7200)
            step("moonraker", "Setting up Moonraker - 10-40 minutes...", timeout=9000)

            mainsail_dir = os.path.join(home_on_host, "mainsail")
            _download_mainsail(mainsail_dir, say)
            _chown_tree(mainsail_dir, uid, gid)
            step("mainsail", "Setting up Mainsail and the web server...")

            if opts["crowsnest"]:
                step("crowsnest", "Installing Crowsnest (webcam support) - 5-20 minutes...", timeout=5400)
                if chroot.pi_zero_cpu and _crowsnest_packages_armv6_problems(root_mp):
                    result["crowsnest_source_build"] = True
                    step("crowsnest_source",
                         "Crowsnest's prebuilt video streamer needs a newer Pi - compiling one for every Pi...",
                         timeout=5400)
                elif not _crowsnest_has_streamer(root_mp, home):
                    # Crowsnest's installer carries on without one if its own
                    # download/build fails (e.g. a network hiccup).
                    result["crowsnest_source_build"] = True
                    step("crowsnest_source", "Crowsnest has no video streamer yet - compiling one...", timeout=5400)
                if not _crowsnest_has_streamer(root_mp, home):
                    result["warnings"].append("Crowsnest was installed, but no video streamer could be set up - "
                                              "webcams won't work until it's reinstalled on the Pi.")

            if chroot.pi_zero_cpu:
                result["repair"] = rebuild_incompatible_in_chroot(chroot, say)

            say("Test-starting Moonraker on the emulated Pi (up to 4 minutes)...")
            result["smoke"] = _smoke_test_moonraker(chroot, user, home, say)

            step("finish", "Tidying up...")
            write_file(os.path.join(root_mp, BUILD_MARKER),
                       json.dumps({"version": APP_VERSION, "user": user, "hostname": hostname, "status": "complete",
                                   "crowsnest": opts["crowsnest"], "pi_zero_build": chroot.pi_zero_cpu}) + "\n",
                       0o644)
            for unit in ("klipper.service", "moonraker.service", "nginx.service"):
                enable_unit(root_mp, unit)
            if opts["crowsnest"]:
                enable_unit(root_mp, "crowsnest.service")
            result["desktop_disabled"] = disable_desktop_at_boot(root_mp)
        finally:
            for path in (pw_on_host, script_on_host):
                try:
                    os.remove(path)
                except OSError:
                    pass
    rep = result.get("repair") or {}
    log.info(
        "Build finished: pi_zero_build=%s smoke=%s desktop_disabled=%s crowsnest_source=%s skipped=%s "
        "rebuilt=%s failed=%s still_crashing=%s remaining=%d warnings=%s",
        result["pi_zero_build"], result["smoke"], result["desktop_disabled"], result["crowsnest_source_build"],
        result["skipped"], [x[1] for x in rep.get("rebuilt", [])], rep.get("failed", []),
        rep.get("still_crashing", []), len(rep.get("remaining", [])), result["warnings"],
    )
    return result


# --------------------------------------------------------------------------- #
# Injection Procedures
# --------------------------------------------------------------------------- #
def inject_hostapd_backend(root_mp, settings, summary):
    etc = os.path.join(root_mp, "etc")
    addrs = subnet_addresses(settings["subnet"])

    # 1. Tell NetworkManager to leave wlan0 alone entirely.
    nm_confd = os.path.join(etc, "NetworkManager", "conf.d")
    os.makedirs(nm_confd, mode=0o755, exist_ok=True)
    write_file(
        os.path.join(nm_confd, "99-unmanaged-wlan0.conf"),
        "[keyfile]\nunmanaged-devices=interface-name:wlan0\n",
        0o644,
    )

    # 2. hostapd.conf (package default path) + /etc/default/hostapd as a
    #    belt-and-braces pointer for the legacy init-script fallback.
    hostapd_dir = os.path.join(etc, "hostapd")
    os.makedirs(hostapd_dir, mode=0o755, exist_ok=True)
    write_file(os.path.join(hostapd_dir, "hostapd.conf"), build_hostapd_conf(settings), 0o600)

    default_dir = os.path.join(etc, "default")
    os.makedirs(default_dir, mode=0o755, exist_ok=True)
    write_file(os.path.join(default_dir, "hostapd"), 'DAEMON_CONF="/etc/hostapd/hostapd.conf"\n', 0o644)

    # 3. dnsmasq: drop-in config so we don't clobber anything already there.
    dnsmasq_d = os.path.join(etc, "dnsmasq.d")
    os.makedirs(dnsmasq_d, mode=0o755, exist_ok=True)
    write_file(
        os.path.join(dnsmasq_d, "hotspot.conf"),
        build_dnsmasq_conf(addrs["ap_gateway"], addrs["dhcp_start"], addrs["dhcp_end"]),
        0o644,
    )

    # 4. Static IP on wlan0 before hostapd/dnsmasq try to bind to it.
    unit_dir = os.path.join(etc, "systemd", "system")
    os.makedirs(unit_dir, exist_ok=True)
    write_file(os.path.join(unit_dir, STATIC_IP_SERVICE), build_static_ip_service(addrs["ap_address"]), 0o644)
    wants_dir = os.path.join(unit_dir, "multi-user.target.wants")
    os.makedirs(wants_dir, exist_ok=True)
    link = os.path.join(wants_dir, STATIC_IP_SERVICE)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink("/etc/systemd/system/" + STATIC_IP_SERVICE, link)

    # 5. Make sure hostapd/dnsmasq start after our static-IP unit, and are
    #    actually enabled (hostapd ships masked by default on Debian so it
    #    can't start accidentally with no config).
    for unit in ("hostapd.service", "dnsmasq.service"):
        dropin_dir = os.path.join(unit_dir, unit + ".d")
        os.makedirs(dropin_dir, exist_ok=True)
        write_file(
            os.path.join(dropin_dir, "wait-for-static-ip.conf"),
            "[Unit]\nAfter=%s\nRequires=%s\n" % (STATIC_IP_SERVICE, STATIC_IP_SERVICE),
            0o644,
        )
        if not enable_unit(root_mp, unit):
            raise InjectError(
                "%s's systemd unit file wasn't found on this image - hostapd/dnsmasq "
                "may not actually be installed despite the binaries being present. "
                "Try the NetworkManager backend instead, or reinstall the package." % unit
            )

    summary.append("hostapd + dnsmasq backend configured (bypasses NetworkManager's AP mode).")
    summary.append("Hotspot profile '%s' written (channel %d)." % (settings["ssid"], settings["channel"]))
    summary.append(
        "Gateway %s, DHCP %s-%s. wlan0 marked unmanaged in NetworkManager."
        % (addrs["ap_gateway"], addrs["dhcp_start"], addrs["dhcp_end"])
    )


def inject_networkmanager_backend(root_mp, settings, summary):
    nm_dir = os.path.join(root_mp, "etc", "NetworkManager")
    if not os.path.isdir(nm_dir):
        raise InjectError("Target image does not use NetworkManager.")

    addrs = subnet_addresses(settings["subnet"])
    conn_dir = os.path.join(nm_dir, "system-connections")
    os.makedirs(conn_dir, mode=0o700, exist_ok=True)
    conn_path = os.path.join(conn_dir, CONN_FILE)
    keyfile = build_nm_keyfile(settings["ssid"], settings["password"], settings["channel"], addrs["ap_address"])
    write_file(conn_path, keyfile, 0o600)
    summary.append(
        "Hotspot profile '%s' written (channel %d) via NetworkManager. Gateway %s."
        % (settings["ssid"], settings["channel"], addrs["ap_gateway"])
    )


def inject_card(card, settings, say):
    session = MountSession()
    summary, warnings = [], []
    error = None

    try:
        ensure_writable_card(card)
        root_dev, boot_dev = card["root"], card["boot"]
        say("Unmounting existing mounts...")
        unmount_existing(root_dev)
        unmount_existing(boot_dev)

        root_mp = session.mount(root_dev)
        boot_mp = session.mount(boot_dev)

        cmdline_path = os.path.join(boot_mp, "cmdline.txt")
        backend = settings["ap_backend"]

        card_arch, _prefix = verify_pi_card(card, boot_mp, root_mp)
        try:
            with open(os.path.join(root_mp, "etc", "hostname")) as f:
                settings["card_hostname"] = f.read().strip()
        except OSError:
            pass

        if backend == "hostapd" and not hostapd_dnsmasq_available(root_mp):
            raise MissingPackagesError(
                "This card's image doesn't have hostapd and dnsmasq installed yet, so "
                "the recommended hostapd + dnsmasq hotspot can't be set up on it."
            )

        session.remount_rw(root_mp)
        session.remount_rw(boot_mp)

        # cmdline.txt: country + rfkill/regdom tweaks benefit both backends.
        with open(cmdline_path, encoding="utf-8") as f:
            original = f.read()
        write_file(cmdline_path, build_cmdline(original, settings["country"]), posix=False)
        summary.append("Wi-Fi country set to %s." % settings["country"])

        # MainsailOS's own first-boot Wi-Fi provisioning file can fight with
        # a pre-seeded hotspot profile - clear it out regardless of backend.
        for name in ("headless_nm.txt",):
            p = os.path.join(boot_mp, name)
            if os.path.exists(p):
                os.remove(p)
                summary.append("Removed conflicting %s from boot partition." % name)

        if backend == "hostapd":
            inject_hostapd_backend(root_mp, settings, summary)
        else:
            inject_networkmanager_backend(root_mp, settings, summary)

            # Watchdog only makes sense for the NetworkManager backend - it
            # retries `nmcli connection up Hotspot` a few times after boot.
            if settings["watchdog"]:
                unit_dir = os.path.join(root_mp, "etc", "systemd", "system")
                wants_dir = os.path.join(unit_dir, "multi-user.target.wants")
                os.makedirs(wants_dir, exist_ok=True)
                service_content = build_service_unit(settings["country"])
                write_file(os.path.join(unit_dir, SERVICE_NAME), service_content, 0o644)
                link = os.path.join(wants_dir, SERVICE_NAME)
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink("/etc/systemd/system/" + SERVICE_NAME, link)
                summary.append("Watchdog service installed.")

        # User Account (both backends)
        if settings["want_user"] and os.path.exists(os.path.join(root_mp, BUILD_MARKER)):
            summary.append("Login user was already set up when this card's Klipper was built - left as it is.")
        elif settings["want_user"]:
            hashed = hash_password(settings["user_pw"])
            write_file(os.path.join(boot_mp, "ssh"), "", posix=False)
            write_file(os.path.join(boot_mp, "userconf.txt"), "%s:%s\n" % (settings["username"], hashed), posix=False)
            summary.append("SSH & User '%s' configured." % settings["username"])

        # Heads-up for Pi Zero / Zero W / Pi 1 owners: an image whose extra
        # software was built for a newer ARM CPU makes Moonraker (etc.) crash
        # with "Illegal instruction" on those boards.
        say("Checking the card is compatible with every Pi model...")
        if card_arch == "arm64":
            warnings.append(
                "This card has a 64-bit system: it will NOT start on a Pi Zero, Zero W or Pi 1 "
                "(fine on a Pi Zero 2 W, 3, 4 or 5)."
            )
        else:
            bad = scan_card_for_armv6_problems(root_mp, quick=True)
            if bad:
                pkgs = sorted({p["package"] for p in bad if p["package"]})
                warnings.append(
                    "If this card is for a Pi Zero, Zero W or Pi 1: %d program file%s on it %s built "
                    "for a newer processor (%s) and will crash there with 'Illegal instruction' - "
                    "that's what stops Moonraker working. Use Extras -> 'Fix for Pi Zero / Pi 1'. "
                    "(A Pi Zero 2 W, 3, 4 or 5 is not affected.)"
                    % (len(bad), "" if len(bad) == 1 else "s", "is" if len(bad) == 1 else "are",
                       ", ".join(pkgs[:6]) or "e.g. " + bad[0]["path"])
                )

    except BaseException as exc:
        error = exc

    say("Flushing writes...")
    problems = session.cleanup()
    if error:
        raise error
    if problems:
        raise InjectError("Cleanup failed:\n" + "\n".join(problems))
    return summary, warnings


def inject_inquisitor(card, say):
    session = MountSession()
    error = None
    try:
        ensure_writable_card(card)
        root_dev, boot_dev = card["root"], card["boot"]
        say("Unmounting existing mounts...")
        unmount_existing(root_dev)
        unmount_existing(boot_dev)

        root_mp = session.mount(root_dev)
        boot_mp = session.mount(boot_dev)
        verify_pi_card(card, boot_mp, root_mp)

        session.remount_rw(root_mp)
        session.remount_rw(boot_mp)
        say("Installing the diagnostics collector...")

        # Remove a previous run's report so a stale one can't be mistaken
        # for fresh results after the next boot.
        for old in ("XXXXX_DIAGNOSTICS.txt", "BOOT_DIAGNOSTICS.txt"):
            p = os.path.join(boot_mp, old)
            if os.path.exists(p):
                os.remove(p)

        diag_script_path = os.path.join(root_mp, "usr", "local", "bin", "pi_diag.sh")
        unit_path = os.path.join(root_mp, "etc", "systemd", "system", DIAG_SERVICE_NAME)
        wants_dir = os.path.join(root_mp, "etc", "systemd", "system", "multi-user.target.wants")

        os.makedirs(os.path.dirname(diag_script_path), mode=0o755, exist_ok=True)
        os.makedirs(os.path.dirname(unit_path), mode=0o755, exist_ok=True)
        write_file(diag_script_path, DIAG_SCRIPT_CONTENT, mode=0o755)
        write_file(unit_path, DIAG_SERVICE_UNIT, mode=0o644)

        os.makedirs(wants_dir, exist_ok=True)
        wants_link = os.path.join(wants_dir, DIAG_SERVICE_NAME)
        if os.path.lexists(wants_link):
            os.remove(wants_link)
        os.symlink("/etc/systemd/system/" + DIAG_SERVICE_NAME, wants_link)

    except BaseException as exc:
        error = exc

    say("Flushing writes...")
    problems = session.cleanup()
    if error:
        raise error
    if problems:
        raise InjectError("Cleanup failed:\n" + "\n".join(problems))


# --------------------------------------------------------------------------- #
# Image writer (OS image -> SD card / USB stick)
# --------------------------------------------------------------------------- #
IMAGE_FILE_TYPES = [
    ("Disk images", "*.img *.img.xz *.img.gz *.img.bz2 *.img.zst *.zip *.xz *.gz *.bz2 *.zst *.iso *.IMG"),
    ("All files", "*"),
]

def is_emmc(disk):
    """Built-in eMMC chips (cheap laptops, some thin clients) show up as
    /dev/mmcblkN just like an SD card in a reader. The kernel knows the
    difference: /sys/block/<dev>/device/type is 'MMC' for eMMC, 'SD' for
    an SD card."""
    base = os.path.basename(disk)
    try:
        with open("/sys/block/%s/device/type" % base) as f:
            return f.read().strip().upper() == "MMC"
    except OSError:
        return False


def classify_write_targets(quiet=False):
    """Split every whole disk into (eligible, rejected).

    eligible: dicts for removable drives inside the size window that
    nothing on this computer is using. rejected: (description, reason)
    pairs, so the GUI can answer "why isn't my drive listed?" instead of
    leaving someone to guess."""
    eligible, rejected = [], []
    for dev in lsblk_devices(quiet=quiet):
        if dev.get("type") != "disk":
            continue
        name = dev.get("name", "")
        base = os.path.basename(name)
        if base.startswith(("loop", "zram", "ram", "sr", "fd", "dm-", "md", "nbd")):
            continue
        if re.match(r"^mmcblk\d+(boot\d+|rpmb)$", base):
            continue

        size = device_size_bytes(dev)
        desc = "%s, %s (%s)" % (device_model(dev), human_size(size), name)
        is_mmc = base.startswith("mmcblk")

        if is_mmc and is_emmc(name):
            rejected.append((desc, "built-in eMMC storage - part of this computer"))
            continue
        if not is_external_disk(dev):
            rejected.append((desc, "internal drive (not a USB stick or SD card)"))
            continue

        in_use = [mp for node in walk(dev) for mp in node_mountpoints(node) if is_critical_mount(mp)]
        if in_use:
            rejected.append((desc, "in use by this computer (%s)" % ", ".join(in_use)))
            continue
        if size <= 0:
            rejected.append((desc, "no card inserted in this reader"))
            continue
        if size > MAX_WRITE_TARGET_BYTES:
            rejected.append((desc, "bigger than the %s safety limit" % WRITE_TARGET_LABEL))
            continue
        if size < MIN_WRITE_TARGET_BYTES:
            rejected.append((desc, "smaller than the %s safety limit" % WRITE_TARGET_LABEL))
            continue
        if truthy(dev.get("ro")):
            rejected.append((desc, "write-protected (check the SD card's LOCK switch)"))
            continue

        partitions = [c for c in walk(dev) if c is not dev]
        contents = []
        for p in partitions:
            if p.get("type") != "part":
                continue
            label = (p.get("label") or "").strip()
            bits = [label or "(no name)", p.get("fstype") or "unknown format", human_size(p.get("size"))]
            contents.append("%s: %s" % (os.path.basename(p.get("name", "")), ", ".join(bits)))

        eligible.append(
            {
                "disk": name,
                "size": size,
                "serial": (dev.get("serial") or "").strip(),
                "model": device_model(dev),
                "desc": desc,
                "partitions": [p.get("name") for p in partitions if p.get("name")],
                "contents": contents,
                "mounted": sorted({mp for node in walk(dev) for mp in node_mountpoints(node)}),
            }
        )
    return eligible, rejected


def sniff_image(path):
    """Identify the file by its first bytes rather than its name - a
    renamed or double-extensioned download ('x.img.xz.img') still works,
    and something that isn't an image at all gets caught."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zst"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:4] == b"Rar!":
        return "rar"
    return "raw"


def xz_uncompressed_size(path):
    if shutil.which("xz") is None:
        return None
    res = run(["xz", "--robot", "--list", path], check=False, timeout=60)
    for line in res.stdout.splitlines():
        fields = line.split("\t")
        if fields and fields[0] == "totals" and len(fields) > 4 and fields[4].isdigit():
            return int(fields[4])
    return None


_LZMA_ERROR = lzma.LZMAError if lzma is not None else EOFError


class ImageSource:
    """A readable stream of the *decompressed* image, plus whatever we can
    learn about its final size, so progress and the does-it-fit check work
    for every supported format."""

    def __init__(self, path):
        self.path = path
        self.kind = sniff_image(path)
        self.file_size = os.path.getsize(path)
        self.size = None  # uncompressed size, when knowable up front
        self.inner_name = None
        self._raw = None
        self._zip = None
        self._proc = None
        self.stream = None

    def open(self):
        k = self.kind
        if k in ("7z", "rar"):
            raise InjectError(
                "That's a .%s archive, which this tool can't read directly. Extract it "
                "first (right-click it -> Extract Here) and choose the .img file inside." % k
            )
        if k == "raw":
            self._raw = open(self.path, "rb")
            self.stream = self._raw
            self.size = self.file_size
        elif k in ("xz", "gz", "bz2"):
            module = {"xz": lzma, "gz": gzip, "bz2": bz2}[k]
            if module is None:
                raise InjectError(
                    "This Python installation can't read .%s files (its '%s' module is missing). "
                    "Extract the image first, or run this tool with your distribution's own "
                    "python3." % (k, "lzma" if k == "xz" else k)
                )
            self._raw = open(self.path, "rb")
            opener = module.open
            self.stream = opener(self._raw, "rb")
            if k == "xz":
                self.size = xz_uncompressed_size(self.path)
        elif k == "zip":
            try:
                self._zip = zipfile.ZipFile(self.path)
            except zipfile.BadZipFile as exc:
                raise InjectError("The .zip file is damaged or incomplete (%s). Try downloading it again." % exc)
            files = [i for i in self._zip.infolist() if not i.filename.endswith("/")]
            images = [i for i in files if i.filename.lower().endswith((".img", ".iso", ".raw", ".bin"))]
            if not images:
                raise InjectError(
                    "No disk image (.img) found inside that .zip. Files in it:\n\n%s"
                    % "\n".join("  " + i.filename for i in files[:15])
                )
            pick = max(images, key=lambda i: i.file_size)
            self.inner_name = pick.filename
            self.size = pick.file_size
            self.stream = self._zip.open(pick)
        elif k == "zst":
            if shutil.which("zstd") is None:
                raise InjectError(
                    "This image is zstd-compressed (.zst) and this PC doesn't have the 'zstd' "
                    "tool. Install it (sudo apt install zstd) or extract the image first."
                )
            # zstd reads the file through an fd it shares with us, so its read
            # position (= our progress) is visible via lseek on our side.
            self._raw = open(self.path, "rb")
            self._proc = subprocess.Popen(
                ["zstd", "-dcq"], stdin=self._raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            self.stream = self._proc.stdout
        return self

    def read(self, n):
        try:
            return self.stream.read(n)
        except (EOFError, OSError, zipfile.BadZipFile, zlib.error, _LZMA_ERROR) as exc:
            raise InjectError(
                "The image file is damaged or incomplete (%s: %s). It was probably not fully "
                "downloaded - download it again and retry.\n\nThe drive now holds a partial "
                "image and won't boot until it's written again." % (type(exc).__name__, exc)
            )

    def check_complete(self):
        """Call after the stream hits EOF. The Python decompressors raise on a
        truncated file by themselves, but an external zstd process just stops
        early - so check how it exited."""
        if self._proc is not None:
            rc = self._proc.wait(timeout=60)
            if rc != 0:
                raise InjectError(
                    "The image file is damaged or incomplete (zstd exit code %d). Download it "
                    "again and retry.\n\nThe drive now holds a partial image and won't boot "
                    "until it's written again." % rc
                )

    def fraction_consumed(self):
        """How far through the compressed file we are (for formats whose
        uncompressed size is unknown up front)."""
        if self._raw is None or self.file_size <= 0:
            return None
        try:
            pos = os.lseek(self._raw.fileno(), 0, os.SEEK_CUR)
        except OSError:
            return None
        return min(1.0, pos / float(self.file_size))

    def close(self):
        for thing in (self.stream, self._zip, self._raw):
            try:
                if thing is not None:
                    thing.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            if self._proc.returncode not in (0, None, -9, -13):  # -9/-13: we stopped it early
                log.warning("zstd exited with %s", self._proc.returncode)


def peek_image(path):
    """Decompress just the first sector to (a) prove the file can actually
    be read in its format and (b) check it looks like a bootable disk image
    (MBR/GPT boot signature 0x55AA at byte 510). Returns
    (looks_like_disk_image, uncompressed_size_or_None, description)."""
    src = ImageSource(path)
    try:
        src.open()
        first = src.read(512)
        size = src.size
        kind = src.kind
        inner = src.inner_name
    finally:
        src.close()
    if not first:
        raise InjectError("The image file is empty.")
    looks_ok = len(first) == 512 and first[510:512] == b"\x55\xaa"
    what = {
        "raw": "uncompressed image",
        "xz": "xz-compressed image",
        "gz": "gzip-compressed image",
        "bz2": "bzip2-compressed image",
        "zip": "zip archive (%s)" % inner,
        "zst": "zstd-compressed image",
    }.get(kind, kind)
    return looks_ok, size, what


# --------------------------------------------------------------------------- #
# Getting the RIGHT image: official Raspberry Pi OS Lite download, and
# spotting the classic mistake (a PC/Mac ISO)
# --------------------------------------------------------------------------- #
# raspberrypi.com's "latest" links always redirect to the newest release;
# the .sha256 file next to it names that release and its checksum.
PI_OS_LITE_32_URL = "https://downloads.raspberrypi.com/raspios_lite_armhf_latest"
PI_OS_LITE_32_SHA_URL = PI_OS_LITE_32_URL + ".sha256"
PI_OS_PAGE_URL = "https://www.raspberrypi.com/software/operating-systems/"


def describe_image_contents(path):
    """'iso' for a CD/DVD-style PC or Mac installer image (ISO 9660 - e.g.
    'Raspberry Pi Desktop for PC and Mac', which does NOT run on a Pi),
    'pi' for the usual Raspberry Pi OS layout (FAT boot partition + Linux
    partition), otherwise 'other'. Never raises: this is only for warnings."""
    try:
        src = ImageSource(path)
        try:
            src.open()
            head = b""
            while len(head) < 0x8806:
                chunk = src.read(0x8806 - len(head))
                if not chunk:
                    break
                head += chunk
        finally:
            src.close()
    except Exception:
        return "other"
    if head[0x8001:0x8006] == b"CD001":
        return "iso"
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        types = [head[446 + 16 * i + 4] for i in range(4)]
        if types[0] in (0x0B, 0x0C, 0x0E) and 0x83 in types[1:]:
            return "pi"
    return "other"


DOWNLOAD_CHECKPOINT = 10 * 1024 * 1024  # bytes safely on disk between checkpoints
DOWNLOAD_BACKOFF = (5, 10, 20, 30, 45, 60, 60, 60)  # waits after consecutive failures without progress


class DownloadPaused(InjectError):
    """Download stopped (too many failures in a row, or cancelled) - the
    progress so far is kept, and the next attempt carries on from it."""


def resumable_download(url, dest, say, progress, cancel_event, expected_sha256=None, expected_name=None,
                       label=None, checkpoint=DOWNLOAD_CHECKPOINT, backoff=DOWNLOAD_BACKOFF):
    """Download url to dest, surviving dropouts.

    Every `checkpoint` bytes the data is flushed to disk and the position is
    saved in dest + '.part.json'. After a crash, power cut or kill, the next
    start cuts the part file back to that last checkpoint (anything after it
    may be half-written). When the connection merely drops, everything
    received so far is complete, so it's forced to disk as a new checkpoint
    and the download resumes from exactly there with an HTTP Range request,
    waiting a little longer after each failure in a row (the count resets
    whenever progress is made). A cancelled or given-up download keeps its checkpoint,
    so the next call continues where it stopped - even after a restart.
    If the file on the server changed meanwhile (new ETag / Last-Modified,
    or the server ignores Range), it starts again from zero rather than
    stitching two different files together.

    expected_name: the redirected URL's file name must match this (catches a
    new release being published mid-download). expected_sha256: verified
    over the whole finished file. Returns dest."""
    import http.client
    import socket
    import urllib.error
    import urllib.request

    label = label or os.path.basename(dest)
    part, state_path = dest + ".part", dest + ".part.json"
    state = {}
    try:
        with open(state_path) as f:
            state = json.load(f)
        if state.get("url") != url:
            state = {}
    except (OSError, ValueError):
        state = {}
    good = int(state.get("checkpoint", 0)) if os.path.isfile(part) else 0
    good = min(good, os.path.getsize(part)) if os.path.isfile(part) else 0

    def save_state():
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_path)

    def rollback(to):
        """Cut the part file back to the last checkpoint."""
        with open(part, "ab") as f:
            f.truncate(to)
            f.flush()
            os.fsync(f.fileno())

    def restart_from_zero(why):
        log.info("download %s: starting again from zero (%s)", label, why)
        state.update({"checkpoint": 0, "validator": None, "total": None})
        rollback(0)
        return 0

    if not os.path.isfile(part):
        open(part, "wb").close()
        state = {"url": url, "checkpoint": 0}
    rollback(good)
    state.update({"url": url, "checkpoint": good})
    if good:
        say("Resuming the download of %s from %s..." % (label, human_size(good)))
        log.info("download %s: resuming from checkpoint %d", label, good)

    failures, last_error, start_time, start_bytes = 0, "", time.time(), good
    while True:
        if cancel_event.is_set():
            raise DownloadPaused("Download paused at %s - press Download again to carry on from there."
                                 % human_size(good))
        headers = {"User-Agent": "PiHotspotInjector/%s" % APP_VERSION}
        if good:
            headers["Range"] = "bytes=%d-" % good
            if state.get("validator"):
                headers["If-Range"] = state["validator"]
        pos = good
        try:
            req = urllib.request.Request(state.get("resolved") or url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_name = os.path.basename(resp.geturl().split("?", 1)[0])
                if expected_name and final_name != expected_name:
                    raise InjectError(
                        "raspberrypi.com seems to have just published a new version (%s) - please press "
                        "Download again." % final_name
                    )
                status = resp.getcode()
                validator = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
                if good and status == 206:
                    m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", resp.headers.get("Content-Range", ""))
                    if not m or int(m.group(1)) != good:
                        raise InjectError("The server resumed from the wrong place - please try again.")
                    total = int(m.group(3)) if m.group(3) != "*" else None
                else:
                    if good:  # server ignored Range, or the file changed (If-Range mismatch)
                        good = pos = restart_from_zero("server sent the whole file (HTTP %s)" % status)
                    length = resp.headers.get("Content-Length")
                    total = int(length) if length and length.isdigit() else None
                if state.get("total") and total and state["total"] != total:
                    good = pos = restart_from_zero("file size on the server changed")
                    continue
                state.update({"resolved": resp.geturl(), "validator": validator, "total": total})
                save_state()
                if total and good == 0 and shutil.disk_usage(os.path.dirname(dest) or ".").free < total + 200 * 1024 * 1024:
                    raise InjectError("Not enough free space in %s for %s (%s needed)."
                                      % (os.path.dirname(dest), label, human_size(total)))

                last_report = 0.0
                with open(part, "r+b") as out:
                    out.seek(pos)
                    while True:
                        if cancel_event.is_set():  # pause: keep everything received so far
                            out.flush()
                            os.fsync(out.fileno())
                            out.truncate(pos)
                            good = pos
                            state["checkpoint"] = good
                            save_state()
                            raise DownloadPaused("Download paused at %s - press Download again to carry on "
                                                 "from there." % human_size(good))
                        block = resp.read(256 * 1024)
                        if not block:
                            break
                        out.write(block)
                        pos += len(block)
                        if pos - good >= checkpoint:  # checkpoint: make it durable, remember it
                            out.flush()
                            os.fsync(out.fileno())
                            good = pos
                            state["checkpoint"] = good
                            save_state()
                            failures = 0
                        now = time.time()
                        if now - last_report >= 0.4:
                            last_report = now
                            elapsed = max(now - start_time, 0.001)
                            speed = (pos - start_bytes) / elapsed
                            frac = pos / float(total) if total else None
                            eta = _fmt_eta((total - pos) / speed) if total and speed > 0 and pos > start_bytes else ""
                            progress(frac, ("Downloading %s: %s%s - %s/s %s" % (
                                label, human_size(pos), (" of %s" % human_size(total)) if total else "",
                                human_size(speed), ("- " + eta) if eta else "")).strip())
                    out.flush()
                    os.fsync(out.fileno())
                if total and pos != total:
                    raise http.client.IncompleteRead(b"", total - pos)
                good = pos
                state["checkpoint"] = good
                save_state()
                break  # finished
        except (DownloadPaused, CancelledError):
            raise
        except InjectError:
            raise
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException, ConnectionError, OSError) as exc:
            last_error = "%s: %s" % (type(exc).__name__, getattr(exc, "reason", exc))
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 416 and good:
                good = restart_from_zero("server refused the resume point")
                continue
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 404, 410):
                raise InjectError("The download address doesn't work any more (HTTP %d): %s"
                                  % (exc.code, state.get("resolved") or url))
            if pos > good:
                # Everything received before the drop was fully written (the
                # network layer only hands over complete pieces), so force it
                # to disk and make it the new checkpoint - otherwise a
                # connection that drops more often than every 10 MB would
                # never get anywhere. (After a crash or power cut, the start
                # of this function instead rolls back to the last checkpoint
                # saved every 10 MB, as data after it may be half-written.)
                with open(part, "r+b") as f:
                    f.truncate(pos)
                    f.flush()
                    os.fsync(f.fileno())
                good = pos
                state["checkpoint"] = good
                save_state()
                failures = 0
            else:
                rollback(good)
            failures += 1  # consecutive failures without any progress
            if failures > len(backoff):
                raise DownloadPaused(
                    "The internet connection keeps dropping. The download is paused at %s (safely saved) - "
                    "check the Wi-Fi or cable and press Download again to carry on from there.\n\nLast "
                    "error: %s" % (human_size(good), last_error)
                )
            wait = backoff[failures - 1]
            log.warning("download %s: %s at %d bytes - checkpoint %d, retrying in %ds",
                        label, last_error, pos, good, wait)
            for left in range(wait, 0, -1):
                if cancel_event.is_set():
                    break
                progress(None, "Connection dropped - saved at %s. Resuming in %d s..." % (human_size(good), left))
                time.sleep(1)

    if expected_sha256:
        say("Checking the download...")
        progress(None, "Checking %s against its published checksum..." % label)
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(block)
        if h.hexdigest() != expected_sha256.lower():
            for p in (part, state_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            raise InjectError("The downloaded file is damaged (its checksum doesn't match) - press Download "
                              "again to fetch a fresh copy.")
    os.replace(part, dest)
    try:
        os.remove(state_path)
    except OSError:
        pass
    if os.geteuid() == 0 and REAL_UID != 0:
        try:
            os.chown(dest, REAL_UID, REAL_GID)
        except OSError:
            pass
    log.info("downloaded %s (%d bytes)", dest, os.path.getsize(dest))
    return dest


def download_pi_os_lite(say, progress, cancel_event, url=None, sha_url=None, dest_dir=None):
    """Download the latest official Raspberry Pi OS Lite (32-bit) image into
    the user's Downloads folder and verify it against raspberrypi.com's
    published SHA-256. Returns the file's path. Re-uses an existing copy if
    its checksum matches."""
    import urllib.error
    import urllib.request

    url = url or PI_OS_LITE_32_URL
    sha_url = sha_url or PI_OS_LITE_32_SHA_URL
    dest_dir = dest_dir or os.path.join(REAL_HOME, "Downloads")
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        if os.geteuid() == 0 and REAL_UID != 0:
            os.chown(dest_dir, REAL_UID, REAL_GID)

    say("Asking raspberrypi.com for the latest Raspberry Pi OS Lite (32-bit)...")
    sha_text, exc = None, None
    for pause in (0, 5, 15, 30):
        for _ in range(pause):
            if cancel_event.is_set():
                raise CancelledError("Download cancelled.")
            time.sleep(1)
        try:
            with urllib.request.urlopen(sha_url, timeout=60) as resp:
                sha_text = resp.read(4096).decode("utf-8", "replace").strip()
            break
        except (urllib.error.URLError, OSError) as e:
            exc = e
    if sha_text is None:
        raise InjectError(
            "Couldn't reach raspberrypi.com (%s).\n\nCheck this PC's internet connection - or download "
            "'Raspberry Pi OS Lite' (32-bit) yourself from:\n%s" % (getattr(exc, "reason", exc), PI_OS_PAGE_URL)
        )
    fields = sha_text.split()
    if len(fields) < 2 or not re.match(r"^[0-9a-fA-F]{64}$", fields[0]):
        raise InjectError("raspberrypi.com sent an unexpected checksum file - please try again later.")
    expected = fields[0].lower()
    name = os.path.basename(fields[1].lstrip("*"))
    if not re.match(r"^[\w.+-]+\.img\.xz$", name):
        raise InjectError("raspberrypi.com sent an unexpected file name (%r) - please try again later." % name)
    dest = os.path.join(dest_dir, name)

    def sha256_of(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
                if cancel_event.is_set():
                    raise CancelledError("Download cancelled.")
                h.update(block)
        return h.hexdigest()

    if os.path.isfile(dest):
        say("Checking the copy already in your Downloads folder...")
        progress(None, "Checking the copy already in your Downloads folder...")
        if sha256_of(dest) == expected:
            log.info("Pi OS Lite already downloaded and verified: %s", dest)
            return dest

    return resumable_download(url, dest, say, progress, cancel_event, expected_sha256=expected,
                              expected_name=name, label=name)


def explain_missing_cards():
    """Why find_cards() found nothing, when we can tell: a removable drive
    holding a PC/Mac ISO, or one with no Linux system on it at all."""
    try:
        devs = lsblk_devices(quiet=True)
    except Exception:
        return None
    for dev in devs:
        if dev.get("type") != "disk" or not is_external_disk(dev):
            continue
        fstypes = [str(n.get("fstype") or "") for n in walk(dev)]
        desc = "%s, %s (%s)" % (device_model(dev), human_size(dev.get("size")), dev.get("name"))
        if "iso9660" in fstypes:
            return (
                "%s has a PC/Mac installer (an ISO disc image) on it - not Raspberry Pi OS. That kind of "
                "image (e.g. 'Raspberry Pi Desktop for PC and Mac') can't run on a Pi.\n\nIn tab 1, press "
                "'Download Raspberry Pi OS Lite (32-bit)', then write that to the card." % desc
            )
        if "vfat" in fstypes and "ext4" not in fstypes and device_size_bytes(dev) > 0:
            return (
                "%s only has a normal memory-card (FAT) partition - no Raspberry Pi OS on it yet.\n\nIn tab 1, "
                "press 'Download Raspberry Pi OS Lite (32-bit)', then write that to the card." % desc
            )
    return None


def disks_holding_path(path):
    """Whole-disk device names that the filesystem holding `path` lives on
    (following LVM/LUKS/partitions back to the physical disk)."""
    res = run(["findmnt", "-n", "-o", "SOURCE", "--target", path], check=False)
    source = re.sub(r"\[.*\]$", "", res.stdout.strip())
    if not source.startswith("/dev/"):
        return set()
    res = run(["lsblk", "-s", "-n", "-p", "-r", "-o", "NAME,TYPE", source], check=False)
    disks = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            disks.add(parts[0])
    return disks


def _fmt_eta(seconds):
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return ""
    if seconds < 60:
        return "under a minute left"
    if seconds < 3600:
        return "about %d min left" % round(seconds / 60.0)
    return "about %dh %02dm left" % (seconds // 3600, (seconds % 3600) // 60)


def write_image(image_path, target, verify, say, progress, cancel_event):
    """Stream image_path (decompressing on the fly) onto the whole-disk
    device target['disk'], optionally reading it back to verify.

    progress(fraction_or_None, text) is called from this worker thread;
    the GUI marshals it to the main thread. Raises CancelledError if
    cancel_event gets set."""
    disk = target["disk"]

    # Re-check the drive right before touching it: between choosing it and
    # confirming twice, someone may have unplugged it and plugged in
    # something else that got the same /dev name.
    say("Re-checking the selected drive...")
    current = {t["disk"]: t for t in classify_write_targets()[0]}.get(disk)
    if (current is None or current["size"] != target["size"] or current["serial"] != target["serial"]
            or current["contents"] != target["contents"]):
        raise InjectError(
            "The selected drive (%s) has changed or disappeared since you chose it - was it "
            "unplugged or swapped? For safety NOTHING was written.\n\nPress Refresh and "
            "select the drive again." % target["desc"]
        )
    if disk in disks_holding_path(image_path):
        raise InjectError(
            "The image file is stored ON the drive you're about to overwrite (%s), so it "
            "would destroy itself halfway through. NOTHING was written.\n\nCopy the image "
            "file to this computer (e.g. your Downloads folder) first." % target["desc"]
        )

    src = ImageSource(image_path)
    fd = None
    try:
        src.open()
        if src.size is not None and src.size > target["size"]:
            raise InjectError(
                "This image needs %s but %s only holds %s. NOTHING was written - use a "
                "bigger card." % (human_size(src.size), target["desc"], human_size(target["size"]))
            )

        say("Unmounting the drive's partitions...")
        for part in current["partitions"]:
            unmount_existing(part)
        run(["sync"], check=False)

        try:
            # O_EXCL on a block device = the kernel refuses if anything still
            # has it (or any of its partitions) mounted or exclusively open,
            # and nothing can mount it while we hold it. The flock tells udev
            # to leave it alone too. We keep this ONE handle open through
            # writing AND verifying: closing it in between would let the
            # desktop auto-mount the new partitions, which changes a few bytes
            # on the card and would make verification fail for no real reason.
            fd = os.open(disk, os.O_RDWR | os.O_EXCL | getattr(os, "O_CLOEXEC", 0))
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, OSError):
                pass  # best effort; O_EXCL is the real guard
        except OSError as exc:
            raise InjectError(
                "Couldn't open %s for writing: %s.\n\nSomething is still using it - close "
                "any file manager windows or programs showing its files, then try again. "
                "NOTHING was written." % (target["desc"], exc.strerror or exc)
            )

        hasher = hashlib.sha256()
        written = 0
        last_sync = 0
        last_report = 0.0
        start = time.time()
        total_text = human_size(src.size) if src.size else None
        progress(0.0, "Writing...")

        while True:
            if cancel_event.is_set():
                raise CancelledError(
                    "Writing was cancelled. The drive now holds a PARTIAL image and won't "
                    "boot - write it again before using it."
                )
            chunk = src.read(WRITE_CHUNK)
            if not chunk:
                break
            if written + len(chunk) > target["size"]:
                raise InjectError(
                    "The image turned out to be bigger than %s (%s). The drive now holds an "
                    "incomplete image - use a bigger card." % (target["desc"], human_size(target["size"]))
                )
            view = memoryview(chunk)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            hasher.update(chunk)
            written += len(chunk)

            # Flush regularly so progress reflects what's really on the
            # card, not just what's queued in RAM (otherwise the bar sits at
            # 100% for minutes during the final sync).
            if written - last_sync >= 64 * 1024 * 1024:
                os.fsync(fd)
                last_sync = written

            now = time.time()
            if now - last_report >= 0.4:
                last_report = now
                elapsed = max(now - start, 0.001)
                speed = written / elapsed
                frac = (written / float(src.size)) if src.size else src.fraction_consumed()
                eta = _fmt_eta(elapsed / frac - elapsed) if frac and frac > 0.02 else ""
                text = "Writing: %s%s - %s/s %s" % (
                    human_size(written),
                    (" of %s" % total_text) if total_text else "",
                    human_size(speed),
                    ("- " + eta) if eta else "",
                )
                progress(frac, text.strip())

        if written == 0:
            raise InjectError("The image file contained no data. NOTHING useful was written.")
        src.check_complete()
        if src.size is not None and written != src.size and src.kind in ("raw", "zip"):
            raise InjectError(
                "Only %s of the image's %s could be read - the file changed or its drive "
                "had a problem. Write it again." % (human_size(written), human_size(src.size))
            )

        progress(None, "Finishing - flushing the last data to the drive (can take a minute)...")
        os.fsync(fd)

        # Clear the last 1 MiB: a drive previously formatted with GPT keeps a
        # backup partition table at the very end, which the Pi's image
        # doesn't overwrite and some tools then get confused by.
        tail = 1024 * 1024
        if target["size"] - written >= 2 * tail:
            os.lseek(fd, target["size"] - tail, os.SEEK_SET)
            view = memoryview(b"\0" * tail)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)

        digest = hasher.hexdigest()
        verified = False
        if verify:
            verified = verify_written(fd, disk, written, digest, progress, cancel_event)
        os.close(fd)
        fd = None

        say("Asking the system to re-read the new partitions...")
        try:
            run(["blockdev", "--rereadpt", disk], check=False, timeout=30)
            if shutil.which("udevadm"):
                run(["udevadm", "settle", "--timeout=15"], check=False, timeout=30)
        except RuntimeError as exc:
            log.warning("post-write partition re-read: %s", exc)

        elapsed = time.time() - start
        log.info("wrote %d bytes from %s to %s in %.0fs (verified=%s)", written, image_path, disk, elapsed, verified)
        return {"written": written, "seconds": elapsed, "verified": verified}
    finally:
        if fd is not None:
            try:
                os.fsync(fd)
            except OSError:
                pass
            os.close(fd)
        src.close()


def verify_written(fd, disk, length, expected_digest, progress, cancel_event):
    """Read back what was written (through the same exclusive handle) and
    compare checksums. Catches bad cards, flaky readers, and the classic
    fake-capacity card that silently throws away data past its real size."""
    os.fsync(fd)
    # Drop the cached copies so we read what's really on the card.
    try:
        run(["blockdev", "--flushbufs", disk], check=False, timeout=60)
    except RuntimeError as exc:
        log.warning("blockdev --flushbufs: %s", exc)
    if hasattr(os, "posix_fadvise"):
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.lseek(fd, 0, os.SEEK_SET)
    hasher = hashlib.sha256()
    done = 0
    start = time.time()
    last_report = 0.0
    while done < length:
        if cancel_event.is_set():
            raise CancelledError(
                "Verification was cancelled. The image WAS fully written, but hasn't "
                "been checked - it will probably work fine."
            )
        data = os.read(fd, min(WRITE_CHUNK, length - done))
        if not data:
            break
        hasher.update(data)
        done += len(data)
        now = time.time()
        if now - last_report >= 0.4:
            last_report = now
            elapsed = max(now - start, 0.001)
            frac = done / float(length)
            eta = _fmt_eta(elapsed / frac - elapsed) if frac > 0.02 else ""
            progress(frac, ("Verifying: %s of %s - %s/s %s" % (
                human_size(done), human_size(length), human_size(done / elapsed),
                ("- " + eta) if eta else "")).strip())
    if done != length or hasher.hexdigest() != expected_digest:
        raise InjectError(
            "VERIFICATION FAILED: what's on the drive doesn't match the image.\n\n"
            "The card/stick is probably faulty or counterfeit (fake-capacity cards are "
            "common), or the card reader/USB cable is flaky. Try again with a different "
            "card, and ideally a different USB port or reader. Don't use this card in "
            "the Pi - it may fail randomly."
        )
    return True


# --------------------------------------------------------------------------- #
# Licence (PolyForm Noncommercial 1.0.0, verbatim) + disclaimer
# --------------------------------------------------------------------------- #
REQUIRED_NOTICE = "Required Notice: Copyright %s" % COPYRIGHT_HOLDER

LICENSE_TEXT = r"""# PolyForm Noncommercial License 1.0.0

<https://polyformproject.org/licenses/noncommercial/1.0.0>

## Acceptance

In order to get any license under these terms, you must agree to them as both strict obligations and conditions to all your licenses.

## Copyright License

The licensor grants you a copyright license for the software to do everything you might do with the software that would otherwise infringe the licensor's copyright in it for any permitted purpose.  However, you may only distribute the software according to [Distribution License](#distribution-license) and make changes or new works based on the software according to [Changes and New Works License](#changes-and-new-works-license).

## Distribution License

The licensor grants you an additional copyright license to distribute copies of the software.  Your license to distribute covers distributing the software with changes and new works permitted by [Changes and New Works License](#changes-and-new-works-license).

## Notices

You must ensure that anyone who gets a copy of any part of the software from you also gets a copy of these terms or the URL for them above, as well as copies of any plain-text lines beginning with `Required Notice:` that the licensor provided with the software.  For example:

> Required Notice: Copyright Yoyodyne, Inc. (http://example.com)

## Changes and New Works License

The licensor grants you an additional copyright license to make changes and new works based on the software for any permitted purpose.

## Patent License

The licensor grants you a patent license for the software that covers patent claims the licensor can license, or becomes able to license, that you would infringe by using the software.

## Noncommercial Purposes

Any noncommercial purpose is a permitted purpose.

## Personal Uses

Personal use for research, experiment, and testing for the benefit of public knowledge, personal study, private entertainment, hobby projects, amateur pursuits, or religious observance, without any anticipated commercial application, is use for a permitted purpose.

## Noncommercial Organizations

Use by any charitable organization, educational institution, public research organization, public safety or health organization, environmental protection organization, or government institution is use for a permitted purpose regardless of the source of funding or obligations resulting from the funding.

## Fair Use

You may have "fair use" rights for the software under the law. These terms do not limit them.

## No Other Rights

These terms do not allow you to sublicense or transfer any of your licenses to anyone else, or prevent the licensor from granting licenses to anyone else.  These terms do not imply any other licenses.

## Patent Defense

If you make any written claim that the software infringes or contributes to infringement of any patent, your patent license for the software granted under these terms ends immediately. If your company makes such a claim, your patent license ends immediately for work on behalf of your company.

## Violations

The first time you are notified in writing that you have violated any of these terms, or done anything with the software not covered by your licenses, your licenses can nonetheless continue if you come into full compliance with these terms, and take practical steps to correct past violations, within 32 days of receiving notice.  Otherwise, all your licenses end immediately.

## No Liability

***As far as the law allows, the software comes as is, without any warranty or condition, and the licensor will not be liable to you for any damages arising out of these terms or the use or nature of the software, under any kind of legal claim.***

## Definitions

The **licensor** is the individual or entity offering these terms, and the **software** is the software the licensor makes available under these terms.

**You** refers to the individual or entity agreeing to these terms.

**Your company** is any legal entity, sole proprietorship, or other kind of organization that you work for, plus all organizations that have control over, are under the control of, or are under common control with that organization.  **Control** means ownership of substantially all the assets of an entity, or the power to direct its management and policies by vote, contract, or otherwise.  Control can be direct or indirect.

**Your licenses** are all the licenses granted to you for the software under these terms.

**Use** means anything you do with the software requiring one of your licenses.
"""


DISCLAIMER_TEXT = (
    "This tool writes directly to disks. Pointed at the wrong drive, it can "
    "permanently erase that drive's data.\n\n"
    "•  It's free for NON-COMMERCIAL use only: personal, hobby, educational "
    "and charity use. Commercial use needs the author's permission.\n\n"
    "•  It comes AS IS, with NO WARRANTY of any kind. You use it entirely at "
    "your own risk.\n\n"
    "•  The author(s) accept NO LIABILITY for any loss or damage, including "
    "lost data or damaged SD cards, USB drives, computers, Raspberry Pis or "
    "printers.\n\n"
    "Full terms: PolyForm Noncommercial License 1.0.0. Reading them is optional."
)


# --------------------------------------------------------------------------- #
# Clone & backup: card -> image file (optionally shrunk / compressed), and
# image -> card with the system partition grown back and a fresh identity.
# --------------------------------------------------------------------------- #
CLONE_SLACK = 512 * 1024 * 1024  # free room left in a shrunk image's system partition
SSH_KEYS_UNIT = "pi-injector-ssh-keys.service"
SSH_KEYS_UNIT_TEXT = """[Unit]
Description=Create this Pi's own SSH keys (card was cloned by Pi Hotspot Injector)
Before=ssh.service ssh.socket
ConditionPathExistsGlob=!/etc/ssh/ssh_host_*_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
"""


def _chown_to_real_user(path):
    if os.geteuid() == 0 and REAL_UID != 0:
        try:
            os.chown(path, REAL_UID, REAL_GID)
        except OSError:
            pass


def _recheck_target(target, what="card"):
    current = {t["disk"]: t for t in classify_write_targets()[0]}.get(target["disk"])
    if current is None or current["size"] != target["size"] or current["serial"] != target["serial"]:
        raise InjectError(
            "The selected %s (%s) has changed or disappeared since you chose it - was it unplugged or "
            "swapped? Nothing was done.\n\nPress Refresh and choose it again." % (what, target["desc"])
        )
    return current


def _open_exclusive(disk, flags, desc):
    try:
        fd = os.open(disk, flags | os.O_EXCL | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise InjectError(
            "Couldn't open %s: %s.\n\nSomething is still using it - close any file manager windows or "
            "programs showing its files, then try again." % (desc, exc.strerror or exc)
        )
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        pass
    return fd


def shrink_image_file(path, say, slack=CLONE_SLACK):
    """Shrink the last (ext4) partition of a raw disk image to its contents
    plus `slack`, then cut the file off after it. Returns the new file size,
    or None if the image can't be shrunk (not a Pi-style layout)."""
    for tool in ("losetup", "sfdisk", "e2fsck", "resize2fs", "dumpe2fs", "blkid"):
        if shutil.which(tool) is None:
            say("Can't shrink: this PC is missing the '%s' tool - keeping the full-size image." % tool)
            return None
    table = json.loads(run(["sfdisk", "-J", path]).stdout)["partitiontable"]
    if table.get("label") != "dos":
        log.info("shrink: %s has a %s partition table - not shrinking", path, table.get("label"))
        return None
    sector = int(table.get("sectorsize", 512))
    parts = table.get("partitions", [])
    if not parts:
        return None
    last = max(parts, key=lambda p: int(p["start"]))
    m = re.search(r"(\d+)$", last["node"])
    partno, start, old_sectors = int(m.group(1)), int(last["start"]), int(last["size"])

    # Attach just that partition (offset + size): no dependence on the
    # kernel creating partition device nodes for loop devices.
    loop = run(["losetup", "--find", "--show", "--offset", str(start * sector), "--sizelimit",
                str(old_sectors * sector), path], timeout=60).stdout.strip()
    try:
        pdev = loop
        fstype = run(["blkid", "-o", "value", "-s", "TYPE", pdev], check=False).stdout.strip()
        if fstype != "ext4":
            log.info("shrink: last partition is %r, not ext4 - not shrinking", fstype)
            return None
        say("Checking the copied system partition...")
        res = run(["e2fsck", "-f", "-y", pdev], check=False, timeout=3600)
        if res.returncode >= 4:
            raise InjectError(
                "The copied card's system partition has errors that couldn't be repaired (e2fsck exit %d), so "
                "the image wasn't shrunk. Is the source card healthy? It boots fine in its Pi?\n\n%s"
                % (res.returncode, (res.stdout or res.stderr)[-600:])
            )
        say("Shrinking the image to what's actually used (a few minutes)...")
        run(["resize2fs", "-M", pdev], timeout=7200)
        target_kib = (_ext_fs_bytes(pdev) + slack) // 1024
        if target_kib * 1024 < old_sectors * sector:
            run(["resize2fs", pdev, "%dK" % target_kib], timeout=3600)
        fs_bytes = _ext_fs_bytes(pdev)
    finally:
        run(["losetup", "-d", loop], check=False, timeout=60)

    align = (1024 * 1024) // sector
    new_sectors = -(-fs_bytes // sector)
    new_sectors = -(-new_sectors // align) * align
    if new_sectors >= old_sectors:
        return os.path.getsize(path)
    run(["sfdisk", "--no-reread", "--no-tell-kernel", "-N", str(partno), path],
        input_text=",%d\n" % new_sectors, timeout=120)
    new_end = (start + new_sectors) * sector
    with open(path, "r+b") as f:
        f.truncate(new_end)
        f.flush()
        os.fsync(f.fileno())
    log.info("shrink: %s partition %d %d -> %d sectors, file now %d bytes", path, partno, old_sectors,
             new_sectors, new_end)
    return new_end


def _gzip_file(src, dst, progress, cancel_event):
    total = os.path.getsize(src)
    done, last = 0, 0.0
    work = dst + ".partial"
    try:
        with open(src, "rb") as fin, gzip.open(work, "wb", compresslevel=1) as fout:
            while True:
                if cancel_event.is_set():
                    raise CancelledError("Stopped while compressing. The card wasn't changed; the unfinished "
                                         "file was deleted.")
                block = fin.read(WRITE_CHUNK)
                if not block:
                    break
                fout.write(block)
                done += len(block)
                if time.time() - last >= 0.4:
                    last = time.time()
                    progress(done / float(total or 1), "Compressing: %s of %s" % (human_size(done), human_size(total)))
        os.replace(work, dst)
    except BaseException:
        try:
            os.remove(work)
        except OSError:
            pass
        raise


def read_card_to_image(target, dest, shrink, compress, say, progress, cancel_event):
    """Copy the whole card (dd-style, read-only) into the image file dest,
    then optionally shrink and gzip it. Returns info about the result."""
    disk = target["disk"]
    say("Re-checking the selected card...")
    current = _recheck_target(target)
    dest = os.path.abspath(dest)
    final = dest + ".gz" if compress else dest
    out_dir = os.path.dirname(dest)
    if disk in disks_holding_path(out_dir if os.path.isdir(out_dir) else os.path.dirname(out_dir)):
        raise InjectError("The image can't be saved ON the card being copied - choose a folder on this computer.")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
        _chown_to_real_user(out_dir)
    need = target["size"] + 300 * 1024 * 1024
    free = shutil.disk_usage(out_dir).free
    if free < need:
        raise InjectError(
            "Not enough free space in %s: copying this card needs %s free while it works (you have %s). "
            "The finished file is much smaller when shrunk, but the whole card is read first.\n\nFree some "
            "space or choose another folder." % (out_dir, human_size(need), human_size(free))
        )

    say("Unmounting the card's partitions...")
    for part in current["partitions"]:
        unmount_existing(part)
    run(["sync"], check=False)
    fd = _open_exclusive(disk, os.O_RDONLY, target["desc"])
    work = dest + ".partial"
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, 0, os.SEEK_SET)
        zero = bytes(WRITE_CHUNK)
        pos, last, start = 0, 0.0, time.time()
        with open(work, "wb") as out:
            while pos < size:
                if cancel_event.is_set():
                    raise CancelledError("Copying was stopped. The card wasn't changed; the unfinished image "
                                         "file was deleted.")
                chunk = os.read(fd, min(WRITE_CHUNK, size - pos))
                if not chunk:
                    raise InjectError("The card stopped giving data at %s of %s - is it failing, or was it "
                                      "unplugged?" % (human_size(pos), human_size(size)))
                if chunk == (zero if len(chunk) == WRITE_CHUNK else bytes(len(chunk))):
                    out.seek(len(chunk), os.SEEK_CUR)  # leave a hole: empty areas take no disk space
                else:
                    out.write(chunk)
                pos += len(chunk)
                now = time.time()
                if now - last >= 0.4:
                    last = now
                    speed = pos / max(now - start, 0.001)
                    frac = pos / float(size)
                    eta = _fmt_eta((size - pos) / speed) if speed > 0 and frac > 0.02 else ""
                    progress(frac, ("Copying the card: %s of %s - %s/s %s" % (
                        human_size(pos), human_size(size), human_size(speed), ("- " + eta) if eta else "")).strip())
            out.truncate(pos)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        os.close(fd)
        fd = None
        try:
            os.remove(work)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
    log.info("read %d bytes from %s into %s", size, disk, work)

    shrunk = None
    try:
        if shrink:
            progress(None, "Shrinking the image...")
            shrunk = shrink_image_file(work, say)
        os.replace(work, dest)
        work = None
        if compress:
            _gzip_file(dest, final, progress, cancel_event)
            os.remove(dest)
    except BaseException:
        for p in (work, final if compress else None):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
        raise
    _chown_to_real_user(final)
    info = {"path": final, "card_size": size, "file_size": os.path.getsize(final), "shrunk": shrunk is not None,
            "image_size": shrunk or size}
    log.info("card image saved: %s", info)
    return info


def make_card_unique(card, hostname, say):
    """Give a cloned card its own identity: new hostname, a fresh machine ID
    and fresh SSH host keys (both created on its next start), so two Pis
    made from the same master never get mixed up on a network. Returns the
    old hostname."""
    session = MountSession()
    error, old = None, None
    try:
        ensure_writable_card(card)
        say("Giving the card its own identity (%s)..." % hostname)
        unmount_existing(card["root"])
        unmount_existing(card["boot"])
        root_mp = session.mount(card["root"])
        boot_mp = session.mount(card["boot"])
        verify_pi_card(card, boot_mp, root_mp)
        session.remount_rw(root_mp)
        etc = os.path.join(root_mp, "etc")
        try:
            with open(os.path.join(etc, "hostname")) as f:
                old = f.read().strip() or None
        except OSError:
            pass
        write_file(os.path.join(etc, "hostname"), hostname + "\n", 0o644)
        hosts_path = os.path.join(etc, "hosts")
        try:
            with open(hosts_path) as f:
                hosts = f.read().splitlines()
        except OSError:
            hosts = ["127.0.0.1\tlocalhost"]
        hosts = [ln for ln in hosts if not ln.startswith("127.0.1.1")] + ["127.0.1.1\t%s" % hostname]
        write_file(hosts_path, "\n".join(hosts) + "\n", 0o644)
        # Empty machine-id = systemd creates a new one on the next start.
        write_file(os.path.join(etc, "machine-id"), "", 0o444)
        dbus_id = os.path.join(root_mp, "var", "lib", "dbus", "machine-id")
        if os.path.isfile(dbus_id) and not os.path.islink(dbus_id):
            os.remove(dbus_id)
        seed = os.path.join(root_mp, "var", "lib", "systemd", "random-seed")
        if os.path.isfile(seed):
            os.remove(seed)
        removed = 0
        for key in glob.glob(os.path.join(etc, "ssh", "ssh_host_*")):
            os.remove(key)
            removed += 1
        unit_dir = os.path.join(etc, "systemd", "system")
        os.makedirs(os.path.join(unit_dir, "multi-user.target.wants"), exist_ok=True)
        write_file(os.path.join(unit_dir, SSH_KEYS_UNIT), SSH_KEYS_UNIT_TEXT, 0o644)
        link = os.path.join(unit_dir, "multi-user.target.wants", SSH_KEYS_UNIT)
        if os.path.lexists(link):
            os.remove(link)
        os.symlink("/etc/systemd/system/" + SSH_KEYS_UNIT, link)
        log.info("made card unique: hostname %s -> %s, %d ssh host key files removed", old, hostname, removed)
    except BaseException as exc:
        error = exc
    say("Flushing writes...")
    problems = session.cleanup()
    if error:
        raise error
    if problems:
        raise InjectError("Cleanup failed:\n" + "\n".join(problems))
    return old


def _find_card_on_disk(disk, wait=20):
    for _ in range(wait):
        try:
            cards = [c for c in find_cards() if c["disk"] == disk]
        except Exception:
            cards = []
        if cards:
            return cards[0]
        time.sleep(1)
    return None


def write_clone_to_card(image_path, target, verify, grow, hostname, say, progress, cancel_event):
    """Write an image, then grow the system partition to fill the card and
    (if hostname is given) give it its own identity."""
    result = write_image(image_path, target, verify, say, progress, cancel_event)
    result.update({"grew": False, "old_hostname": None, "hostname": None})
    if not grow and not hostname:
        return result
    progress(None, "Looking for the new card's partitions...")
    card = _find_card_on_disk(target["disk"])
    if card is None:
        raise InjectError(
            "The image was written fine, but this PC can't see a Raspberry Pi layout on %s afterwards, so it "
            "wasn't enlarged or given its own name.\n\nIf it IS a Pi image: unplug the card, plug it back in "
            "and write it again." % target["desc"]
        )
    if grow:
        result["grew"] = grow_root_partition(card, say)
    if hostname:
        result["old_hostname"] = make_card_unique(card, hostname, say)
        result["hostname"] = hostname
    return result


def validate_hostname(hostname):
    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", hostname or ""):
        return "Name may only use letters, digits and '-' (e.g. 'klipper2'), and can't start or end with '-'."
    return None


def licence_for_display():
    """LICENSE_TEXT is kept byte-for-byte as published; this only strips
    the Markdown punctuation so it reads cleanly in a plain text box."""
    text = LICENSE_TEXT
    text = re.sub(r"\[([^\]]+)\]\(#[^)]+\)", r"\1", text)
    text = text.replace("***", "").replace("**", "").replace("`", "")
    text = re.sub(r"^## (.+)$", lambda m: m.group(1).upper(), text, flags=re.M)
    text = re.sub(r"^# ", "", text, flags=re.M)
    text = re.sub(r"^<(.+)>$", r"\1", text, flags=re.M)
    return "%s\n\n%s" % (REQUIRED_NOTICE, text.strip())


def center_window(win, width=None, height=None):
    win.update_idletasks()
    w = width or win.winfo_reqwidth()
    h = height or win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = min(w, sw - 20), min(h, sh - 60)
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))


def make_modal(win):
    """grab_set() fails on a window that isn't mapped yet, so wait for it."""
    try:
        win.wait_visibility()
        win.grab_set()
    except tk.TclError:
        pass
    win.lift()
    win.focus_force()



def _mb(fn, title, text, **kw):
    """Tk's Linux message boxes render the main message in a narrow bold
    font; long text becomes a hard-to-read wall. Show the first paragraph
    as the bold headline and the rest in the normal-weight 'detail' area."""
    head, _sep, rest = str(text).partition("\n\n")
    if rest.strip():
        kw["detail"] = rest
    return fn(title, head, **kw)


def mb_error(title, text, **kw):
    return _mb(messagebox.showerror, title, text, **kw)


def mb_info(title, text, **kw):
    return _mb(messagebox.showinfo, title, text, **kw)


def mb_warning(title, text, **kw):
    return _mb(messagebox.showwarning, title, text, **kw)


def mb_yesno(title, text, **kw):
    return _mb(messagebox.askyesno, title, text, **kw)


def show_licence(parent):
    win = tk.Toplevel(parent)
    win.title("Licence - PolyForm Noncommercial 1.0.0")
    frame = tk.Frame(win, padx=10, pady=10)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, wrap="word", font=FONT_LABEL, width=78, height=28, padx=8, pady=8)
    sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    text.insert("1.0", licence_for_display())
    text.configure(state="disabled")
    tk.Button(win, text="Close", font=FONT_BTN, width=12, command=win.destroy).pack(pady=(0, 10))
    center_window(win, 720, 620)
    make_modal(win)
    win.wait_window()


class DisclaimerDialog(tk.Toplevel):
    """Shown every launch, before anything else. Nothing can be done until
    the box is ticked and 'I agree' pressed; closing it quits."""

    def __init__(self, master):
        super().__init__(master)
        self.accepted = False
        self.title("Before you start - %s" % APP_TITLE)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._quit)

        body = tk.Frame(self, padx=22, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Before you start", font=FONT_HEADER).pack(anchor="w")
        tk.Label(
            body, text="Please read this - it's short.", font=FONT_SMALL, fg="#4a5568"
        ).pack(anchor="w", pady=(0, 10))
        tk.Label(
            body, text=DISCLAIMER_TEXT, font=FONT_LABEL, justify="left", wraplength=540, anchor="w"
        ).pack(anchor="w")

        self.ack_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            body,
            text="I understand and accept these terms, and I've backed up anything "
            "important on the drives I'll use with this tool.",
            variable=self.ack_var,
            command=self._update,
            font=FONT_LABEL,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

        btns = tk.Frame(body)
        btns.pack(fill="x", pady=(16, 0))
        tk.Button(btns, text="View licence (optional)", font=FONT_LABEL, command=lambda: show_licence(self)).pack(
            side="left"
        )
        self.agree_btn = tk.Button(
            btns, text="I agree - continue", font=FONT_BTN, bg="#2f855a", fg="white",
            state="disabled", command=self._agree,
        )
        self.agree_btn.pack(side="right")
        tk.Button(btns, text="Quit", font=FONT_LABEL, width=8, command=self._quit).pack(side="right", padx=8)

        self.bind("<Escape>", lambda e: self._quit())
        center_window(self)
        make_modal(self)

    def _update(self):
        self.agree_btn.config(state="normal" if self.ack_var.get() else "disabled")

    def _agree(self):
        if not self.ack_var.get():
            return
        self.accepted = True
        log.info("Disclaimer accepted.")
        self.destroy()

    def _quit(self):
        self.accepted = False
        self.destroy()


class ReallySureDialog(tk.Toplevel):
    """Sanity check 2 of 2 before erasing a drive: the drive's short name
    must be typed in, so a reflexive Enter/click can't get past it."""

    def __init__(self, master, target):
        super().__init__(master)
        self.confirmed = False
        self.expected = os.path.basename(target["disk"])
        self.title("Sanity check 2 of 2")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        body = tk.Frame(self, padx=22, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="Are you reeeeley sure you want to overwrite disk\n%s ?" % target["desc"],
            font=("Sans", 14, "bold"), fg="#c53030", justify="left",
        ).pack(anchor="w")
        tk.Label(
            body,
            text="Everything on it will be gone for good. This cannot be undone.\n\n"
            "To confirm, type the drive's name below:   %s" % self.expected,
            font=FONT_LABEL, justify="left",
        ).pack(anchor="w", pady=(12, 6))

        self.entry_var = tk.StringVar()
        self.entry_var.trace_add("write", lambda *a: self._update())
        entry = tk.Entry(body, textvariable=self.entry_var, font=("Monospace", 13), width=14)
        entry.pack(anchor="w")

        btns = tk.Frame(body)
        btns.pack(fill="x", pady=(16, 0))
        self.go_btn = tk.Button(
            btns, text="Yes - overwrite it", font=FONT_BTN, bg="#c53030", fg="white",
            state="disabled", command=self._go,
        )
        self.go_btn.pack(side="right")
        tk.Button(btns, text="Cancel", font=FONT_BTN, width=10, command=self.destroy).pack(side="right", padx=8)

        self.bind("<Escape>", lambda e: self.destroy())
        entry.bind("<Return>", lambda e: self._go())
        center_window(self)
        make_modal(self)
        entry.focus_set()

    def _matches(self):
        typed = self.entry_var.get().strip()
        return typed in (self.expected, "/dev/" + self.expected)

    def _update(self):
        self.go_btn.config(state="normal" if self._matches() else "disabled")

    def _go(self):
        if self._matches():
            self.confirmed = True
            self.destroy()


class ScrollFrame(tk.Frame):
    """A frame whose contents scroll vertically when the window is shorter
    than they are - so everything is still reachable on a small laptop
    screen (1366x768 with a taskbar leaves ~700px)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, padx=14, pady=10)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _event=None):
        self.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

    def _unbind_wheel(self, _event=None):
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.unbind_all(seq)


def desktop_quote(arg):
    """Quote one argument for a .desktop Exec= line (freedesktop spec)."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"%s"' % escaped.replace("%", "%%")


def install_menu_launcher():
    """Add 'Pi Hotspot Injector' to the real user's applications menu, with
    absolute paths, so it can be started like any other app from then on."""
    if REAL_UID == 0:
        raise InjectError("Couldn't work out which user account to add the menu entry for.")
    script = os.path.abspath(__file__)
    apps_dir = os.path.join(REAL_HOME, ".local", "share", "applications")
    created = []
    path = apps_dir
    while not os.path.isdir(path):
        created.append(path)
        path = os.path.dirname(path)
    os.makedirs(apps_dir, exist_ok=True)
    for d in created:
        os.chown(d, REAL_UID, REAL_GID)
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Pi Hotspot Injector\n"
        "Comment=Write Pi OS images and set up a Wi-Fi hotspot on Raspberry Pi SD cards\n"
        "Exec=%s %s\n"
        "Path=%s\n"
        "Icon=network-wireless\n"
        "Terminal=false\n"
        "Categories=Utility;System;\n"
        % (desktop_quote(sys.executable or "python3"), desktop_quote(script), os.path.dirname(script))
    )
    dest = os.path.join(apps_dir, "pi-hotspot-injector.desktop")
    with open(dest, "w") as f:
        f.write(entry)
    os.chown(dest, REAL_UID, REAL_GID)
    os.chmod(dest, 0o755)
    return dest


# --------------------------------------------------------------------------- #
# GUI Application
# --------------------------------------------------------------------------- #
TAB_WRITE, TAB_BUILD, TAB_HOTSPOT, TAB_CLONE, TAB_EXTRAS = 0, 1, 2, 3, 4
COLOR_INFO = "#2b6cb0"
COLOR_OK = "#2f855a"
COLOR_BAD = "#c53030"
COLOR_HINT = "#4a5568"


class PiInjectorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()  # stays hidden until the disclaimer is accepted
        self.title("%s  v%s" % (APP_TITLE, APP_VERSION))
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass
        self.countries = load_country_codes()
        self.busy = False
        self.busy_kind = None
        self.cancel_event = CANCEL
        self._ui_queue = queue.Queue()
        self.write_targets = []
        self.rejected_targets = []
        self._targets_signature = None
        self.action_widgets = []
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_queue()

    def report_callback_exception(self, exc_type, exc_value, exc_tb):
        log.error("Unexpected error in the window", exc_info=(exc_type, exc_value, exc_tb))
        try:
            mb_error(
                "Unexpected error",
                "Something unexpected went wrong:\n\n%s: %s\n\n(Details are in the log file: %s)"
                % (exc_type.__name__, exc_value, LOG_FILE),
            )
        except Exception:
            pass

    def start(self):
        self.build_ui()
        self.refresh_targets()
        self.after(3000, self._auto_refresh_targets)
        sh = self.winfo_screenheight()
        center_window(self, 700, min(780, sh - 80))
        self.minsize(620, 480)
        self.deiconify()
        self.lift()

    # ---- layout -------------------------------------------------------- #
    def build_ui(self):
        header = tk.Frame(self, padx=14, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="Pi Hotspot Injector", font=FONT_HEADER).pack(anchor="w")
        tk.Label(
            header,
            text="1. Write the image   →   2. Build Klipper (plain Pi OS)   →   "
            "3. Hotspot   →   4. Card into the Pi",
            font=FONT_SMALL, fg=COLOR_HINT,
        ).pack(anchor="w")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8)

        # Each tab = scrollable content + a fixed footer, so the main action
        # button is always on screen even on a short laptop display.
        tabs = []
        for label in ("  1. Write Image  ", "  2. Build Klipper  ", "  3. Hotspot Setup  ", "  Clone & Backup  ",
                      "  Extras  "):
            outer = tk.Frame(self.notebook)
            footer = tk.Frame(outer, pady=6)
            footer.pack(side="bottom", fill="x")
            ttk.Separator(outer, orient="horizontal").pack(side="bottom", fill="x")
            sf = ScrollFrame(outer)
            sf.pack(side="top", fill="both", expand=True)
            self.notebook.add(outer, text=label)
            tabs.append((sf.inner, footer))
        self.build_write_tab(*tabs[TAB_WRITE])
        self.build_klipper_tab(*tabs[TAB_BUILD])
        self.build_hotspot_tab(*tabs[TAB_HOTSPOT])
        self.build_clone_tab(*tabs[TAB_CLONE])
        self.build_extras_tab(tabs[TAB_EXTRAS][0])
        tabs[TAB_EXTRAS][1].pack_forget()

        bottom = tk.Frame(self, padx=14, pady=8)
        bottom.pack(fill="x")
        row = tk.Frame(bottom)
        row.pack(fill="x")
        self.progress = ttk.Progressbar(row, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_btn = tk.Button(row, text="Cancel", font=FONT_LABEL, state="disabled", command=self.on_cancel)
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.status_lbl = tk.Label(
            bottom, text="Ready.", font=FONT_LABEL, fg=COLOR_INFO, anchor="w", justify="left", wraplength=640
        )
        self.status_lbl.pack(fill="x", pady=(4, 0))

    def _hint(self, parent, text, **grid_or_pack):
        wrap = 440 if grid_or_pack else 600
        lbl = tk.Label(parent, text=text, font=FONT_SMALL, fg=COLOR_HINT, justify="left", anchor="w", wraplength=wrap)
        if grid_or_pack:
            lbl.grid(**grid_or_pack)
        else:
            lbl.pack(anchor="w", fill="x")
        return lbl

    def build_write_tab(self, f, footer):
        tk.Label(
            f,
            text="Write a Raspberry Pi OS / MainsailOS image onto an SD card or USB stick.",
            font=FONT_LABEL, anchor="w", justify="left",
        ).pack(anchor="w", fill="x")
        self._hint(
            f,
            "EVERYTHING on the chosen drive gets erased. For safety, the only drives offered "
            "are removable ones sized %s (±1 GB). Internal disks and bigger drives are never "
            "offered." % WRITE_TARGET_LABEL,
        )

        img = tk.LabelFrame(f, text=" Image file ", font=FONT_LABEL, padx=10, pady=8)
        img.pack(fill="x", pady=(10, 6))
        row = tk.Frame(img)
        row.pack(fill="x")
        self.image_var = tk.StringVar()
        tk.Entry(row, textvariable=self.image_var, font=FONT_LABEL).pack(side="left", fill="x", expand=True)
        self.browse_btn = tk.Button(row, text="Browse...", font=FONT_LABEL, command=self.on_browse)
        self.browse_btn.pack(side="left", padx=(6, 0))
        self._hint(img, "Works straight from the download: .img, .img.xz, .img.gz, .img.bz2, .zip, .zst - no need to unzip.")
        dl_row = tk.Frame(img)
        dl_row.pack(fill="x", pady=(8, 0))
        self.download_btn = tk.Button(
            dl_row, text="Download Raspberry Pi OS Lite (32-bit)", font=FONT_LABEL, bg="#c6f6d5",
            command=self.on_download_pi_os,
        )
        self.download_btn.pack(side="left")
        self._hint(
            img,
            "Not sure which image? This gets the right one for Klipper on ANY Pi (about 530 MB, official, "
            "checked automatically). Don't use 'Raspberry Pi Desktop for PC and Mac' - that ISO is for PCs.",
        )

        drv = tk.LabelFrame(f, text=" Drive to ERASE and write to ", font=FONT_LABEL, padx=10, pady=8)
        drv.pack(fill="x", pady=6)
        row = tk.Frame(drv)
        row.pack(fill="x")
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(row, textvariable=self.target_var, state="readonly", font=FONT_LABEL)
        self.target_combo.pack(side="left", fill="x", expand=True)
        self.target_combo.bind("<<ComboboxSelected>>", lambda e: self._show_target_details())
        self.refresh_btn = tk.Button(row, text="Refresh", font=FONT_LABEL, command=self.refresh_targets)
        self.refresh_btn.pack(side="left", padx=(6, 0))
        self.target_details = tk.Label(drv, text="", font=FONT_SMALL, fg=COLOR_BAD, justify="left", anchor="w", wraplength=600)
        self.target_details.pack(anchor="w", fill="x", pady=(4, 0))
        tk.Button(
            drv, text="Why isn't my drive listed?", font=FONT_SMALL, relief="flat", fg=COLOR_INFO,
            cursor="hand2", command=self.on_why_not_listed,
        ).pack(anchor="w")

        self.verify_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            f, text="Verify after writing (recommended - catches bad or fake cards, roughly doubles the time)",
            variable=self.verify_var, font=FONT_LABEL, wraplength=600, justify="left",
        ).pack(anchor="w", pady=(6, 4))

        self.write_btn = tk.Button(
            footer, text="Write Image to Drive", font=FONT_BTN, bg=COLOR_BAD, fg="white", height=2, width=30,
            command=self.on_write,
        )
        self.write_btn.pack()
        self.action_widgets += [self.browse_btn, self.refresh_btn, self.write_btn, self.target_combo,
                                self.download_btn]

    def build_klipper_tab(self, f, footer):
        tk.Label(
            f, text="Turn a card with plain Raspberry Pi OS Lite on it into a Klipper printer controller.",
            font=FONT_LABEL, anchor="w", justify="left", wraplength=600,
        ).pack(anchor="w", fill="x")
        self._hint(
            f,
            "Installs Klipper, Moonraker and Mainsail (and optionally Crowsnest) onto the card from this "
            "PC - clean, straight from each project, no MainsailOS extras.\n\n"
            "Use Raspberry Pi OS Lite (32-bit): the card then works in EVERY Pi - Pi 1, Zero, Zero W, "
            "Zero 2 W, 2, 3, 4, 400 and 5 - because everything is compiled for the oldest one. "
            "(64-bit Lite works too, for a Pi 3, 4, 400 or 5 only.) Write it in tab 1, then build straight "
            "away - don't start the card in a Pi first.\n\n"
            "Needs this PC online. Takes about 1-2 hours - keep the PC awake. Cancel is safe, and "
            "a stopped build can simply be started again.",
        )

        form = tk.Frame(f)
        form.pack(fill="x", pady=(10, 4))
        form.columnconfigure(1, weight=1)
        tk.Label(form, text="Login user:", font=FONT_LABEL).grid(row=0, column=0, sticky="e", pady=3, padx=(0, 6))
        self.build_user_entry = tk.Entry(form, font=FONT_LABEL, width=16)
        self.build_user_entry.insert(0, "pi")
        self.build_user_entry.grid(row=0, column=1, sticky="w", pady=3)

        tk.Label(form, text="Login password:", font=FONT_LABEL).grid(row=1, column=0, sticky="e", pady=3, padx=(0, 6))
        pw_row = tk.Frame(form)
        pw_row.grid(row=1, column=1, sticky="w", pady=3)
        self.build_pw_entry = tk.Entry(pw_row, font=FONT_LABEL, width=20, show="*")
        self.build_pw_entry.pack(side="left")
        self.build_show_pw = tk.BooleanVar(value=False)
        tk.Checkbutton(
            pw_row, text="Show", variable=self.build_show_pw, font=FONT_SMALL,
            command=lambda: self.build_pw_entry.config(show="" if self.build_show_pw.get() else "*"),
        ).pack(side="left", padx=4)
        self._hint(form, "For SSH logins and when Mainsail asks for it during updates. Write it down!",
                   row=2, column=1, sticky="w")

        tk.Label(form, text="Hostname:", font=FONT_LABEL).grid(row=3, column=0, sticky="e", pady=3, padx=(0, 6))
        self.build_host_entry = tk.Entry(form, font=FONT_LABEL, width=16)
        self.build_host_entry.insert(0, "klipper")
        self.build_host_entry.grid(row=3, column=1, sticky="w", pady=3)
        self._hint(form, "The Pi's name on a network. Give each printer its own.", row=4, column=1, sticky="w")

        opts = tk.LabelFrame(f, text=" Options ", font=FONT_LABEL, padx=10, pady=6)
        opts.pack(fill="x", pady=8)
        self.build_fw_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            opts, text="Printer firmware build tools", variable=self.build_fw_var, font=FONT_LABEL,
        ).pack(anchor="w")
        self._hint(opts, "      Lets you compile and flash your printer board's Klipper firmware on the Pi "
                         "(as most guides do). Uses about 1 GB more space.")
        self.build_crowsnest_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            opts, text="Crowsnest (webcam support)", variable=self.build_crowsnest_var, font=FONT_LABEL,
        ).pack(anchor="w")
        self._hint(opts, "      Streams a USB or Pi camera to Mainsail. A Pi Zero / Zero W can just about "
                         "manage one low-resolution USB webcam.")

        self.build_btn = tk.Button(
            footer, text="Build Klipper Card", font=FONT_BTN, bg="#2c7a7b", fg="white", height=2, width=30,
            command=self.on_build_klipper,
        )
        self.build_btn.pack()
        self.action_widgets.append(self.build_btn)

    def on_build_klipper(self):
        if self.busy:
            return
        opts = {
            "username": self.build_user_entry.get().strip(),
            "password": self.build_pw_entry.get(),
            "hostname": self.build_host_entry.get().strip(),
            "fw_tools": self.build_fw_var.get(),
            "crowsnest": self.build_crowsnest_var.get(),
        }
        err = validate_build_inputs(opts["username"], opts["password"], opts["hostname"])
        if err:
            mb_error("Please check the settings", err)
            return
        card = self._select_single_card()
        if card is None:
            return
        extras = [name for name, on in (("firmware build tools", opts["fw_tools"]), ("Crowsnest", opts["crowsnest"])) if on]
        if not mb_yesno(
            "Build Klipper card?",
            "Install Klipper, Moonraker and Mainsail%s onto:\n\n      %s\n\n"
            "Login user: %s     Hostname: %s\n\n"
            "• Use a card with freshly written plain Raspberry Pi OS Lite on it.\n"
            "• The card's system partition is first enlarged to fill the card (as the Pi "
            "itself would do on its first start).\n"
            "• Needs this PC online; takes about 1-2 hours - keep the PC awake.\n\nStart?"
            % ((" + " + " + ".join(extras)) if extras else "", card["desc"], opts["username"], opts["hostname"]),
        ):
            return
        log.info("Building Klipper card on %s (user %s, host %s, fw=%s, crowsnest=%s)", card["desc"],
                 opts["username"], opts["hostname"], opts["fw_tools"], opts["crowsnest"])

        def on_success(result):
            self._finish_ok("Klipper card built on %s." % card["desc"])
            lines = ["Klipper, Moonraker and Mainsail%s are installed." % (" and Crowsnest" if result["crowsnest"] else "")]
            if result["pi_zero_build"]:
                lines.append("Built for every Pi: works in a Pi 1, Zero, Zero W, Zero 2 W, 2, 3, 4, 400 or 5.")
            else:
                lines.append("64-bit build: for a Pi 3, 4, 400 or 5.")
            ok, detail = result["smoke"]
            if ok:
                lines.append("Test: Moonraker was started on the card%s and answered. ✔" %
                             (" (on an emulated Pi Zero CPU)" if result["pi_zero_build"] else ""))
            elif ok is None:
                lines.append("Moonraker start test %s." % detail)
            rep = result.get("repair") or {}
            if rep.get("rebuilt"):
                lines.append("Rebuilt for older Pis: " + ", ".join(n for _v, n, _ver, _f in rep["rebuilt"]))
            if result["crowsnest_source_build"]:
                lines.append("Crowsnest's video streamer was compiled on the card.")
            if result.get("desktop_disabled"):
                lines.append("This was Raspberry Pi OS *with desktop*: it now starts without the desktop, to "
                             "leave memory for Klipper (switch it back on any time with 'sudo raspi-config' "
                             "\u2192 System Options \u2192 Boot). Next time, Lite is the better fit - tab 1's "
                             "Download button gets it.")
            lines.append("Login: user '%s' with your password.\nWeb address once it's running: http://%s.local "
                         "(on a phone, use the number step 3 shows, e.g. http://192.168.50.1 - phones often "
                         "can't open .local names)." % (opts["username"], opts["hostname"]))
            lines.append("Next: step 3 (Hotspot Setup) - the card can stay plugged in. Then put it in the Pi; "
                         "the first start takes a few minutes. Mainsail will show a Klipper error until you "
                         "add your printer's config - that's expected.")
            problems = list(result["warnings"])
            if result.get("skipped"):
                problems.append("These optional packages couldn't be installed: %s" % ", ".join(result["skipped"]))
            if rep.get("unfixable") or rep.get("remaining"):
                bad = rep.get("remaining") or rep.get("unfixable")
                problems.append("Some programs are still built for newer Pis and may not work on a Pi Zero / Pi 1:\n"
                                + "\n".join(summarize_armv6_problems(bad)))
            if ok is False:
                problems.append("Moonraker didn't start in the test: " + detail[:600])
            if rep.get("failed") or rep.get("still_crashing"):
                problems.append("Some packages couldn't be made to work on older Pis: %s" % ", ".join(
                    [f[0] for f in rep.get("failed", [])] + rep.get("still_crashing", [])))
            if problems:
                mb_warning("Built - with warnings", "\n\n".join(lines) + "\n\nWARNINGS:\n" + "\n".join(
                    "• " + p for p in problems) + "\n\nDetails are in the log file: %s" % LOG_FILE)
                return
            if mb_yesno("Klipper card ready!", "\n\n".join(lines) + "\n\nGo to step 3 (Hotspot Setup) now?"):
                self.notebook.select(TAB_HOTSPOT)

        self._run_async(
            lambda: build_klipper_card(card, opts, self.say), on_success,
            "Preparing to build %s..." % card["desc"], kind="install", cancellable=True,
        )

    def build_hotspot_tab(self, f, footer):
        self._hint(
            f,
            "Plug in a card that already has Klipper on it (built in step 2, a MainsailOS card, or "
            "flashed with another tool). These settings are written onto it so the Pi starts "
            "its own Wi-Fi network when it boots.",
        )
        form = tk.Frame(f)
        form.pack(fill="x", pady=(8, 4))
        form.columnconfigure(1, weight=1)
        r = 0

        tk.Label(form, text="Wi-Fi name (SSID):", font=FONT_LABEL).grid(row=r, column=0, sticky="e", pady=3, padx=(0, 6))
        self.ssid_entry = tk.Entry(form, font=FONT_LABEL, width=24)
        self.ssid_entry.insert(0, "MainsailAP")
        self.ssid_entry.grid(row=r, column=1, sticky="w", pady=3)
        r += 1
        self._hint(form, "Several Pis? Give each its own name (e.g. Printer1, Printer2). If a phone has joined "
                   "a hotspot with this name before, choose 'Forget network' on it first - otherwise it may "
                   "silently try the old saved password.", row=r, column=1, sticky="w")
        r += 1

        tk.Label(form, text="Wi-Fi password:", font=FONT_LABEL).grid(row=r, column=0, sticky="e", pady=3, padx=(0, 6))
        pw_row = tk.Frame(form)
        pw_row.grid(row=r, column=1, sticky="w", pady=3)
        self.pass_entry = tk.Entry(pw_row, font=FONT_LABEL, width=24)
        self.pass_entry.insert(0, random_wifi_password())
        self.pass_entry.pack(side="left")
        self.show_pw_var = tk.BooleanVar(value=True)
        tk.Checkbutton(pw_row, text="Show", variable=self.show_pw_var, font=FONT_SMALL, command=self._toggle_pw).pack(side="left", padx=4)
        tk.Button(pw_row, text="New random", font=FONT_SMALL, command=self._new_password).pack(side="left")
        r += 1
        self._hint(form, "8-63 characters. A random one is made for you each time - write it down!", row=r, column=1, sticky="w")
        r += 1

        tk.Label(form, text="Country code:", font=FONT_LABEL).grid(row=r, column=0, sticky="e", pady=3, padx=(0, 6))
        self.country_entry = tk.Entry(form, font=FONT_LABEL, width=6)
        self.country_entry.insert(0, default_country_code())
        self.country_entry.grid(row=r, column=1, sticky="w", pady=3)
        r += 1
        self._hint(form, "Where the Pi will be used: GB, US, DE, FR, AU... (sets the legal Wi-Fi channels).", row=r, column=1, sticky="w")
        r += 1

        tk.Label(form, text="Wi-Fi channel:", font=FONT_LABEL).grid(row=r, column=0, sticky="e", pady=3, padx=(0, 6))
        self.channel_var = tk.IntVar(value=6)
        ttk.Combobox(form, textvariable=self.channel_var, values=list(range(1, 12)), width=5, state="readonly").grid(
            row=r, column=1, sticky="w", pady=3
        )
        r += 1

        tk.Label(form, text="Hotspot address:", font=FONT_LABEL).grid(row=r, column=0, sticky="e", pady=3, padx=(0, 6))
        sn_row = tk.Frame(form)
        sn_row.grid(row=r, column=1, sticky="w", pady=3)
        tk.Label(sn_row, text="192.168.", font=FONT_LABEL).pack(side="left")
        self.subnet_entry = tk.Entry(sn_row, font=FONT_LABEL, width=4)
        self.subnet_entry.insert(0, str(DEFAULT_SUBNET_OCTET))
        self.subnet_entry.pack(side="left")
        tk.Label(sn_row, text=".1", font=FONT_LABEL).pack(side="left")
        tk.Button(sn_row, text="Random", font=FONT_SMALL, command=self._random_subnet).pack(side="left", padx=6)
        r += 1
        self._hint(
            form,
            "Give each Pi a different number (2-254) if more than one hotspot might be on at "
            "once. This is also the address you'll type into your browser.",
            row=r, column=1, sticky="w",
        )

        backend = tk.LabelFrame(f, text=" Hotspot method ", font=FONT_LABEL, padx=10, pady=6)
        backend.pack(fill="x", pady=6)
        self.backend_var = tk.StringVar(value="hostapd")
        tk.Radiobutton(
            backend, text="hostapd + dnsmasq  (recommended)", variable=self.backend_var, value="hostapd", font=FONT_LABEL
        ).pack(anchor="w")
        self._hint(backend, "      Most reliable. If the card doesn't have these yet, you'll be offered to install them automatically.")
        tk.Radiobutton(
            backend, text="NetworkManager (built-in AP mode)", variable=self.backend_var, value="networkmanager",
            font=FONT_LABEL,
        ).pack(anchor="w")
        self._hint(backend, "      No extra packages, but unreliable on the Pi Zero W's Wi-Fi chip with newer (trixie) images.")

        opts = tk.LabelFrame(f, text=" Options ", font=FONT_LABEL, padx=10, pady=6)
        opts.pack(fill="x", pady=6)
        self.watchdog_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Boot-time watchdog (NetworkManager method only)", variable=self.watchdog_var, font=FONT_LABEL).pack(anchor="w")
        self.user_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opts, text="Enable SSH & create a login user", variable=self.user_var, font=FONT_LABEL, command=self.toggle_user).pack(anchor="w")
        user_frame = tk.Frame(opts)
        user_frame.pack(fill="x", pady=2)
        tk.Label(user_frame, text="    User:", font=FONT_LABEL).pack(side="left")
        self.user_entry = tk.Entry(user_frame, font=FONT_LABEL, width=10, state="disabled")
        self.user_entry.pack(side="left", padx=5)
        tk.Label(user_frame, text="Password:", font=FONT_LABEL).pack(side="left")
        self.userpw_entry = tk.Entry(user_frame, font=FONT_LABEL, width=14, show="*", state="disabled")
        self.userpw_entry.pack(side="left", padx=5)

        self.inject_btn = tk.Button(
            footer, text="Inject Hotspot", font=FONT_BTN, bg=COLOR_OK, fg="white", height=2, width=30,
            command=self.on_inject,
        )
        self.inject_btn.pack()
        self.action_widgets.append(self.inject_btn)

    def build_clone_tab(self, f, footer):
        footer.pack_forget()  # two actions here, each with its own button
        tk.Label(
            f, text="Make copies of a finished card - for your other Pis, or as a backup.",
            font=FONT_LABEL, anchor="w", justify="left", wraplength=600,
        ).pack(anchor="w", fill="x")
        self._hint(
            f,
            "Optional - the normal steps 1-3 don't need this. Typical use: build ONE card with steps 1-3, "
            "save it here as an image, then write that image onto the other cards. Each copy gets its own "
            "name here, then step 3 gives it its own hotspot name and password.",
        )

        # ---- A: card -> image file
        save = tk.LabelFrame(f, text=" A. Save a card as an image file (backup / master copy) ", font=FONT_LABEL,
                             padx=10, pady=8)
        save.pack(fill="x", pady=(10, 6))
        tk.Label(save, text="Card to copy (only read - never changed):", font=FONT_LABEL).pack(anchor="w")
        row = tk.Frame(save)
        row.pack(fill="x")
        self.clone_src_var = tk.StringVar()
        self.clone_src_combo = ttk.Combobox(row, textvariable=self.clone_src_var, state="readonly", font=FONT_LABEL)
        self.clone_src_combo.pack(side="left", fill="x", expand=True)
        self.clone_refresh_btn = tk.Button(row, text="Refresh", font=FONT_LABEL, command=self.refresh_targets)
        self.clone_refresh_btn.pack(side="left", padx=(6, 0))

        tk.Label(save, text="Save as:", font=FONT_LABEL).pack(anchor="w", pady=(6, 0))
        row = tk.Frame(save)
        row.pack(fill="x")
        self.clone_dest_var = tk.StringVar(value=os.path.join(
            REAL_HOME, "Pi-images", "pi-card-%s.img" % time.strftime("%Y-%m-%d")))
        tk.Entry(row, textvariable=self.clone_dest_var, font=FONT_LABEL).pack(side="left", fill="x", expand=True)
        self.clone_dest_btn = tk.Button(row, text="Browse...", font=FONT_LABEL, command=self.on_clone_dest_browse)
        self.clone_dest_btn.pack(side="left", padx=(6, 0))

        self.clone_shrink_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            save, text="Shrink to what's used (recommended)", variable=self.clone_shrink_var, font=FONT_LABEL,
        ).pack(anchor="w", pady=(6, 0))
        self._hint(save, "A '32 GB' card from another brand is often slightly smaller, so an exact copy wouldn't "
                   "fit. Shrunk, the file is only as big as what's on the card (a few GB) and fits any card "
                   "big enough - it's grown back to fill the card when written below.")
        self.clone_gzip_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            save, text="Also compress it (.img.gz - smaller file, takes longer)", variable=self.clone_gzip_var,
            font=FONT_LABEL,
        ).pack(anchor="w")
        self.clone_save_btn = tk.Button(
            save, text="Save Card to Image File", font=FONT_BTN, bg="#bee3f8", width=30,
            command=self.on_save_card_image,
        )
        self.clone_save_btn.pack(pady=(8, 0))

        # ---- B: image file -> card
        wr = tk.LabelFrame(f, text=" B. Write a copy onto another card ", font=FONT_LABEL, padx=10, pady=8)
        wr.pack(fill="x", pady=6)
        tk.Label(wr, text="Image file:", font=FONT_LABEL).pack(anchor="w")
        row = tk.Frame(wr)
        row.pack(fill="x")
        self.clone_img_var = tk.StringVar()
        tk.Entry(row, textvariable=self.clone_img_var, font=FONT_LABEL).pack(side="left", fill="x", expand=True)
        self.clone_img_btn = tk.Button(row, text="Browse...", font=FONT_LABEL, command=self.on_clone_img_browse)
        self.clone_img_btn.pack(side="left", padx=(6, 0))

        tk.Label(wr, text="Card to ERASE and write to:", font=FONT_LABEL).pack(anchor="w", pady=(6, 0))
        self.clone_dst_var = tk.StringVar()
        self.clone_dst_combo = ttk.Combobox(wr, textvariable=self.clone_dst_var, state="readonly", font=FONT_LABEL)
        self.clone_dst_combo.pack(fill="x")

        self.clone_grow_var = tk.BooleanVar(value=True)
        tk.Checkbutton(wr, text="Grow it to fill the whole card", variable=self.clone_grow_var,
                       font=FONT_LABEL).pack(anchor="w", pady=(6, 0))
        name_row = tk.Frame(wr)
        name_row.pack(anchor="w", fill="x")
        self.clone_unique_var = tk.BooleanVar(value=True)
        tk.Checkbutton(name_row, text="Make it a separate Pi, named:", variable=self.clone_unique_var,
                       font=FONT_LABEL).pack(side="left")
        self.clone_name_entry = tk.Entry(name_row, font=FONT_LABEL, width=16)
        self.clone_name_entry.insert(0, "klipper2")
        self.clone_name_entry.pack(side="left", padx=(4, 0))
        self._hint(wr, "Gives the copy its own network name (http://klipper2.local), and fresh internal ID and "
                   "SSH keys - so two Pis never get mixed up. Leave ticked unless you're restoring a backup "
                   "onto the same Pi.")
        self.clone_verify_var = tk.BooleanVar(value=True)
        tk.Checkbutton(wr, text="Verify after writing (recommended)", variable=self.clone_verify_var,
                       font=FONT_LABEL).pack(anchor="w")
        self.clone_write_btn = tk.Button(
            wr, text="Write Copy to Card", font=FONT_BTN, bg=COLOR_BAD, fg="white", width=30,
            command=self.on_write_clone,
        )
        self.clone_write_btn.pack(pady=(8, 0))
        self._hint(wr, "Then use step 3 on the new card to give it its own hotspot name and password.")

        self.action_widgets += [self.clone_src_combo, self.clone_refresh_btn, self.clone_dest_btn,
                                self.clone_save_btn, self.clone_img_btn, self.clone_dst_combo, self.clone_write_btn]

    def _combo_target(self, combo):
        idx = combo.current()
        if idx is None or idx < 0 or idx >= len(self.write_targets):
            return None
        return self.write_targets[idx]

    def on_clone_dest_browse(self):
        current = self.clone_dest_var.get().strip()
        start = os.path.dirname(current) if current and os.path.isdir(os.path.dirname(current)) else REAL_HOME
        path = filedialog.asksaveasfilename(
            parent=self, title="Save the card image as", initialdir=start,
            initialfile=os.path.basename(current) or "pi-card.img", defaultextension=".img",
            filetypes=[("Disk image", "*.img"), ("All files", "*")],
        )
        if path:
            self.clone_dest_var.set(path)

    def on_clone_img_browse(self):
        current = self.clone_img_var.get().strip() or self.clone_dest_var.get().strip()
        start = os.path.dirname(current) if current and os.path.isdir(os.path.dirname(current)) else REAL_HOME
        path = filedialog.askopenfilename(parent=self, title="Choose the image to copy onto a card",
                                          initialdir=start, filetypes=IMAGE_FILE_TYPES)
        if path:
            self.clone_img_var.set(path)

    def on_save_card_image(self):
        if self.busy:
            return
        src = self._combo_target(self.clone_src_combo)
        if src is None:
            mb_error("No card chosen", "Choose the card to copy from the list first.\n\nIf yours isn't there, "
                     "use 'Why isn't my drive listed?' on tab 1.")
            return
        dest = os.path.expanduser(self.clone_dest_var.get().strip())
        if not dest:
            mb_error("No file name", "Choose where to save the image (Browse...).")
            return
        if dest.endswith(".gz"):
            dest = dest[:-3]
        if not dest.endswith(".img"):
            dest += ".img"
        shrink, compress = self.clone_shrink_var.get(), self.clone_gzip_var.get()
        final = dest + ".gz" if compress else dest
        if os.path.exists(final) and not mb_yesno(
                "Replace the file?", "%s already exists.\n\nReplace it?" % final, icon="warning", default="no"):
            return
        if not mb_yesno(
            "Save card to image?",
            "Copy the whole card:\n\n      %s\n\ninto the file:\n\n      %s\n\nThe card is only READ - nothing on "
            "it is changed. It takes a while (the whole card is read: roughly 10-30 minutes for 32 GB, "
            "depending on the reader)%s.\n\nContinue?"
            % (src["desc"], final, ", then the copy is shrunk" if shrink else ""),
        ):
            return
        log.info("Saving %s to image %s (shrink=%s, compress=%s)", src["disk"], final, shrink, compress)

        def on_success(info):
            self._finish_ok("Card saved to %s." % os.path.basename(info["path"]))
            self.clone_img_var.set(info["path"])
            self.refresh_targets(quiet=True)
            mb_info(
                "Image saved!",
                "The card was copied to:\n\n      %s\n\nFile size: %s%s\n\nNext: take the card out, put in a "
                "blank one, and use 'B. Write a copy onto another card' below (the image is already filled in)."
                % (info["path"], human_size(info["file_size"]),
                   (" (shrunk from %s - it fits any card of %s or more)"
                    % (human_size(info["card_size"]), human_size(info["image_size"]))) if info["shrunk"]
                   else ("\n\n(Not shrunk - it needs a card at least as big as the original: %s.)"
                         % human_size(info["card_size"]))),
            )

        self._run_async(
            lambda: read_card_to_image(src, dest, shrink, compress, self.say, self.report_progress,
                                       self.cancel_event),
            on_success, "Preparing to copy %s..." % src["desc"], kind="clone-read", cancellable=True,
        )

    def on_write_clone(self):
        if self.busy:
            return
        path = os.path.expanduser(self.clone_img_var.get().strip())
        if not path or not os.path.isfile(path):
            mb_error("No image chosen", "Choose the image file to copy onto the card first (Browse...).")
            return
        target = self._combo_target(self.clone_dst_combo)
        if target is None:
            mb_error("No card chosen", "Choose the card to write to from the list first.")
            return
        hostname = None
        if self.clone_unique_var.get():
            hostname = self.clone_name_entry.get().strip()
            err = validate_hostname(hostname)
            if err:
                mb_error("Please check the name", err)
                return
        if not self._confirm_image_write(path, target):
            return
        grow, verify = self.clone_grow_var.get(), self.clone_verify_var.get()
        log.info("User confirmed writing copy %s to %s (grow=%s, hostname=%s)", path, target["disk"], grow, hostname)

        def on_success(res):
            self._finish_ok("Copy written to %s." % target["desc"])
            lines = ["The copy was written %s to:\n\n      %s\n"
                     % ("and verified" if res["verified"] else "(not verified)", target["desc"])]
            if res["grew"]:
                lines.append("• Its system partition now fills the whole card.")
            if res["hostname"]:
                lines.append("• It's now a separate Pi called '%s' (was '%s'), with its own ID and SSH keys "
                             "(made on its first start). Web address: http://%s.local"
                             % (res["hostname"], res["old_hostname"] or "?", res["hostname"]))
            lines.append("\nNext: step 3 (Hotspot Setup) to give it its own hotspot name and password - the "
                         "card can stay plugged in.")
            # suggest the next name: klipper2 -> klipper3
            if res["hostname"]:
                m = re.match(r"^(.*?)(\d+)$", res["hostname"])
                self.clone_name_entry.delete(0, "end")
                self.clone_name_entry.insert(0, "%s%d" % (m.group(1), int(m.group(2)) + 1) if m
                                             else res["hostname"] + "2")
            self.refresh_targets(quiet=True)
            if mb_yesno("Copy ready!", "\n".join(lines) + "\n\nGo to step 3 now?"):
                self.notebook.select(TAB_HOTSPOT)

        self._run_async(
            lambda: write_clone_to_card(path, target, verify, grow, hostname, self.say, self.report_progress,
                                        self.cancel_event),
            on_success, "Preparing to write...", kind="write", cancellable=True,
        )

    def build_extras_tab(self, f):
        box = tk.LabelFrame(f, text=" Install hostapd + dnsmasq onto the card ", font=FONT_LABEL, padx=10, pady=8)
        box.pack(fill="x", pady=(0, 8))
        self._hint(
            box,
            "Installs the recommended hotspot software onto the card from this PC, without booting the "
            "Pi. Needs this PC to be online; takes about 5-15 minutes. 'Inject Hotspot' offers this "
            "automatically when needed, so you rarely have to press it yourself.",
        )
        self.install_btn = tk.Button(box, text="Install packages", font=FONT_BTN, bg="#805ad5", fg="white", width=22, command=self.on_install_packages)
        self.install_btn.pack(anchor="w", pady=(6, 0))

        box = tk.LabelFrame(f, text=" Pi Zero / Zero W / Pi 1 fix ", font=FONT_LABEL, padx=10, pady=8)
        box.pack(fill="x", pady=8)
        self._hint(
            box,
            "Moonraker (or Klipper) keeps crashing on a Pi Zero, Zero W or Pi 1 with 'Illegal instruction' "
            "(status=4/ILL)? Some images contain software built for newer Pis. 'Check card' finds it "
            "(read-only, 1-2 minutes). 'Fix' rebuilds it for these older Pis from this PC - needs "
            "internet and can take 30-90 minutes. Not needed for a Pi Zero 2 W, 3, 4 or 5.",
        )
        row = tk.Frame(box)
        row.pack(anchor="w", pady=(6, 0))
        self.armv6_check_btn = tk.Button(row, text="Check card", font=FONT_BTN, width=14, command=self.on_armv6_check)
        self.armv6_check_btn.pack(side="left")
        self.armv6_fix_btn = tk.Button(
            row, text="Fix for Pi Zero / Pi 1", font=FONT_BTN, bg="#c05621", fg="white", width=20,
            command=self.on_armv6_fix,
        )
        self.armv6_fix_btn.pack(side="left", padx=8)

        box = tk.LabelFrame(f, text=" Card Inquisitor (boot diagnostics) ", font=FONT_LABEL, padx=10, pady=8)
        box.pack(fill="x", pady=8)
        self._hint(
            box,
            "Something not working? This adds a one-off report to the card. Boot the Pi with it, wait 6 "
            "minutes, then put the card back in this PC and open XXXXX_DIAGNOSTICS.txt on its boot "
            "partition. Attach that file if you ask for help.",
        )
        self.inquisitor_btn = tk.Button(box, text="Add diagnostics", font=FONT_BTN, bg=COLOR_INFO, fg="white", width=22, command=self.on_inquisitor)
        self.inquisitor_btn.pack(anchor="w", pady=(6, 0))

        box = tk.LabelFrame(f, text=" Start menu shortcut ", font=FONT_LABEL, padx=10, pady=8)
        box.pack(fill="x", pady=8)
        self._hint(box, "Adds 'Pi Hotspot Injector' to your applications menu so you can start it like any other app.")
        self.launcher_btn = tk.Button(box, text="Add to applications menu", font=FONT_LABEL, width=22, command=self.on_add_launcher)
        self.launcher_btn.pack(anchor="w", pady=(6, 0))

        box = tk.LabelFrame(f, text=" About ", font=FONT_LABEL, padx=10, pady=8)
        box.pack(fill="x", pady=8)
        tk.Label(
            box,
            text="%s v%s\nCopyright (c) 2026 %s\nFree for non-commercial use - PolyForm Noncommercial 1.0.0.\n"
            "Provided as is, with no warranty and no liability.\nProject page: %s\nLog file: %s"
            % (APP_TITLE, APP_VERSION, COPYRIGHT_HOLDER, PROJECT_URL, LOG_FILE),
            font=FONT_SMALL, justify="left", anchor="w", wraplength=600,
        ).pack(anchor="w", fill="x")
        tk.Button(box, text="View licence", font=FONT_LABEL, command=lambda: show_licence(self)).pack(anchor="w", pady=(6, 0))

        self.action_widgets += [self.install_btn, self.inquisitor_btn, self.launcher_btn,
                                self.armv6_check_btn, self.armv6_fix_btn]

    # ---- small form helpers -------------------------------------------- #
    def toggle_user(self):
        state = "normal" if self.user_var.get() else "disabled"
        self.user_entry.config(state=state)
        self.userpw_entry.config(state=state)
        if state == "normal" and not self.user_entry.get():
            self.user_entry.insert(0, "pi")

    def _toggle_pw(self):
        self.pass_entry.config(show="" if self.show_pw_var.get() else "*")

    def _new_password(self):
        self.pass_entry.delete(0, "end")
        self.pass_entry.insert(0, random_wifi_password())

    def _random_subnet(self):
        self.subnet_entry.delete(0, "end")
        self.subnet_entry.insert(0, str(20 + secrets.randbelow(230)))  # 20-249

    # ---- thread-safe status/progress/completion plumbing ---------------- #
    # Long jobs (image writes, chroot installs) run on a background thread
    # so the window never freezes; Tk widgets may only be touched from the
    # main thread, so the worker just queues messages for _poll_queue.
    def _poll_queue(self):
        # Reschedule FIRST: a "done" callback below may open a modal popup,
        # and the poller (plus any pending SIGTERM handling, which needs
        # Python code to run) must keep ticking while it's open.
        try:
            self.after(100, self._poll_queue)
        except tk.TclError:
            return  # window is being destroyed
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "status":
                    text, color = payload
                    self.status_lbl.config(text=text, fg=color)
                elif kind == "progress":
                    self._apply_progress(*payload)
                elif kind == "done":
                    payload()
        except queue.Empty:
            pass
        except tk.TclError:
            pass  # window is being destroyed

    def say(self, text, color=COLOR_INFO):
        if threading.current_thread() is threading.main_thread():
            self.status_lbl.config(text=text, fg=color)  # show now, even if a popup opens next
        else:
            self._ui_queue.put(("status", (text, color)))

    def report_progress(self, fraction, text):
        self._ui_queue.put(("progress", (fraction, text)))

    def _apply_progress(self, fraction, text):
        if fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.config(mode="indeterminate")
                self.progress.start(15)
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress["value"] = max(0.0, min(100.0, fraction * 100.0))
        if text:
            self.status_lbl.config(text=text, fg=COLOR_INFO)

    def _reset_progress(self):
        self.progress.stop()
        self.progress.config(mode="determinate")
        self.progress["value"] = 0

    def _set_buttons_enabled(self, enabled):
        for w in self.action_widgets:
            try:
                if isinstance(w, ttk.Combobox):
                    w.config(state="readonly" if enabled else "disabled")
                else:
                    w.config(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _end_busy(self):
        self.busy = False
        self.busy_kind = None
        self.cancel_btn.config(state="disabled")
        self._reset_progress()
        self._set_buttons_enabled(True)

    def _on_operation_error(self, exc):
        self._end_busy()
        if isinstance(exc, DownloadPaused):
            self.say("Download paused - press Download again to carry on.", COLOR_BAD)
            mb_warning("Download paused", str(exc))
            return
        if isinstance(exc, CancelledError):
            self.say("Cancelled.", COLOR_BAD)
            mb_warning("Cancelled", str(exc))
            return
        self.say("Failed - see the message for details.", COLOR_BAD)
        msg = str(exc) or type(exc).__name__
        if not isinstance(exc, InjectError):
            msg = "Something unexpected went wrong:\n\n%s: %s" % (type(exc).__name__, msg)
        mb_error("Error", "%s\n\n(Technical details are in the log file: %s)" % (msg, LOG_FILE))

    def _finish_ok(self, status_text):
        self._end_busy()
        self.say(status_text, COLOR_OK)

    def _run_async(self, work_fn, on_success, initial_status, kind="job", cancellable=False, on_error=None):
        self.busy = True
        self.busy_kind = kind
        self.cancel_event.clear()
        self._set_buttons_enabled(False)
        self.cancel_btn.config(state="normal" if cancellable else "disabled")
        self.report_progress(None, initial_status)

        def worker():
            try:
                result = work_fn()
            except Exception as exc:
                if not isinstance(exc, (InjectError,)):
                    log.exception("Unexpected error during %s", kind)
                else:
                    log.warning("%s failed: %s", kind, exc)
                handler = on_error or self._on_operation_error
                # Bind exc now: Python deletes the 'except ... as exc' name
                # when this block ends, before the main thread runs the lambda.
                self._ui_queue.put(("done", lambda e=exc, h=handler: h(e)))
            else:
                self._ui_queue.put(("done", lambda r=result: on_success(r)))

        thread = threading.Thread(target=worker, daemon=True)
        CURRENT_WORKER[0] = thread
        thread.start()

    def on_cancel(self):
        if not self.busy or self.cancel_event.is_set():
            return
        if self.busy_kind == "download":
            text = ("Pause the download?\n\nWhat's downloaded so far is kept - press Download again any time "
                    "to carry on from the last checkpoint.")
        elif self.busy_kind == "clone-read":
            text = "Stop copying?\n\nThe card isn't changed; the unfinished image file is deleted."
        elif self.busy_kind == "install":
            text = ("Stop now?\n\nThe card will be tidied up safely, but the step that was running "
                    "will be unfinished - just start it again later and it carries on.")
        else:
            text = ("Stop now?\n\nIf the image is still being written, the drive will be left "
                    "half-written and won't boot until you write it again.")
        if mb_yesno("Cancel?", text, icon="warning", default="no"):
            self.cancel_event.set()
            self.cancel_btn.config(state="disabled")
            self.say("Cancelling - tidying up, please wait...", COLOR_BAD)
            # Stopping apt inside the chroot can take a few seconds; don't
            # freeze the window while it happens.
            threading.Thread(target=kill_active_children, daemon=True).start()

    def on_close(self):
        if self.busy:
            warnings_by_kind = {
                "clone-read": "A card is being copied to an image file. Quitting now stops it (the card isn't changed; the unfinished file is deleted).",
                "download": "A download is still running. Quitting now pauses it - next time, Download carries on from the last checkpoint.",
                "write": "An image is still being written. Quitting now leaves the drive half-written and unbootable.",
                "install": "Software is still being installed onto the card. Quitting now stops it safely, but the card will need the step run again.",
            }
            text = warnings_by_kind.get(
                self.busy_kind, "The card is still being worked on. Quitting now may leave it incomplete."
            )
            if not mb_yesno(
                "Still working!", text + "\n\nQuit anyway?", icon="warning", default="no"
            ):
                return
            self.say("Stopping safely and tidying up the card - please wait...", COLOR_BAD)
            self.config(cursor="watch")
            self.update_idletasks()
            stop_background_work(timeout=45)
        self.destroy()

    # ---- image writer tab ------------------------------------------------ #
    def on_browse(self):
        start = os.path.join(REAL_HOME, "Downloads")
        if not os.path.isdir(start):
            start = REAL_HOME
        current = self.image_var.get().strip()
        if current and os.path.isdir(os.path.dirname(current)):
            start = os.path.dirname(current)
        path = filedialog.askopenfilename(
            parent=self, title="Choose the OS image to write", initialdir=start, filetypes=IMAGE_FILE_TYPES
        )
        if path:
            self.image_var.set(path)

    def on_download_pi_os(self):
        if self.busy:
            return

        def on_success(path):
            self._finish_ok("Downloaded and checked: %s" % os.path.basename(path))
            self.image_var.set(path)
            mb_info(
                "Ready",
                "Raspberry Pi OS Lite (32-bit) is downloaded and verified:\n\n      %s\n\nIt's filled in "
                "as the image to write. Next: choose your card below and press 'Write Image to Drive'."
                % path,
            )

        self._run_async(
            lambda: download_pi_os_lite(self.say, self.report_progress, self.cancel_event), on_success,
            "Contacting raspberrypi.com...", kind="download", cancellable=True,
        )

    def refresh_targets(self, quiet=False):
        try:
            eligible, rejected = classify_write_targets(quiet=quiet)
        except Exception as exc:
            if not quiet:
                mb_error("Error", "Couldn't list drives: %s" % exc)
            return
        signature = [(t["disk"], t["size"], t["serial"], tuple(t["contents"])) for t in eligible]
        if quiet and signature == self._targets_signature:
            self.rejected_targets = rejected
            return
        self._targets_signature = signature
        previous = self._selected_target()
        self.write_targets = eligible
        self.rejected_targets = rejected
        values = [t["desc"] for t in eligible]
        self.target_combo.config(values=values)
        keep = None
        if previous:
            for i, t in enumerate(eligible):
                if t["disk"] == previous["disk"] and t["serial"] == previous["serial"]:
                    keep = i
        if keep is not None:
            self.target_combo.current(keep)
        elif len(eligible) == 1:
            self.target_combo.current(0)
        else:
            self.target_var.set("")
        self._show_target_details()
        for combo in (getattr(self, "clone_src_combo", None), getattr(self, "clone_dst_combo", None)):
            if combo is None:
                continue
            old = combo.get()
            combo.config(values=values)
            if old in values:
                combo.current(values.index(old))
            elif len(values) == 1 and combo is self.clone_src_combo:
                combo.current(0)
            else:
                combo.set("")

    def _auto_refresh_targets(self):
        """Pick up drives plugged in after the window opened."""
        try:
            if not self.busy:
                self.refresh_targets(quiet=True)
        finally:
            self.after(3000, self._auto_refresh_targets)

    def _selected_target(self):
        idx = self.target_combo.current() if hasattr(self, "target_combo") else -1
        if idx is None or idx < 0 or idx >= len(self.write_targets):
            return None
        return self.write_targets[idx]

    def _show_target_details(self):
        t = self._selected_target()
        if not self.write_targets:
            self.target_details.config(
                text="No suitable drive found. Plug in an %s SD card or USB stick (the list updates "
                "by itself), or click the link below to see why a drive isn't offered." % WRITE_TARGET_LABEL,
                fg=COLOR_HINT,
            )
        elif t is None:
            self.target_details.config(text="Choose the drive to erase from the list above.", fg=COLOR_HINT)
        else:
            contents = "; ".join(t["contents"]) or "no partitions (blank or unformatted)"
            self.target_details.config(text="Currently on it (will be ERASED): %s" % contents, fg=COLOR_BAD)

    def on_why_not_listed(self):
        lines = ["Only removable drives (USB sticks, SD cards) sized %s, give or take 1 GB, are "
                 "offered - so an internal disk or an external hard drive can never be wiped "
                 "by mistake.\n" % WRITE_TARGET_LABEL]
        if self.rejected_targets:
            lines.append("Drives found but not offered:\n")
            lines += ["• %s\n     → %s" % (d, r) for d, r in self.rejected_targets]
        else:
            lines.append("No other drives were found at all. If your card is plugged in, try a "
                         "different USB port or card reader.")
        mb_info("Why isn't my drive listed?", "\n".join(lines))

    def _confirm_image_write(self, path, target):
        """Checks the image, then the two sanity checks. True = go ahead."""
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            looks_ok, size, what = peek_image(path)
        except InjectError as exc:
            mb_error("Can't use this image", str(exc))
            return False
        except Exception as exc:
            mb_error("Can't use this image", "Couldn't read the image file: %s" % exc)
            return False
        finally:
            self.config(cursor="")

        if size is not None and size > target["size"]:
            mb_error(
                "Image too big",
                "This image needs %s, but the selected drive only holds %s.\n\nUse a bigger card."
                % (human_size(size), human_size(target["size"])),
            )
            return False
        contents = describe_image_contents(path)
        if contents == "iso":
            if not mb_yesno(
                "This is a PC/Mac installer, not Raspberry Pi OS",
                "'%s' is an ISO disc image - a PC or Mac installer. (For example 'Raspberry Pi Desktop for "
                "PC and Mac' is for PCs.) It will NOT start on a Raspberry Pi.\n\nFor a Pi, use Raspberry "
                "Pi OS Lite (32-bit): the 'Download Raspberry Pi OS Lite' button above gets the right "
                "file.\n\nOnly continue if you're deliberately making a PC boot stick. Write it anyway?"
                % os.path.basename(path),
                icon="warning", default="no",
            ):
                self.say("Write cancelled - nothing was changed.")
                return False
            looks_ok = True  # warned already; skip the generic warning below
        if not looks_ok and not mb_yesno(
            "Is this the right file?",
            "'%s' doesn't look like a bootable disk image (there's no partition table at its "
            "start).\n\nRaspberry Pi OS / MainsailOS downloads are usually named something like "
            "'...-raspios-....img.xz' or 'mainsailos-....img.xz'.\n\nWrite it anyway?"
            % os.path.basename(path),
            icon="warning", default="no",
        ):
            return False

        # ---- Sanity check 1 of 2 ----
        contents = "\n".join("      " + c for c in target["contents"]) or "      (no partitions - blank or unformatted)"
        mounted = [m for m in target["mounted"]]
        msg = (
            "ALL DATA on this drive will be PERMANENTLY ERASED:\n\n"
            "      %s\n\n"
            "It currently contains:\n%s\n%s\n"
            "It will get this image:\n      %s\n      (%s, %s)\n\n"
            "Is this definitely the right drive?"
            % (
                target["desc"],
                contents,
                ("\nOpen right now at: %s\n" % ", ".join(mounted)) if mounted else "",
                os.path.basename(path),
                what,
                human_size(size) if size else "final size shown while writing",
            )
        )
        if not mb_yesno("Sanity check 1 of 2", msg, icon="warning", default="no"):
            self.say("Write cancelled - nothing was changed.")
            return False

        # ---- Sanity check 2 of 2 ----
        dlg = ReallySureDialog(self, target)
        self.wait_window(dlg)
        if not dlg.confirmed:
            self.say("Write cancelled - nothing was changed.")
            return False
        return True

    def on_write(self):
        if self.busy:
            return
        path = self.image_var.get().strip()
        if not path:
            mb_error("No image chosen", "Choose the image file to write first (the Browse... button).")
            return
        if not os.path.isfile(path):
            mb_error("Image not found", "Can't find this file:\n\n%s" % path)
            return
        target = self._selected_target()
        if target is None:
            mb_error(
                "No drive chosen",
                "Choose the drive to write to from the list first.\n\nIf yours isn't in the list, "
                "click 'Why isn't my drive listed?'.",
            )
            return

        if not self._confirm_image_write(path, target):
            return

        verify = self.verify_var.get()
        log.info("User confirmed writing %s to %s (%s)", path, target["disk"], target["desc"])

        def on_success(result):
            self._finish_ok("Image written to %s." % target["desc"])
            minutes = result["seconds"] / 60.0
            checked = "and verified" if result["verified"] else "(not verified)"
            is_mainsailos = "mainsail" in os.path.basename(path).lower()
            next_tab, next_name = ((TAB_HOTSPOT, "step 3 (Hotspot Setup)") if is_mainsailos
                                   else (TAB_BUILD, "step 2 (Build Klipper)"))
            go = mb_yesno(
                "Done!",
                "%s was written %s to:\n\n      %s\n\nin %s.\n\n"
                "Your desktop may pop up windows for the card's new partitions - that's normal, "
                "you can close them.\n\nGo to %s now? The card can stay plugged in.\n\n(Plain "
                "Raspberry Pi OS: step 2 installs Klipper. MainsailOS already has it: go "
                "straight to step 3.)"
                % (os.path.basename(path), checked, target["desc"],
                   "under a minute" if minutes < 1 else "%d minute%s" % (round(minutes), "" if round(minutes) == 1 else "s"),
                   next_name),
            )
            self.refresh_targets(quiet=True)
            if go:
                self.notebook.select(next_tab)

        self._run_async(
            lambda: write_image(path, target, verify, self.say, self.report_progress, self.cancel_event),
            on_success, "Preparing to write...", kind="write", cancellable=True,
        )

    # ---- Pi-card operations -------------------------------------------- #
    def _select_single_card(self):
        """Exactly one Pi-looking card, or an explanation and None. Refuses
        to guess when several are plugged in."""
        try:
            cards = find_cards()
        except Exception as exc:
            mb_error("Error", str(exc))
            return None
        if not cards:
            reason = explain_missing_cards()
            if reason:
                mb_error("No Raspberry Pi OS on the card", reason)
                return None
            mb_error(
                "No Pi SD card found",
                "Couldn't find a Raspberry Pi OS / MainsailOS card.\n\n"
                "• Is the card plugged in? (Give it a few seconds after plugging in.)\n"
                "• Is it a card that already has the Pi OS on it? A new or blank card needs "
                "an image written first - use tab 1, 'Write Image'.",
            )
            return None
        if len(cards) > 1:
            mb_error(
                "More than one card found",
                "Found %d drives that look like Pi SD cards:\n\n%s\n\n"
                "To be sure the right one gets changed, unplug all but the one you want, then "
                "try again." % (len(cards), describe_cards(cards)),
            )
            return None
        return cards[0]

    def on_inject(self):
        if self.busy:
            return
        settings = {
            "ssid": self.ssid_entry.get(),
            "password": self.pass_entry.get(),
            "country": self.country_entry.get().strip().upper(),
            "channel": int(self.channel_var.get()),
            "ap_backend": self.backend_var.get(),
            "watchdog": self.watchdog_var.get(),
            "want_user": self.user_var.get(),
            "username": self.user_entry.get().strip(),
            "user_pw": self.userpw_entry.get(),
        }
        subnet_str = self.subnet_entry.get().strip()
        err = validate_inputs(
            settings["ssid"], settings["password"], settings["country"], self.countries,
            settings["want_user"], settings["username"], settings["user_pw"], subnet_str,
        )
        if err:
            mb_error("Please check the settings", err)
            return
        settings["subnet"] = int(subnet_str)

        card = self._select_single_card()
        if card is None:
            return
        if not mb_yesno(
            "Inject hotspot?",
            "Set up the hotspot on:\n\n      %s\n\nWi-Fi name:  %s\nPassword:     %s\nAddress:      http://%s\n\nContinue?"
            % (card["desc"], settings["ssid"], settings["password"], subnet_addresses(settings["subnet"])["ap_gateway"]),
        ):
            return

        def on_success(result):
            summary, warns = result
            self._finish_ok("Hotspot set up on %s - safe to remove the card." % card["desc"])
            gw = subnet_addresses(settings["subnet"])["ap_gateway"]
            lines = [
                "The hotspot is set up on %s.\n" % card["desc"],
                "WRITE THESE DOWN:",
                "      Wi-Fi name:       %s" % settings["ssid"],
                "      Wi-Fi password:  %s" % settings["password"],
                "      Web address:     http://%s" % gw,
            ]
            if settings.get("card_hostname"):
                lines.append("                               (or http://%s.local on a laptop)" % settings["card_hostname"])
            if settings["want_user"]:
                lines.append("      SSH:                  ssh %s@%s" % (settings["username"], gw))
            lines += [
                "",
                "It's safe to remove the card now. Put it in the Pi and power it on. The FIRST "
                "boot takes a few minutes (the Pi sets itself up and may restart once) before "
                "the Wi-Fi network appears.",
                "",
                "Details:",
            ] + ["  • " + s for s in summary]
            mb_info("Hotspot ready!", "\n".join(lines))
            if warns:
                mb_warning("Heads-up", "Please read this before using the card:\n\n" + "\n\n".join(warns))

        def on_error(exc):
            if not isinstance(exc, MissingPackagesError):
                self._on_operation_error(exc)
                return
            self._end_busy()
            self.say("hostapd + dnsmasq aren't on this card yet - waiting for your answer...")
            if mb_yesno(
                "Install hostapd + dnsmasq?",
                "%s\n\nThis tool can install them onto the card for you right now, from this "
                "PC. This PC needs to be online, and it takes about 5-15 minutes. Then the "
                "hotspot setup finishes by itself.\n\nInstall them now?\n\n(If you choose No, "
                "nothing is changed. You could instead pick the 'NetworkManager' method, which "
                "needs no extra packages but is less reliable on a Pi Zero W.)" % exc,
            ):
                def install_then_inject():
                    install_ap_packages_via_chroot(card, self.say)
                    return inject_card(card, settings, self.say)

                self._run_async(
                    install_then_inject, on_success, "Installing packages on %s..." % card["desc"],
                    kind="install", cancellable=True,
                )
            else:
                self.say("Ready.")

        self._run_async(
            lambda: inject_card(card, settings, self.say), on_success, "Working on %s..." % card["desc"],
            kind="inject", on_error=on_error,
        )

    def on_inquisitor(self):
        if self.busy:
            return
        card = self._select_single_card()
        if card is None:
            return
        if not mb_yesno(
            "Card Inquisitor",
            "Add the boot diagnostics collector to:\n\n      %s\n\n"
            "On its next start-up the Pi will write 'XXXXX_DIAGNOSTICS.txt' to its boot "
            "partition. Nothing else on the card is changed." % card["desc"],
        ):
            return

        def on_success(_result):
            self._finish_ok("Diagnostics added to %s - safe to remove the card." % card["desc"])
            mb_info(
                "Diagnostics added",
                "Done! It's safe to remove the card.\n\n"
                "1. Put the card in the Pi and power it on.\n"
                "2. Wait at least 6 minutes. The collector deliberately waits about 100 "
                "seconds after boot so it can catch a service that keeps crashing, then test-runs "
                "Moonraker with crash tracing for up to 90 seconds.\n"
                "3. Power the Pi off, put the card back in this PC and open "
                "XXXXX_DIAGNOSTICS.txt on its boot partition (the 'XXXXX' name makes it "
                "easy to spot).",
            )

        self._run_async(lambda: inject_inquisitor(card, self.say), on_success, "Working on %s..." % card["desc"], kind="inject")

    def on_install_packages(self):
        if self.busy:
            return
        card = self._select_single_card()
        if card is None:
            return
        if not mb_yesno(
            "Install hotspot packages",
            "Install hostapd + dnsmasq onto:\n\n      %s\n\n"
            "This runs the card's own package manager from this PC (via a qemu chroot). If "
            "this PC doesn't have the qemu tools needed, they're installed here first.\n\n"
            "Needs this PC to be online. Takes about 5-15 minutes - the window stays usable "
            "and shows progress. Continue?" % card["desc"],
        ):
            return

        def on_success(_result):
            self._finish_ok("Packages installed on %s." % card["desc"])
            mb_info(
                "Done",
                "hostapd and dnsmasq are installed on %s.\n\nNext: tab 3, 'Inject Hotspot'." % card["desc"],
            )

        self._run_async(
            lambda: install_ap_packages_via_chroot(card, self.say), on_success,
            "Working on %s..." % card["desc"], kind="install", cancellable=True,
        )

    def on_armv6_check(self):
        if self.busy:
            return
        card = self._select_single_card()
        if card is None:
            return

        def on_success(result):
            self._finish_ok("Check finished for %s." % card["desc"])
            if result["arch"] == "arm64":
                mb_warning(
                    "64-bit system",
                    "This card has a 64-bit system.\n\nThat's fine on a Pi Zero 2 W, 3, 4 or 5, but it "
                    "won't start at all on a Pi Zero, Zero W or Pi 1 - those need the 32-bit image.",
                )
                return
            problems = result["problems"]
            if not problems:
                mb_info(
                    "All good",
                    "Nothing on this card is built for a newer processor than the Pi Zero's - it's "
                    "compatible with the Pi Zero, Zero W and Pi 1.\n\nIf Moonraker still crashes, add "
                    "the Card Inquisitor diagnostics and look at section 11 of the report.",
                )
                return
            lines = summarize_armv6_problems(problems)
            if mb_yesno(
                "Won't work on a Pi Zero / Pi 1",
                "%d program file%s on this card %s built for a newer processor than the Pi Zero, "
                "Zero W and Pi 1 have. On those boards they crash with 'Illegal instruction' (the "
                "status=4/ILL in Moonraker's log).\n\n%s\n\n(On a Pi Zero 2 W, 3, 4 or 5 this card is "
                "fine as it is.)\n\nFix it now? This rebuilds the affected packages for the older "
                "Pis - it needs this PC online and can take 30-90 minutes."
                % (len(problems), "" if len(problems) == 1 else "s", "is" if len(problems) == 1 else "are",
                   "\n".join(lines)),
                icon="warning",
            ):
                self._start_armv6_fix(card)

        self._run_async(
            lambda: check_card_armv6(card, self.say), on_success, "Checking %s..." % card["desc"], kind="check"
        )

    def on_armv6_fix(self):
        if self.busy:
            return
        card = self._select_single_card()
        if card is None:
            return
        if not mb_yesno(
            "Fix for Pi Zero / Pi 1",
            "Rebuild the software on:\n\n      %s\n\nso it runs on a Pi Zero, Zero W or Pi 1?\n\n"
            "• Finds every package built for a newer processor and recompiles it from source for "
            "the Pi Zero's CPU, then tests each one on an emulated Pi Zero.\n"
            "• Installs build tools onto the card (about 150-250 MB).\n"
            "• Needs this PC online. Takes 30-90 minutes - keep the PC awake. The window stays "
            "usable, and Cancel is safe (the card is tidied up).\n\nContinue?" % card["desc"],
        ):
            return
        self._start_armv6_fix(card)

    def _start_armv6_fix(self, card):
        def on_success(result):
            self._finish_ok("Pi Zero fix finished for %s." % card["desc"])
            if result["nothing_to_do"]:
                mb_info(
                    "Nothing to fix",
                    "Nothing on this card is built for a newer processor than the Pi Zero's - no "
                    "changes were needed.",
                )
                return
            ok = not (result["failed"] or result["still_crashing"] or result["remaining"])
            lines = []
            if result["rebuilt"]:
                lines.append("Rebuilt for the Pi Zero's CPU: " + ", ".join(
                    ("%s %s" % (n, v or "")).strip() for _venv, n, v, _f in result["rebuilt"]))
            if result["deleted"]:
                lines.append("Removed %d prebuilt Klipper helper file(s) - Klipper rebuilds them on the "
                             "Pi at its next start." % len(result["deleted"]))
            if result["failed"]:
                lines.append("Couldn't rebuild: " + "; ".join("%s (%s)" % f for f in result["failed"]))
            if result["still_crashing"]:
                lines.append("Still crash on an emulated Pi Zero: " + ", ".join(result["still_crashing"]))
            if result["remaining"]:
                lines.append("Still not compatible:\n" + "\n".join(summarize_armv6_problems(result["remaining"])))
            if ok:
                mb_info(
                    "Fixed!",
                    "Done - every affected package was rebuilt, and each one loads correctly on an "
                    "emulated Pi Zero CPU.\n\n%s\n\nPut the card in the Pi and power it on. The first "
                    "start takes a few minutes; then Moonraker should stay up." % "\n\n".join(lines),
                )
            else:
                mb_warning(
                    "Partly fixed",
                    "Some things couldn't be fixed.\n\n%s\n\nDetails are in the log file: %s\n\nThe "
                    "simplest guaranteed fix is a Pi Zero 2 W (same size and price class), which runs "
                    "this image as it is." % ("\n\n".join(lines), LOG_FILE),
                )

        self._run_async(
            lambda: repair_card_for_armv6(card, self.say), on_success, "Preparing to fix %s..." % card["desc"],
            kind="install", cancellable=True,
        )

    def on_add_launcher(self):
        try:
            dest = install_menu_launcher()
        except Exception as exc:
            mb_error("Couldn't add the shortcut", str(exc))
            return
        mb_info(
            "Added",
            "'Pi Hotspot Injector' is now in your applications menu (it may take a moment "
            "to appear).\n\nIf you move pi_injector.py to another folder later, press this "
            "button again.\n\n(%s)" % dest,
        )


# --------------------------------------------------------------------------- #
# Start-up
# --------------------------------------------------------------------------- #
def setup_logging():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        logging.basicConfig(
            filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
        )
        if os.geteuid() == 0 and REAL_UID != 0:
            os.chown(LOG_FILE, REAL_UID, REAL_GID)
    except OSError:
        logging.basicConfig(level=logging.INFO)


def relaunch_with_privileges():
    """Re-run this script as root via a graphical password prompt.

    This is what makes double-clicking work: started without root, we hand
    over to pkexec (what GNOME/KDE/Cinnamon/XFCE's own admin tools use),
    falling back to the older kdesudo/gksudo/gksu. os.execvp replaces this
    process, so on success this never returns."""
    script = os.path.abspath(__file__)
    python = sys.executable or "python3"

    # On Wayland desktops, programs running as root may not be allowed to
    # open windows on your screen. This grants that to the local root user
    # only, for this login session (a standard, narrowly-scoped fix).
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("xhost"):
        subprocess.run(["xhost", "+SI:localuser:root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # pkexec deliberately wipes the environment, including DISPLAY and
    # XAUTHORITY, so a GUI started through it can't reach your screen.
    # Hand the few variables the window needs back via 'env'.
    keep = {}
    for var in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
                "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES"):
        if os.environ.get(var):
            keep[var] = os.environ[var]
    if "XAUTHORITY" not in keep:
        default_xauth = os.path.expanduser("~/.Xauthority")
        if os.path.isfile(default_xauth):
            keep["XAUTHORITY"] = default_xauth
    env_args = ["env"] + ["%s=%s" % kv for kv in sorted(keep.items())]

    for elevator in ("pkexec", "kdesudo", "gksudo", "gksu"):
        if shutil.which(elevator) is None:
            continue
        cmd = [python, script] + sys.argv[1:]
        if elevator == "pkexec":
            cmd = env_args + cmd
        try:
            os.execvp(elevator, [elevator] + cmd)
        except OSError:
            continue

    _desktop_error(
        APP_TITLE,
        "This tool needs administrator rights to write to SD cards, but no graphical "
        "password prompt (pkexec) was found on this system.\n\n"
        "Either install it (Ubuntu/Mint/Debian: sudo apt install pkexec - on older "
        "versions the package is called policykit-1), or run this in a terminal:\n\n"
        "    sudo python3 \"%s\"" % script,
    )
    sys.exit(1)


def _terminate(signum, _frame):
    """SIGTERM/SIGHUP (logout, shutdown, 'kill', closing a terminal):
    unmount anything we mounted, then exit at once. Raising SystemExit
    isn't enough here - if a popup is open, Tkinter holds exceptions
    raised in its callbacks until the popup closes, which it never will."""
    try:
        log.warning("Received signal %d - cleaning up and exiting.", signum)
        stop_background_work(timeout=20)
        global_cleanup_safety_net()
        logging.shutdown()
    finally:
        os._exit(128 + signum)


def main():
    setup_logging()
    log.info("=== %s v%s starting (uid %d) ===", APP_TITLE, APP_VERSION, os.geteuid())
    if os.geteuid() != 0:
        relaunch_with_privileges()
        return  # pragma: no cover - execvp replaces this process on success

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _terminate)
        except (ValueError, OSError):
            pass

    try:
        app = PiInjectorApp()
    except tk.TclError as exc:
        _desktop_error(
            APP_TITLE,
            "Couldn't open the program window (%s).\n\nIf your desktop uses Wayland, open a "
            "terminal, run:\n    xhost +SI:localuser:root\nand then start the tool again." % exc,
        )
        sys.exit(1)

    missing = missing_required_tools()
    if missing:
        mb_error(
            APP_TITLE,
            "This computer is missing some standard system tools this program needs:\n\n    %s\n\n"
            "On Ubuntu/Mint/Debian they come from the 'util-linux' and 'mount' packages "
            "(sudo apt install util-linux mount)." % ", ".join(missing),
        )
        app.destroy()
        sys.exit(1)

    dialog = DisclaimerDialog(app)
    app.wait_window(dialog)
    if not dialog.accepted:
        log.info("Disclaimer declined - exiting.")
        app.destroy()
        return

    app.start()
    app.mainloop()


if __name__ == "__main__":
    main()
