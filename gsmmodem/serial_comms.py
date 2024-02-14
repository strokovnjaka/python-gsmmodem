#!/usr/bin/env python

""" Low-level serial communications handling """

import logging
import re
import asyncio
# from serial_asyncio import open_serial_connection
import serial_asyncio # https://github.com/pyserial/pyserial-asyncio
import threading

from .exceptions import TimeoutException


class SerialComms(asyncio.Protocol):
    """ Wraps all low-level serial communications (actual read/write operations) """

    log = logging.getLogger('gsmmodem.serial_comms.SerialComms')

    # End-of-line read terminator
    RX_EOL_SEQ = b'\r\n'
    # End-of-response terminator
    RESPONSE_TERM = re.compile('^OK|ERROR|(\+CM[ES] ERROR: \d+)|(COMMAND NOT SUPPORT)$')

    def __init__(self, port, baudrate=115200, notifyCallbackFunc=None, fatalErrorCallbackFunc=None, *args, **kwargs):
        """ Constructor

        :param fatalErrorCallbackFunc: function to call if a fatal error occurs in the serial device reading thread
        :type fatalErrorCallbackFunc: func
        """
        # serial port
        self._port = port
        # baud rate for communication
        self._baudrate = baudrate

        # protocol's transport
        self._transport = None
        # device's receive buffer
        self._rxBuffer = bytearray()
        # expected response terminator sequence
        self._expectResponseTermSeq = None
        # buffer containing responses to a written command
        self._response = None
        # queue for getting requested responses out
        self._responseQueue = asyncio.Queue()
        # buffer containing lines from an unsolicited notification from the modem
        self._notification = []

        # callback for non requested responses
        self._notificationCallback = notifyCallbackFunc
        # callback for fatal errors
        self._fatalErrorCallback = fatalErrorCallbackFunc

        # additional arguments for opening serial port
        self._com_args = args
        self._com_kwargs = kwargs

    def connection_made(self, transport):
        """ Called when connection is made 
        
        From asyncio.Protocol.
        """
        self.log.debug(f"Connection made to {self._port}")
        self._transport = transport

    def connection_lost(self, exc):
        """ Called when connection is lost 
        
        From asyncio.Protocol.
        """
        self.log.debug(f"Connection with {self._port} lost: {exc}")
        if exc and self._fatalErrorCallback:
            self._loop.call_soon_threadsafe(self._fatalErrorCallback(exc))

    def pause_writing(self):
        """ Called when writing is paused
        
        From asyncio.Protocol.
        """
        self.log.debug(f"Pause writing to {self._port}")

    def resume_writing(self):
        """ Called when writing is resumed 
        
        From asyncio.Protocol.
        """
        self.log.debug(f"Resume writing to {self._port}")

    def _handleLineRead(self, line, checkForResponseTerm):
        if self._response:
            # a response has been requested on write
            self._response.append(line)
            if not checkForResponseTerm or self.RESPONSE_TERM.match(line):
                self._responseQueue.put_nowait(self._response)
                self._response = []
        else:
            # nothing was waiting for this - treat it as a notification
            self._notification.append(line)

    def data_received(self, data):
        """ Called when data is received
        
        From asyncio.Protocol.
        """
        self.log.debug(f"Data received on {self._port}: {data.decode()}")
        self._rxBuffer += data
        lines = self._rxBuffer.split(self.RX_EOL_SEQ)
        for line in lines[:-1]:
            self._handleLinesRead(line.decode(), not (self._expectResponseTermSeq == line))
        self._rxBuffer = lines[-1]
        if not self._rxBuffer and self._notification:
            # nothing else waiting for this notification
            self.log.debug('Notification: %s', self._notification)
            if self._notificationCallback:
                self._loop.call_soon_threadsafe(self._notificationCallback(self._notification))
            self._notification = []

    def eof_received(self):
        """ Called when EOF is received
        
        From asyncio.Protocol.
        """
        self.log.debug(f"EOF received on {self._port}")

    def connect(self):
        """ Connects to the device and gets reader enqueued to execution """
        self._loop = asyncio.get_event_loop()
        threading.Thread(target=self._loop.run_forever)
        coro = serial_asyncio.create_serial_connection(self._loop, self, self._port, 
                                                baudrate=self._baudrate, 
                                                *self._com_args,**self._com_kwargs)
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def close(self):
        """ Stops the protocol, closing the serial transport """
        self._loop.call_soon_threadsafe(self._transport.close())

    async def write(self, data, waitForResponse=True, timeout=5, expectedResponseTermSeq=None):
        future = asyncio.run_coroutine_threadsafe(self._write(data, waitForResponse, expectedResponseTermSeq))
        try:
            return future.result(timeout)
        except TimeoutError:
            self._loop.call_soon_threadsafe(self._clear_erts())
            raise TimeoutException()

    async def _clear_erts(self):
        self._expectResponseTermSeq = None

    async def _write(self, data, waitForResponse, expectedResponseTermSeq):
        if waitForResponse:
            if expectedResponseTermSeq:
                self._expectResponseTermSeq = bytearray(expectedResponseTermSeq.encode())
            self._response = []
        self._transport.serial.write(data.encode())
        if waitForResponse:
            response = await self._responseQueue.get()
            self._expectResponseTermSeq = None
            return response
