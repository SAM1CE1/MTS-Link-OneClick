"""FFmpeg/Pillow integration test. Uses locally generated sample media only."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mts_backend as m

@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow not installed')
class RenderTests(unittest.TestCase):
    def test_slides_and_screen_and_audio(self):
        import subprocess
        from PIL import Image, ImageDraw
        ffmpeg = m.require_ffmpeg()
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            voice = parent / 'voice.m4a'
            screen = parent / 'screen.mp4'
            subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'sine=frequency=550:sample_rate=32000:duration=8',
                            '-c:a', 'aac', str(voice)], check=True)
            subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'color=c=blue:s=640x360:r=15:d=2',
                            '-c:v', 'libx264', str(screen)], check=True)
            base = 'https://my.mts-link.ru/assets/'
            data = {'name': 'Offline synthetic test', 'duration': 8.0, 'eventLogs': [
                {'module': 'eventsession.start', 'time': 100, 'relativeTime': 0,
                 'snapshot': {'data': {'presentation': [
                    {'fileReference': {'slide': {'url': base+'slide0'}}}],
                    'mediasession': []}}},
                {'module': 'mediasession.add', 'relativeTime': 0,
                 'data': {'url': base+'mic', 'id': 1, 'stream': {'conference': {'id': 4}}}},
                {'module': 'presentation.update', 'relativeTime': 3,
                 'data': {'fileReference': {'slide': {'url': base+'slide1'}}}},
                {'module': 'mediasession.add', 'relativeTime': 4,
                 'data': {'url': base+'screen', 'id': 2, 'stream': {'screensharing': {'id': 5}}}},
            ]}
            def save_slide(url, path):
                img = Image.new('RGB', (1280, 720), 'yellow' if url.endswith('slide0') else 'red')
                ImageDraw.Draw(img).rectangle((20, 20, 280, 150), fill='white')
                img.save(path)
            def extract_audio(ff, media, folder):
                return voice if media['url'].endswith('mic') else None
            with patch.object(m, 'get_metadata', return_value=(data,8.0)), \
                 patch.object(m, 'save_slide', side_effect=save_slide), \
                 patch.object(m, 'extract_one_audio', side_effect=extract_audio), \
                 patch.object(m, 'download_screen_video', return_value=screen):
                result = m.run_recording(
                    'https://my.mts-link.ru/j/12345678/example/record-new/1234567890', parent)
                self.assertGreater(result.stat().st_size, 10000)
                m.verify_audio_track(ffmpeg, result)
                for second, color in ((1, 'yellow'), (5, 'blue'), (7, 'red')):
                    picture = parent / f'frame_{second}.png'
                    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                                    '-ss', str(second), '-i', str(result), '-frames:v', '1', str(picture)], check=True)
                    rgb = Image.open(picture).getpixel((640,360))
                    if color == 'yellow':
                        self.assertGreater(rgb[0], 160);self.assertGreater(rgb[1], 140);self.assertLess(rgb[2], 120)
                    elif color == 'blue':
                        self.assertGreater(rgb[2], 160);self.assertLess(rgb[0], 120)
                    else:
                        self.assertGreater(rgb[0], 160);self.assertLess(rgb[1], 120)

