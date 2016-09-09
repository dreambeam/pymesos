import os
import re
import sys
import json
import time
import errno
import select
import signal
import socket
import thread
import logging
from six import reraise
from threading import Thread, RLock
from http_parser.http import HttpParser

logger = logging.getLogger(__name__)

_exc_info = None


def _raise(e):
    global _exc_info
    try:
        raise e
    except Exception:
        _exc_info = sys.exc_info()
        thread.interrupt_main()


def _handle_sigint(signum, frame):
    global _prev_handler, _exc_info
    assert signum == signal.SIGINT
    if _exc_info is not None:
        exc_info = _exc_info
        _exc_info = None
        reraise(*exc_info)
    elif _prev_handler is not None:
        return _prev_handler(signum, frame)

    raise KeyboardInterrupt

_prev_handler = signal.signal(signal.SIGINT, _handle_sigint)


LENGTH_PATTERN = re.compile(br'\d+\n')


class Connection(object):

    def __init__(self, addr, callback):
        host, port = addr.split(':', 2)
        port = int(port)
        self._addr = (host, port)
        self._sock = socket.socket()
        self._sock.setblocking(True)
        self._sock.connect(self._addr)
        self._parser = HttpParser()
        self._callback = callback
        self._stream_id = None
        self._request = callback.gen_request()
        self._response = ''

    @property
    def addr(self):
        return self._addr

    @property
    def stream_id(self):
        return self._stream_id

    def write(self):
        try:
            sent = self._sock.send(self._request)
            self._request = self._request[sent:]
            return True
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return True

            logger.exception('Failed to send to %s', self._addr)
            return False

    def read(self):
        try:
            buf = self._sock.recv(select.PIPE_BUF)
            n_recv = len(buf)
            if n_recv == 0:
                logger.error('Remote %s closed', self.addr)
                return False

            n_parsed = self._parser.execute(buf, n_recv)
            if n_parsed != n_recv:
                logger.error('Failed to parse')
                return False

            if self._stream_id is None and self._parser.is_headers_complete():
                code = self._parser.get_status_code()
                if code != 200:
                    logger.error('Failed with HTTP %s', code)
                    return False
                if not self._parser.is_chunked():
                    logger.error('Response is not chunked')
                    return False

                self._stream_id = self._parser.get_headers().get(
                    'Mesos-Stream-Id', ''
                )
                self._callback.stream_id = self._stream_id

            if self._parser.is_partial_body():
                self._response += self._parser.recv_body()
                while True:
                    m = LENGTH_PATTERN.match(self._response)
                    if not m:
                        break

                    captured = m.group(0)
                    length = int(captured.strip())
                    if len(self._response) >= len(captured) + length:
                        data = self._response[
                            len(captured):len(captured) + length]
                        self._response = self._response[
                            len(captured) + length:]
                        try:
                            event = json.loads(data.decode('utf-8'))
                            self._callback.on_event(event)
                        except Exception:
                            logger.error('Failed parse json')
                            return False

            if self._parser.is_message_complete():
                logger.error('Response EOF')
                return False

            return True
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return True

            logger.exception('Failed to recv from %s', self._addr)
            return False

    def want_write(self):
        return bool(self._request)

    def fileno(self):
        return self._sock.fileno()

    def close(self):
        self._sock.close()
        self._sock = None
        self._parser = None
        self._request = None
        self._response = None


class Process(object):

    def __init__(self, master=None):
        self._master = None
        self._stop = False
        self._lock = RLock()
        self._wakeup_fds = os.pipe()
        self._io_thread = None
        self._new_master = master
        self._stream_id = None

    @property
    def master(self):
        with self._lock:
            return self._master

    @property
    def stream_id(self):
        with self._lock:
            return self._stream_id

    @stream_id.setter
    def stream_id(self, _stream_id):
        with self._lock:
            self._stream_id = _stream_id

    def gen_request(self):
        raise NotImplementedError

    def on_event(self, event):
        raise NotImplementedError

    def change_master(self, new_master):
        with self._lock:
            self._new_master = new_master

        self._notify()

    def _notify(self):
        with self._lock:
            if self._wakeup_fds:
                os.write(self._wakeup_fds[1], b'\0')

    def _run(self):
        try:
            with self._lock:
                _wakeup_fd = self._wakeup_fds[0]

            conn = None
            self.stream_id = None
            next_connect_deadline = 0
            CONNECT_TIMEOUT = 2
            CONNECT_RETRY_INTERVAL = 2
            while True:
                to_write = set()
                to_read = set([_wakeup_fd])
                with self._lock:
                    if self._stop:
                        break

                    if self._new_master != self._master:
                        if conn is not None:
                            conn.close()
                            conn = None
                            self.stream_id = None
                            next_connect_deadline = (
                                time.time() + CONNECT_RETRY_INTERVAL
                            )

                        self._master = self._new_master

                    if (conn is None and self._master is not None and
                            time.time() > next_connect_deadline):
                        conn = Connection(self._master, self)

                if conn is not None:
                    if conn.want_write():
                        to_write.add(conn.fileno())

                    to_read.add(conn.fileno())

                now = time.time()
                if next_connect_deadline > now:
                    timeout = next_connect_deadline - now
                elif next_connect_deadline + CONNECT_TIMEOUT > now:
                    timeout = next_connect_deadline + CONNECT_TIMEOUT - now
                else:
                    timeout = None

                readable, writeable, _ = select.select(
                    to_read, to_write, [], timeout
                )

                for fd in writeable:
                    assert fd == conn.fileno()
                    next_connect_deadline = 0
                    if not conn.write():
                        conn.close()
                        conn = None
                        self.stream_id = None
                        next_connect_deadline = (
                            time.time() + CONNECT_RETRY_INTERVAL
                        )

                for fd in readable:
                    if fd == _wakeup_fd:
                        os.read(_wakeup_fd, select.PIPE_BUF)
                    elif fd == conn.fileno():
                        if not conn.read():
                            conn.close()
                            conn = None
                            self.stream_id = None
                            next_connect_deadline = (
                                time.time() + CONNECT_RETRY_INTERVAL
                            )

                if (conn is not None and next_connect_deadline != 0 and
                        time.time() > next_connect_deadline + CONNECT_TIMEOUT):
                    logger.error('Connect to %s timeout', conn.addr)
                    conn.close()
                    conn = None
                    self.stream_id = None
                    next_connect_deadline = (
                        time.time() + CONNECT_RETRY_INTERVAL
                    )

        except Exception as e:
            with self._lock:
                self._stop = True

            _raise(e)

        finally:
            with self._lock:
                r, w = self._wakeup_fds
                os.close(r)
                os.close(w)
                self._wakeup_fds = None

    def start(self):
        self._io_thread = Thread(target=self._run)
        self._io_thread.daemon = True
        self._io_thread.start()

    def stop(self):
        with self._lock:
            self._stop = True

        self._notify()
