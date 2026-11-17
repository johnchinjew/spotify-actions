# Spotify Actions

My custom controls for Spotify on macOS.

## Actions

- **Like:** Save the current song to Liked Songs.
- **Dislike and Skip:** Remove the current song from Liked Songs and skip to the next track.
- **Fast Forward 15s:** Fast forward 15 seconds in the current song.

## Setup

Requires Python 3 and Spotify Premium.

Create a Spotify app in the [developer console](https://developer.spotify.com/) with `http://127.0.0.1:8765/callback` as
the app's redirect URI. Then, set the `CLIENT_ID` variable in `spotify_actions.py` to your Spotify app's client ID.

Authorize Spotify Actions to access your Spotify account:

```sh
python3 spotify_actions.py auth
```

The access will expire after six months. Run the authorization command again when needed.

You can also install the actions as macOS apps in `~/Applications`:

```sh
python3 install.py
```

I use the macOS apps to trigger the actions via Logitech Options+.

## Usage

Trigger actions from command line:

```sh
python3 spotify_actions.py like
python3 spotify_actions.py dislike-and-skip
python3 spotify_actions.py fast-forward-15s
```

Trigger actions by opening the corresponding app:

- `Spotify Like.app`
- `Spotify Dislike and Skip.app`
- `Spotify Fast Forward 15s.app`

## Development

Reinstall the macOS apps after changing source code:

```sh
python3 install.py
```

Run tests:

```sh
python3 -m unittest
```
