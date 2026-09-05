"""One long-lived ``rclone rcd``, shared by every rclone-backed upload.

A fresh ``rclone`` process spends seconds re-creating the remote before it does
any work at all: resolving the drive, walking the path, opening a TLS
connection. Measured against OneDrive on a developer machine, a ``copyto
--dry-run`` that transfers nothing cost 3.6 s and a single ``lsjson --stat``
6.9 s — of which only 0.45 s was starting the executable. The pipeline needs
three such calls per photo, so twenty seconds of every upload were spent
re-learning what rclone already knew a moment earlier.

The daemon keeps that state warm between operations, which takes the same calls
to roughly 0.6 s. It listens on the loopback interface only, on a port nobody is
told about, behind a password generated fresh for each run and passed through
the environment rather than through argv, where any other process on the machine
could read it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
from collections import deque

import aiohttp

from ..core.errors import RetryableError, StorageError

log = logging.getLogger(__name__)

#: How long a freshly spawned daemon has to answer before we give up on it.
STARTUP_TIMEOUT = 20.0
#: Default ceiling for one call. Transfers pass their own, much larger, timeout.
CALL_TIMEOUT = 180
#: The username half of the loopback credentials; the password is generated.
RC_USER = "fotohu"


class RcloneRemoteError(StorageError):
    """An RC call came back with an error status.

    ``status`` is the HTTP code, which is how callers tell "no such directory"
    (404) from a real failure without matching on message text.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RcloneDaemon:
    """Owns the ``rclone rcd`` process and talks to it over the RC API."""

    def __init__(self, binary: str = "rclone", config_path: str | None = None) -> None:
        self.binary = binary
        self.config_path = config_path
        self._proc: asyncio.subprocess.Process | None = None
        self._session: aiohttp.ClientSession | None = None
        self._url: str | None = None
        self._auth: str | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None
        #: The last few lines rclone complained about, for the startup error.
        self._recent_errors: deque[str] = deque(maxlen=5)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ----------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            await self._stop_locked()
            await self._start_locked()

    async def restart(self) -> None:
        async with self._lock:
            await self._stop_locked()
            await self._start_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _start_locked(self) -> None:
        binary = shutil.which(self.binary) or (
            self.binary if os.path.isfile(self.binary) else None
        )
        if binary is None:
            raise StorageError(
                f"rclone binary '{self.binary}' not found on PATH — install rclone "
                "or set RCLONE_BINARY"
            )

        port = _free_port()
        password = secrets.token_urlsafe(24)
        args = [binary]
        if self.config_path:
            args += ["--config", self.config_path]
        args += ["rcd", "--rc-addr", f"127.0.0.1:{port}", "--log-level", "ERROR"]

        self._recent_errors.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            env={**os.environ, "RCLONE_RC_USER": RC_USER, "RCLONE_RC_PASS": password},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._url = f"http://127.0.0.1:{port}"
        self._auth = base64.b64encode(f"{RC_USER}:{password}".encode()).decode()
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
        await self._await_ready()
        log.info("rclone daemon ready on 127.0.0.1:%d", port)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Keep the pipe empty — a full one would wedge the daemon — and keep a tail.

        Deliberately ``debug``: rclone logs at ERROR every rejection it hands back,
        including the ones we ask for on purpose. Listing a month folder that does
        not exist yet is how the pipeline learns the name is free, and reporting
        that as a warning meant every first-of-the-month upload looked like a
        failure in the log. Anything that actually breaks an upload reaches us as
        the RC response instead, and the worker logs it with its own context.
        """
        assert proc.stderr is not None
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").strip()
            if text:
                self._recent_errors.append(text)
                log.debug("rclone daemon: %s", text)

    async def _await_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_TIMEOUT
        while loop.time() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                break
            try:
                await self._post("core/version", {}, timeout=2)
                return
            except (aiohttp.ClientError, TimeoutError, OSError):
                await asyncio.sleep(0.2)
        detail = "; ".join(self._recent_errors) or "it printed nothing"
        await self._stop_locked()
        raise StorageError(f"the rclone daemon did not come up: {detail}")

    async def _stop_locked(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            # Ask first: core/quit lets rclone finish writing its config file,
            # which is where a refreshed OAuth token has just been saved.
            with contextlib.suppress(Exception):
                await self._post("core/quit", {}, timeout=5)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._proc = None
        self._session = None
        self._url = None
        self._auth = None
        self._stderr_task = None

    # ---------------------------------------------------------------------- calls

    async def call(self, path: str, payload: dict, *, timeout: int = CALL_TIMEOUT) -> dict:
        if not self.running:
            await self.start()
        try:
            return await self._post(path, payload, timeout=timeout)
        except (aiohttp.ClientConnectionError, ConnectionResetError) as exc:
            # It died mid-flight. One restart, then hand the failure to the
            # worker's own backoff rather than looping here.
            log.warning("rclone daemon stopped answering (%s); restarting it", exc)
            await self.restart()
            try:
                return await self._post(path, payload, timeout=timeout)
            except (aiohttp.ClientConnectionError, ConnectionResetError) as retry_exc:
                raise RetryableError(f"rclone daemon unreachable: {retry_exc}") from retry_exc
        except TimeoutError as exc:
            raise RetryableError(f"rclone {path} timed out after {timeout}s") from exc

    async def _post(self, path: str, payload: dict, *, timeout: float) -> dict:
        session = await self._ensure_session()
        assert self._url is not None
        async with session.post(
            f"{self._url}/{path}",
            json=payload,
            headers={"Authorization": f"Basic {self._auth}"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            body = await response.read()
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError:
                data = {"error": (body or b"").decode(errors="replace")[:300]}
            if response.status >= 400:
                message = str(data.get("error") or data)[:400]
                raise RcloneRemoteError(f"{path}: {message}", response.status)
            return data

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session


__all__ = ["RcloneDaemon", "RcloneRemoteError", "CALL_TIMEOUT", "RC_USER"]
