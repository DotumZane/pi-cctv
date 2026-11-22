import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from cctv.recording import scrub_frames_dir
from cctv.scrub_overview import build_overview


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class ScrubOverviewTests(unittest.TestCase):
    def test_index_uses_existing_atlas_frames_and_keeps_their_times(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hour = int(time.time() // 3600) * 3600 - 7200
            for offset, color in [(0, 'red'), (120, 'blue')]:
                stamp = datetime.fromtimestamp(hour + offset).astimezone()
                source = root / 'cam-1' / stamp.strftime('%Y-%m-%d') / (stamp.strftime('%H-%M-%S') + '.mp4')
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b'recording data')
                os.utime(source, (time.time() - 30, time.time() - 30))
                atlas = scrub_frames_dir(root, source)
                atlas.mkdir(parents=True)
                (atlas / 'manifest.json').write_text(json.dumps({'width': 384, 'height': 216}))
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:s=2304x864',
                                '-frames:v', '1', '-y', str(atlas / '000.jpg')], check=True)
            result = build_overview(root, 'cam-1', hour)
            self.assertEqual(result['times'], [hour, hour + 120])
            self.assertEqual((result['width'], result['height']), (384, 216))
            image = base64.b64decode(result['image'])
            self.assertTrue(image.startswith(b'\xff\xd8'))
            sheet = root / 'sheet.jpg'
            sheet.write_bytes(image)
            for x, expected in [(0, 0), (384, 2)]:
                decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(sheet), '-vf', f'format=rgb24,crop=2:2:{x}:0',
                                          '-frames:v', '1', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
                self.assertGreater(decoded[expected], decoded[2 - expected] + 100)

    def test_empty_hour_is_a_valid_empty_index(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(build_overview(Path(temp), 'missing', 0), {'ready': True, 'times': [], 'image': None})


if __name__ == '__main__':
    unittest.main()
