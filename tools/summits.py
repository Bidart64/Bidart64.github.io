"""Builds summits.json for the home page: which Strava activities reached a summit, and their tracks.

Strava does not record summits, so this works them out. A peak counts as climbed when an activity's track passes
within RADIUS metres of a named peak in OpenStreetMap, or within NEAR metres when the activity's title names the
peak (tracks on big volcanoes end at the crater rim, short of the mapped summit point). The file next to this
one, summits-overrides.json, corrects the result by hand: peaks to skip, to rename, and to add. It also holds
the climbing grade of each peak's route, which no data source gives and is looked up by hand.

Input: a folder of JSON files, each the result of a Strava activity search that includes "reduced_polyline".
Without a folder it reads tools/.strava, which is kept out of git because it holds every activity's details.
    python3 tools/summits.py [folder of Strava results]

Photos and videos: tools/photos.py fills assets/summits/ from my folder of pictures. By hand, name a peak's
pictures after its slug (borah-peak-1.jpg, borah-peak-2.jpg, borah-peak-3.mp4 ...) and run this again. The slug of each peak is printed below and is also the end of its link, #summit/borah-peak.

Text: write a peak's story in _summits/<slug>.md and run this again. It is plain text with a little Markdown:
blank lines between paragraphs, "## " for a heading, "- " for list items, **bold**, *italic*, [a link](https://...)
and ![a caption](borah-peak-2.jpg) on a line of its own to place one of the peak's photos in the text. Photos
placed that way are left out of the gallery under the text.

Only the peak, date, sport and track of each ascent are written: no activity titles or descriptions.
"""
import glob, html, json, math, os, re, sys, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RADIUS, NEAR = 60, 250  # metres
OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter',
            'https://overpass.private.coffee/api/interpreter']
SPORTS = {'Hike': 'hike', 'TrailRun': 'run', 'BackcountrySki': 'ski', 'Snowshoe': 'snowshoe', 'RockClimbing': 'climb'}
US_STATES = {s.replace('_', ' ') for s in '''Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware
Florida Georgia Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan
Minnesota Mississippi Missouri Montana Nebraska Nevada New_Hampshire New_Jersey New_Mexico New_York North_Carolina
North_Dakota Ohio Oklahoma Oregon Pennsylvania Rhode_Island South_Carolina South_Dakota Tennessee Texas Utah Vermont
Virginia Washington West_Virginia Wisconsin Wyoming'''.split()}
GENERIC = {'mount', 'mont', 'monte', 'peak', 'pic', 'volcan', 'mountain', 'point', 'little'}


def decode(s):
    """An encoded polyline as a list of (lat, lon)."""
    out, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for axis in (0, 1):
            shift = value = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                value |= (b & 31) << shift
                shift += 5
                if b < 32:
                    break
            delta = ~(value >> 1) if value & 1 else value >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        out.append((lat / 1e5, lon / 1e5))
    return out


def metres_to_track(lat, lon, pts):
    """Distance from a point to the nearest part of a track."""
    kx, ky = 111320 * math.cos(math.radians(lat)), 110540
    best, prev = 1e9, None
    for la, lo in pts:
        cur = ((lo - lon) * kx, (la - lat) * ky)
        if prev is None:
            best = min(best, math.hypot(*cur))
        else:
            (x1, y1), (x2, y2) = prev, cur
            dx, dy = x2 - x1, y2 - y1
            length = dx * dx + dy * dy
            t = 0 if length == 0 else max(0, min(1, -(x1 * dx + y1 * dy) / length))
            best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
        prev = cur
    return best


def words(text):
    plain = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    return {w for w in ''.join(c if c.isalnum() else ' ' for c in plain).split() if len(w) > 3 and w not in GENERIC}


def slug(name):
    plain = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    return '-'.join(''.join(c if c.isalnum() else ' ' for c in plain).split())


def story(text, used):
    """A peak's text as HTML. `used` collects the photos it places."""
    def picture(match):
        alt, src = match.group(1), match.group(2)
        if '/' not in src:
            src = 'assets/summits/' + src
        used.add(src)
        return f'<img src="{src}" alt="{alt}" loading="lazy">'

    def inline(line):
        line = html.escape(line, quote=True)
        line = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)', picture, line)
        line = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', r'<a href="\2">\1</a>', line)
        line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
        return re.sub(r'\*(.+?)\*', r'<em>\1</em>', line)

    out = []
    for block in re.split(r'\n\s*\n', text.strip()):
        rows = [r.strip() for r in block.strip().splitlines()]
        alone = re.fullmatch(r'!\[([^\]]*)\]\(([^)\s]+)\)', block.strip())
        if alone:  # a photo on its own: shown full width, with its caption under it
            caption = f'<figcaption>{html.escape(alone.group(1))}</figcaption>' if alone.group(1) else ''
            out.append(f'<figure>{inline(block.strip())}{caption}</figure>')
        elif rows[0].startswith('#'):
            out.append(f'<h4>{inline(rows[0].lstrip("#").strip())}</h4>')
            if rows[1:]:
                out.append(f'<p>{inline(" ".join(rows[1:]))}</p>')
        elif all(r.startswith('- ') for r in rows):
            out.append('<ul>' + ''.join(f'<li>{inline(r[2:])}</li>' for r in rows) + '</ul>')
        else:
            out.append(f'<p>{inline(" ".join(rows))}</p>')
    return ''.join(out)


