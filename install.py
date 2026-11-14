import pathlib
import plistlib
import shutil
import sys

def main():
    source = pathlib.Path(__file__).resolve().parent / 'spotify_actions.py'
    for name, action in [('Spotify Like', 'like'), ('Spotify Dislike and Skip', 'dislike-and-skip')]:
        app = pathlib.Path.home() / f'Applications/{name}.app'
        contents = app / 'Contents'
        (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
        (contents / 'Resources').mkdir(exist_ok=True)
        shutil.copy2(source, contents / 'Resources/spotify_actions.py')
        with (contents / 'Info.plist').open('wb') as output:
            plistlib.dump({
                'CFBundleName': name,
                'CFBundleDisplayName': name,
                'CFBundleIdentifier': f'local.spotify-actions.{action}',
                'CFBundleVersion': '1',
                'CFBundlePackageType': 'APPL',
                'CFBundleExecutable': 'run',
                'LSUIElement': True,
            }, output)
        launcher = contents / 'MacOS/run'
        launcher.write_text(f'''#!{sys.executable}
import pathlib
import runpy
import sys

script = pathlib.Path(__file__).resolve().parents[1] / 'Resources/spotify_actions.py'
sys.argv = [str(script), {action!r}]
runpy.run_path(str(script), run_name='__main__')
''')
        launcher.chmod(0o755)
        print(f'Installed {app}')


if __name__ == '__main__':
    main()
