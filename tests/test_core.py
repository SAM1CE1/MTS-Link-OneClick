"""Offline checks. Run: python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mts_backend as m


class UrlTests(unittest.TestCase):
    def test_supported_recording_links(self):
        for url in (
            'https://my.mts-link.ru/12345678/87654321/record-new/1234567890',
            'https://my.mts-link.ru/j/12345678/room/record-new/1234567890',
        ):
            self.assertEqual(m.parse_recording_url(url)[0], '1234567890')

    def test_rejects_untrusted_domains(self):
        for url in (
            'http://my.mts-link.ru/1/2/record-new/1234567890',
            'https://my.mts-link.ru.evil.example/record-new/1234567890',
            'https://example.org/record-new/1234567890',
        ):
            with self.assertRaises(ValueError):
                m.parse_recording_url(url)

    def test_record_file(self):
        _, rid, api = m.parse_recording_url(
            'https://my.mts-link.ru/j/12345678/room/record-new/1234567890/record-file/11223344')
        self.assertEqual(rid, '11223344')
        self.assertIn('/record-files/11223344/flow?', api)


class RecordingPlanTests(unittest.TestCase):
    def setUp(self):
        self.media = {
            'duration': 15.0,
            'eventLogs': [
                {'module': 'eventsession.start', 'time': 100.0, 'relativeTime': 0,
                 'snapshot': {'data': {'presentation': [
                     {'fileReference': {'slide': {'url': 'https://my.mts-link.ru/img/slide0.png'}}}
                 ], 'mediasession': []}}},
                {'module': 'presentation.update', 'relativeTime': 4.0,
                 'data': {'fileReference': {'slide': {'url': 'https://my.mts-link.ru/img/slide1.png'}}}},
                {'module': 'mediasession.add', 'relativeTime': 0.0,
                 'data': {'id': 1, 'url': 'https://events-storage.webinar.ru/voice.mp4',
                          'stream': {'conference': {'id': 1}}}},
                {'module': 'mediasession.add', 'relativeTime': 6.0,
                 'data': {'id': 2, 'url': 'https://events-storage.webinar.ru/screen.mp4',
                          'stream': {'screensharing': {'id': 2}}}},
            ]
        }

    def test_slides_timeline(self):
        timeline, stats = m.slideshow_timeline(self.media, 15.0)
        self.assertEqual([x[0] for x in timeline], [0.0, 4.0])
        self.assertEqual(stats['presentation_events'], 1)

    def test_media_roles(self):
        roles = [x['kind'] for x in m.audio_plan(self.media, 15.0)]
        self.assertEqual(roles, ['conference', 'screensharing'])


if __name__ == '__main__':
    unittest.main()