def height(tags):
    try:
        return float(str(tags.get('ele', '')).replace(',', '.').split()[0])
    except (ValueError, IndexError):
        return None


def osm_peaks(activities, cache):
    """Named peaks around the tracks. Asked for by 0.1 degree cell, so the request does not carry the tracks."""
    cells = sorted({(math.floor(la * 10), math.floor(lo * 10)) for a in activities for la, lo in a['pts']})
    kept = json.load(open(cache)) if os.path.exists(cache) else {}
    if kept.get('cells') != [list(c) for c in cells]:
        query = '[out:json][timeout:120];(' + ''.join(
            f'node[natural~"^(peak|volcano)$"]({y / 10:.1f},{x / 10:.1f},{(y + 1) / 10:.1f},{(x + 1) / 10:.1f});'
            for y, x in cells) + ');out;'
        for url in OVERPASS * 2:
            try:
                request = urllib.request.Request(url, data=urllib.parse.urlencode({'data': query}).encode(),
                                                 headers={'User-Agent': 'summit-matcher/1.0'})
                elements = json.loads(urllib.request.urlopen(request, timeout=180).read())['elements']
                break
            except Exception as error:  # these servers are often busy: try the next one
                print(url, error, file=sys.stderr)
        else:
            sys.exit('No OpenStreetMap server answered')
        kept = {'cells': cells, 'elements': elements}
        json.dump(kept, open(cache, 'w'))
    return [e for e in kept['elements'] if e.get('tags', {}).get('name')]


def main(folder):
    rules = json.load(open(os.path.join(HERE, 'summits-overrides.json')))
    activities = {}
    for path in glob.glob(os.path.join(folder, '*')):
        try:
            found = json.load(open(path)).get('activities', [])
        except (ValueError, AttributeError, UnicodeDecodeError, IsADirectoryError):
            continue
        for a in found:
            if a.get('reduced_polyline') and a.get('sport_type') in SPORTS:
                activities[a['id']] = a
    activities = sorted(activities.values(), key=lambda a: a['start_local'])
    for a in activities:
        a['pts'] = decode(a['reduced_polyline'])
    peaks = osm_peaks(activities, os.path.join(HERE, '.peaks-cache.json'))

    summits, tracks = {}, []

    def ascent(a, name, ele, lat, lon):
        name = rules['rename'].get(name, name.split(' / ')[0].split(' - ')[0])
        key = (name, round(lat, 1), round(lon, 1))
        # Strava ends a place with the country, or with the state for the United States.
        region = a['location_summary'].split(', ')[-1]
        state = region in US_STATES
        peak = summits.setdefault(key, {'name': name, 'ele': round(ele), 'lat': round(lat, 5), 'lon': round(lon, 5),
                                        'country': 'United States' if state else rules.get('countries', {}).get(region, region),
                                        'state': region if state else None,
                                        'grade': rules.get('grades', {}).get(name), 'ascents': []})
        if 'track' not in a:
            a['track'] = len(tracks)
            tracks.append(a['reduced_polyline'])
        peak['ascents'].append({'date': a['start_local'][:10], 'sport': SPORTS[a['sport_type']], 'track': a['track']})

    for a in activities:
        lats, lons = [p[0] for p in a['pts']], [p[1] for p in a['pts']]
        seen = set()
        for p in peaks:
            if not (min(lats) - .003 < p['lat'] < max(lats) + .003 and min(lons) - .005 < p['lon'] < max(lons) + .005):
                continue
            name, ele = p['tags']['name'], height(p['tags'])
            if name in rules['skip'] or name in seen or ele is None or ele < rules['min_ele']:
                continue
            away = metres_to_track(p['lat'], p['lon'], a['pts'])
            if away <= RADIUS or (away <= NEAR and words(name) & words(a['name'])):
                seen.add(name)
                ascent(a, name, ele, p['lat'], p['lon'])
        for extra in rules['add']:
            if extra['activity'] == a['id']:
                ascent(a, extra['name'], extra['ele'], extra['lat'], extra['lon'])

    site = os.path.join(HERE, '..')
    for p in summits.values():
        p['slug'] = slug(p['name'])
        number = lambda f: os.path.basename(f).rsplit('.', 1)[0].removeprefix(p['slug']).lstrip('-')
        shots = sorted((f for f in glob.glob(os.path.join(site, 'assets', 'summits', p['slug'] + '*'))
                        if number(f).isdigit() or number(f) == ''), key=lambda f: int(number(f) or 0))
        used, words_file = set(), os.path.join(site, '_summits', p['slug'] + '.md')
        p['text'] = story(open(words_file, encoding='utf-8').read(), used) if os.path.exists(words_file) else ''
        p['photos'] = [path for path in (os.path.relpath(f, site) for f in shots)
                       if path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.mp4')) and path not in used]
    out = {'peaks': sorted(summits.values(), key=lambda p: -p['ele']), 'tracks': tracks}
    json.dump(out, open(os.path.join(HERE, '..', 'summits.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    for p in out['peaks']:
        print(f"{p['ele']:5} m  {p['slug']:26} {len(p['photos']) or '':2} {'text' if p['text'] else '':4} {p['name']:28} {p['state'] or p['country']:16} {' '.join(x['date'] for x in p['ascents'])}")
    print(len(out['peaks']), 'peaks,', sum(len(p['ascents']) for p in out['peaks']), 'ascents,', len(tracks), 'tracks')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '.strava'))
