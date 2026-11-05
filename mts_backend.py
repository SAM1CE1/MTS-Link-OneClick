# -*- coding: utf-8 -*-
"""MTS Link OneClick: presentation/screenshare + audio, never webcams in video."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter, deque
from pathlib import Path

SESSION_ID = ''
API = ''
OUTPUT_PARENT = None
REPORT_CALLBACK = None
CANCEL_EVENT = None
SAMPLE_RATE = 32000
DURATION_LIMIT = 24 * 3600
VIDEO_WIDTH, VIDEO_HEIGHT = 1280, 720
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'


class Cancelled(Exception):
    pass


def check_cancel():
    if CANCEL_EVENT is not None and CANCEL_EVENT.is_set():
        raise Cancelled('Операция остановлена пользователем.')


def say(message):
    check_cancel()
    if REPORT_CALLBACK is not None:
        REPORT_CALLBACK(str(message))
    else:
        print(message, flush=True)


def parse_recording_url(value):
    """Parse public MTS Link recording links and optional record-file IDs."""
    value = (value or '').strip()
    p = urllib.parse.urlparse(value)
    if p.scheme != 'https' or p.hostname not in ('my.mts-link.ru', 'events.webinar.ru', 'webinar.ru'):
        raise ValueError('Нужна ссылка https://my.mts-link.ru/.../record-new/ID')
    if p.username or p.password:
        raise ValueError('Неверный адрес записи.')
    m = re.search(r'/record(?:-new)?/(\d+)(?:/record-file/(\d+))?(?:/([^/]+))?/?$', p.path)
    if not m:
        raise ValueError('Не удалось найти ID записи в ссылке. Проверь ссылку на запись.')
    event_id, record_file, token = m.groups()
    if record_file:
        endpoint = f'https://my.mts-link.ru/api/event-sessions/{event_id}/record-files/{record_file}/flow?withoutCuts=false'
    else:
        endpoint = f'https://my.mts-link.ru/api/eventsessions/{event_id}/record?withoutCuts=false'
    if token:
        endpoint += '&' + urllib.parse.urlencode({'recordAccessToken': urllib.parse.unquote(token)})
    return event_id, record_file, endpoint



def require_ffmpeg():
    binary = shutil.which('ffmpeg')
    if binary:
        return binary
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError('FFmpeg not found. First run: py -m pip install imageio-ffmpeg')


def require_pillow():
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise RuntimeError('Pillow not found. Run: py -m pip install Pillow')


def work_directory():
    folder = Path(OUTPUT_PARENT) if OUTPUT_PARENT is not None else Path.home() / 'Downloads'
    if not folder.is_dir():
        folder = Path(__file__).resolve().parent
    out = folder / f'MTS_Link_{SESSION_ID}'
    out.mkdir(parents=True, exist_ok=True)
    return out


def valid_source_url(url):
    if not isinstance(url, str):
        raise RuntimeError('Invalid media URL returned by the recording metadata.')
    u = urllib.parse.urlparse(url)
    host = (u.hostname or '').lower()
    permitted = (host == 'mts-link.ru' or host.endswith('.mts-link.ru') or
                 host == 'webinar.ru' or host.endswith('.webinar.ru'))
    if u.scheme != 'https' or not permitted or u.username or u.password:
        raise RuntimeError('Unexpected MTS Link media URL host: ' + host)
    return url


def get_metadata():
    say('Reading MTS Link recording metadata (no camera downloads)...')
    req = urllib.request.Request(API, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=50) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError('Нет доступа к записи (HTTP %s). Запись может требовать авторизацию или разрешение организатора.' % exc.code)
        raise RuntimeError('Сервер МТС Линк ответил HTTP %s.' % exc.code)
    except Exception as exc:
        raise RuntimeError('Не удалось получить данные записи: ' + str(exc))
    if not isinstance(data, dict) or not isinstance(data.get('eventLogs'), list):
        raise RuntimeError('MTS Link API returned an unexpected structure.')
    duration = float(data.get('duration') or 0)
    if not math.isfinite(duration) or not 0 < duration <= DURATION_LIMIT:
        raise RuntimeError('Invalid recording duration in metadata.')
    return data, duration


def first_slide_url(obj):
    if isinstance(obj, dict):
        ref = obj.get('fileReference')
        if isinstance(ref, dict):
            slide = ref.get('slide')
            if isinstance(slide, dict) and isinstance(slide.get('url'), str):
                return slide['url']
        slide = obj.get('slide')
        if isinstance(slide, dict) and isinstance(slide.get('url'), str):
            return slide['url']
        for k in ('presentation', 'current', 'data', 'value', 'fileReference'):
            if k in obj:
                found = first_slide_url(obj[k])
                if found:
                    return found
    elif isinstance(obj, list):
        for v in obj:
            found = first_slide_url(v)
            if found:
                return found
    return None


def slideshow_timeline(data, duration):
    evs = data['eventLogs']
    changes = []
    snapshot = (evs[0].get('snapshot') or {}).get('data') if evs else {}
    initial = first_slide_url((snapshot or {}).get('presentation', []))
    if initial:
        changes.append((0.0, valid_source_url(initial), 'snapshot'))
    total = missing = 0
    for e in evs:
        if not isinstance(e, dict) or e.get('module') != 'presentation.update':
            continue
        total += 1
        url = first_slide_url(e.get('data'))
        if not url:
            missing += 1
            continue
        try:
            when = float(e.get('relativeTime', 0))
        except (ValueError, TypeError):
            missing += 1
            continue
        if math.isfinite(when) and 0 <= when < duration:
            changes.append((when, valid_source_url(url), 'presentation.update'))
    changes.sort(key=lambda v: v[0])
    tidy = []
    for t, url, kind in changes:
        if tidy and abs(t - tidy[-1][0]) < 0.001:
            tidy[-1] = (t, url, kind)
        elif not tidy or tidy[-1][1] != url:
            tidy.append((t, url, kind))
    if not tidy:
        return [], {'presentation_events': total, 'missing': missing,
                    'initial_snapshot': bool(initial), 'assumed_start': False}
    assumed_start = False
    if tidy[0][0] > 0.01:
        tidy.insert(0, (0.0, tidy[0][1], 'assumed until first slide'))
        assumed_start = True
    return tidy, {'presentation_events': total, 'missing': missing,
                  'initial_snapshot': bool(initial), 'assumed_start': assumed_start}


def audio_plan(data, duration):
    """Interpret mediasession objects from initial snapshot and subsequent additions."""
    evs = data['eventLogs']
    if not evs:
        return []
    origin = float(evs[0].get('time') or 0) - float(evs[0].get('relativeTime') or 0)
    unique = {}

    def add_media(entry, when, origin_label):
        if not isinstance(entry, dict):
            return
        url = entry.get('url') or entry.get('hlsUrl')
        if not url:
            return
        try:
            when = float(when)
        except (ValueError, TypeError):
            return
        if not math.isfinite(when) or when >= duration or when < -DURATION_LIMIT:
            return
        url = valid_source_url(url)
        key = str(entry.get('id') or url)
        streams = entry.get('stream') or {}
        if not isinstance(streams, dict):
            streams = {}
        screen_kind = 'screensharing' if 'screensharing' in streams else 'conference' if 'conference' in streams else 'unknown'
        conference = streams.get('conference')
        conference_id = conference.get('id') if isinstance(conference, dict) else None
        unique[key] = {'key': key, 'url': url, 'start': when, 'source': origin_label,
                       'conference_id': str(conference_id) if conference_id is not None else None,
                       'kind': screen_kind}

    snap = (evs[0].get('snapshot') or {}).get('data') or {}
    for media in snap.get('mediasession') or []:
        ts = media.get('time') if isinstance(media, dict) else None
        add_media(media, float(ts) - origin if ts is not None else 0, 'snapshot')
    for e in evs:
        if not isinstance(e, dict) or e.get('module') not in ('mediasession.add', 'screensharing.stream.add'):
            continue
        add_media(e.get('data'), e.get('relativeTime', 0), 'mediasession.add')
    # The same URL can appear in a snapshot and in an add event; keep the actual event.
    urls = {}
    for v in unique.values():
        previous = urls.get(v['url'])
        if previous is None or (previous['source'] == 'snapshot' and v['source'] == 'mediasession.add'):
            urls[v['url']] = v
    return sorted(urls.values(), key=lambda v: v['start'])


def save_slide(url, path):
    from PIL import Image, ImageOps
    req = urllib.request.Request(valid_source_url(url), headers={
        'User-Agent': USER_AGENT, 'Accept': 'image/*,*/*;q=0.8'})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            content = r.read(26_000_001)
    except Exception as exc:
        raise RuntimeError('Slide image download failed: ' + str(exc))
    if len(content) > 26_000_000:
        raise RuntimeError('A slide image exceeded the 26 MB safety limit.')
    try:
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            im = im.convert('RGB')
            frame = ImageOps.contain(im, (VIDEO_WIDTH, VIDEO_HEIGHT), method=Image.Resampling.LANCZOS)
            canvas = Image.new('RGB', (VIDEO_WIDTH, VIDEO_HEIGHT), (245, 245, 245))
            canvas.paste(frame, ((VIDEO_WIDTH - frame.width) // 2, (VIDEO_HEIGHT - frame.height) // 2))
            canvas.save(path, 'PNG')
    except Exception as exc:
        raise RuntimeError('Slide URL did not return a valid image: ' + str(exc))


def slides_preview(paths, outfile):
    from PIL import Image, ImageDraw, ImageStat
    paths = list(paths)
    previews = paths[:min(12, len(paths))]
    if not paths:
        raise RuntimeError('No slide images available for a preview.')
    sheet = Image.new('RGB', (960, 210 * math.ceil(len(previews) / 3)), (220, 220, 220))
    non_blank = 0
    for i, path in enumerate(paths):
        with Image.open(path) as im:
            stat = ImageStat.Stat(im.resize((32, 18)))
            # A uniform black or near-white slide may be legitimate, but an entire
            # deck of them is suspicious. Use variance, not just brightness.
            if max(stat.stddev) >= 3.5:
                non_blank += 1
            if i >= len(previews):
                continue
            thumb = im.copy()
            thumb.thumbnail((310, 176))
            x = i % 3 * 320 + (320 - thumb.width) // 2
            y = i // 3 * 210 + 4
            sheet.paste(thumb, (x, y))
            ImageDraw.Draw(sheet).text((i % 3 * 320 + 10, i // 3 * 210 + 185),
                                        f'Slide {i + 1}', fill=(0, 0, 0))
    sheet.save(outfile, 'JPEG', quality=88)
    if non_blank == 0:
        raise RuntimeError('All slide images look uniform/blank. Open SLIDE_PREVIEW.jpg; no MP4 was created.')


def prepare_slides(timeline, assets):
    slide_dir = assets / 'slides'
    slide_dir.mkdir(parents=True, exist_ok=True)
    mapping = {}
    urls = list(dict.fromkeys(url for _, url, _ in timeline))
    if not urls:
        return {}
    say(f'Downloading slide images: {len(urls)} unique URL(s). No webcams.')
    for idx, url in enumerate(urls, 1):
        check_cancel()
        key = hashlib.sha256(url.encode('utf8')).hexdigest()[:16]
        path = slide_dir / f'slide_{idx:03d}_{key}.png'
        if not (path.exists() and path.stat().st_size > 500):
            say(f'  Slide {idx}/{len(urls)}')
            save_slide(url, path)
        mapping[url] = path
    slides_preview(mapping.values(), assets / 'SLIDE_PREVIEW.jpg')
    return mapping


def ffmpeg_tail(args, stage, elapsed_total=None, timeout=None):
    """Run without shell, show compact ongoing progress and preserve the latest error lines."""
    args = list(args)
    say(stage)
    if '-progress' not in args:
        args[-1:-1] = ['-nostats', '-stats_period', '5', '-progress', 'pipe:1']
    process = subprocess.Popen(args, text=True, encoding='utf-8', errors='replace',
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    last = deque(maxlen=25)
    prev = -1
    for raw in process.stdout:
        if CANCEL_EVENT is not None and CANCEL_EVENT.is_set():
            process.kill()
            process.wait()
            raise Cancelled('Операция остановлена пользователем.')
        s = raw.strip()
        if not s:
            continue
        last.append(s)
        if s.startswith('out_time=') and elapsed_total:
            try:
                h, m, sec = s.partition('=')[2].split(':')
                pos = float(h)*3600 + float(m)*60 + float(sec)
                step = int(pos // 120)
                if step > prev:
                    prev = step
                    say(f'  {min(pos/elapsed_total*100,100):.0f}% ({pos/60:.1f} / {elapsed_total/60:.1f} min)')
            except (ValueError, ZeroDivisionError):
                pass
    returncode = process.wait(timeout=timeout)
    if process.stdout is not None:
        process.stdout.close()
    if returncode != 0:
        # Do not include signed URLs in error messages.
        msg = re.sub(r'https?://\S+', '[URL hidden]', '\n'.join(last))
        raise RuntimeError(f'{stage} FAILED (ffmpeg exit={returncode}):\n{msg[-1800:]}')


def audio_cache_name(media):
    return 'voice_' + hashlib.sha256(media['url'].encode('utf8')).hexdigest()[:18]


def run_external_cancellable(cmd, wait_interval=1):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace')
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=wait_interval)
                return process.returncode, output
            except subprocess.TimeoutExpired:
                if CANCEL_EVENT is not None and CANCEL_EVENT.is_set():
                    process.kill()
                    process.communicate()
                    raise Cancelled('Операция остановлена пользователем.')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def extract_one_audio(ffmpeg, media, folder):
    dest = folder / (audio_cache_name(media) + '.m4a')
    nonefile = folder / (audio_cache_name(media) + '.no-audio')
    if dest.is_file() and dest.stat().st_size > 4096:
        return dest
    if nonefile.is_file():
        return None
    tmp = dest.with_suffix('.partial.m4a')
    tmp.unlink(missing_ok=True)
    # Stream is read over HTTPS, but video is never saved; only audio is encoded.
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
           '-i', media['url'], '-map', '0:a:0', '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE),
           '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', str(tmp)]
    exit_code, output_text = run_external_cancellable(cmd)
    if exit_code != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        if ('matches no streams' in output_text or 'does not contain any stream' in output_text or
                'Output file #0 does not contain any stream' in output_text):
            nonefile.write_text('Source has no audio track.\n', encoding='utf8')
            return None
        msg = re.sub(r'https?://\S+', '[URL hidden]', output_text)
        raise RuntimeError('Cannot extract audio from MTS Link: ' + msg[-1200:])
    if tmp.stat().st_size <= 4096:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return dest


def collect_audio(ffmpeg, data, duration, assets):
    media = audio_plan(data, duration)
    if not media:
        raise RuntimeError('No media sessions found in the recording metadata. No audio source.')
    audio_dir = assets / 'audio'
    audio_dir.mkdir(parents=True, exist_ok=True)
    say(f'Checking {len(media)} source stream(s) for audio. Media downloaded for decoding only.')
    output = []
    for i, entry in enumerate(media, 1):
        check_cancel()
        say(f'  Audio stream {i}/{len(media)} (start {entry["start"]:.1f}s)')
        file = extract_one_audio(ffmpeg, entry, audio_dir)
        if file:
            output.append((file, entry))
    if not output:
        raise RuntimeError('None of the available streams had an audio track. No MP4 assembled.')
    return output


def create_audio(ffmpeg, clips, duration, assets):
    result = assets / 'recording_audio.m4a'
    if result.is_file() and result.stat().st_size > 10000:
        say('Using previously assembled audio cache.')
        return result
    inputs = [ffmpeg, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
              '-f', 'lavfi', '-i', f'anullsrc=r={SAMPLE_RATE}:cl=mono']
    filters = [f'[0:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[sil]']
    labels = ['[sil]']
    for i,(path, entry) in enumerate(clips, 1):
        inputs += ['-i', str(path)]
        start = entry['start']
        trim = max(0.0, -start)
        delay_ms = round(max(0.0, start) * 1000)
        filters.append(f'[{i}:a]aresample={SAMPLE_RATE}:async=1:first_pts=0,'
                       f'atrim=start={trim:.3f},asetpts=PTS-STARTPTS,'
                       f'adelay={delay_ms}:all=1[a{i}]')
        labels.append(f'[a{i}]')
    n = len(labels)
    filters.append(''.join(labels) + f'amix=inputs={n}:duration=first:'
                   'dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]')
    cmd = inputs + ['-filter_complex', ';'.join(filters), '-map', '[aout]',
                    '-c:a', 'aac', '-b:a', '112k', '-t', f'{duration:.3f}',
                    '-movflags', '+faststart', str(result.with_suffix('.partial.m4a'))]
    tmp = result.with_suffix('.partial.m4a')
    tmp.unlink(missing_ok=True)
    ffmpeg_tail(cmd, 'Mixing voice tracks along original recording timeline...', duration)
    if not tmp.is_file() or tmp.stat().st_size < 10000:
        raise RuntimeError('Audio assembly did not produce a valid file.')
    tmp.replace(result)
    return result


def make_concat(timeline, images, duration, assets):
    path = assets / 'slides.ffconcat'
    with path.open('w', encoding='utf-8', newline='\n') as f:
        f.write('ffconcat version 1.0\n')
        for idx, (t, url, _) in enumerate(timeline):
            following = timeline[idx+1][0] if idx+1 < len(timeline) else duration
            period = following - t
            if period <= 0.001:
                continue
            filename = images[url].name
            f.write(f"file 'slides/{filename}'\n")
            f.write('duration {:.6f}\n'.format(period))
        f.write(f"file 'slides/{images[timeline[-1][1]].name}'\n")
    return path


def verify_audio_track(ffmpeg, path):
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-t', '1', '-i', str(path),
           '-map', '0:a:0', '-vn', '-f', 'null', 'NUL' if os.name == 'nt' else '/dev/null']
    done = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if done.returncode != 0:
        raise RuntimeError('Final MP4 has no readable audio stream. ' + done.stdout[-500:])


def download_screen_video(ffmpeg, media, folder):
    """Download only actual screensharing video. Never cache conference webcam video."""
    key = hashlib.sha256(media['url'].encode('utf8')).hexdigest()[:18]
    out = folder / ('screenshare_' + key + '.mp4')
    if out.is_file() and out.stat().st_size > 8192:
        return out
    temp = folder / ('screenshare_' + key + '.partial.mp4')
    temp.unlink(missing_ok=True)
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
           '-i', media['url'], '-map', '0:v:0', '-an', '-c:v', 'copy',
           '-movflags', '+faststart', str(temp)]
    exit_code, output_text = run_external_cancellable(cmd)
    if exit_code != 0 or not temp.exists() or temp.stat().st_size < 8192:
        temp.unlink(missing_ok=True)
        error = re.sub(r'https?://\S+', '[URL hidden]', output_text)
        raise RuntimeError('Screen share stream could not be retrieved: ' + error[-900:])
    temp.replace(out)
    return out


def find_duration(ffmpeg, path):
    """Obtain mp4 duration without ffprobe: bundled imageio-ffmpeg also works."""
    p = subprocess.run([ffmpeg, '-hide_banner', '-i', str(path)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', p.stderr)
    if not m:
        raise RuntimeError('Cannot determine screensharing clip duration: ' + path.name)
    return int(m[1])*3600 + int(m[2])*60 + float(m[3])


def collect_screens(ffmpeg, media, duration, assets):
    screens = [m for m in media if m['kind'] == 'screensharing']
    if not screens:
        return []
    folder = assets / 'screensharing_only'
    folder.mkdir(exist_ok=True)
    result = []
    for i, entry in enumerate(screens, 1):
        check_cancel()
        say(f'Downloading actual SCREENSHARE stream {i}/{len(screens)} (no webcam video)...')
        file = download_screen_video(ffmpeg, entry, folder)
        length = find_duration(ffmpeg, file)
        if length <= 0.1:
            raise RuntimeError('A screenshare clip has zero duration.')
        start = float(entry['start'])
        end = min(duration, start+length)
        if end <= max(0, start):
            continue
        result.append({'file': file, 'start': start, 'end': end, 'length': length})
    return sorted(result, key=lambda m: m['start'])


def create_mp4(ffmpeg, timeline, images, screens, audio, duration, folder, assets):
    """Slides show as base layer; genuine screenshare overrides slides while active."""
    if not images and not screens:
        raise RuntimeError('No presentation slides OR screen-sharing streams. No video made.')
    output = folder / 'PRESENTATION_OR_SCREEN_WITH_AUDIO_NO_WEBCAMS.mp4'
    tmp = folder / '_VIDEO_IN_PROGRESS.mp4'
    tmp.unlink(missing_ok=True)
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error']
    if images:
        concat = make_concat(timeline, images, duration, assets)
        cmd += ['-f', 'concat', '-safe', '0', '-i', str(concat)]
    else:
        cmd += ['-f', 'lavfi', '-i', f'color=c=black:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:r=15:d={duration:.3f}']
    for screen in screens:
        cmd += ['-i', str(screen['file'])]
    # Audio is always its own input. Webcam *video* never becomes an input.
    audio_index = len(screens) + 1
    cmd += ['-i', str(audio)]
    f = ['[0:v]fps=15,format=yuv420p[v0]']
    last = '[v0]'
    for i, screen in enumerate(screens, 1):
        start, end = screen['start'], screen['end']
        first = max(0.0, -start)
        offset = max(0.0, start)
        # When a screenshare is active, display it at full frame instead of the webcam.
        f.append(f'[{i}:v]trim=start={first:.3f},setpts=PTS-STARTPTS+{offset:.3f}/TB,'
                 'scale=1280:720:force_original_aspect_ratio=decrease,'
                 'pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p'
                 f'[screen{i}]')
        nextlabel = f'[v{i}]'
        f.append(last + f'[screen{i}]overlay=eof_action=pass:shortest=0:repeatlast=0:'
                 f"enable='between(t,{max(start,0):.3f},{end:.3f})'" + nextlabel)
        last = nextlabel
    f.append(last + 'format=yuv420p[vout]')
    cmd += ['-filter_complex', ';'.join(f), '-map', '[vout]', '-map', f'{audio_index}:a:0',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-r', '15',
            '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-t', f'{duration:.3f}',
            '-movflags', '+faststart', str(tmp)]
    ffmpeg_tail(cmd, 'Rendering CONTENT (slides/screenshare) + AUDIO; no webcams...', duration)
    if not tmp.is_file() or tmp.stat().st_size < 10000:
        raise RuntimeError('Rendering did not produce a valid MP4.')
    verify_audio_track(ffmpeg, tmp)
    tmp.replace(output)
    return output


def save_report(out, data, duration, slide_stats, timeline, images, media, screens, status):
    lines = [f'Session: {SESSION_ID}',f'Title: {data.get("name", "")}',
             f'Recording duration: {duration:.3f} sec',
             f'Presentation update events: {slide_stats["presentation_events"]}',
             f'Without slide URL: {slide_stats["missing"]}',
             f'Slide in first snapshot: {slide_stats["initial_snapshot"]}',
             f'Assumed start slide: {slide_stats["assumed_start"]}',
             f'Unique images: {len(images)}',
             f'Media source count: {len(media)}',
             f'Screensharing media source count: {sum(m["kind"]=="screensharing" for m in media)}',
             f'Screensharing clips downloaded: {len(screens)}',
             f'Process status: {status}',
             'Slides timeline:']
    lines += [f'  {t:.2f}s  {images[u].name} {why}' for t,u,why in timeline if u in images]
    lines += ['Screensharing timeline:']
    lines += [f'  {m["start"]:.2f}s - {m["end"]:.2f}s : {m["file"].name}' for m in screens]
    (out / 'MTS_RESULT_REPORT.txt').write_text('\n'.join(lines) + '\n', encoding='utf8')


def run_recording(url, output_parent=None, callback=None, stop_event=None):
    """Run one recording job. Suitable for worker thread in tkinter app."""
    global SESSION_ID, API, OUTPUT_PARENT, REPORT_CALLBACK, CANCEL_EVENT
    SESSION_ID, record_file, API = parse_recording_url(url)
    OUTPUT_PARENT = output_parent
    REPORT_CALLBACK = callback
    CANCEL_EVENT = stop_event
    say('МТС Линк: материалы трансляции + звук, без видео с камер')
    ffmpeg = require_ffmpeg()
    require_pillow()
    folder = work_directory()
    assets = folder / '_assets'
    assets.mkdir(exist_ok=True)
    data, duration = get_metadata()
    say(f'Запись: {data.get("name", "(без названия)")}')
    say(f'Длительность: {duration/60:.1f} мин')
    timeline, stats = slideshow_timeline(data, duration)
    media = audio_plan(data, duration)
    screen_count = sum(item['kind'] == 'screensharing' for item in media)
    say(f'Материалы: смен слайдов={len(timeline)}, потоков экрана={screen_count}, медиаисточников={len(media)}')
    save_report(folder, data, duration, stats, timeline, {}, media, [], 'METADATA READY')
    if not timeline and screen_count == 0:
        save_report(folder, data, duration, stats, timeline, {}, media, [], 'NO DISPLAY CONTENT')
        raise RuntimeError('Нет ни слайдов, ни потока демонстрации экрана. Собрать только веб-камеры программа не позволит.')
    images = prepare_slides(timeline, assets) if timeline else {}
    screens = collect_screens(ffmpeg, media, duration, assets)
    save_report(folder, data, duration, stats, timeline, images, media, screens, 'CONTENT READY, AUDIO PENDING')
    tracks = collect_audio(ffmpeg, data, duration, assets)
    audio = create_audio(ffmpeg, tracks, duration, assets)
    save_report(folder, data, duration, stats, timeline, images, media, screens, 'AUDIO READY, RENDER PENDING')
    video = create_mp4(ffmpeg, timeline, images, screens, audio, duration, folder, assets)
    save_report(folder, data, duration, stats, timeline, images, media, screens, 'MP4 CREATED (PLAYBACK NOT VERIFIED)')
    say('ГОТОВО: ' + str(video))
    say('Проверь изображение и звук в начале, середине и конце.')
    return video


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Использование: python mts_backend.py URL_ЗАПИСИ')
        sys.exit(2)
    try:
        run_recording(sys.argv[1])
    except (KeyboardInterrupt, Cancelled):
        print('Операция остановлена.')
        sys.exit(130)
    except Exception as exc:
        print('ОШИБКА:', exc)
        sys.exit(1)
