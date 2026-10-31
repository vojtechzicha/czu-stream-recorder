import unittest
from datetime import datetime, timedelta

from recorder import silence

T0 = datetime(2026, 11, 21, 15, 30)
RULE = silence.SilenceStop(earliest=T0 + timedelta(minutes=5), window=timedelta(minutes=5), threshold_db=-45)


def seq(start, peaks, step=10):
    return [(start + timedelta(seconds=i * step), p) for i, p in enumerate(peaks)]


class ShouldStop(unittest.TestCase):
    def test_not_before_earliest(self):
        recent = seq(T0 - timedelta(minutes=10), [-70] * 60)
        self.assertIsNone(silence.should_stop(T0 + timedelta(minutes=4), RULE, recent, recent[-1][0], 10))

    def test_quiet_window_stops(self):
        recent = seq(T0, [-12] * 10 + [-70] * 30)            # speech, then 5 min of an empty room
        now = recent[-1][0]
        self.assertIn("room quiet", silence.should_stop(now, RULE, recent, now, 10))

    def test_one_loud_segment_keeps_going(self):
        recent = seq(T0, [-70] * 20 + [-15] + [-70] * 9)    # someone still talking 1.5 min ago
        now = recent[-1][0]
        self.assertIsNone(silence.should_stop(now, RULE, recent, now, 10))

    def test_speech_keeps_going(self):
        recent = seq(T0, [-14, -60, -20] * 20)               # pauses between sentences
        now = recent[-1][0]
        self.assertIsNone(silence.should_stop(now, RULE, recent, now, 10))

    def test_stream_stalled(self):
        recent = seq(T0, [-12] * 10)
        now = recent[-1][0] + timedelta(minutes=6)
        self.assertIn("no new segments", silence.should_stop(now, RULE, recent, recent[-1][0], 10))

    def test_unmeasured_counts_as_quiet(self):
        recent = seq(T0 + timedelta(minutes=1), [None] * 30)
        now = recent[-1][0]
        self.assertIsNotNone(silence.should_stop(now, RULE, recent, now, 10))


class TrimTail(unittest.TestCase):
    def names(self, start, n):
        return [f"{start + timedelta(seconds=10 * i):%Y%m%d%H%M%S}-{i:010d}.ts" for i in range(n)]

    def test_trims_quiet_tail_after_end(self):
        names = self.names(T0 - timedelta(minutes=1), 50)   # 6 before the end, 44 after
        levels = {n: (-15 if i < 20 else -70) for i, n in enumerate(names)}
        kept = silence.trim_tail(names, levels, T0, -45)
        self.assertEqual(kept, names[:23])                   # last loud = 19, plus 3

    def test_never_trims_before_scheduled_end(self):
        names = self.names(T0 - timedelta(minutes=10), 70)
        levels = {n: -70 for n in names}                     # silent all along (e.g. mic off)
        kept = silence.trim_tail(names, levels, T0, -45)
        self.assertEqual(len(kept), 60)                      # the 60 parts before 15:30 stay

    def test_unmeasured_parts_are_kept(self):
        names = self.names(T0, 10)
        self.assertEqual(silence.trim_tail(names, {}, T0, -45), names)

    def test_part_time(self):
        self.assertEqual(silence.part_time("20261121153005-0000012345.ts"), datetime(2026, 11, 21, 15, 30, 5))


if __name__ == "__main__":
    unittest.main()
