import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / 'database' / 'clips.db'
ROSTER_PATH = BASE / 'data' / 'roster.json'
load_dotenv(BASE / '.env')
app = Flask(__name__)

LOL_GAME_NAME = 'League of Legends'
LOL_GAME_ID = '21779'
JST = timezone(timedelta(hours=9))
EVENT_START = datetime(2026, 10, 15, tzinfo=JST).astimezone(timezone.utc)
EVENT_END = datetime(2026, 11, 23, tzinfo=JST).astimezone(timezone.utc)
PAGE_SIZE = 100
VIEW_CACHE = {}
TEAMS = {'DD': 'DAHLIA DIADEM', 'CC': 'CAMELLIA CROWN', 'IT': 'IRIS TIARA', 'LR': 'LAUREL REGALIA'}


def load_roster():
    return json.loads(ROSTER_PATH.read_text(encoding='utf-8'))


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS clips (
            clip_id TEXT PRIMARY KEY, streamer TEXT NOT NULL, login TEXT NOT NULL,
            team TEXT NOT NULL, title TEXT, url TEXT, thumbnail TEXT,
            views INTEGER DEFAULT 0, duration REAL DEFAULT 0, created_at TEXT,
            stream_at TEXT, video_id TEXT, vod_offset REAL, game_id TEXT DEFAULT '',
            time_is_estimated INTEGER DEFAULT 1
        )''')
        columns = {r['name'] for r in con.execute('PRAGMA table_info(clips)')}
        migrations = {
            'game_id': "ALTER TABLE clips ADD COLUMN game_id TEXT DEFAULT ''",
            'time_is_estimated': 'ALTER TABLE clips ADD COLUMN time_is_estimated INTEGER DEFAULT 1'
        }
        for name, sql in migrations.items():
            if name not in columns:
                con.execute(sql)
        con.execute('CREATE INDEX IF NOT EXISTS idx_clips_game_stream ON clips(game_id, stream_at)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_clips_login ON clips(login)')
        con.execute('CREATE TABLE IF NOT EXISTS video_cache (video_id TEXT PRIMARY KEY, created_at TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)')


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def as_utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def jst_day(value):
    dt = parse_iso(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).date().isoformat()


class TwitchAPI:
    def __init__(self):
        client_id = os.getenv('TWITCH_CLIENT_ID', '').strip()
        secret = os.getenv('TWITCH_CLIENT_SECRET', '').strip()
        if not client_id or not secret:
            raise RuntimeError('.env に TWITCH_CLIENT_ID と TWITCH_CLIENT_SECRET を設定してください。')
        response = requests.post('https://id.twitch.tv/oauth2/token', data={
            'client_id': client_id, 'client_secret': secret, 'grant_type': 'client_credentials'
        }, timeout=20)
        response.raise_for_status()
        token = response.json()['access_token']
        self.session = requests.Session()
        self.session.headers.update({'Client-Id': client_id, 'Authorization': f'Bearer {token}'})

    def get(self, path, params):
        response = self.session.get('https://api.twitch.tv/helix/' + path, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def user_id(self, login):
        data = self.get('users', {'login': login}).get('data', [])
        return data[0]['id'] if data else None

    def clips(self, broadcaster_id, start, end):
        # Helix Get Clips accepts broadcaster_id OR game_id, not both.
        cursor, collected = None, []
        while True:
            params = {'broadcaster_id': broadcaster_id, 'first': 100,
                      'started_at': as_utc_iso(start), 'ended_at': as_utc_iso(end)}
            if cursor:
                params['after'] = cursor
            payload = self.get('clips', params)
            collected.extend(payload.get('data', []))
            cursor = payload.get('pagination', {}).get('cursor')
            if not cursor or not payload.get('data'):
                break
            time.sleep(0.12)
        return [c for c in collected if str(c.get('game_id') or '') == LOL_GAME_ID]

    def videos_created_at(self, video_ids):
        ids = list(dict.fromkeys(v for v in video_ids if v))
        found = {}
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            payload = self.get('videos', [('id', v) for v in batch])
            found.update({v['id']: v['created_at'] for v in payload.get('data', [])})
        return found


def cached_video_dates(api, video_ids):
    ids = list(dict.fromkeys(v for v in video_ids if v))
    if not ids:
        return {}
    with db() as con:
        cached = {r['video_id']: r['created_at'] for r in con.execute(
            f"SELECT video_id, created_at FROM video_cache WHERE video_id IN ({','.join('?' for _ in ids)})", ids
        )}
    missing = [v for v in ids if v not in cached]
    if missing:
        fetched = api.videos_created_at(missing)
        with db() as con:
            con.executemany('INSERT OR REPLACE INTO video_cache(video_id,created_at) VALUES (?,?)', fetched.items())
        cached.update(fetched)
    return cached


def deduplicate_overlaps(clips):
    """Same-channel overlapping clips collapse to the highest-view clip; channels stay independent."""
    by_channel = {}
    for clip in clips:
        by_channel.setdefault((clip.get('login') or '').lower(), []).append(clip)
    kept = []
    for channel_clips in by_channel.values():
        channel_clips.sort(key=lambda c: parse_iso(c['stream_at']) or datetime.min.replace(tzinfo=timezone.utc))
        cluster, cluster_end = [], None
        def flush(items):
            if items:
                kept.append(max(items, key=lambda c: (int(c.get('views') or 0), float(c.get('duration') or 0))))
        for clip in channel_clips:
            start = parse_iso(clip['stream_at'])
            if not start:
                flush(cluster); cluster, cluster_end = [], None
                kept.append(clip)
                continue
            end = start + timedelta(seconds=max(float(clip.get('duration') or 0), 1))
            if cluster and start < cluster_end:
                cluster.append(clip)
                cluster_end = max(cluster_end, end)
            else:
                flush(cluster)
                cluster, cluster_end = [clip], end
        flush(cluster)
    return sorted(kept, key=lambda c: parse_iso(c['stream_at']) or datetime.min.replace(tzinfo=timezone.utc))


def filtered_clips(team='', streamer='', q=''):
    cache_key = (team, streamer, q.lower())
    cached = VIEW_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < 15:
        return [dict(item) for item in cached[1]]
    clauses, args = ['game_id=?'], [LOL_GAME_ID]
    if team:
        clauses.append('team=?'); args.append(team)
    if streamer:
        clauses.append('streamer=?'); args.append(streamer)
    if q:
        clauses.append('(LOWER(title) LIKE ? OR LOWER(streamer) LIKE ?)')
        args.extend([f'%{q.lower()}%', f'%{q.lower()}%'])
    with db() as con:
        rows = [dict(r) for r in con.execute('SELECT * FROM clips WHERE ' + ' AND '.join(clauses), args)]
    result = deduplicate_overlaps(rows)
    VIEW_CACHE[cache_key] = (time.time(), result)
    return [dict(item) for item in result]


def sync_clips():
    roster = load_roster()
    configured = [p for p in roster if p.get('login', '').strip()]
    if not configured:
        raise RuntimeError('配信者のTwitchログインIDを先に登録してください。')
    api = TwitchAPI()
    now = datetime.now(timezone.utc)
    with db() as con:
        row = con.execute("SELECT value FROM app_state WHERE key='last_successful_sync'").fetchone()
    previous = parse_iso(row['value']) if row else None
    start = EVENT_START if previous is None else max(EVENT_START, previous - timedelta(days=1))
    finish = min(now, EVENT_END)
    if start >= finish:
        return {'processed': 0, 'new': 0, 'streamers': [], 'unknown': [], 'errors': [], 'message': '対象期間の同期は完了しています。'}

    summary, unknown, errors = [], [], []
    total_processed = total_new = 0
    for person in configured:
        login = person['login'].strip().lstrip('@')
        try:
            uid = api.user_id(login)
            if not uid:
                unknown.append(login)
                summary.append({'name': person['name'], 'login': login, 'count': 0, 'status': 'not_found'})
                continue
            cursor, person_clips = start, []
            while cursor < finish:
                edge = min(cursor + timedelta(days=7), finish)
                person_clips.extend(api.clips(uid, cursor, edge))
                cursor = edge
            video_dates = cached_video_dates(api, [c.get('video_id') for c in person_clips])
            person_new = 0
            with db() as con:
                for c in person_clips:
                    clip_id = c['id']
                    exists = con.execute('SELECT 1 FROM clips WHERE clip_id=?', (clip_id,)).fetchone()
                    if not exists:
                        person_new += 1
                    created = c.get('created_at')
                    vod_created = video_dates.get(c.get('video_id'))
                    offset = c.get('vod_offset')
                    estimated = not (vod_created and offset is not None)
                    stream_at = (parse_iso(vod_created) + timedelta(seconds=float(offset))).isoformat() if not estimated else created
                    con.execute('''INSERT INTO clips
                        (clip_id,streamer,login,team,title,url,thumbnail,views,duration,created_at,stream_at,video_id,vod_offset,game_id,time_is_estimated)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(clip_id) DO UPDATE SET streamer=excluded.streamer,login=excluded.login,
                        team=excluded.team,views=excluded.views,title=excluded.title,thumbnail=excluded.thumbnail,
                        stream_at=excluded.stream_at,video_id=excluded.video_id,vod_offset=excluded.vod_offset,
                        game_id=excluded.game_id,time_is_estimated=excluded.time_is_estimated''',
                        (clip_id, person['name'], login, person['team'], c.get('title', ''), c.get('url', ''),
                         c.get('thumbnail_url', ''), c.get('view_count', 0), c.get('duration', 0), created,
                         stream_at, c.get('video_id'), offset, c.get('game_id') or '', int(estimated)))
            total_processed += len(person_clips); total_new += person_new
            summary.append({'name': person['name'], 'login': login, 'count': len(person_clips), 'new': person_new, 'status': 'ok'})
        except requests.HTTPError as exc:
            detail = exc.response.text[:300] if exc.response is not None else str(exc)
            errors.append({'login': login, 'error': f'HTTP {exc.response.status_code if exc.response is not None else "?"}: {detail}'})
            summary.append({'name': person['name'], 'login': login, 'count': 0, 'status': 'error'})
        except Exception as exc:
            errors.append({'login': login, 'error': str(exc)})
            summary.append({'name': person['name'], 'login': login, 'count': 0, 'status': 'error'})
    if not errors:
        VIEW_CACHE.clear()
        with db() as con:
            con.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES ('last_successful_sync',?)", (finish.isoformat(),))
    return {'processed': total_processed, 'new': total_new, 'streamers': summary,
            'unknown': unknown, 'errors': errors, 'range_start': start.isoformat(), 'range_end': finish.isoformat()}


@app.get('/')
def index():
    roster = load_roster()
    with db() as con:
        count = con.execute('SELECT COUNT(*) FROM clips WHERE game_id=?', (LOL_GAME_ID,)).fetchone()[0]
    return render_template('index.html', roster=roster, teams=TEAMS, clip_count=count)


@app.get('/api/clips')
def api_clips():
    team = request.args.get('team', '')
    streamer = request.args.get('streamer', '')
    q = request.args.get('q', '').strip()
    date = request.args.get('date', '')
    sort = request.args.get('sort', 'oldest')
    limit = min(max(request.args.get('limit', PAGE_SIZE, type=int), 1), 200)
    offset = max(request.args.get('offset', 0, type=int), 0)
    clips = filtered_clips(team, streamer, q)
    if date:
        clips = [c for c in clips if jst_day(c.get('stream_at')) == date]
    if sort == 'views':
        clips.sort(key=lambda c: int(c.get('views') or 0), reverse=True)
    total = len(clips)
    page = clips[offset:offset + limit]
    return jsonify({'clips': page, 'total': total, 'limit': limit, 'offset': offset, 'has_more': offset + limit < total})


@app.get('/api/calendar')
def api_calendar():
    month = request.args.get('month', '')
    team, streamer, q = request.args.get('team', ''), request.args.get('streamer', ''), request.args.get('q', '').strip()
    clips = filtered_clips(team, streamer, q)
    counts = {}
    for clip in clips:
        day = jst_day(clip.get('stream_at'))
        if day and (not month or day.startswith(month)):
            counts[day] = counts.get(day, 0) + 1
    return jsonify({'counts': counts})


@app.get('/api/roster')
def api_roster():
    return jsonify(load_roster())


@app.post('/api/roster')
def save_roster():
    payload = request.get_json(force=True)
    current = load_roster()
    updates = {p['name']: p for p in payload}
    for person in current:
        if person['name'] in updates:
            person['login'] = updates[person['name']].get('login', '').strip().lstrip('@')
    ROSTER_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'ok': True})


@app.get('/api/streamers')
def api_streamers():
    with db() as con:
        rows = con.execute('SELECT DISTINCT streamer FROM clips WHERE game_id=? ORDER BY streamer', (LOL_GAME_ID,)).fetchall()
    return jsonify([r['streamer'] for r in rows])


@app.post('/api/sync')
def api_sync():
    try:
        result = sync_clips()
        with db() as con:
            result['saved_total'] = con.execute('SELECT COUNT(*) FROM clips WHERE game_id=?', (LOL_GAME_ID,)).fetchone()[0]
        return jsonify({'ok': True, **result})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
