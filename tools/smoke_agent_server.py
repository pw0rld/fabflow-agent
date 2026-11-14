"""Boot the actual CLI without AI credentials, using an isolated remote store."""
import json
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener


def main():
    if platform.system() != 'Linux':
        raise SystemExit('Run this smoke check on the remote build server only.')
    project = Path(__file__).resolve().parents[1]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('OPENAI_', 'FABFLOW_', 'DSH_'))}
    environment['PYTHONUNBUFFERED'] = '1'
    with tempfile.TemporaryDirectory(prefix='fabflow-sdk-server-smoke-') as temporary:
        root = Path(temporary)
        process = subprocess.Popen([sys.executable, str(project / 'src/workbench_server.py'),
            '--root', str(root / 'tasks'), '--price-db', str(root / 'prices.sqlite'),
            '--reader-root', str(root / 'reader'), '--agent-backend', 'openai-agents', '--port', '0'],
            cwd=project, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 20
            port = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Isolated server exited before readiness.')
                if selector.select(timeout=.2):
                    line = process.stdout.readline().strip()
                    prefix = 'Quotation workbench listening on 127.0.0.1:'
                    if line.startswith(prefix):
                        port = int(line[len(prefix):]); break
            selector.close()
            if port is None:
                raise RuntimeError('Isolated server did not report readiness.')
            with build_opener(ProxyHandler({})).open('http://127.0.0.1:' + str(port) + '/healthz', timeout=5) as reply:
                if reply.status != 200 or json.load(reply).get('ok') is not True:
                    raise RuntimeError('Isolated server health response was invalid.')
            print(json.dumps({'ok': True, 'backend': 'openai_agents', 'configured_model': False,
                              'provider_requests': 0, 'production_store_used': False}))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
