import json
from pathlib import Path
import tempfile
import time
from io import BytesIO
import unittest
from unittest.mock import Mock, patch, call
import spotify_actions as actions


def playing(uri='spotify:track:one', device='mac'):
    return {'item': {'type': 'track', 'uri': uri, 'duration_ms': 200000},
            'device': {'id': device}, 'is_playing': True, 'progress_ms': 10000}


class Tests(unittest.TestCase):
    def test_fast_forward_15s_seeks_on_active_device_without_changing_library(self):
        for progress, is_playing in [(0, True), (10000, False), (199000, True)]:
            with self.subTest(progress=progress, is_playing=is_playing):
                client = Mock()
                client.playback.return_value = {**playing(), 'progress_ms': progress,
                                                'is_playing': is_playing}
                actions.fast_forward_15s(client)
                self.assertEqual(client.mock_calls, [call.playback(),
                    call.api('PUT', '/me/player/seek',
                             {'position_ms': progress + 15000, 'device_id': 'mac'})])

    def test_fast_forward_15s_rejects_unavailable_or_restricted_playback(self):
        states = [None, {**playing(), 'device': None},
                  {**playing(), 'device': {'id': 'mac', 'is_restricted': True}},
                  {**playing(), 'actions': {'disallows': {'seeking': True}}}]
        states += [{**playing(), 'progress_ms': value} for value in [None, -1, '1000', True]]
        for state in states:
            with self.subTest(state=state):
                client = Mock()
                client.playback.return_value = state
                with self.assertRaises(RuntimeError):
                    actions.fast_forward_15s(client)
                client.api.assert_not_called()

    def test_fast_forward_15s_command_dispatch_and_duplicate_suppression(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(actions, 'DATA', Path(directory)), patch.object(actions, 'Spotify'), \
                 patch.object(actions, 'fast_forward_15s', return_value='Fast forward requested') as fast_forward, \
                 patch('sys.argv', ['spotify_actions.py', 'fast-forward-15s']):
                self.assertEqual(actions.main(), 0)
                self.assertEqual(actions.main(), 0)
                fast_forward.assert_called_once()

    def test_removal_is_verified_before_skip(self):
        client = Mock()
        client.playback.side_effect = [playing(), playing()]
        client.saved.side_effect = [True, False]
        actions.dislike_and_skip(client)
        self.assertEqual(client.mock_calls, [
            call.playback(), call.saved('spotify:track:one'),
            call.api('DELETE', '/me/library', {'uris': 'spotify:track:one'}),
            call.saved('spotify:track:one'), call.playback(),
            call.api('POST', '/me/player/next', {'device_id': 'mac'})])

    def test_success_response_without_library_change_never_skips(self):
        client = Mock()
        client.playback.return_value = playing()
        client.saved.return_value = True
        with patch.object(actions.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'did not confirm'):
            actions.dislike_and_skip(client)
        client.api.assert_called_once_with('DELETE', '/me/library', {'uris': 'spotify:track:one'})

    def test_library_check_failure_prevents_any_mutation(self):
        client = Mock()
        client.playback.return_value = playing()
        client.saved.side_effect = RuntimeError('Insufficient scope')
        with self.assertRaises(RuntimeError):
            actions.dislike_and_skip(client)
        client.api.assert_not_called()

    def test_already_unliked_song_only_skips(self):
        client = Mock()
        client.playback.side_effect = [playing(), playing()]
        client.saved.return_value = False
        actions.dislike_and_skip(client)
        client.api.assert_called_once_with('POST', '/me/player/next', {'device_id': 'mac'})

    def test_removal_failure_never_skips(self):
        client = Mock()
        client.playback.return_value = playing()
        client.saved.return_value = True
        client.api.side_effect = RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            actions.dislike_and_skip(client)
        self.assertEqual(client.api.call_count, 1)

    def test_delayed_library_update_is_verified_without_repeating_delete(self):
        client = Mock()
        client.saved.side_effect = [True, True, False]
        with patch.object(actions.time, 'sleep'):
            actions.set_saved(client, 'spotify:track:one', False)
        client.api.assert_called_once_with('DELETE', '/me/library', {'uris': 'spotify:track:one'})

    def test_like_verifies_and_repeated_like_does_nothing(self):
        client = Mock()
        client.playback.return_value = playing()
        client.saved.side_effect = [False, True, True]
        actions.like(client)
        actions.like(client)
        client.api.assert_called_once_with('PUT', '/me/library', {'uris': 'spotify:track:one'})

    def test_like_failure_is_not_reported_as_success(self):
        client = Mock()
        client.playback.return_value = playing()
        client.saved.return_value = False
        with patch.object(actions.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'did not confirm'):
            actions.like(client)
        client.api.assert_called_once_with('PUT', '/me/library', {'uris': 'spotify:track:one'})

    def test_unsupported_tracks_do_nothing(self):
        for state in [None, {'item': {'type': 'episode', 'uri': 'spotify:episode:one'}},
                      {'item': {'type': 'track', 'uri': 'spotify:track:one', 'is_local': True}}]:
            for action in [actions.like, actions.dislike_and_skip, actions.fast_forward_15s]:
                client = Mock()
                client.playback.return_value = state
                with self.assertRaises(RuntimeError):
                    action(client)
                client.api.assert_not_called()
                client.saved.assert_not_called()

    def test_changed_track_device_or_stopped_playback_never_skips(self):
        for after in [playing('spotify:track:two'), playing(device='phone'), None]:
            client = Mock()
            client.playback.side_effect = [playing(), after]
            client.saved.side_effect = [True, False]
            actions.dislike_and_skip(client)
            self.assertEqual(client.api.call_count, 1)

    def test_end_of_track_does_not_double_skip(self):
        client = Mock()
        after = playing()
        after['progress_ms'] = 199000
        client.playback.side_effect = [playing(), after]
        client.saved.side_effect = [True, False]
        actions.dislike_and_skip(client)
        self.assertEqual(client.api.call_count, 1)

    def test_invalid_saved_state_is_not_treated_as_unliked(self):
        client = actions.Spotify()
        for value in [None, [], [0], ['false'], {'saved': False}, [False, True]]:
            with patch.object(client, 'api', return_value=value), self.assertRaises(RuntimeError):
                client.saved('spotify:track:one')

    def test_text_mutation_response_requires_verified_library_change(self):
        with tempfile.TemporaryDirectory() as directory:
            client = actions.Spotify(Path(directory))
            actions.write_private(client.token_path, {'access_token': 'test', 'expires_at': time.time() + 3600})
            replies = [json.dumps(playing()).encode(), b'[true]', b'OK', b'[false]',
                       json.dumps(playing()).encode(), b'OK']
            with patch.object(actions, 'urlopen', side_effect=[BytesIO(body) for body in replies]) as network:
                actions.dislike_and_skip(client)
            self.assertEqual([c.args[0].method for c in network.call_args_list], ['GET', 'GET', 'DELETE', 'GET', 'GET', 'POST'])

    def test_invalid_playback_json_stops_before_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            client = actions.Spotify(Path(directory))
            actions.write_private(client.token_path, {'access_token': 'test', 'expires_at': time.time() + 3600})
            with patch.object(actions, 'urlopen', return_value=BytesIO(b'not json')) as network:
                with self.assertRaisesRegex(RuntimeError, 'invalid JSON for GET /v1/me/player'):
                    actions.dislike_and_skip(client)
            network.assert_called_once()

    def test_refresh_preserves_refresh_token_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            client = actions.Spotify(Path(directory))
            with patch.object(actions, 'request', return_value={'access_token': 'new', 'expires_in': 3600}):
                client.exchange({'grant_type': 'refresh_token'}, {'refresh_token': 'retained'})
            self.assertEqual(json.loads(client.token_path.read_text())['refresh_token'], 'retained')
            self.assertEqual(client.token_path.stat().st_mode & 0o777, 0o600)

    def test_like_does_not_suppress_dislike_and_skip_immediately_afterward(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(actions, 'DATA', Path(directory)), patch.object(actions, 'Spotify'), \
                 patch.object(actions, 'like', return_value='Liked') as like, \
                 patch.object(actions, 'dislike_and_skip', return_value='Skipped') as dislike_and_skip:
                with patch('sys.argv', ['spotify_actions.py', 'like']):
                    self.assertEqual(actions.main(), 0)
                with patch('sys.argv', ['spotify_actions.py', 'dislike-and-skip']):
                    self.assertEqual(actions.main(), 0)
                like.assert_called_once()
                dislike_and_skip.assert_called_once()


if __name__ == '__main__':
    unittest.main()
