# Contributing

Thanks for wanting to help! Bug reports are the most useful thing you can send.

## Reporting a problem

Open an [issue](https://github.com/The-Dorkknight/pi-hotspot-injector/issues/new/choose) and include:

1. What you were doing and what you expected.
2. Your PC's Linux version, and the Pi model.
3. The log file, `~/pi_injector.log`. It never contains passwords. Please attach it as a file.
4. If the Pi itself misbehaves: **Extras → Add diagnostics**, boot the Pi, then attach `XXXXX_DIAGNOSTICS.txt` from the card's boot partition.

A tip if the log looks empty after moving it by USB stick: eject the stick properly first. Linux keeps writes in memory until you do.

## Code changes

- It's deliberately a single file with no dependencies beyond Python's standard library and common Linux tools. Please keep it that way.
- Safety comes first: anything that writes to a disk must go through the existing checks (removable, 8–32 GB, two confirmations, re-check before writing). Please don't loosen those.
- Never log passwords.
- Test with loop devices or a spare card, never a drive you care about.
- Small, focused pull requests are easiest to review.

## Licence of contributions

By submitting a contribution you agree it may be distributed under this project's licence (PolyForm Noncommercial 1.0.0), and that the project owner may also offer it under other terms (for example a commercial licence).
