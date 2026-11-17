#!/usr/bin/env python3
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

CLIENT_ID = 'e527dfaee6184ca592fc0d746493b6b1'
REDIRECT_URI = 'http://127.0.0.1:8765/callback'
SCOPES = 'user-library-read user-library-modify user-read-playback-state user-modify-playback-state'
DATA = Path.home() / 'Library/Application Support/Spotify Actions'


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request(method, url, data=None, headers=None, expect_json=True):
    try:
        with urlopen(Request(url, data=data, headers=headers or {}, method=method), timeout=15) as response:
            body = response.read()
            if not expect_json or not body.strip():
                return None
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(f'Spotify returned invalid JSON for {method} {urlparse(url).path}.') from None
    except HTTPError as error:
        # Do not include response bodies or URLs that may contain credentials.
        raise RuntimeError(f'Spotify returned HTTP {error.code}. Check authorization, Premium status, and app access; for 429, wait before retrying.') from None


class Spotify:
    def __init__(self, directory=DATA):
        self.directory = directory
        self.token_path = directory / 'tokens.json'

    def exchange(self, parameters, previous=None):
        token = request('POST', 'https://accounts.spotify.com/api/token',
                        urlencode({'client_id': CLIENT_ID, **parameters}).encode(),
                        {'Content-Type': 'application/x-www-form-urlencoded'})
        if not token.get('refresh_token') and previous:
            token['refresh_token'] = previous['refresh_token']
        if not token.get('access_token') or not token.get('refresh_token'):
            raise RuntimeError('Spotify did not return the required tokens. Authorize again.')
        token['expires_at'] = time.time() + token['expires_in']
        write_private(self.token_path, token)
        return token

    def api(self, method, path, parameters=None):
        if not self.token_path.exists():
            raise RuntimeError('Run spotify_actions.py auth first.')
        token = json.loads(self.token_path.read_text())
        if token['expires_at'] < time.time() + 60:
            token = self.exchange({'grant_type': 'refresh_token', 'refresh_token': token['refresh_token']}, token)
        url = 'https://api.spotify.com/v1' + path
        if parameters:
            url += '?' + urlencode(parameters)
        return request(method, url, headers={'Authorization': 'Bearer ' + token['access_token']},
                       expect_json=method == 'GET')

    def saved(self, uri):
        result = self.api('GET', '/me/library/contains', {'uris': uri})
        if not isinstance(result, list) or len(result) != 1 or type(result[0]) is not bool:
            raise RuntimeError('Spotify returned an invalid saved-song status. Nothing else was changed.')
        return result[0]

    def playback(self):
        return self.api('GET', '/me/player')


def track(state):
    item = (state or {}).get('item') or {}
    uri = item.get('uri', '')
    if item.get('type') != 'track' or item.get('is_local') or not uri.startswith('spotify:track:'):
        raise RuntimeError('No supported Spotify song is active. Nothing changed.')
    return item


def set_saved(client, uri, saved):
    if client.saved(uri) == saved:
        return
    client.api('PUT' if saved else 'DELETE', '/me/library', {'uris': uri})
    for delay in [0, 0.3, 0.7]:
        if delay:
            time.sleep(delay)
        if client.saved(uri) == saved:
            return
    raise RuntimeError('Spotify did not confirm the library change. No further action was taken.')


def like(client):
    item = track(client.playback())
    set_saved(client, item['uri'], True)
    return 'Saved to Liked Songs.'


def dislike_and_skip(client):
    before = client.playback()
    item = track(before)
    device = (before.get('device') or {}).get('id')
    if not device or before.get('device', {}).get('is_restricted'):
        raise RuntimeError('The active Spotify device cannot be controlled. Nothing changed.')
    if before.get('actions', {}).get('disallows', {}).get('skipping_next'):
        raise RuntimeError('Spotify does not allow skipping right now. Nothing changed.')
    set_saved(client, item['uri'], False)
    after = client.playback()
    current = (after or {}).get('item') or {}
    if current.get('uri') != item['uri'] or (after.get('device') or {}).get('id') != device:
        return 'Removed from Liked Songs. Playback changed, so no extra skip was sent.'
    # Spotify has no atomic "skip this exact track" endpoint. Avoid the end-of-track window.
    remaining = current.get('duration_ms', 0) - (after.get('progress_ms') or 0)
    if after.get('is_playing') and remaining < 3000:
        return 'Removed from Liked Songs. Song is ending, so no extra skip was sent.'
    client.api('POST', '/me/player/next', {'device_id': device})
    return 'Removed from Liked Songs and requested next track.'


def fast_forward_15s(client):
    state = client.playback()
    track(state)
    device = state.get('device') or {}
    if not device.get('id') or device.get('is_restricted'):
        raise RuntimeError('The active Spotify device cannot be controlled. Nothing changed.')
    if (state.get('actions') or {}).get('disallows', {}).get('seeking'):
        raise RuntimeError('Spotify does not allow fast forwarding right now. Nothing changed.')
    progress = state.get('progress_ms')
    if type(progress) is not int or progress < 0:
        raise RuntimeError('Spotify did not return a valid playback position. Nothing changed.')
    client.api('PUT', '/me/player/seek',
               {'position_ms': progress + 15000, 'device_id': device['id']})
    return 'Requested fast forward by 15 seconds.'


def authorize(client):
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == '/callback' and secrets.compare_digest(query.get('state', [''])[0], state)
            if not valid:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid authorization callback.')
                return
            try:
                if query.get('error') or not query.get('code'):
                    raise RuntimeError('Spotify authorization was declined.')
                client.exchange({'grant_type': 'authorization_code', 'code': query['code'][0],
                                 'redirect_uri': REDIRECT_URI, 'code_verifier': verifier})
                message = 'Spotify Actions is connected. You can close this tab.'
                result['ok'] = True
            except Exception as error:
                message = 'Authorization failed. Return to the terminal for details.'
                result['error'] = error
            self.send_response(200 if result.get('ok') else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(message.encode())

    parameters = {'client_id': CLIENT_ID, 'response_type': 'code', 'redirect_uri': REDIRECT_URI,
                  'scope': SCOPES, 'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge}
    with HTTPServer(('127.0.0.1', 8765), Callback) as server:
        server.timeout = 1
        print('Open this link and approve Spotify Actions (valid for 10 minutes):', flush=True)
        print('https://accounts.spotify.com/authorize?' + urlencode(parameters), flush=True)
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if result.get('error'):
        raise result['error']
    if not result.get('ok'):
        raise RuntimeError('Authorization timed out. Run auth again.')
    return 'Spotify authorization saved.'


def main():
    parser = argparse.ArgumentParser(description='Spotify controls for macOS and Logitech+ shortcuts.')
    handlers = {'like': like, 'dislike-and-skip': dislike_and_skip,
                'fast-forward-15s': fast_forward_15s}
    parser.add_argument('action', choices=['auth', *handlers],
                        help='authorize access, save the current song, remove it and skip, or fast forward 15 seconds')
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DATA / 'action.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            client = Spotify()
            if args.action == 'auth':
                message = authorize(client)
            else:
                stamp = DATA / f'last-press-{args.action}.json'
                if stamp.exists() and time.time() - json.loads(stamp.read_text()) < 2:
                    return 0
                write_private(stamp, time.time())
                message = handlers[args.action](client)
            print(message)
            return 0
        except Exception as error:
            print(error, file=sys.stderr)
            return 1


if __name__ == '__main__':
    sys.exit(main())
