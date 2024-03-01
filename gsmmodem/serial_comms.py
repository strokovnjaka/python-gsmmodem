#!/usr/bin/env python

""" Low-level serial communications handling """

import logging
import re
import asyncio
import traceback
import serial_asyncio_fast # https://github.com/home-assistant-libs/pyserial-asyncio-fast
import threading

from .exceptions import TimeoutException


class SerialComms():
    """ Wraps all low-level serial communications (actual read/write operations) """

    # logger
    _log = logging.getLogger('gsmmodem.serial_comms.SerialComms')
    # protocol's transport
    _transport = None
    # device's receive buffer
    _rxBuffer = bytearray()
    # expected response terminator sequence
    _expectResponseTermSeq_lock = threading.Lock()
    _expectResponseTermSeq = None
    # buffer containing responses to a written command
    _response = None
    # queue for getting requested responses out
    _responseQueue = None
    # buffer containing lines from an unsolicited notification from the modem
    _notification = []
    # reader, writer
    _reader = None
    _writer = None
    # reading task
    _reading_task = None
    # let's go! flag
    _started_lock = threading.Lock()
    _started = None

    # End-of-line read terminator
    RX_EOL_SEQ = b'\r\n'
    # End-of-response terminator
    RESPONSE_TERM = re.compile('^OK|ERROR|(\+CM[ES] ERROR: \d+)|(COMMAND NOT SUPPORT)$')

    def __init__(self, port, baudrate=115200, notifyCallbackFunc=None, fatalErrorCallbackFunc=None, *args, **kwargs):
        """ Constructor

        :param fatalErrorCallbackFunc: function to call if a fatal error occurs in the serial device reading thread
        :type fatalErrorCallbackFunc: func
        """
        self._log.debug(f"Initializing serial on {port}")
        # serial port
        self._port = port
        # baud rate for communication
        self._baudrate = baudrate
        # callback for non requested responses
        self._notificationCallback = notifyCallbackFunc
        # callback for fatal errors
        self._fatalErrorCallback = fatalErrorCallbackFunc
        # additional arguments for opening serial port
        self._com_args = args
        self._com_kwargs = kwargs

    def _init_response_queue(self):
        if not self._responseQueue:
            self._responseQueue = asyncio.Queue()

    def _handle_line(self, line, checkForResponseTerm):
        if self._response is not None:
            # a response has been requested on write
            self._response.append(line)
            if not checkForResponseTerm or self.RESPONSE_TERM.match(line):
                self._init_response_queue()
                self._responseQueue.put_nowait(self._response)
                self._response = None
        else:
            # nothing was waiting for this - treat it as a notification
            self._notification.append(line)

    def _read(self, data):
        self._log.debug(f"Read [{self._port}]: {data.decode().strip()}")
        self._rxBuffer += data
        lines = self._rxBuffer.split(self.RX_EOL_SEQ)
        for line in lines[:-1]:
            self._handle_line(line.decode(), not (self._expectResponseTermSeq == line))
        self._rxBuffer = lines[-1]
        if not self._rxBuffer and self._notification:
            # nothing else waiting for this notification
            self._log.debug('Notification: %s', self._notification)
            if self._notificationCallback:
                asyncio.run_coroutine_threadsafe(self._notificationCallback(self._notification), self._loop)
            self._notification = []

    def _init_started(self):
        # self._log.debug(f'init_started: {self._started}')
        with self._started_lock:
            if self._started is None:
                self._started = asyncio.Event()

    async def _open(self):
        """ Opens serial communication with the device """
        self._log.debug(f"Opening [{self._port}]")
        self._init_started()
        self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
            loop=self._loop, url=self._port, baudrate=self._baudrate,
            *self._com_args,**self._com_kwargs
        )
        self._started.set()
        while True:
            # self._reading_task = self._reader.readuntil(self.RX_EOL_SEQ)
            try:
                self._reading_task = asyncio.ensure_future(self._reader.readuntil(self.RX_EOL_SEQ))
                self._log.debug(f"Reading task await")
                r = await self._reading_task
                self._log.debug(f"Reading task result = {r}")
                self._read(r)
            except asyncio.CancelledError:
                self._log.debug(f"Reading task CancelledError")
                break
            except Exception as e:
                self._log.debug(f"Serial error: {e}")
                self._log.debug(traceback.format_exc())
                if self._fatalErrorCallback:
                    self._loop.call_soon_threadsafe(self._fatalErrorCallback(e))
                break
            self._reading_task = None
        self._log.debug(f"Finished [{self._port}]")

    async def close(self):
        """ Closes serial communication with the device """
        self._log.debug('Closing the device')
        asyncio.run_coroutine_threadsafe(self._close(), self._loop)

    async def _close(self):
        if self._reading_task:
            self._log.debug(f"Closing writer")
            self._writer.close()
            self._log.debug(f"Cancelling reading task")
            self._reading_task.cancel()
            # sleep for _reading_task to get cancelled
            while not self._reading_task.cancelled():
                await asyncio.sleep(0.0001)
            self._log.debug(f"Reading task {'is' if self._reading_task.cancelled() else 'not'} cancelled")
        else:
            self._log.debug(f"Nothing to close [{self._port}]")
        if self._loop:
            self._log.debug("Stopping the loop")
            self._loop.stop()
            self._log.debug("Loop stopped")
        self._clear_serial()

    def _clear_serial(self):
        self._reading_task = None
        self._reader = None
        self._writer = None
        self._loop = None

    async def write(self, data, waitForResponse=True, timeout=5, expectedResponseTermSeq=None):
        """ Writes data to serial device """
        # self._log.debug(f"write [{self._port}]: {data}")
        future = asyncio.run_coroutine_threadsafe(self._write(data, waitForResponse, expectedResponseTermSeq), self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            with self._expectResponseTermSeq_lock:
                self._expectResponseTermSeq = None
            raise TimeoutException()

    async def _write(self, data, waitForResponse, expectedResponseTermSeq):
        """ Writes data to serial device """
        self._log.debug(f"_write [{self._port}]: {str(data).strip()} -> expecting ({waitForResponse}) {expectedResponseTermSeq}")
        self._init_started()
        await self._started.wait()
        if waitForResponse:
            if expectedResponseTermSeq:
                with self._expectResponseTermSeq_lock:
                    self._expectResponseTermSeq = bytearray(expectedResponseTermSeq.encode())
            self._response = []
        if self._writer:
            self._writer.write(data.encode())
            await self._writer.drain()
        if waitForResponse:
            self._init_response_queue()
            response = await self._responseQueue.get()
            with self._expectResponseTermSeq_lock:
                self._expectResponseTermSeq = None
            return response

    def connect(self):
        """ Start serial communication in another thead """
        self._log.debug('Starting serial')
        def theloop(loop):
            self._log.debug('Thread SerialComms started')
            asyncio.set_event_loop(loop)
            loop.run_forever()
            self._log.debug('Thread SerialComms finished')

        self._loop = asyncio.new_event_loop()
        threading.Thread(target=lambda: theloop(self._loop)).start()
        asyncio.run_coroutine_threadsafe(self._open(), self._loop)
        self._log.debug('Serial started')
