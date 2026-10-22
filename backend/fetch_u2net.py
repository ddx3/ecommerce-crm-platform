"""Downloads the u2net ML model at image build time.

Why a separate script instead of a plain `new_session('u2net')`: the GitHub releases CDN
cuts off a continuous transfer of a large file (~176 MB): the connection
is established, but the body breaks off with RemoteDisconnected. Range requests
work fine, though. So we download in chunks with retries, and only then
hand control to rembg, which will just verify the md5 of the existing file.

We try the normal path first (in case the network is fine); the fallback kicks in
only when the direct path failed.
"""
import hashlib
import os
import sys
import time
import urllib.request

URL = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx'
DEST = '/root/.u2net/u2net.onnx'
EXPECTED_MD5 = '60024c5c889badc19c04ad937298a77b'
CHUNK = 8 * 1024 * 1024
MAX_FAILS = 10


def _direct() -> bool:
    try:
        from rembg import new_session
        new_session('u2net')
        return True
    except Exception as exc:
        print(f'u2net: direct download failed ({type(exc).__name__}), downloading in chunks', flush=True)
        return False


def _chunked() -> None:
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    got, fails = 0, 0
    digest = hashlib.md5()
    with open(DEST, 'wb') as fh:
        while True:
            req = urllib.request.Request(URL, headers={'Range': f'bytes={got}-{got + CHUNK - 1}'})
            try:
                data = urllib.request.urlopen(req, timeout=60).read()
            except Exception as exc:
                fails += 1
                if fails > MAX_FAILS:
                    raise RuntimeError(f'u2net: gave up at byte {got}: {exc}') from exc
                time.sleep(2 * fails)
                continue
            fails = 0
            if not data:
                break
            fh.write(data)
            digest.update(data)
            got += len(data)
            if len(data) < CHUNK:
                break

    if digest.hexdigest() != EXPECTED_MD5:
        os.remove(DEST)
        raise RuntimeError(f'u2net: md5 mismatch ({digest.hexdigest()}), file removed')
    print(f'u2net: downloaded {got} bytes, md5 ok', flush=True)


if __name__ == '__main__':
    if _direct():
        sys.exit(0)
    _chunked()
    # Control run: rembg must pick up the ready file without the network.
    from rembg import new_session
    new_session('u2net')
