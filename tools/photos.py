"""Copies my mountain photos and videos into assets/summits/, made small enough for the web.

The source is a folder with one folder per peak ("Borah", "Mt Triumph" ...). Each is matched to a peak in
summits.json by name; ALIASES below covers the folders whose name does not match. Folders with no peak are
reported and left out.
    python3 tools/photos.py [folder of peak folders]     (without one: ../Mountains, next to this repo)
    python3 tools/summits.py                              (then, to list them in summits.json)

Photos become JPEGs at most LONG pixels on the long side, videos become 1080p MP4s with a poster picture. Both lose
their metadata, so the published files do not carry the camera's GPS position or the time. They are numbered in
the order they were taken: borah-peak-1.jpg, borah-peak-2.jpg ... Running this again replaces a peak's files.

Needs Pillow (pip install pillow) and, for videos, ffmpeg.
"""
import glob, hashlib, json, os, subprocess, sys

from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, '..')
OUT = os.path.join(SITE, 'assets', 'summits')
LONG, QUALITY = 3840, 82  # 4K on the long side
PHOTO, VIDEO = ('.jpg', '.jpeg', '.png', '.heic', '.webp'), ('.mov', '.mp4', '.m4v')
ALIASES = {'hector': 'mount-hector', 'orhy': 'pic-d-orhi', 'sacajewa': 'sacajawea-peak', 'villarirca': 'volcan-villarrica',
           'mt-idaho': 'mount-idaho', 'mt-triumph': 'mount-triumph', 'larrun': 'larrun'}


def slug(name):
    return '-'.join(''.join(c if c.isalnum() else ' ' for c in name.lower()).split())


def peak_for(folder, slugs):
    """The peak a folder belongs to: an alias, the same name, or the only peak whose name contains the folder's."""
    name = slug(folder)
    if name in ALIASES:
        return ALIASES[name]
    if name in slugs:
        return name
    inside = [s for s in slugs if name in s.split('-') or set(name.split('-')) <= set(s.split('-'))]
    return inside[0] if len(inside) == 1 else None


def taken(path):
    """When a photo or video was taken, as text that sorts by time. Empty when the file does not say."""
    if path.lower().endswith(VIDEO):
        # The phone's own tag is in local time, like a photo's. The standard one is in UTC, so it is the fallback.
        for tag in ('com.apple.quicktime.creationdate', 'creation_time'):
            found = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=' + tag, '-of',
                                    'csv=p=0', path], capture_output=True, text=True).stdout.strip()
            if found:
                return found[:19].replace('-', ':').replace('T', ' ')
        return ''
    try:
        exif = Image.open(path).getexif()
        return str(exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132) or '')
    except Exception:
        return ''


def photo(source, target):
    picture = ImageOps.exif_transpose(Image.open(source)).convert('RGB')
    picture.thumbnail((LONG, LONG), Image.LANCZOS)
    picture.save(target, 'JPEG', quality=QUALITY, optimize=True, progressive=True)  # a new file: no metadata


def video(source, target):
    small = "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))'"  # 1080p, never enlarged
    run = lambda *args: subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', source, *args], check=True)
    run('-vf', small, '-c:v', 'libx264', '-crf', '27', '-preset', 'slow', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-b:a', '96k', '-movflags', '+faststart', '-map_metadata', '-1', target)
    run('-vf', small, '-frames:v', '1', '-q:v', '4', '-map_metadata', '-1', target.rsplit('.', 1)[0] + '.poster.jpg')


def main(source):
    slugs = [p['slug'] for p in json.load(open(os.path.join(SITE, 'summits.json')))['peaks']]
    os.makedirs(OUT, exist_ok=True)
    for folder in sorted(os.listdir(source)):
        files = [f for f in glob.glob(os.path.join(source, folder, '*')) if f.lower().endswith(PHOTO + VIDEO)]
        if not files:
            continue
        peak = peak_for(folder, slugs)
        if not peak:
            print(f'{folder}: no peak with this name in summits.json, left out')
            continue
        for old in glob.glob(os.path.join(OUT, peak + '-*')):
            if os.path.basename(old)[len(peak) + 1:].split('.')[0].isdigit():
                os.remove(old)
        seen, count = set(), 0
        for path in sorted(files, key=lambda f: (taken(f), f)):
            digest = hashlib.md5(open(path, 'rb').read()).hexdigest()
            if digest in seen:  # the same photo saved twice
                continue
            seen.add(digest)
            count += 1
            if path.lower().endswith(VIDEO):
                video(path, os.path.join(OUT, f'{peak}-{count}.mp4'))
            else:
                photo(path, os.path.join(OUT, f'{peak}-{count}.jpg'))
        print(f'{folder}: {count} -> {peak}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(SITE, '..', 'Mountains'))
