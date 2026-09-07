"""Offline smoke check inside the built image, with real FFmpeg and Deno."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, '/app')

import landlock  # noqa: E402
import worker_rpc  # noqa: E402


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, **kwargs)


assert landlock.abi_version() >= 3, 'Landlock ABI 3 is required by the media worker'
assert asyncio.run(worker_rpc.supported_urls([])) == []
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    media = root / 'media'
    media.mkdir()
    ff_temp = root / 'ffmpeg'
    ff_temp.mkdir()
    source = media / 'input.mp4'
    result = run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=1',
                  '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)])
    assert result.returncode == 0, result.stderr
    env = {**os.environ, 'YTDLP_WORKER_SANDBOX': '1', 'YTDLP_MEDIA_ROOT': str(media),
           'YTDLP_FFMPEG_NETWORK': '0', 'YTDLP_FFMPEG_TMP': str(ff_temp)}
    result = run(['/app/ffmpeg_guard/ffmpeg', '-v', 'error', '-i', str(source),
                  '-c', 'copy', '-movflags', '+faststart', str(media / 'output.mp4')], env=env)
    assert result.returncode == 0, result.stderr
    result = run(['/app/ffmpeg_guard/ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name',
                  '-of', 'csv=p=0', str(media / 'output.mp4')], env=env)
    assert result.returncode == 0 and 'h264' in result.stdout, result.stderr
    outside = root / 'other-job.mp4'
    outside.write_bytes(source.read_bytes())
    concat = media / 'input.txt'
    concat.write_text(f"file '{outside}'\n")
    result = run(['/app/ffmpeg_guard/ffmpeg', '-v', 'error', '-f', 'concat', '-safe', '0',
                  '-i', str(concat), '-c', 'copy', str(media / 'forbidden.mp4')], env=env)
    assert result.returncode != 0, 'FFmpeg read another job through a playlist'
    deno_code = ('import os,sys; from pathlib import Path; from landlock import restrict,system_paths; '
                 'restrict(system_paths(), [Path(sys.argv[1])]); '
                 'os.execvp("deno", ["deno", "eval", "console.log(42)"])')
    result = run([sys.executable, '-c', deno_code, str(root)], env={**os.environ, 'DENO_DIR': str(root / 'deno')})
    assert result.returncode == 0 and result.stdout.strip() == '42', result.stderr
print('Worker, FFmpeg, FFprobe and Deno isolation checks passed')
