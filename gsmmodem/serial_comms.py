#!/usr/bin/env python

""" Low-level serial communications handling """

import logging
import re
import asyncio
# from serial_asyncio import open_serial_connection
import serial_asyncio # https://github.com/pyserial/pyserial-asyncio

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
        self._port = port
        self._baudrate = baudrate

        # expected response terminator sequence
        self._expectResponseTermSeq = None
        # buffer containing response to a written command
        self._response = None
        # buffer containing lines from an unsolicited notification from the modem
        self._notification = []
        # protocol's transport
        self._transport = None
        self._rxBuffer = bytearray()
        self._responseQueue = asyncio.Queue()

        self.notifyCallback = notifyCallbackFunc or self._placeholderCallback
        self.fatalErrorCallback = fatalErrorCallbackFunc or self._placeholderCallback

        self.com_args = args
        self.com_kwargs = kwargs

    def _placeholderCallback(self, *args, **kwargs):
        """ Placeholder callback function (does nothing) """

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
        if exc:
            self.fatalErrorCallback(exc)

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
            self.notifyCallback(self._notification)
            self._notification = []

    def eof_received(self):
        """ Called when EOF is received
        
        From asyncio.Protocol.
        """
        self.log.debug(f"EOF received on {self._port}")

    def connect(self):
        """ Connects to the device and gets reader enqueued to execution """
        loop = asyncio.get_event_loop()
        coro = serial_asyncio.create_serial_connection(loop, self, self._port, 
                                                baudrate=self._baudrate, 
                                                *self.com_args,**self.com_kwargs)
        asyncio.ensure_future(coro, loop=loop)

    def close(self):
        """ Stops the protocol, closing the serial transport """
        self._transport.close()

    async def write(self, data, waitForResponse=True, timeout=5, expectedResponseTermSeq=None):
        if waitForResponse:
            if expectedResponseTermSeq:
                self._expectResponseTermSeq = bytearray(expectedResponseTermSeq.encode())
            self._response = []
        self._transport.serial.write(data.encode())
        if waitForResponse:
            try:
                async with asyncio.timeout(timeout):
                    response = await self._responseQueue.get()
            except TimeoutError:
                self._expectResponseTermSeq = None
                raise TimeoutException()
            self._expectResponseTermSeq = None
            return response
