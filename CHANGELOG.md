# Changelog

## 2.3.0
- New **Clone & Backup** tab: save a finished card as an image (optionally shrunk and compressed) and write copies onto other cards. Copies are grown to fill the card and can be made into a separate Pi (own hostname, machine ID and SSH keys).
- Build and hotspot "done" messages now show the web address (`http://<hostname>.local` and the IP), and the Hotspot tab reminds you to use a different hotspot name per Pi and to "Forget network" on phones.

## 2.2.5
- No more false "optional package couldn't be installed" warning when a ready-made Python package isn't in apt but pip installs it anyway.

## 2.2.4
- **Download manager:** 10 MB checkpoints, automatic reconnect and resume after dropouts, pause/resume across restarts, checksum verification. Also used for the Mainsail download.

## 2.2.3
- Network dropouts during the build no longer fail it: git clones are retried, and a clear "press Build again, it carries on" message appears if the connection stays down.

## 2.2.2
- Builds started from Raspberry Pi OS *with desktop* now boot without the desktop, leaving memory for Klipper.

## 2.2.0 – 2.2.1
- **Build Klipper** tab: install Klipper, Moonraker, Mainsail (and optionally Crowsnest) onto clean Raspberry Pi OS Lite from your PC, compiled for the oldest Pi so one card works in every Pi. Test-starts Moonraker before finishing.
- **Download** button for the correct Raspberry Pi OS Lite image, checked against its published checksum.
- Cards holding a PC installer ISO are recognised and explained.

## 2.1.0
- **Write Image** tab limited to removable 8–32 GB drives, with two sanity checks and read-back verification.
- Start-up disclaimer with optional licence view; PolyForm Noncommercial 1.0.0 licence.
- Pi Zero / Zero W / Pi 1 (ARMv6) fix for images built for newer Pis.
- Double-click launcher for all Linux desktops.
