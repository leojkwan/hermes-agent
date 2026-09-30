"""Stop a private cua-driver daemon when the Hermes process that started it dies without cleanup.

``_EmbeddedCuaDaemon.start()`` runs this in its own session with the read end of a pipe on stdin and keeps the
write end. A clean ``stop()`` writes one byte first. EOF without that byte means the owner died without stopping
the daemon: SIGKILL, SIGTERM, a crash. atexit never runs on a signal, and on macOS launchd owns the daemon from
birth, so nothing else would stop it. Standard library only: it runs as ``python -I``, outside the Hermes tree.
"""

import os
import subprocess
import sys
import time

_READY_WAIT_SECONDS = 15.0  # the owner may die mid-startup; the daemon's socket can still appear afterwards


def main(argv):
    command, socket_path = argv[1], argv[2]
    # The owner must know the guard is executing before it launches the daemon.
    os.write(1, b"r")
    os.close(1)
    if os.read(0, 1):
        return 0
    deadline = time.monotonic() + _READY_WAIT_SECONDS
    while not os.path.exists(socket_path) and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        subprocess.run([command, "stop", "--socket", socket_path], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.remove(socket_path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
