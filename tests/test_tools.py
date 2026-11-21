import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recorder import tools


class Find(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        exe = self.dir / "ffmpeg"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_launchd_path_falls_back(self):
        # launchd/cron: PATH without Homebrew; the binary sits in a fallback folder
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertEqual(tools.find("ffmpeg", (str(self.dir),)), str(self.dir / "ffmpeg"))

    def test_path_wins(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.dir)}):
            self.assertEqual(tools.find("ffmpeg", ()), str(self.dir / "ffmpeg"))

    def test_not_installed(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIsNone(tools.find("ffprobe", (str(self.dir),)))
            with mock.patch.object(tools, "FALLBACK_DIRS", (str(self.dir),)):
                self.assertEqual(tools.cmd("ffprobe"), "ffprobe")

    def test_not_executable_is_skipped(self):
        (self.dir / "ffprobe").write_text("x")
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIsNone(tools.find("ffprobe", (str(self.dir),)))


if __name__ == "__main__":
    unittest.main()
