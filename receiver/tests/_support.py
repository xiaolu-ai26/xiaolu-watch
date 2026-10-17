"""Test helpers. Audio fixtures are synthesized with FFmpeg at test time (a sine tone),
so the repository ships no recorded audio. Secrets used in tests are public fixtures only."""
import atexit
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

RECEIVER = Path(__file__).resolve().parents[1]
REPO = RECEIVER.parent
sys.path.insert(0, str(RECEIVER))

_cache = {}
_tmp = None


def synthetic_audio(extension):
    """Return (bytes, duration_s) for a 16 kHz mono synthetic tone (.caf PCM or .m4a AAC)."""
    global _tmp
    if extension in _cache:
        return _cache[extension]
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise unittest.SkipTest('ffmpeg/ffprobe not installed')
    if _tmp is None:
        _tmp = tempfile.TemporaryDirectory(prefix='xiaolu-watch-fixture-')
        atexit.register(_tmp.cleanup)
    out = Path(_tmp.name) / ('tone' + extension)
    codec = ['-c:a', 'pcm_s16le'] if extension == '.caf' else ['-c:a', 'aac', '-b:a', '32k']
    subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000:duration=3',
                    '-ac', '1', '-ar', '16000', *codec, str(out)], check=True)
    info = json.loads(subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(out)],
                                     check=True, capture_output=True, text=True).stdout)
    _cache[extension] = (out.read_bytes(), round(float(info['format']['duration']), 3))
    return _cache[extension]
