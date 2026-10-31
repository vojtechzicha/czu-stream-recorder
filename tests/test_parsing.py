import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from recorder import hls, schedule

BASE = "https://example.cz/live/smil:x.smil/playlist.m3u8"

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=4256000,RESOLUTION=1920x1080
chunklist_w1_b4256000_slcs.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2756000,RESOLUTION=1280x720
chunklist_w1_b2756000_slcs.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=946000,RESOLUTION=640x360,CODECS="avc1.4d401f,mp4a.40.2"
chunklist_w1_b946000_slcs.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:11
#EXT-X-MEDIA-SEQUENCE:9598
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXTINF:10.0,
media_w1_b4256000_slcs_9598.ts
#EXTINF:9.966,
media_w1_b4256000_slcs_9599.ts
#EXTINF:10.0,
media_w1_b4256000_slcs_9600.ts
"""


class HlsTest(unittest.TestCase):
    def test_master(self):
        vs = hls.parse_master(MASTER, BASE)
        self.assertEqual([v.height for v in vs], [1080, 720, 360])
        self.assertEqual(vs[2].bandwidth, 946000)  # quoted CODECS with a comma
        self.assertEqual(vs[0].url, "https://example.cz/live/smil:x.smil/chunklist_w1_b4256000_slcs.m3u8")
        self.assertEqual(hls.pick_variant(vs, None).height, 1080)
        self.assertEqual(hls.pick_variant(vs, 720).height, 720)
        self.assertEqual(hls.pick_variant(vs, 100).height, 1080)  # nothing fits → best

    def test_media(self):
        pl = hls.parse_media(MEDIA, BASE)
        self.assertEqual([s.seq for s in pl.segments], [9598, 9599, 9600])
        self.assertAlmostEqual(pl.segments[1].duration, 9.966)
        self.assertEqual(pl.target_duration, 11)
        self.assertFalse(pl.ended)
        self.assertTrue(pl.segments[0].url.endswith("/smil:x.smil/media_w1_b4256000_slcs_9598.ts"))


class ScheduleTest(unittest.TestCase):
    def test_load(self):
        toml = """
[defaults]
stream = "ks"
tail_minutes = 20
[streams]
ks = "https://example.cz/p.m3u8"
[[recording]]
title = "Seminář"
start = 2026-11-21T08:45:00
end = 2026-11-21T15:30:00
[[recording]]
title = "Other/room: X"
stream = "https://other.cz/p.m3u8"
start = 2026-12-12T08:45:00
end = 2026-12-12T09:45:00
lead_minutes = 0
video = "Plex/S01 Předmět/s01e02.Cvičení"
notes = "Předmět/2026-12-12 Cvičení"
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.toml"
            p.write_text(toml, encoding="utf-8")
            _, recs = schedule.load(p)
        a, b = recs
        self.assertEqual(a.stream_url, "https://example.cz/p.m3u8")
        self.assertEqual(a.window_start, datetime(2026, 11, 21, 8, 35))
        self.assertEqual(a.window_end, datetime(2026, 11, 21, 15, 50))
        self.assertEqual(a.basename, "2026-11-21 Seminář")
        self.assertEqual(a.id, "20261121-0845-seminar")
        self.assertEqual(b.lead, timedelta(0))
        self.assertEqual(b.basename, "2026-12-12 Other-room- X")
        self.assertEqual(a.video_path, "2026-11-21 Seminář")
        self.assertEqual(b.video_path, "Plex/S01 Předmět/s01e02.Cvičení")
        self.assertEqual(b.notes_path, "Předmět/2026-12-12 Cvičení")

    def test_bad_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.toml"
            p.write_text('[[recording]]\ntitle="x"\nstream="https://e.cz/p.m3u8"\nstart=2026-01-01T10:00:00\n'
                         'end=2026-01-01T11:00:00\nvideo="../../etc/x"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                schedule.load(p)

    def test_unknown_stream(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.toml"
            p.write_text('[[recording]]\ntitle="x"\nstream="nope"\nstart=2026-01-01T10:00:00\n'
                         'end=2026-01-01T11:00:00\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                schedule.load(p)


if __name__ == "__main__":
    unittest.main()
