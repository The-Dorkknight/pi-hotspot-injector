"""Tests for the optional KlipPlug install on the Build Klipper tab.

These need no SD card and no root: they exercise the code that finds,
unpacks and installs KlipPlug, using a made-up card folder and a stand-in
for the "run this step inside the card" function.

    python3 -m unittest discover -s tests
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import pi_injector as pi
except SystemExit:  # pi_injector exits when tkinter is missing
    pi = None

MARKED_BLOCK = ("# >>> KlipPlug (managed by Install-On-Printer.sh) >>>\n[update_manager klipplug]\n"
                "type: git_repo\n# <<< KlipPlug <<<\n")


def make_klipplug_folder(path):
    os.makedirs(os.path.join(path, "tools"))
    os.makedirs(os.path.join(path, "src", "klipplug"))
    for name in ("tools/printer_install.py", "src/klipplug/__main__.py", "README.md"):
        with open(os.path.join(path, name), "w") as f:
            f.write("# stand-in\n")
    with open(os.path.join(path, "Install-On-Printer.sh"), "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(os.path.join(path, "Install-On-Printer.sh"), 0o755)


def zip_folder(folder, zip_path, top="2026-10-07_1053_klipplug"):
    with zipfile.ZipFile(zip_path, "w") as zf:
        for dirpath, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(dirpath, name)
                zf.write(full, os.path.join(top, os.path.relpath(full, folder)))


@unittest.skipIf(pi is None, "pi_injector needs tkinter to import")
class KlipPlugOption(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pi-klipplug-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tooldir = os.path.join(self.tmp, "tool")
        self.root = os.path.join(self.tmp, "card")
        self.home = os.path.join(self.root, "home", "pi")
        os.makedirs(self.tooldir)
        os.makedirs(os.path.join(self.home, "printer_data", "config"))
        self.conf = os.path.join(self.home, "printer_data", "config", "moonraker.conf")
        with open(self.conf, "w") as f:
            f.write("[server]\nport: 7125\n")
        self.said = []
        self.uid, self.gid = os.getuid(), os.getgid()

    def say(self, text):
        self.said.append(text)

    def unit_paths(self):
        base = os.path.join(self.root, "etc", "systemd", "system")
        return os.path.join(base, "klipplug.service"), os.path.join(base, "multi-user.target.wants", "klipplug.service")

    def good_step(self, name, message, timeout=0, extra=()):
        """What KlipPlug's installer does on the card, as far as this tool checks."""
        self.assertEqual(name, "klipplug")
        unit, link = self.unit_paths()
        os.makedirs(os.path.dirname(link))
        with open(unit, "w") as f:
            f.write("[Unit]\n")
        os.symlink("/etc/systemd/system/klipplug.service", link)
        with open(self.conf, "a") as f:
            f.write("\n" + MARKED_BLOCK)

    # -- finding a local copy ------------------------------------------------
    def test_nothing_next_to_the_tool_means_no_local_copy(self):
        with open(os.path.join(self.tooldir, "klipplug-notes.txt"), "w") as f:
            f.write("not a package")
        os.makedirs(os.path.join(self.tooldir, "klipplug-empty"))
        self.assertIsNone(pi.find_local_klipplug(self.tooldir))

    def test_finds_a_folder_and_a_zip_and_prefers_the_newest(self):
        folder = os.path.join(self.tooldir, "2026-10-07_1053_klipplug")
        make_klipplug_folder(folder)
        self.assertEqual(pi.find_local_klipplug(self.tooldir), ("folder", folder))
        zip_path = os.path.join(self.tooldir, "klipplug-0.2.0-wip.zip")
        zip_folder(folder, zip_path)
        os.utime(zip_path, (os.path.getmtime(folder) + 100,) * 2)
        self.assertEqual(pi.find_local_klipplug(self.tooldir), ("zip", zip_path))

    # -- unpacking -----------------------------------------------------------
    def test_zip_with_one_top_folder_is_unpacked_and_keeps_run_permission(self):
        source = os.path.join(self.tmp, "src-folder")
        make_klipplug_folder(source)
        zip_path = os.path.join(self.tooldir, "klipplug.zip")
        zip_folder(source, zip_path)
        dest = os.path.join(self.home, "klipplug")
        pi._extract_klipplug_zip(zip_path, dest)
        self.assertTrue(pi._is_klipplug_folder(dest))
        self.assertTrue(os.access(os.path.join(dest, "Install-On-Printer.sh"), os.X_OK))
        self.assertFalse(os.path.exists(dest + ".unpack"))

    def test_zip_that_reaches_outside_is_refused_and_nothing_escapes(self):
        zip_path = os.path.join(self.tooldir, "klipplug-bad.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../escaped.txt", "x")
            zf.writestr("tools/printer_install.py", "x")
            zf.writestr("src/klipplug/__main__.py", "x")
        dest = os.path.join(self.home, "klipplug")
        with self.assertRaises(pi.InjectError):
            pi._extract_klipplug_zip(zip_path, dest)
        self.assertFalse(os.path.exists(os.path.join(self.root, "escaped.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "home", "escaped.txt")))
        self.assertFalse(os.path.exists(dest))

    def test_zip_links_are_not_recreated(self):
        zip_path = os.path.join(self.tooldir, "klipplug-link.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("tools/printer_install.py", "x")
            zf.writestr("src/klipplug/__main__.py", "x")
            link = zipfile.ZipInfo("passwd-link")
            link.external_attr = (0o120777 << 16)
            zf.writestr(link, "/etc/passwd")
        dest = os.path.join(self.home, "klipplug")
        pi._extract_klipplug_zip(zip_path, dest)
        self.assertFalse(os.path.lexists(os.path.join(dest, "passwd-link")))

    def test_zip_that_is_not_klipplug_is_refused(self):
        zip_path = os.path.join(self.tooldir, "klipplug-other.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "hello")
        with self.assertRaises(pi.InjectError):
            pi._extract_klipplug_zip(zip_path, os.path.join(self.home, "klipplug"))

    # -- the whole optional install -------------------------------------------
    def test_install_from_a_local_folder(self):
        make_klipplug_folder(os.path.join(self.tooldir, "klipplug"))
        installed, source = pi.install_klipplug(self.root, self.home, self.uid, self.gid, self.good_step, self.say,
                                                local_folder=self.tooldir)
        self.assertTrue(installed)
        self.assertIn("next to this tool", source)
        self.assertTrue(pi._is_klipplug_folder(os.path.join(self.home, "klipplug")))

    def test_local_copies_go_onto_the_card_without_their_git_folder(self):
        folder = os.path.join(self.tooldir, "klipplug")
        make_klipplug_folder(folder)
        os.makedirs(os.path.join(folder, ".git"))
        with open(os.path.join(folder, ".git", "config"), "w") as f:
            f.write('[remote "origin"]\n\turl = https://user:SECRET-TOKEN@github.com/x/klipplug.git\n')
        dest = os.path.join(self.home, "klipplug")
        pi.fetch_klipplug(dest, self.say, self.tooldir)
        self.assertTrue(pi._is_klipplug_folder(dest))
        self.assertFalse(os.path.exists(os.path.join(dest, ".git")))
        # and the same from a zip of that folder
        zip_path = os.path.join(self.tmp, "klipplug-with-git.zip")
        zip_folder(folder, zip_path)
        dest2 = os.path.join(self.home, "klipplug2")
        pi._extract_klipplug_zip(zip_path, dest2)
        self.assertTrue(pi._is_klipplug_folder(dest2))
        self.assertFalse(os.path.exists(os.path.join(dest2, ".git")))

    def test_failed_step_is_a_warning_not_an_error_and_is_tidied_up(self):
        make_klipplug_folder(os.path.join(self.tooldir, "klipplug"))

        def failing_step(name, message, timeout=0, extra=()):
            self.good_step(name, message)  # it got part-way...
            raise pi.InjectError("Build step 'klipplug' failed:\nsomething broke")

        installed, detail = pi.install_klipplug(self.root, self.home, self.uid, self.gid, failing_step, self.say,
                                                local_folder=self.tooldir)
        self.assertFalse(installed)
        self.assertIn("something broke", detail)
        unit, link = self.unit_paths()
        self.assertFalse(os.path.lexists(unit))
        self.assertFalse(os.path.lexists(link))
        with open(self.conf) as f:
            self.assertEqual(f.read().strip(), "[server]\nport: 7125")

    def test_step_that_leaves_no_service_counts_as_failed(self):
        make_klipplug_folder(os.path.join(self.tooldir, "klipplug"))
        installed, detail = pi.install_klipplug(self.root, self.home, self.uid, self.gid,
                                                lambda *a, **k: None, self.say, local_folder=self.tooldir)
        self.assertFalse(installed)
        self.assertIn("service isn't on the card", detail)

    def test_cancel_is_passed_on(self):
        make_klipplug_folder(os.path.join(self.tooldir, "klipplug"))

        def cancelled_step(name, message, timeout=0, extra=()):
            raise pi.CancelledError("Build cancelled.")

        with self.assertRaises(pi.CancelledError):
            pi.install_klipplug(self.root, self.home, self.uid, self.gid, cancelled_step, self.say,
                                local_folder=self.tooldir)

    # -- the script that runs inside the card ----------------------------------
    def test_build_script_has_the_klipplug_step_and_is_valid_bash(self):
        self.assertIn("klipplug) step_klipplug ;;", pi.BUILD_SCRIPT)
        self.assertIn('tools/printer_install.py image --user "${U}"', pi.BUILD_SCRIPT)
        if shutil.which("bash"):
            path = os.path.join(self.tmp, "build.sh")
            with open(path, "w") as f:
                f.write(pi.BUILD_SCRIPT)
            result = subprocess.run(["bash", "-n", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
