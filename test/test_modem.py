#!/usr/bin/env python
# -*- coding: utf8 -*-

""" Test suite for gsmmodem.modem """

import asyncio
import queue

import sys
import threading
import time
import logging
import codecs
from unittest import IsolatedAsyncioTestCase
from datetime import datetime
from copy import copy
import unittest

from gsmmodem.exceptions import PinRequiredError, CommandError, InvalidStateException, TimeoutException,\
    CmsError, CmeError, EncodingError
from gsmmodem.modem import StatusReport, Sms, ReceivedSms

import gsmmodem.serial_comms
import gsmmodem.modem
import gsmmodem.pdu
from gsmmodem.util import SimpleOffsetTzInfo

from . import fakemodems

# # Silence logging exceptions
# logging.raiseExceptions = False
# logging.getLogger('gsmmodem').addHandler(logging.NullHandler())

# debug logging
root = logging.getLogger()
root.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

# The fake modem to use (if any)
FAKE_MODEM = None
# # Write callback to use during Serial.__init__() - usually None, but useful for setting write callbacks during modem.connect()
SERIAL_WRITE_CALLBACK_FUNC = None

def set_writeCallbackFunc(wcb=None):
    global SERIAL_WRITE_CALLBACK_FUNC
    SERIAL_WRITE_CALLBACK_FUNC = wcb

response_sequence = queue.Queue()

def set_response_sequence(rs):
    global response_sequence
    for r in rs: response_sequence.put(r)

class MockModem(asyncio.Protocol):
    """ Mock modem protocol that uses whatever is set in FAKE_MODEM for responding """
    
    log = logging.getLogger('gsmmodem.test.MockModem')
    # log.setLevel(logging.INFO)

    _REPONSE_TIME = 0.0123

    def __init__(self):
        self.log.debug(f"Initializing MockModem")
        super().__init__()
        self._transport = None
        global FAKE_MODEM
        if FAKE_MODEM != None:
            self._modem = copy(FAKE_MODEM)
        else:
            self._modem = fakemodems.GenericTestModem()
        self.log.debug("Initialized MockModem")

    def connection_made(self, transport):
        self.log.debug(f"Connection to MockModem made {transport}")
        self._transport = transport

    def data_received(self, data):
        self.log.debug(f"Data received: {str(data).strip()}")
        global SERIAL_WRITE_CALLBACK_FUNC
        if SERIAL_WRITE_CALLBACK_FUNC is not None:
            SERIAL_WRITE_CALLBACK_FUNC(data.decode())
        for r in self._modem.getResponse(data):
            response_sequence.put(r)
        try:
            while True:
                if type(r := response_sequence.get_nowait()) in (float, int):
                    asyncio.sleep(r)
                else:
                    self._transport.write(r.encode())
                    self.log.debug(f"Data sent: {r.strip()}")
        except queue.Empty:
            ...
            
    def connection_lost(self, e):
        self.log.debug(f"Connection to MockModem lost with exception {e}")
        self._transport.close()
        self.log.debug(f"MockModem closed _transport")


_HOST = "127.0.0.1"
_PORT = 8888
_SERIAL = f"socket://{_HOST}:{_PORT}"

mock_modem = None
mock_modem_server = None

class TestUsingMockModem(IsolatedAsyncioTestCase):

    async def startMockModem(self):
        async def mockModemServer(reader, writer):
            self._started.set()
            while True:
                try:
                    self._reading_task = asyncio.ensure_future(self._reader.readuntil(self.RX_EOL_SEQ))
                    # self._log.debug(f"Reading task await")
                    r = await self._reading_task
                    # self._log.debug(f"Reading task result = {r}")
                    self._read(r)
                except asyncio.CancelledError:
                    self._log.debug(f"Reading task CancelledError")
                    break
                except Exception as e:
                    self._log.debug(f"Serial error: {e}")
                    self._log.debug(traceback.format_exc())
                    if self._fatalErrorCallback:
                        asyncio.run_coroutine_threadsafe(self._fatalErrorCallback(e), self._modem_loop)
                    break
                self._reading_task = None
            self._log.debug(f"Finished [{self._port}]")


            data = await reader.read(100)
            message = data.decode()
            addr = writer.get_extra_info('peername')

            print(f"Received {message!r} from {addr!r}")

            print(f"Send: {message!r}")
            writer.write(data)
            await writer.drain()

            print("Close the connection")
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(self.mockModemServer, _HOST, _PORT)
        addrs = ', '.join(str(sock.getsockname()) for sock in server.sockets)
        print(f'Serving on {addrs}')
        async with server:
            await server.serve_forever()


    async def startMockModem(self):
        def theloop(loop):
            self.log.debug('Thread MockModem started')
            asyncio.set_event_loop(loop)
            loop.run_forever()
            self.log.debug(f"Thread MockModem finished")

        self.log.debug(f"Starting theloop")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=lambda: theloop(self._loop), daemon=True)
        self._thread.start()
        self.log.debug(f"Started theloop")

        async def themodem(loop):
            global mock_modem
            global mock_modem_server
            self.log.debug(f"MockModem creating server")
            mock_modem = await loop.create_server(MockModem, _HOST, _PORT)
            self.log.debug(f"MockModem starting serve_forever")
            mock_modem_server = asyncio.create_task(mock_modem.serve_forever())
            self.log.debug(f"MockModem serve_forever started")
            self._started.set()
            self.log.debug(f"MockModem started")

        self.log.debug(f"Starting themodem")
        self._started = threading.Event()
        asyncio.run_coroutine_threadsafe(themodem(self._loop), self._loop)
        self._started.wait()
        self.log.debug(f"Started themodem")

    async def _stopMockModem(self):
        global mock_modem, mock_modem_server
        self.log.debug(f"Closing mock_modem")
        mock_modem.close()
        self.log.debug(f"Cancelling mock_modem_server")
        mock_modem_server.cancel()
        # sleep for mock_modem_server to get cancelled
        self.log.debug(f"Waiting for mock_modem_server to get cancelled")
        while not mock_modem_server.cancelled():
            await asyncio.sleep(0)
        self.log.debug(f"mock_modem_server {'is' if mock_modem_server.cancelled() else 'not'} cancelled")
        self.log.debug(f"Stopping the loop")
        self._loop.stop()
        self.log.debug("Loop stopped")
        self._ended.set()
        self.log.debug("MockModem ended")

    async def stopMockModem(self):
        self.log.debug('Stopping themodem')
        self._ended = threading.Event()
        asyncio.run_coroutine_threadsafe(self._stopMockModem(), self._loop)
        self._ended.wait()
        self.log.debug('Stopped themodem')

    async def asyncSetUp(self):
        self.log.debug("Starting MockModem")
        await self.startMockModem()
        self.log.debug("Creating GsmModem")
        self.modem = gsmmodem.modem.GsmModem(_SERIAL)
        self.log.debug("Connecting GsmModem")
        await self.modem.connect()
        self.log.debug("Connected to GsmModem")

    async def asyncTearDown(self):
        # self.log.debug("Closing mock modem and gsmmodem")
        await self.modem.close()
        # self.log.debug("Closed gsmmodem")
        await self.stopMockModem()
        self.log.debug("Closed mock modem")


class TestGsmModemGeneralApi(TestUsingMockModem):
    """ Tests the API of GsmModem class (excluding connect/close) """
    
    log = logging.getLogger('gsmmodem.test.TestGsmModemGeneralApi')

    modem_lock = asyncio.Lock()

    async def test_manufacturer(self):
        async with self.modem_lock:
            print("TEST: test_manufacturer")
            set_writeCallbackFunc(lambda data: 
                self.assertEqual('AT+CGMI\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CGMI\r', data))
            )
            tests = ['huawei', 'ABCDefgh1235', 'Some Random Manufacturer']
            for test in tests:
                set_response_sequence(['{0}\r\n'.format(test), 'OK\r\n'])
                self.assertEqual(test, await self.modem.manufacturer())
            set_writeCallbackFunc()

    async def test_model(self):
        async with self.modem_lock:
            print("TEST: test_model")
            set_writeCallbackFunc(lambda data: 
                self.assertEqual('AT+CGMM\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CGMM\r', data))
            )
            tests = ['K3715', '1324-Qwerty', 'Some Random Model']
            for test in tests:
                set_response_sequence(['{0}\r\n'.format(test), 'OK\r\n'])
                self.assertEqual(test, await self.modem.model())
            set_writeCallbackFunc()

    async def test_revision(self):
        async with self.modem_lock:
            print("TEST: test_revision")
            set_writeCallbackFunc(lambda data: 
                self.assertEqual('AT+CGMR\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CGMR\r', data))
            )
            tests = ['1', '1324-56768-23414', 'r987']
            for test in tests:
                set_response_sequence(['{0}\r\n'.format(test), 'OK\r\n'])
                self.assertEqual(test, await self.modem.revision())
            # Fake a modem that does not support this command
            response_sequence.put('ERROR\r\n')
            self.assertEqual(None, await self.modem.revision())
            set_writeCallbackFunc()

    async def test_imei(self):
        async with self.modem_lock:
            print("TEST: test_imei")
            set_writeCallbackFunc(lambda data:
                self.assertEqual('AT+CGSN\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CGSN\r', data))
            )
            tests = ['012345678912345']
            for test in tests:
                set_response_sequence(['{0}\r\n'.format(test), 'OK\r\n'])
                self.assertEqual(test, await self.modem.imei())
            set_writeCallbackFunc()
                
    async def test_imsi(self):
        async with self.modem_lock:
            print("TEST: test_imsi")
            set_writeCallbackFunc(lambda data:
                self.assertEqual('AT+CIMI\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CIMI\r', data))
            )
            tests = ['987654321012345']
            for test in tests:
                set_response_sequence(['{0}\r\n'.format(test), 'OK\r\n'])
                self.assertEqual(test, await self.modem.imsi())
            set_writeCallbackFunc()

    # async def test_networkName(self):
    #     async with self.modem_lock:
    #         print("TEST: test_networkName")
    #         set_writeCallbackFunc(lambda data:
    #             self.assertEqual('AT+COPS?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+COPS', data))
    #         )
    #         tests = [('MTN', '+COPS: 0,0,"MTN",2'),
    #                 ('I OMNITEL', '+COPS: 0,0,"I OMNITEL"'),
    #                 (None, 'SOME RANDOM RESPONSE')]
    #         for name, toWrite in tests:
    #             set_response_sequence(['{0}\r\n'.format(toWrite), 'OK\r\n'])
    #             self.assertEqual(name, await self.modem.networkName())
    #         set_writeCallbackFunc()

    # async def test_supportedCommands(self):
    #     async with self.modem_lock:
    #         print("TEST: test_supportedCommands")
    #         def writeCallbackFunc(data):
    #             if data == 'AT\r': # Handle keep-alive AT command
    #                 return
    #             self.assertEqual('AT+CLAC\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CLAC\r', data))
    #         set_writeCallbackFunc(writeCallbackFunc)
    #         tests = ((['+CLAC:&C,D,E,\\S,+CGMM,^DTMF\r\n', 'OK\r\n'], ['&C', 'D', 'E', '\\S', '+CGMM', '^DTMF']),
    #                 (['+CLAC:Z\r\n', 'OK\r\n'], ['Z']),
    #                 (['FGH,RTY,UIO\r\n', 'OK\r\n'], ['FGH', 'RTY', 'UIO']), # nasty, but possible
    #                 # ZTE-like response: do not start with +CLAC, and use multiple lines
    #                 (['A\r\n', 'BCD\r\n', 'EFGH\r\n', 'OK\r\n'], ['A', 'BCD', 'EFGH']),
    #                 # Teleofis response: like ZTE but each command has AT prefix
    #                 (['AT&F\r\n', 'AT&V\r\n', 'AT&W\r\n', 'AT+CACM\r\n', 'OK\r\n'], ['&F', '&V', '&W', '+CACM']),
    #                 # Some Huawei modems have a ZTE-like response, but add an addition \r character at the end of each listed command
    #                 (['Q\r\r\n', 'QWERTY\r\r\n', '^DTMF\r\r\n', 'OK\r\n'], ['Q', 'QWERTY', '^DTMF']))
    #         for responseSequence, expected in tests:
    #             for r in responseSequence: response_sequence.put(r)
    #             commands = await self.modem.supportedCommands()
    #             self.assertEqual(expected, commands)
    #         # TODO: solve this, default reponse should be ERROR I think
    #         # # Fake a modem that does not support this command
    #         # set_response_sequence(['ERROR\r\n'])
    #         # commands = await self.modem.supportedCommands()
    #         # self.assertEqual(None, commands)
    #         # Test unhandled response format
    #         set_response_sequence(['OK\r\n'])
    #         commands = await self.modem.supportedCommands()
    #         self.assertEqual(None, commands)
    #         set_writeCallbackFunc()

    # async def test_smsc(self):
    #     async with self.modem_lock:
    #         print("TEST: test_smsc")
    #         """ Tests reading and writing the SMSC number from the SIM card """
    #         def writeCallbackFunc1(data):
    #             self.assertEqual('AT+CSCA?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCA?', data))
    #         set_writeCallbackFunc(writeCallbackFunc1)
    #         tests = [None, '+12345678']
    #         for test in tests:
    #             if test:
    #                 set_response_sequence(['+CSCA: "{0}",145\r\n'.format(test), 'OK\r\n'])
    #             else:
    #                 set_response_sequence(['OK\r\n'])
    #             self.assertEqual(test, await self.modem.smsc())
    #         # Reset SMSC number internally
    #         self.modem._smscNumber = None
    #         self.assertEqual(await self.modem.smsc(), None)
    #         # Now test setting the SMSC number
    #         for test in tests:
    #             if not test:
    #                 continue
    #             def writeCallbackFunc2(data):
    #                 self.assertEqual('AT+CSCA="{0}"\r'.format(test), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCA="{0}"'.format(test), data))
    #             def writeCallbackFunc3(data):
    #                 # This method should not be called - it merely exists to make sure nothing is written to the modem
    #                 self.fail("Nothing should have been written to modem, but got: {0}".format(data))
    #             set_writeCallbackFunc(writeCallbackFunc2)
    #             await self.modem.smsc(test)
    #             self.assertEqual(test, await self.modem.smsc())
    #             # Now see if the SMSC value was cached properly
    #             set_writeCallbackFunc(writeCallbackFunc3)
    #             self.assertEqual(test, await self.modem.smsc())
    #             await self.modem.smsc(test) # Shouldn't do anything
    #         # Check response if modem returns a +CMS ERROR: 330 (SMSC number unknown) on querying the SMSC
    #         self.modem._smscNumber = None
    #         set_response_sequence(['+CMS ERROR: 330\r\n'])
    #         set_writeCallbackFunc(writeCallbackFunc1)
    #         self.assertEqual(await self.modem.smsc(), None) # Should just return None
    #         set_writeCallbackFunc()

    # async def test_signalStrength(self):
    #     async with self.modem_lock:
    #         print("TEST: test_signalStrength")
    #         """ Tests reading signal strength from the modem """
    #         def writeCallbackFunc(data):
    #             self.assertEqual('AT+CSQ\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSQ', data))
    #         set_writeCallbackFunc(writeCallbackFunc)
    #         tests = (('+CSQ: 18,99', 18),
    #                 ('+CSQ:4,0', 4),
    #                 ('+CSQ: 99,99', -1))
    #         for response, expected in tests:
    #             set_response_sequence(['{0}\r\n'.format(response), 'OK\r\n'])
    #             self.assertEqual(expected, await self.modem.signalStrength())
    #         # Test error condition (unparseable response)
    #         set_response_sequence(['OK\r\n'])
    #         try:
    #             await self.modem.signalStrength()
    #         except CommandError:
    #             pass
    #         else:
    #             self.fail('CommandError not raised on error condition')
    #         set_writeCallbackFunc()

    # async def test_waitForNetorkCoverageNoCreg(self):
    #     async with self.modem_lock:
    #         print("TEST: test_waitForNetorkCoverageNoCreg")
    #         """ Tests waiting for network coverage (no AT+CREG support) """
    #         tests = ((82,),
    #                 (99, 99, 47),)
    #         for seq in tests:
    #             items = iter(seq)
    #             def writeCallbackFunc(data):
    #                 if data == 'AT+CSQ\r':
    #                     try:
    #                         set_response_sequence(['+CSQ: {0},99\r\n'.format(next(items)), 'OK\r\n'])
    #                     except StopIteration:
    #                         self.fail("Too many AT+CSQ writes issued")
    #             set_writeCallbackFunc(writeCallbackFunc)
    #             signalStrength = await self.modem.waitForNetworkCoverage()
    #             self.assertNotEqual(signalStrength, -1, '"Unknown" signal strength returned - should still have blocked')
    #             self.assertEqual(seq[-1], signalStrength, 'Incorrect signal strength returned')
    #         set_writeCallbackFunc()

    # async def test_waitForNetorkCoverage(self):
    #     async with self.modem_lock:
    #         print("TEST: test_waitForNetorkCoverage")
    #         """ Tests waiting for network coverage (normal) """
    #         tests = (('0,2', '0,2', '0,1', 82),
    #                 ('0,5', 47),)
    #         for seq in tests:
    #             items = iter(seq)
    #             def writeCallbackFunc(data):
    #                 if data == 'AT+CSQ\r':
    #                     try:
    #                         set_response_sequence(['+CSQ: {0},99\r\n'.format(next(items)), 'OK\r\n'])
    #                     except StopIteration:
    #                         self.fail("Too many writes issued")
    #                 elif data == 'AT+CREG?\r':
    #                     try:
    #                         set_response_sequence(['+CREG: {0}\r\n'.format(next(items)), 'OK\r\n'])
    #                     except StopIteration:
    #                         self.fail("Too many writes issued")
    #             set_writeCallbackFunc(writeCallbackFunc)
    #             signalStrength = await self.modem.waitForNetworkCoverage()
    #             self.assertNotEqual(signalStrength, -1, '"Unknown" signal strength returned - should still have blocked')
    #             self.assertEqual(seq[-1], signalStrength, 'Incorrect signal strength returned')
    #         # Test InvalidStateException
    #         # 0,3: network registration denied; 0,0: SIM not searching for network
    #         tests = ('0,3', '0,0')
    #         for result in tests:
    #             def writeCallbackFunc(data):
    #                 if data == 'AT+CREG?\r':
    #                     set_response_sequence(['+CREG: {0}\r\n'.format(result), 'OK\r\n'])
    #             set_writeCallbackFunc(writeCallbackFunc)
    #             with self.assertRaises(InvalidStateException):
    #                 await self.modem.waitForNetworkCoverage()
    #         # Test TimeoutException
    #         def writeCallbackFunc2(data):
    #             set_response_sequence(['+CREG: 0,1\r\n', 'OK\r\n'])
    #         set_writeCallbackFunc(writeCallbackFunc2)
    #         with self.assertRaises(TimeoutException):
    #             await self.modem.waitForNetworkCoverage(timeout=1)
    #         set_writeCallbackFunc()

    # async def test_errorTypes(self):
    #     async with self.modem_lock:
    #         print("TEST: test_errorTypes")
    #         """ Tests error type detection- and handling by throwing random errors to commands """
    #         # Throw unnamed error
    #         set_response_sequence(['ERROR\r\n'])
    #         try:
    #             await self.modem.write('AT')
    #         except CommandError as e:
    #             self.assertIsInstance(e, CommandError)
    #             self.assertEqual(e.command, 'AT')
    #             self.assertEqual(e.type, None)
    #             self.assertEqual(e.code, None)
    #         # Throw CME error
    #         set_response_sequence(['+CME ERROR: 22\r\n'])
    #         try:
    #             await self.modem.write('AT+ZZZ')
    #         except CommandError as e:
    #             self.assertIsInstance(e, CmeError)
    #             self.assertEqual(e.command, 'AT+ZZZ')
    #             self.assertEqual(e.type, 'CME')
    #             self.assertEqual(e.code, 22)
    #         # Throw CMS error
    #         set_response_sequence(['+CMS ERROR: 310\r\n'])
    #         try:
    #             await self.modem.write('AT+XYZ')
    #         except CommandError as e:
    #             self.assertIsInstance(e, CmsError)
    #             self.assertEqual(e.command, 'AT+XYZ')
    #             self.assertEqual(e.type, 'CMS')
    #             self.assertEqual(e.code, 310)

    # async def test_smsEncoding(self):
    #     async with self.modem_lock:
    #         print("TEST: test_smsEncoding")
    #         def writeCallbackFunc(data):
    #             if type(data) == bytes:
    #                 data = data.decode()
    #             self.assertEqual('AT+CSCS?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS?', data))
    #         set_writeCallbackFunc(writeCallbackFunc)
    #         tests = [('UNKNOWN', '+CSCSERROR'),
    #                 ('UCS2', '+CSCS: "UCS2"'),
    #                 ('ISO', '+CSCS:"ISO"'),
    #                 ('IRA', '+CSCS: "IRA"'),
    #                 ('UNKNOWN', '+CSCS: ("GSM", "UCS2")'),
    #                 ('UNKNOWN', 'OK'),]
    #         for name, toWrite in tests:
    #             self.modem._smsEncoding = 'UNKNOWN'
    #             set_response_sequence(['{0}\r\n'.format(toWrite), 'OK\r\n'])
    #             self.assertEqual(name, await self.modem.smsEncoding())
    #         set_writeCallbackFunc()

    # async def test_smsSupportedEncoding(self):
    #     async with self.modem_lock:
    #         print("TEST: test_smsSupportedEncoding")
    #         def writeCallbackFunc(data):
    #             if type(data) == bytes:
    #                 data = data.decode()
    #             self.assertEqual('AT+CSCS=?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS?', data))
    #         set_writeCallbackFunc(writeCallbackFunc)
    #         tests = [(['GSM'], '+CSCS: ("GSM")'),
    #                 (['GSM', 'UCS2'], '+CSCS: ("GSM", "UCS2")'),
    #                 (['GSM', 'UCS2'], '+CSCS:("GSM","UCS2")'),
    #                 (['GSM', 'UCS2'], '+CSCS:   (  "GSM"  ,   "UCS2"  )'),
    #                 (['GSM'], '+CSCS: ("GSM" "UCS2")'),]
    #         for name, toWrite in tests:
    #             self.modem._smsSupportedEncodingNames = None
    #             set_response_sequence(['{0}\r\n'.format(toWrite), 'OK\r\n'])
    #             self.assertEqual(name, await self.modem.smsSupportedEncoding())
    #             # let the other threads run
    #             await asyncio.sleep(0)
    #         set_writeCallbackFunc()

class TestUssd(TestUsingMockModem):
    """ Tests USSD session handling """

    log = logging.getLogger('gsmmodem.test.TestGsmModemGeneralApi')

    async def asyncSetUp(self):
        self.tests = [('*101#', 'AT+CUSD=1,"*101#",15\r', '+CUSD: 0,"Available Balance: R 96.45 .",15\r\n', 'Available Balance: R 96.45 .', False),
                 ('*120*500#', 'AT+CUSD=1,"*120*500#",15\r', '+CUSD: 1,"Hallo daar",15\r\n', 'Hallo daar', True),
                 ('*130*111#', 'AT+CUSD=1,"*130*111#",15\r', '+CUSD: 2,"Totsiens",15\r\n', 'Totsiens', False),
                 ('*111*502#', 'AT+CUSD=1,"*111*502#",15\r', '+CUSD: 2,"You have the following remaining balances:\n0 free minutes\n20 MORE Weekend minutes ",15\r\n', 'You have the following remaining balances:\n0 free minutes\n20 MORE Weekend minutes ', False),
                 ('#100#', 'AT+CUSD=1,"#100#",15\r', '+CUSD: 1,"Bal:$100.00 *\r\nExp 01 Jan 2013\r\n1. Recharge\r\n2. Balance\r\n3. My Offer\r\n4. PlusPacks\r\n5. Tones&Extras\r\n6. History\r\n7. CredMe2U\r\n8. Hlp\r\n00. Home\r\n*charges can take 48hrs",15\r\n', 
                  'Bal:$100.00 *\r\nExp 01 Jan 2013\r\n1. Recharge\r\n2. Balance\r\n3. My Offer\r\n4. PlusPacks\r\n5. Tones&Extras\r\n6. History\r\n7. CredMe2U\r\n8. Hlp\r\n00. Home\r\n*charges can take 48hrs', True)]
        return await super().asyncSetUp()

    # TODO: fix
    async def test_sendUssd(self):
        """ Standard USSD tests """
        # tests tuple format: (USSD_STRING_TO_WRITE, MODEM_WRITE, MODEM_RESPONSE, USSD_MESSAGE, USSD_SESSION_ACTIVE)
        for test in self.tests:
            def writeCallbackFunc(data):
                self.assertEqual(test[1], data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format(test[1], data))
            set_response_sequence(['OK\r\n', test[2]])
            set_writeCallbackFunc(writeCallbackFunc)
            ussd = await self.modem.sendUssd(test[0])
            self.assertIsInstance(ussd, gsmmodem.modem.Ussd)
            self.assertEqual(ussd.sessionActive, test[4], 'Session state is invalid for test case: {0}'.format(test))
            self.assertEqual(ussd.message, test[3])
            if ussd.sessionActive:
                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CUSD=2\r', data, 'Invalid data written to modem; expected "AT+CUSD=2", got: "{0}"'.format(data))
                set_writeCallbackFunc(writeCallbackFunc2)
                await ussd.cancel()
            else:
                await ussd.cancel() # This call shouldn't do anything
            del ussd
        set_writeCallbackFunc()

    # TODO: this is different!
    def test_sendUssd_differentModems(self):
        """ Tests sendUssd functionality with different modem behaviours (some modems require mode switching) """
        tests = [('*101#', 'Testing 123')]
        global FAKE_MODEM
        for ussdStr, ussdResponse in tests:
            for fakeModem in fakemodems.createModems():
                fakeModem.responses['AT+CUSD=1,"{0}",15\r'.format(ussdStr)] = ['+CUSD: 2,"{0}",15\r\n'.format(ussdResponse), 'OK\r\n']
                # Init modem and preload SMSC number
                FAKE_MODEM = fakeModem
                mockSerial = MockSerialPackage()
                gsmmodem.serial_comms.serial = mockSerial        
                modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
                modem.connect()
                response = modem.sendUssd(ussdStr)
                self.assertEqual(ussdResponse, response.message)
                modem.close()
        FAKE_MODEM = None
    
    async def test_sendUssdReply(self):
        """ Test replying in a USSD session via Ussd.reply() """
        test = ('First menu. Reply with 1 for blah blah blah...', 'Second menu')
        set_response_sequence(['+CUSD: 1,"{0}",15\r\n'.format(test[0]), 'OK\r\n'])
        ussd = await self.modem.sendUssd('*101#')
        self.assertIsInstance(ussd, gsmmodem.modem.Ussd)
        self.assertTrue(ussd.sessionActive, 'Session should be active')
        self.assertEqual(ussd.message, test[0])
        # Reply to this active session
        set_response_sequence(['+CUSD: 2,"{0}",15\r\n'.format(test[1]), 'OK\r\n'])
        ussd = await ussd.reply('1')
        self.assertIsInstance(ussd, gsmmodem.modem.Ussd)
        self.assertFalse(ussd.sessionActive, 'Session should be inactive')
        self.assertEqual(ussd.message, test[1])
        # Reply to inactive session
        with self.assertRaises(gsmmodem.exceptions.InvalidStateException):
            await ussd.reply('2')
        set_writeCallbackFunc()

    async def test_sendUssdResponseBeforeOk(self):
        """ Tests +CUSD responses that arrive before the +CUSD command's OK is issued (non-standard behaviour) - reported by user """
        # tests tuple format: (USSD_STRING_TO_WRITE, MODEM_WRITE, MODEM_RESPONSE, USSD_MESSAGE, USSD_SESSION_ACTIVE)
        for test in self.tests:
            def writeCallbackFunc(data):
                self.assertEqual(test[1], data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format(test[1], data))
            # Note: The +CUSD response will now be sent before the command is acknowledged
            set_response_sequence([test[2], 'OK\r\n'])
            set_writeCallbackFunc(writeCallbackFunc)
            ussd = await self.modem.sendUssd(test[0])
            self.assertIsInstance(ussd, gsmmodem.modem.Ussd)
            self.assertEqual(ussd.sessionActive, test[4], 'Session state is invalid for test case: {0}'.format(test))
            self.assertEqual(ussd.message, test[3])
            if ussd.sessionActive:
                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CUSD=2\r', data, 'Invalid data written to modem; expected "AT+CUSD=2", got: "{0}"'.format(data))
                set_writeCallbackFunc(writeCallbackFunc2)
                await ussd.cancel()
            else:
                await ussd.cancel() # This call shouldn't do anything
            del ussd
        set_writeCallbackFunc()

    # TODO: fix
    async def test_sendUssdExtraRelease(self):
        """ Some modems send an extra +CUSD: 2 message when the USSD session is released - see issue #14 on github """
        tests = (('*100#', 'Wrong order test message', ['+CUSD: 2,"Initiating Release",15\r\n', '+CUSD: 0,"Wrong order test message",15\r\n', 'OK\r\n']),
                 ('*101#', 'Notifications test', ['OK\r\n', '+CUSD: 2,"Initiating Release",15\r\n', '+CUSD: 0,"Notifications test",15\r\n']),
                 ('*101#', 'Test2', ['OK\r\n', '+CUSD: 3,"Other local client responded",15\r\n', '+CUSD: 0,"Test2",15\r\n']))
        for test in tests:            
            set_response_sequence(test[2])
            ussd = await self.modem.sendUssd(test[0])
            self.assertIsInstance(ussd, gsmmodem.modem.Ussd)
            self.assertEqual(ussd.message, test[1], 'Invalid message received; expected "{0}", got "{1}"'.format(test[1], ussd.message))
            self.assertEqual(ussd.sessionActive, False, 'Invalid session state - should be inactive')
            # Make sure the next call does not include any of the USSD extras
            atResponse = await self.modem.write('AT')
            self.assertEqual(len(atResponse), 1)
            self.assertEqual(atResponse[0], 'OK')
            asyncio.sleep(0)
    
    async def test_sendUssdError(self):
        """ Test error handling in a USSD session """
        set_response_sequence(['+CME ERROR: 30\r\n'])
        with self.assertRaises(gsmmodem.exceptions.CmeError):
            await self.modem.sendUssd('*101#')
        set_response_sequence(['+CMS ERROR: 500\r\n'])
        with self.assertRaises(gsmmodem.exceptions.CmsError):
            await self.modem.sendUssd('*101#')
        set_response_sequence(['ERROR\r\n'])
        with self.assertRaises(gsmmodem.exceptions.CommandError):
            await self.modem.sendUssd('*101#')
        
    # TODO: fix
    async def test_sendUssdExtraLinesInResponse(self):
        """ Test parsing USSD response if it contains extra unsolicited notifications """
        tests = (('Notification appended', ['OK\r\n', 0.1, '+CUSD: 2,"Notification appended",15\r\n', 'Some random notification!\r\n']),
                 ('Notification prepended', ['OK\r\n', 0.1, 'Another random notification!\r\n', '+CUSD: 2,"Notification prepended",15\r\n']),
                 ('Notification before OK', ['Yet another random notification!\r\n', 'OK\r\n', 0.1, '+CUSD: 2,"Notification before OK",15\r\n']))
        for message, responseSeq in tests:
            set_response_sequence(responseSeq)
            ussd = await self.modem.sendUssd('*101#')
            self.assertIsInstance(ussd, gsmmodem.modem.Ussd)
            self.assertEqual(ussd.message, message)
    
    # TODO: fix
    async def test_sendUssd_responseTimeout(self):
        """ Test sendUssd() response timeout event """
        # The following should timeout very quickly due to no +CUSD update being issued
        with self.assertRaises(gsmmodem.exceptions.TimeoutException):
            await self.modem.sendUssd(ussdString='*101#', responseTimeout=0.05)


class TestEdgeCases(IsolatedAsyncioTestCase):
    """ Edge-case testing; some modems do funny things during seemingly normal operations """    

    def test_smscPreloaded(self):
        """ Tests reading the SMSC number if it was pre-loaded on the SIM (some modems delete the number during connect()) """
        tests = [None, '+12345678']
        global FAKE_MODEM
        for test in tests:
            for fakeModem in fakemodems.createModems():
                # Init modem and preload SMSC number
                fakeModem.smscNumber = test
                fakeModem.simBusyErrorCounter = 3 # Enable "SIM busy" errors for modem for more accurate testing
                FAKE_MODEM = fakeModem
                mockSerial = MockSerialPackage()
                gsmmodem.serial_comms.serial = mockSerial        
                modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
                modem.connect()
                # Make sure SMSC number was prevented from being deleted (some modems do this when setting text-mode paramters AT+CSMP)
                self.assertEqual(test, modem.smsc, 'SMSC number was changed/deleted during connect()')
                modem.close()
        FAKE_MODEM = None
    
    def test_cfun0(self):
        """ Tests case where a modem's functionality setting is 0 at startup """
        global FAKE_MODEM
        for fakeModem in fakemodems.createModems():
            fakeModem.cfun = 0
            FAKE_MODEM = fakeModem        
            # This should pass without any problem, and AT+CFUN=1 should be set during connect()
            cfunWritten = [False]
            def writeCallbackFunc(data):
                if data == 'AT+CFUN=1\r':
                    cfunWritten[0] = True
            global SERIAL_WRITE_CALLBACK_FUNC
            SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc         
            mockSerial = MockSerialPackage()
            gsmmodem.serial_comms.serial = mockSerial
            modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
            modem.connect()
            SERIAL_WRITE_CALLBACK_FUNC = None
            self.assertTrue(cfunWritten[0], 'Modem CFUN setting not set to 1 during connect()')
            modem.close()
            FAKE_MODEM = None
    
    def test_cfunNotSupported(self):
        """ Tests case where a modem does not support the AT+CFUN command """
        global FAKE_MODEM            
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.cfun = -1 # disable
        FAKE_MODEM.responses['AT+CFUN?\r'] = ['ERROR\r\n']
        FAKE_MODEM.responses['AT+CFUN=1\r'] = ['ERROR\r\n']
        # This should pass without any problem, and AT+CFUN? should at least have been checked during connect()
        cfunWritten = [False]
        def writeCallbackFunc(data):
            if data == 'AT+CFUN?\r':
                cfunWritten[0] = True
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
        modem.connect()
        SERIAL_WRITE_CALLBACK_FUNC = None        
        self.assertTrue(cfunWritten[0], 'Modem CFUN setting not set to 1 during connect()')
        modem.close()
        FAKE_MODEM = None

    def test_commandNotSupported(self):
        """ Some Huawei modems response with "COMMAND NOT SUPPORT" instead of "ERROR" or "OK"; ensure we detect this """
        global FAKE_MODEM
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.responses['AT+WIND?\r'] = ['COMMAND NOT SUPPORT\r\n']
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        modem.connect()
        self.assertRaises(CommandError, modem.write, 'AT+WIND?')
        modem.close()
        FAKE_MODEM = None
        
    def test_wavecomConnectSpecifics(self):
        """ Wavecom-specific test cases that might not be covered by the modem profiles in fakemodems.py
        - this is mostly to attain 100% code coverage in tests
        """
        global FAKE_MODEM
        FAKE_MODEM = copy(fakemodems.WavecomMultiband900E1800())
        # Test the case where AT+CLAC returns a response for Wavecom devices, and it includes +WIND and +VTS
        FAKE_MODEM.responses['AT+CLAC\r'] = ['+CLAC: D,+CUSD,+WIND,+VTS\r\n', 'OK\r\n']
        # Test the case where the +WIND setting is already what we want it to be
        FAKE_MODEM.responses['AT+WIND?\r'] = ['+WIND: 50\r\n', 'OK\r\n']
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        modem.connect()
        self.assertTrue(gsmmodem.modem.Call.dtmfSupport, '+VTS in AT+CLAC response should have indicated DTMF support')
        modem.close()
        FAKE_MODEM = None

    def test_zteConnectSpecifics(self):
        """ ZTE-specific test cases that might not be covered by the modem profiles in fakemodems.py
        - this is mostly to attain 100% code coverage in tests
        """
        global FAKE_MODEM
        FAKE_MODEM = copy(fakemodems.ZteK3565Z())
        # Test the case where AT+CLAC returns a response for ZTE devices, and it includes +ZPAS and +VTS
        FAKE_MODEM.responses['AT+CLAC\r'][-1] = '+ZPAS\r\n'
        FAKE_MODEM.responses['AT+CLAC\r'].append('OK\r\n')
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        modem.connect()
        self.assertTrue(gsmmodem.modem.Call.dtmfSupport, '+VTS in AT+CLAC response should have indicated DTMF support')
        modem.close()
        FAKE_MODEM = None

    def test_huaweiConnectSpecifics(self):
        """ Huawei-specific test cases that might not be covered by the modem profiles in fakemodems.py
        - this is mostly to attain 100% code coverage in tests
        """
        global FAKE_MODEM
        FAKE_MODEM = copy(fakemodems.HuaweiK3715())
        # Test the case where AT+CLAC returns no response for Huawei devices; causing the need for other methods to detect phone type
        FAKE_MODEM.responses['AT+CLAC\r'] = ['ERROR\r\n']
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        modem.connect()
        # Huawei modems should have DTMF support
        self.assertTrue(gsmmodem.modem.Call.dtmfSupport, 'Huawei modems should have DTMF support')
        modem.close()
        FAKE_MODEM = None

    def test_smscSpecifiedBeforeConnect(self):
        """ Tests connect() operation when an SMSC number is set before connect() is called """
        smscNumber = '123454321'
        global FAKE_MODEM
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.smsc = None
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        # Look for the AT+CSCA write
        cscaWritten = [False]
        def writeCallbackFunc(data):
            if data == 'AT+CSCA="{0}"\r'.format(smscNumber):
                cscaWritten[0] = True
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        # Set the SMSC number before calling connect()
        modem.smsc = smscNumber
        self.assertFalse(cscaWritten[0])
        modem.connect()
        self.assertTrue(cscaWritten[0], 'Preset SMSC value not written to modem during connect()')
        self.assertEqual(modem.smsc, smscNumber, 'Pre-set SMSC not stored correctly during connect()')
        modem.close()
        FAKE_MODEM = None

    def test_cpmsNotSupported(self):
        """ Tests case where a modem does not support the AT+CPMS command """
        global FAKE_MODEM            
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.responses['AT+CPMS=?\r'] = ['+CMS ERROR: 302\r\n']
        # This should pass without any problem, and AT+CPMS=? should at least have been checked during connect()
        cpmsWritten = [False]
        def writeCallbackFunc(data):
            if data == 'AT+CPMS=?\r':
                cpmsWritten[0] = True
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
        modem.connect()
        SERIAL_WRITE_CALLBACK_FUNC = None        
        self.assertTrue(cpmsWritten[0], 'Modem CPMS allowed values not checked during connect()')
        modem.close()
        FAKE_MODEM = None

    def test_cnmiNotSupported(self):
        """ Tests case where a modem does not support the AT+CNMI command (but does support other SMS-related commands) """
        global FAKE_MODEM            
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.responses['AT+CNMI=2,1,0,2\r'] = ['ERROR\r\n']
        FAKE_MODEM.responses['AT+CNMI=2,1,0,1,0\r'] = ['ERROR\r\n']
        # This should pass without any problem, and AT+CNMI=2,1,0,2 should at least have been attempted during connect()
        cnmiWritten = [False]
        def writeCallbackFunc(data):
            if data == 'AT+CNMI=2,1,0,2\r':
                cnmiWritten[0] = True
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
        modem.connect()
        SERIAL_WRITE_CALLBACK_FUNC = None        
        self.assertTrue(cnmiWritten[0], 'AT+CNMI setting not written to modem during connect()')
        self.assertFalse(modem._smsReadSupported, 'Modem\'s internal SMS read support flag should be False if AT+CNMI is not supported')
        modem.close()
        FAKE_MODEM = None

    def test_clipNotSupported(self):
        """ Tests case where a modem does not support the AT+CLIP command """
        global FAKE_MODEM            
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.responses['AT+CLIP=1\r'] = ['ERROR\r\n']
        # This should pass without any problem, and AT+CLIP=1 should at least have been attempted during connect()
        clipWritten = [False]
        crcWritten = [False]
        def writeCallbackFunc(data):
            if data == 'AT+CLIP=1\r':
                clipWritten[0] = True
            elif data == 'AT+CRC=1\r':
                crcWritten[0] = True
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
        modem.connect()
        SERIAL_WRITE_CALLBACK_FUNC = None        
        self.assertTrue(clipWritten[0], 'AT+CLIP=1 not written to modem during connect()')
        self.assertFalse(crcWritten[0], 'AT+CRC=1 should not be attempted if AT+CLIP is not supported')
        self.assertFalse(modem._callingLineIdentification, 'Modem\'s internal calling line identification flag should be False if AT+CLIP is not supported')
        self.assertFalse(modem._extendedIncomingCallIndication, 'Modem\'s internal extended calling line identification information flag should be False if AT+CLIP is not supported')
        modem.close()
        FAKE_MODEM = None

    def test_crcNotSupported(self):
        """ Tests case where a modem does not support the AT+CRC command """
        global FAKE_MODEM            
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        FAKE_MODEM.responses['AT+CRC=1\r'] = ['ERROR\r\n']
        # This should pass without any problem, and AT+CRC=1 should at least have been attempted during connect()
        clipWritten = [False]
        crcWritten = [False]
        def writeCallbackFunc(data):
            if data == 'AT+CLIP=1\r':
                clipWritten[0] = True
            elif data == 'AT+CRC=1\r':
                crcWritten[0] = True
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
        modem.connect()
        SERIAL_WRITE_CALLBACK_FUNC = None        
        self.assertTrue(clipWritten[0], 'AT+CLIP=1 not written to modem during connect()')
        self.assertTrue(crcWritten[0], 'AT+CRC=1 not written to modem during connect()')
        self.assertTrue(modem._callingLineIdentification, 'Modem\'s internal calling line identification flag should be True if AT+CLIP is supported')
        self.assertFalse(modem._extendedIncomingCallIndication, 'Modem\'s internal extended calling line identification information flag should be False if AT+CRC is not supported')
        modem.close()
        FAKE_MODEM = None


class TestGsmModemDial(IsolatedAsyncioTestCase):

    async def asyncTearDown(self):
        await self.modem.close()
        global FAKE_MODEM
        FAKE_MODEM = None
    
    async def init_modem(self, modem):
        global FAKE_MODEM
        FAKE_MODEM = modem
        self.mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = self.mockSerial        
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        await self.modem.connect()
    
    async def test_dial(self):
        """ Tests dialing without specifying a callback function """
        
        tests = (['0123456789', '1', '0'],)
        
        global MODEMS
        testModems = fakemodems.createModems()
        testModems.append(fakemodems.GenericTestModem()) # Test polling only
        for fakeModem in testModems:
            self.init_modem(fakeModem)
            
            modem = self.modem.serial.modem # load the copy()-ed modem instance
            
            for number, callId, callType in tests:
                def writeCallbackFunc(data):
                    if self.modem._mustPollCallStatus and data.startswith('AT+CLCC'):
                        return # Can happen due to polling
                    self.assertEqual('ATD{0};\r'.format(number), data, 'Invalid data written to modem; expected "{0}", got: "{1}". Modem: {2}'.format('ATD{0};'.format(number), data[:-1] if data[-1] == '\r' else data, modem))
                    set_writeCallbackFunc()
                set_writeCallbackFunc(writeCallbackFunc                )
                self.modem.serial.responseSequence = modem.getAtdResponse(number)
                self.modem.serial.responseSequence.extend(modem.getPreCallInitWaitSequence())
                # Fake call initiated notification
                self.modem.serial.responseSequence.extend(modem.getCallInitNotification(callId, callType))
                call = self.modem.dial(number)
                # Wait for the read buffer to clear
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.6)
                self.assertIsInstance(call, gsmmodem.modem.Call)
                self.assertIs(call.number, number)
                # Check status
                self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                self.assertFalse(call.answered, 'Call state invalid: should not yet be answered. Modem: {0}'.format(modem))            
                self.assertIn(call.id, self.modem.activeCalls)
                self.assertEqual(len(self.modem.activeCalls), 1)
                # Fake an answer
                self.modem.serial.responseSequence = modem.getRemoteAnsweredNotification(callId, callType)
                # Wait a bit for the event to be picked up
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:                    
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.6) # Ensure polling picks up event
                elif not self.modem._waitForCallInitUpdate:
                    time.sleep(0.1) # Ensure event is picked up
                self.assertTrue(call.answered, 'Remote call answer was not detected. Modem: {0}'.format(modem))
                self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                def hangupCallback(data):
                    if self.modem._mustPollCallStatus and data.startswith('AT+CLCC'):
                        return # Can happen due to polling
                    self.assertEqual('ATH\r'.format(number), data, 'Invalid data written to modem; expected "{0}", got: "{1}". Modem: {2}'.format('ATH'.format(number), data[:-1] if data[-1] == '\r' else data, modem))
                set_writeCallbackFunc(hangupCallback)
                call.hangup()
                self.assertFalse(call.answered, 'Hangup call did not change answered state. Modem: {0}'.format(modem))
                self.assertFalse(call.active, 'Call state invalid: should not be active (local hangup). Modem: {0}'.format(modem))
                self.assertNotIn(call.id, self.modem.activeCalls)
                self.assertEqual(len(self.modem.activeCalls), 0)

                ############## Check remote hangup detection ###############
                set_writeCallbackFunc(writeCallbackFunc)
                self.modem.serial.responseSequence = modem.getAtdResponse(number)
                self.modem.serial.responseSequence.extend(modem.getPreCallInitWaitSequence())
                # Fake call initiated notification
                self.modem.serial.responseSequence.extend(modem.getCallInitNotification(callId, callType))                
                call = self.modem.dial(number)
                self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                # Wait a bit for the event to be picked up
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.6) # Ensure polling picks up event
                # Fake remote answer
                self.modem.serial.responseSequence = modem.getRemoteAnsweredNotification(callId, callType)
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.5) # Ensure polling picks up event
                elif not self.modem._waitForCallInitUpdate:
                    time.sleep(0.1) # Ensure event is picked up
                self.assertTrue(call.answered, 'Remote call answer was not detected. Modem: {0}'.format(modem))
                self.assertIn(call.id, self.modem.activeCalls)
                self.assertEqual(len(self.modem.activeCalls), 1)
                # Now fake a remote hangup
                self.modem.serial.responseSequence = modem.getRemoteHangupNotification(callId, callType)
                # Wait a bit for the event to be picked up
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.6) # Ensure polling picks up event
                self.assertFalse(call.answered, 'Remote hangup was not detected. Modem: {0}'.format(modem))
                self.assertFalse(call.active, 'Call state invalid: should not be active (remote hangup). Modem: {0}'.format(modem))
                self.assertNotIn(call.id, self.modem.activeCalls)
                self.assertEqual(len(self.modem.activeCalls), 0)

                ############## Check remote call rejection (hangup before answering) ###############
                set_writeCallbackFunc(writeCallbackFunc)
                self.modem.serial.responseSequence = modem.getAtdResponse(number)
                self.modem.serial.responseSequence.extend(modem.getPreCallInitWaitSequence())
                # Fake call initiated notification
                self.modem.serial.responseSequence.extend(modem.getCallInitNotification(callId, callType))
                call = self.modem.dial(number)
                self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                # Wait a bit for the event to be picked up
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.6) # Ensure polling picks up event
                self.assertFalse(call.answered, 'Call should not have been in "answered" state. Modem: {0}'.format(modem))
                self.assertIn(call.id, self.modem.activeCalls)
                self.assertEqual(len(self.modem.activeCalls), 1)
                # Now reject the call
                self.modem.serial.responseSequence = modem.getRemoteRejectCallNotification(callId, callType)
                # Wait a bit for the event to be picked up
                while len(self.modem.serial._readQueue) > 0 or len(self.modem.serial.responseSequence) > 0:
                    time.sleep(0.05)
                if self.modem._mustPollCallStatus:
                    time.sleep(0.6) # Ensure polling picks up event
                time.sleep(0.05)
                self.assertFalse(call.answered, 'Call state invalid: should not be answered (remote call rejection). Modem: {0}'.format(modem))
                self.assertFalse(call.active, 'Call state invalid: should not be active (remote rejection). Modem: {0}'.format(modem))
                self.assertNotIn(call.id, self.modem.activeCalls)
                self.assertEqual(len(self.modem.activeCalls), 0)
            await self.modem.close()

        async def test_dialCallback(self):
            """ Tests the dial method's callback mechanism """
            tests = (['12345678', '1', '0'],)

            global MODEMS
            testModems = fakemodems.createModems()
            testModems.append(fakemodems.GenericTestModem()) # Test polling only
            for fakeModem in testModems:
                self.init_modem(fakeModem)

                modem = self.modem.serial.modem # load the copy()-ed modem instance

                for number, callId, callType in tests:

                    callbackVars = [None, False, 0]

                    def callUpdateCallbackFunc1(call):
                        self.assertIsInstance(call, gsmmodem.modem.Call)
                        self.assertEqual(call, callbackVars[0])
                        # Check call status
                        if callbackVars[2] == 0: # Expected "answer" event
                            self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                            self.assertTrue(call.answered, 'Call state invalid: should have been answered. Modem: {0}'.format(modem))
                        elif callbackVars[2] == 1: # Expected "hangup" event
                            self.assertFalse(call.answered, 'Call state invalid: "answered" should be false after hangup. Modem: {0}'.format(modem))
                            self.assertFalse(call.active, 'Call state invalid: should be inactive. Modem: {0}'.format(modem))
                        callbackVars[1] = True # set "callback called" flag

                    call = self.modem.dial(number, callStatusUpdateCallbackFunc=callUpdateCallbackFunc1)
                    self.assertIsInstance(call, gsmmodem.modem.Call)
                    callbackVars[0] = call
                    self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                    self.assertFalse(call.answered, 'Call state invalid: should not yet be answered. Modem: {0}'.format(modem))
                    # Fake an answer...
                    self.modem.serial.responseSequence = modem.getRemoteAnsweredNotification(callId, callType)
                    # ...and wait for the callback to be called
                    while not callbackVars[1]:
                        time.sleep(0.05)
                    # Double check local call variable
                    self.assertTrue(call.active, 'Call state invalid: should be active. Modem: {0}'.format(modem))
                    self.assertTrue(call.answered, 'Call state invalid: should have been answered. Modem: {0}'.format(modem))
                    # Fake remote hangup...
                    callbackVars[1] = False
                    callbackVars[2] = 1
                    self.modem.serial.responseSequence = modem.getRemoteAnsweredNotification(callId, callType)
                    # ...and wait for the callback to be called
                    while not callbackVars[1]:
                        time.sleep(0.05)
                    # Double check local call variable
                    self.assertFalse(call.answered, 'Call state invalid: "answered" should be false after hangup. Modem: {0}'.format(modem))
                    self.assertFalse(call.active, 'Call state invalid: should be inactive. Modem: {0}'.format(modem))

            await self.modem.close()
    
    def test_dialError(self):
        """ Test error handling when dialing """
        self.init_modem(fakemodems.HuaweiK3715()) # Use a modem that supports call update notifications
        set_response_sequence(['+CME ERROR: 30\r\n'])
        self.assertRaises(gsmmodem.exceptions.CmeError, self.modem.dial, '123')
        set_response_sequence(['+CMS ERROR: 500\r\n'])
        self.assertRaises(gsmmodem.exceptions.CmsError, self.modem.dial, '123')
        set_response_sequence(['ERROR\r\n'])
        self.assertRaises(gsmmodem.exceptions.CommandError, self.modem.dial, '123')
    
    def test_dial_callInitEventTimeout(self):
        """ Test dial() timeout event: call initiated event never occurs """
        self.init_modem(fakemodems.HuaweiK3715()) # Use a modem that supports call update notifications
        # The following should timeout very quickly - ATD does not timeout, but no call is established
        self.assertRaises(gsmmodem.exceptions.TimeoutException, self.modem.dial, **{'number': '123', 'timeout': 0.05})
    
    def test_dial_atdTimeout(self):
        """ Test dial() timeout event: ATD command timeout """
        self.init_modem(fakemodems.GenericTestModem())
        # Disable ATD response
        self.modem.serial.modem.responses['ATD123;\r'] = []
        # The following should timeout very quickly - no ATD command response received
        self.assertRaises(gsmmodem.exceptions.TimeoutException, self.modem.dial, **{'number': '123', 'timeout': 0.05})


class TestGsmModemPinConnect(IsolatedAsyncioTestCase):
    """ Tests PIN unlocking and connect() method of GsmModem class (excluding connect/close) """
    
    async def asyncTearDown(self):
        global FAKE_MODEM
        FAKE_MODEM = None
    
    def init_modem(self, modem):
        global FAKE_MODEM
        FAKE_MODEM = modem
        self.mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = self.mockSerial
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')        
        
    async def test_connectPinLockedNoPin(self):
        """ Test connecting to the modem with a SIM PIN code - no PIN specified"""
        testModems = fakemodems.createModems()
        for modem in testModems:
            modem.pinLock = True
            self.init_modem(modem)
            self.assertRaises(PinRequiredError, self.modem.connect)
            await self.modem.close()
    
    async def test_connectPinLockedWithPin(self):
        """ Test connecting to the modem with a SIM PIN code - PIN specified"""
        testModems = fakemodems.createModems()
        # Also test a modem that allows only CMEE commands before PIN is entered
        edgeCaseModem = fakemodems.GenericTestModem()
        edgeCaseModem.commandsNoPinRequired = ['AT+CMEE=1\r']
        testModems.append(edgeCaseModem)
        for modem in testModems:
            modem.pinLock = True
            self.init_modem(modem)
            # This should succeed
            try:
                self.modem.connect(pin='1234')
            except PinRequiredError:
                self.fail("Pin required exception thrown for modem {0}".format(modem))
            finally:
                await self.modem.close()
    
    async def test_connectPin_incorrect(self):
        """ Test connecting to the modem with a SIM PIN code - incorrect PIN specified """
        def writeCallbackFunc(data):
            if data.startswith('AT+CPIN="'):
                # Fake "incorrect PIN" response
                set_response_sequence(['+CME ERROR: 16\r\n'])
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        fakeModem = fakemodems.GenericTestModem()
        fakeModem.pinLock = True
        self.init_modem(fakeModem)
        self.assertRaises(gsmmodem.exceptions.IncorrectPinError, self.modem.connect, **{'pin': '1234'})
        await self.modem.close()
        SERIAL_WRITE_CALLBACK_FUNC = None
    
    async def test_connectPin_pukRequired(self):
        """ Test connecting to the modem with a SIM PIN code - SIM locked; PUK required """
        def writeCallbackFunc(data):
            if data.startswith('AT+CPIN="'):
                # Fake "PUK required" response
                set_response_sequence(['+CME ERROR: 12\r\n'])
        global SERIAL_WRITE_CALLBACK_FUNC
        SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
        fakeModem = fakemodems.GenericTestModem()
        fakeModem.pinLock = True
        self.init_modem(fakeModem)
        self.assertRaises(gsmmodem.exceptions.PukRequiredError, self.modem.connect, **{'pin': '1234'})
        await self.modem.close()
        SERIAL_WRITE_CALLBACK_FUNC = None
    
    async def test_connectPin_timeoutEvents(self):
        """ Test different TimeoutException scenarios when checking PIN status (github issue #19) """
        
        tests = (([0.05], True), (['+CPIN: READY\r\n'], False), (['FIRST LINE\r\n', 'SECOND LINE\r\n'], True))
        
        for response, shouldTimeout in tests:
            def writeCallbackFunc(data):
                if data.startswith('AT+CPIN?'):
                    # Fake "incorrect PIN" response
                    self.modem.serial.responseSequence = response
        
            global SERIAL_WRITE_CALLBACK_FUNC
            SERIAL_WRITE_CALLBACK_FUNC = writeCallbackFunc
            fakeModem = fakemodems.GenericTestModem()
            fakeModem.pinLock = False
            self.init_modem(fakeModem)
            if shouldTimeout:
                self.assertRaises(gsmmodem.exceptions.TimeoutException, self.modem.connect)
            else:
                await self.modem.connect() # should run fine
            await self.modem.close()
            SERIAL_WRITE_CALLBACK_FUNC = None


class TestIncomingCall(IsolatedAsyncioTestCase):
    
    async def asyncTearDown(self):
        global FAKE_MODEM
        FAKE_MODEM = None
        await self.modem.close()
    
    async def init_modem(self, modem, incomingCallCallbackFunc):
        global FAKE_MODEM
        FAKE_MODEM = modem
        self.mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = self.mockSerial        
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --', incomingCallCallbackFunc=incomingCallCallbackFunc)
        await self.modem.connect()
    
    async def test_incomingCallAnswer(self):

        for modem in fakemodems.createModems():
            callReceived = [False, 'VOICE', '']
            def incomingCallCallbackFunc(call):
                try:                    
                    self.assertIsInstance(call, gsmmodem.modem.IncomingCall)
                    self.assertIn(call.id, self.modem.activeCalls)
                    self.assertEqual(len(self.modem.activeCalls), 1)
                    self.assertEqual(call.number, callReceived[2], 'Caller ID (caller number) incorrect. Expected: "{0}", got: "{1}". Modem: {2}'.format(callReceived[2], call.number, modem))
                    self.assertFalse(call.answered, 'Call state invalid: should not yet be answered. Modem: {0}'.format(modem))
                    self.assertIsInstance(call.type, int)
                    self.assertEqual(call.type, callReceived[1], 'Invalid call type; expected "{0}", got "{1}". Modem: {2}'.format(callReceived[1], call.type, modem))
                    def writeCallbackFunc1(data):
                        self.assertEqual('ATA\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}". Modem: {2}'.format('ATA\r', data, modem))
                    set_writeCallbackFunc(writeCallbackFunc1)
                    call.answer()
                    self.assertTrue(call.answered, 'Call state invalid: should be answered. Modem: {0}'.format(modem))
                    # Call answer() again - shouldn't do anything
                    def writeCallbackShouldNotBeCalled(data):
                        self.fail('Nothing should have been written to modem, but got: {0}'.format(data))
                    set_writeCallbackFunc(writeCallbackShouldNotBeCalled)
                    call.answer()
                    # Hang up
                    def writeCallbackFunc2(data):
                        self.assertEqual('ATH\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}". Modem: {2}'.format('ATH\r', data, modem))
                    set_writeCallbackFunc(writeCallbackFunc2)
                    call.hangup()
                    self.assertFalse(call.answered, 'Call state invalid: hangup did not change call state. Modem: {0}'.format(modem))
                    self.assertNotIn(call.id, self.modem.activeCalls)
                    self.assertEqual(len(self.modem.activeCalls), 0)
                    # Call hangup() again - shouldn't do anything
                    set_writeCallbackFunc(writeCallbackShouldNotBeCalled)
                    call.hangup()
                finally:
                    callReceived[0] = True
        
            self.init_modem(modem, incomingCallCallbackFunc)
        
            tests = (('+27820001234', 'VOICE', 0),)
        
            for number, cringParam, callType in tests:
                callReceived[0] = False
                callReceived[1] = callType
                callReceived[2] = number
                # Fake incoming voice call                
                self.modem.serial.responseSequence = modem.getIncomingCallNotification(number, cringParam)
                # Wait for the handler function to finish
                while callReceived[0] == False:
                    time.sleep(0.05)
            await self.modem.close()
    
    def test_incomingCallCrcNotSupported(self):
        """ Tests handling incoming calls without +CRC support """
        callReceived = [False]
        def callbackFunc(call):
            self.assertIsInstance(call, gsmmodem.modem.IncomingCall)
            self.assertEqual(call.type, None, 'Invalid call type; expected "{0}", got "{1}".'.format(None, call.type))
            callReceived[0] = True
        
        testModem = copy(fakemodems.GenericTestModem())
        testModem.responses['AT+CRC?\r'] = ['ERROR\r\n']
        testModem.responses['AT+CRC=1\r'] = ['ERROR\r\n']
        self.init_modem(testModem, incomingCallCallbackFunc=callbackFunc)
        
        # Ensure extended incoming call indications are active
        self.assertFalse(self.modem._extendedIncomingCallIndication, 'Extended incoming call indicator flag should be False')
        # Fake incoming voice call using basic incoming call indication format
        set_response_sequence(['RING\r\n', '+CLIP: "+27821231234",145,,,,0\r\n'])
        # Wait for the handler function to finish
        while callReceived[0] == False:
            time.sleep(0.1)
        self.assertFalse(self.modem._extendedIncomingCallIndication, 'Extended incoming call indicator flag should be False')
    
    def test_incomingCallCrcChangedExternally(self):
        """ Tests handling incoming call notifications when the +CRC setting \
        was modfied by some external program (issue #18) """
        
        callReceived = [False]
        def callbackFunc(call):
            self.assertIsInstance(call, gsmmodem.modem.IncomingCall)
            callReceived[0] = True
        
        self.init_modem(None, incomingCallCallbackFunc=callbackFunc)
        
        # Ensure extended incoming call indications are active
        self.assertTrue(self.modem._extendedIncomingCallIndication, 'Extended incoming call indicator flag should be True')
        # Fake incoming voice call using extended incoming call indication format
        set_response_sequence(['+CRING: VOICE\r\n', '+CLIP: "+27821231234",145,,,,0\r\n'])
        # Wait for the handler function to finish
        while callReceived[0] == False:
            time.sleep(0.1)
        callReceived[0] = False
        # Now fake incoming call using basic incoming call indication format (without informing GsmModem class about change)
        set_response_sequence(['RING\r\n', '+CLIP: "+27821231234",145,,,,0\r\n'])
        # Wait for the handler function to finish
        while callReceived[0] == False:
            time.sleep(0.05)
        # Ensure extended incoming call indications have been re-enabled
        self.assertTrue(self.modem._extendedIncomingCallIndication, 'Extended incoming call indicator flag should be True')
        
        # Now repeat the test, but cause re-enabling the +CRC setting to fail
        self.modem.serial.modem.responses['AT+CRC=1\r'] = ['ERROR\r\n']
        callReceived[0] = False
        # Basic incoming call indication format (without informing GsmModem class about change)
        set_response_sequence(['RING\r\n', '+CLIP: "+27821231234",145,,,,0\r\n'])
        # Wait for the handler function to finish
        while callReceived[0] == False:
            time.sleep(0.05)
        # Since re-enabling the extended format failed,  extended incoming call indications flag should be False
        self.assertFalse(self.modem._extendedIncomingCallIndication, 'Extended incoming call indicator flag should be False because AT+CRC=1 failed')


class TestCall(IsolatedAsyncioTestCase):
    """ Tests Call object APIs that are not covered by TestIncomingCall and TestGsmModemDial """
    
    async def init_modem(self, modem):
        global FAKE_MODEM
        FAKE_MODEM = modem
        gsmmodem.serial_comms.serial = MockSerialPackage()
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --')
        await self.modem.connect()
        FAKE_MODEM = None

    async def testDtmf(self):
        """ Tests sending DTMF tones in a phone call """
        originalBaseDtmfCommand = gsmmodem.modem.Call.DTMF_COMMAND_BASE
        for fakeModem in fakemodems.createModems():
            gsmmodem.modem.Call.DTMF_COMMAND_BASE = originalBaseDtmfCommand
            await self.init_modem(fakeModem)
            # Make sure everything is set up correctly during connect()
            self.assertEqual(gsmmodem.modem.Call.DTMF_COMMAND_BASE, fakeModem.dtmfCommandBase, 'Invalid base DTMF command for modem: {0}; expected "{1}", got "{2}"'.format(fakeModem, fakeModem.dtmfCommandBase, gsmmodem.modem.Call.DTMF_COMMAND_BASE))
            # Test sending DTMF tones in a call
            call = gsmmodem.modem.Call(self.modem, 1, 1, '+270000000')
            call.answered = True
            
            tests = (('3', 'AT{0}3\r'.format(fakeModem.dtmfCommandBase.format(cid=call.id))),
                     ('1234', 'AT{0}1;{0}2;{0}3;{0}4\r'.format(fakeModem.dtmfCommandBase.format(cid=call.id))),
                     ('#0*', 'AT{0}#;{0}0;{0}*\r'.format(fakeModem.dtmfCommandBase.format(cid=call.id))))

            for tones, expectedCommand in tests:
                def writeCallbackFunc(data):
                    expectedCommand = 'AT{0}{1}\r'.format(fakeModem.dtmfCommandBase.format(cid=call.id), tones[self.currentTone])
                    self.currentTone += 1;
                    self.assertEqual(expectedCommand, data, 'Invalid data written to modem for tones: "{0}"; expected "{1}", got: "{2}". Modem: {3}'.format(tones, expectedCommand[:-1].format(cid=self.id), data[:-1] if data[-1] == '\r' else data, fakeModem))
                set_writeCallbackFunc(writeCallbackFunc)
                self.currentTone = 0;
            
            # Now attempt to send DTMF tones in an inactive call
            set_writeCallbackFunc()
            call.hangup()
            self.assertRaises(gsmmodem.exceptions.InvalidStateException, call.sendDtmfTone, '1')
            await self.modem.close()
        gsmmodem.modem.Call.DTMF_COMMAND_BASE = originalBaseDtmfCommand
    
    async def testDtmfInterrupted(self):
        """ Tests interrupting the playback of DTMF tones """
        await self.init_modem(fakemodems.GenericTestModem())
        call = gsmmodem.modem.Call(self.modem, 1, 1, '+270000000')
        call.answered = True
        # Fake an interruption - no network service
        set_response_sequence([0.1, '+CME ERROR: 30\r\n'])
        self.assertRaises(gsmmodem.exceptions.InterruptedException, call.sendDtmfTone, '5')
        # Fake an interruption - operation not allowed
        set_response_sequence([0.1, '+CME ERROR: 3\r\n'])
        self.assertRaises(gsmmodem.exceptions.InterruptedException, call.sendDtmfTone, '5')
        # Fake some other CME error
        set_response_sequence([0.1, '+CME ERROR: 1234\r\n'])
        self.assertRaises(gsmmodem.exceptions.CmeError, call.sendDtmfTone, '5')
        await self.modem.close()
        
    async def testCallAnsweredCallback(self):
        """ Tests Call object's "call answered" callback mechanism """
        await self.init_modem(fakemodems.GenericTestModem())
        
        callbackCalled = [False]
        def callbackFunc(callObj):
            self.assertEqual(callObj, call)
            callbackCalled[0] = True
        call = gsmmodem.modem.Call(self.modem, 1, 1, '+270000000', callStatusUpdateCallbackFunc=callbackFunc)
        # Answer the call "remotely" - this should trigger the callback
        call.answered = True
        self.assertTrue(callbackCalled[0], "Call status update callback not called for answer event")
        await self.modem.close()


class TestSms(IsolatedAsyncioTestCase):
    """ Tests the SMS API of GsmModem class """
    
    async def asyncSetUp(self):
        self.tests = (('+0123456789', 'Hello world!',                        
                       1,
                       datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                       '+2782913593',
                       '06917228195339040A9110325476980000313080512061800CC8329BFD06DDDF72363904', 29, 142,
                       'SM'),
                      ('+9876543210', 
                       'Hallo\nhoe gaan dit?', 
                       4,
                       datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                       '+2782913593',
                       '06917228195339040A91896745230100003130805120618013C8309BFD56A0DF65D0391C7683C869FA0F', 35, 33, 
                       'SM'),
                      ('+353870000000', 'My message',
                       13,
                       datetime(2013, 4, 20, 20, 22, 27, tzinfo=SimpleOffsetTzInfo(4)),
                       None, None, 0, 0, 'ME'),
                      )
        # address_text data to use for tests when testing PDU mode
        self.testsPduAddressText = ('', '"abc123"', '""', 'Test User 123', '9876543231')
    
    async def initModem(self, smsReceivedCallbackFunc):
        # Override the pyserial import        
        self.mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = self.mockSerial
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --', smsReceivedCallbackFunc=smsReceivedCallbackFunc)        
        await self.modem.connect()

    async def test_sendSmsLeaveTextModeOnInvalidCharacter(self):
        """ Tests sending SMS messages in text mode """
        self.initModem(None)
        self.modem.smsTextMode = True # Set modem to text mode
        self.assertTrue(self.modem.smsTextMode)
        # PDUs checked on https://www.diafaan.com/sms-tutorials/gsm-modem-tutorial/online-sms-pdu-decoder/
        tests = (('+0123456789', 'Helló worłd!',
                  1,
                  datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                  '+2782913593',
                  [('00218D0A91103254769800081800480065006C006C00F300200077006F0072014200640021', 36, 141)],
                  'SM',
                  'UCS2'),
                 ('+0123456789', 'Hellò wor£d!',
                  2,
                  datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                  '82913593',
                  [('00218E0A91103254769800000CC8329B8D00DDDFF2003904', 23, 142)],
                  'SM',
                  'GSM'),
                 ('+0123456789', '12345-010 12345-020 12345-030 12345-040 12345-050 12345-060 12345-070 12345-080 12345-090 12345-100 12345-110 12345-120 12345-130 12345-140 12345-150 12345-160-Hellò wor£d!',
                  3,
                  datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                  '82913593',
                  [('00618F0A9110325476980000A00500038F020162B219ADD682C560A0986C46ABB560321828269BD16A2DD80C068AC966B45A0B46838162B219ADD682D560A0986C46ABB560361828269BD16A2DD80D068AC966B45A0B86838162B219ADD682E560A0986C46ABB562301828269BD16AAD580C068AC966B45A2B26838162B219ADD68ACD60A0986C46ABB562341828269BD16AAD580D068AC966', 152, 143),
('00618F0A91103254769800001A0500038F020268B556CC066B21CB6C3602747FCB03E410', 35, 143)],
                  'SM',
                  'GSM'),
                 ('+0123456789', 'Hello world!\n Hello world!\n Hello world!\n Hello world!\n-> Helló worłd! ',
                  4,
                  datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                  '+2782913593',
                  [('0061900A91103254769800088C05000390020100480065006C006C006F00200077006F0072006C00640021000A002000480065006C006C006F00200077006F0072006C00640021000A002000480065006C006C006F00200077006F0072006C00640021000A002000480065006C006C006F00200077006F0072006C00640021000A002D003E002000480065006C006C00F300200077006F0072', 152, 144),
                   ('0061900A91103254769800080E0500039002020142006400210020', 26, 144)],
                  'SM',
                  'UCS2'),
('+0123456789', '12345-010 12345-020 12345-030 12345-040 12345-050 12345-060 12345-070 12345-080 12345-090 12345-100 12345-110 12345-120 12345-130 12345-140 12345-150 12345-160-Hello word!',
                  5,
                  datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)),
                  '82913593',
                  [('0061910A9110325476980000A005000391020162B219ADD682C560A0986C46ABB560321828269BD16A2DD80C068AC966B45A0B46838162B219ADD682D560A0986C46ABB560361828269BD16A2DD80D068AC966B45A0B86838162B219ADD682E560A0986C46ABB562301828269BD16AAD580C068AC966B45A2B26838162B219ADD68ACD60A0986C46ABB562341828269BD16AAD580D068AC966', 152, 145),
('0061910A91103254769800001905000391020268B556CC066B21CB6CF61B747FCBC921', 34, 145)],
                  'SM',
                  'GSM'),)

        for number, message, index, smsTime, smsc, pdus, mem, encoding in tests:
            def writeCallbackFunc(data):
                def writeCallbackFunc2(data):
                    # Second step - get available encoding schemes
                    self.assertEqual('AT+CSCS=?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS=?', data))
                    set_writeCallbackFunc(writeCallbackFunc3)

                def writeCallbackFunc3(data):
                    # Third step - set encoding
                    self.assertEqual('AT+CSCS="{0}"\r'.format(encoding), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS="{0}"\r'.format(encoding), data))
                    set_writeCallbackFunc(writeCallbackFunc4)

                def writeCallbackFunc4(data):
                    # Fourth step - send PDU length
                    tpdu_length = pdus[self.currentPdu][1]
                    ref = pdus[self.currentPdu][2]
                    self.assertEqual('AT+CMGS={0}\r'.format(tpdu_length), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGS={0}'.format(tpdu_length), data))
                    set_writeCallbackFunc(writeCallbackFunc5)
                    self.modem.serial.flushResponseSequence = False
                    set_response_sequence(['> \r\n', '+CMGS: {0}\r\n'.format(ref), 'OK\r\n'])

                def writeCallbackFuncRaiseError(data):
                    self.assertEqual(self.currentPdu, len(pdus) - 1, 'Invalid data written to modem; expected {0} PDUs, got {1} PDU'.format(len(pdus), self.currentPdu + 1))

                def writeCallbackFunc5(data):
                    # Fifth step - send SMS PDU
                    pdu = pdus[self.currentPdu][0]
                    self.assertEqual('{0}{1}'.format(pdu, chr(26)), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('{0}{1}'.format(pdu, chr(26)), data))
                    self.modem.serial.flushResponseSequence = True
                    self.currentPdu += 1
                    if len(pdus) > self.currentPdu:
                        set_writeCallbackFunc(writeCallbackFunc4)
                    else:
                        set_writeCallbackFunc(writeCallbackFuncRaiseError)

                # First step - change to PDU mode
                self.assertEqual('AT+CMGF=0\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGF=0', data))
                set_writeCallbackFunc(writeCallbackFunc2)
                self.currentPdu = 0
                self.modem._smsRef = pdus[self.currentPdu][2]

            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.serial.flushResponseSequence = True
            sms = self.modem.sendSms(number, message)
            self.assertFalse(self.modem.smsTextMode)
            self.assertEqual(self.modem._smsEncoding, encoding, 'Modem uses invalid encoding. Expected "{0}", got "{1}"'.format(encoding, self.modem._smsEncoding))
            self.assertIsInstance(sms, gsmmodem.modem.SentSms)
            self.assertEqual(sms.number, number, 'Sent SMS has invalid number. Expected "{0}", got "{1}"'.format(number, sms.number))
            self.assertEqual(sms.text, message, 'Sent SMS has invalid text. Expected "{0}", got "{1}"'.format(message, sms.text))
            self.assertIsInstance(sms.reference, int, 'Sent SMS reference type incorrect. Expected "{0}", got "{1}"'.format(int, type(sms.reference)))
            ref = pdus[0][2] # All refference numbers should be equal
            self.assertEqual(sms.reference, ref, 'Sent SMS reference incorrect. Expected "{0}", got "{1}"'.format(ref, sms.reference))
            self.assertEqual(sms.status, gsmmodem.modem.SentSms.ENROUTE, 'Sent SMS status should have been {0} ("ENROUTE"), but is: {1}'.format(gsmmodem.modem.SentSms.ENROUTE, sms.status))
            # Reset mode and encoding
            self.modem._smsTextMode = True # Set modem to text mode
            self.modem._smsEncoding = "GSM" # Set encoding to GSM-7
            self.modem._smsSupportedEncodingNames = None # Force modem to ask about possible encoding names
        await self.modem.close()

    async def test_sendSmsTextMode(self):
        """ Tests sending SMS messages in text mode """
        self.initModem(None)
        self.modem.smsTextMode = True # Set modem to text mode
        self.assertTrue(self.modem.smsTextMode)
        for number, message, index, smsTime, smsc, pdu, tpdu_length, ref, mem in self.tests:
            self.modem._smsRef = ref
            def writeCallbackFunc(data):
                def writeCallbackFunc2(data):
                    self.assertEqual('{0}{1}'.format(message, chr(26)), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('{0}{1}'.format(message, chr(26)), data))
                    self.modem.serial.flushResponseSequence = True                
                self.assertEqual('AT+CMGS="{0}"\r'.format(number), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGS="{0}"'.format(number), data))
                set_writeCallbackFunc(writeCallbackFunc2)
            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.serial.flushResponseSequence = False
            set_response_sequence(['> \r\n', '+CMGS: {0}\r\n'.format(ref), 'OK\r\n'])
            sms = self.modem.sendSms(number, message)
            self.assertIsInstance(sms, gsmmodem.modem.SentSms)
            self.assertEqual(sms.number, number, 'Sent SMS has invalid number. Expected "{0}", got "{1}"'.format(number, sms.number))
            self.assertEqual(sms.text, message, 'Sent SMS has invalid text. Expected "{0}", got "{1}"'.format(message, sms.text))
            self.assertIsInstance(sms.reference, int, 'Sent SMS reference type incorrect. Expected "{0}", got "{1}"'.format(int, type(sms.reference)))
            self.assertEqual(sms.reference, ref, 'Sent SMS reference incorrect. Expected "{0}", got "{1}"'.format(ref, sms.reference))
            self.assertEqual(sms.status, gsmmodem.modem.SentSms.ENROUTE, 'Sent SMS status should have been {0} ("ENROUTE"), but is: {1}'.format(gsmmodem.modem.SentSms.ENROUTE, sms.status))
        await self.modem.close()

    async def test_sendSmsPduMode(self):
        """ Tests sending a SMS messages in PDU mode """
        self.initModem(None)
        self.modem.smsTextMode = False # Set modem to PDU mode
        self.modem._smsEncoding = "GSM"
        self.assertFalse(self.modem.smsTextMode)
        self.firstSMS = True
        for number, message, index, smsTime, smsc, pdu, sms_deliver_tpdu_length, ref, mem in self.tests:
            self.modem._smsRef = ref
            calcPdu = gsmmodem.pdu.encodeSmsSubmitPdu(number, message, ref)[0]
            pduHex = codecs.encode(calcPdu.data, 'hex_codec').upper()
            pduHex = str(pduHex, 'ascii')

            def writeCallbackFunc(data):
                def writeCallbackFuncReadCSCS(data):
                    self.assertEqual('AT+CSCS=?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS=?', data))
                    self.firstSMS = False

                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CMGS={0}\r'.format(calcPdu.tpduLength), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGS={0}'.format(calcPdu.tpduLength), data))
                    set_writeCallbackFunc(writeCallbackFunc3)
                    self.modem.serial.flushResponseSequence = False
                    set_response_sequence(['> \r\n', '+CMGS: {0}\r\n'.format(ref), 'OK\r\n'])

                def writeCallbackFunc3(data):
                    self.assertEqual('{0}{1}'.format(pduHex, chr(26)), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('{0}{1}'.format(pduHex, chr(26)), data))
                    self.modem.serial.flushResponseSequence = True

                if self.firstSMS:
                    return writeCallbackFuncReadCSCS(data)
                self.assertEqual('AT+CSCS="{0}"\r'.format(self.modem._smsEncoding), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS="{0}"'.format(self.modem._smsEncoding), data))
                set_writeCallbackFunc(writeCallbackFunc2)

            set_writeCallbackFunc(writeCallbackFunc)
            sms = self.modem.sendSms(number, message)
            self.assertIsInstance(sms, gsmmodem.modem.SentSms)
            self.assertEqual(sms.number, number, 'Sent SMS has invalid number. Expected "{0}", got "{1}"'.format(number, sms.number))
            self.assertEqual(sms.text, message, 'Sent SMS has invalid text. Expected "{0}", got "{1}"'.format(message, sms.text))
            self.assertIsInstance(sms.reference, int, 'Sent SMS reference type incorrect. Expected "{0}", got "{1}"'.format(int, type(sms.reference)))
            self.assertEqual(sms.reference, ref, 'Sent SMS reference incorrect. Expected "{0}", got "{1}"'.format(ref, sms.reference))
            self.assertEqual(sms.status, gsmmodem.modem.SentSms.ENROUTE, 'Sent SMS status should have been {0} ("ENROUTE"), but is: {1}'.format(gsmmodem.modem.SentSms.ENROUTE, sms.status))
        await self.modem.close()

    async def test_sendSmsResponseMixedWithUnsolictedMessages(self):
        """ Tests sending a SMS messages (PDU mode), but with unsolicted messages mixed into the modem responses
        - the only difference here is that the modem's responseSequence contains unsolicted messages
        taken from github issue #11
        """
        self.initModem(None)
        self.modem.smsTextMode = False # Set modem to PDU mode
        self.modem._smsEncoding = "GSM"
        self.firstSMS = True
        for number, message, index, smsTime, smsc, pdu, sms_deliver_tpdu_length, ref, mem in self.tests:
            self.modem._smsRef = ref
            calcPdu = gsmmodem.pdu.encodeSmsSubmitPdu(number, message, ref)[0]
            pduHex = codecs.encode(calcPdu.data, 'hex_codec').upper()
            pduHex = str(pduHex, 'ascii')

            def writeCallbackFunc(data):
                def writeCallbackFuncReadCSCS(data):
                    self.assertEqual('AT+CSCS=?\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS=?', data))
                    self.firstSMS = False

                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CMGS={0}\r'.format(calcPdu.tpduLength), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGS={0}'.format(calcPdu.tpduLength), data))
                    set_writeCallbackFunc(writeCallbackFunc3)
                    self.modem.serial.flushResponseSequence = True
                    # Note thee +ZDONR and +ZPASR unsolicted messages in the "response"
                    set_response_sequence(['+ZDONR: "METEOR",272,3,"CS_ONLY","ROAM_OFF"\r\n', '+ZPASR: "UMTS"\r\n', '> \r\n'])

                def writeCallbackFunc3(data):
                    self.assertEqual('{0}{1}'.format(pduHex, chr(26)), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('{0}{1}'.format(pduHex, chr(26)), data))
                    # Note thee +ZDONR and +ZPASR unsolicted messages in the "response"
                    self.modem.serial.responseSequence =  ['+ZDONR: "METEOR",272,3,"CS_ONLY","ROAM_OFF"\r\n', '+ZPASR: "UMTS"\r\n', '+ZDONR: "METEOR",272,3,"CS_PS","ROAM_OFF"\r\n', '+ZPASR: "UMTS"\r\n', '+CMGS: {0}\r\n'.format(ref), 'OK\r\n']

                if self.firstSMS:
                    return writeCallbackFuncReadCSCS(data)
                self.assertEqual('AT+CSCS="{0}"\r'.format(self.modem._smsEncoding), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CSCS="{0}"'.format(self.modem._smsEncoding), data))
                set_writeCallbackFunc(writeCallbackFunc2)

            set_writeCallbackFunc(writeCallbackFunc)
            sms = self.modem.sendSms(number, message)
            self.assertIsInstance(sms, gsmmodem.modem.SentSms)
            self.assertEqual(sms.number, number, 'Sent SMS has invalid number. Expected "{0}", got "{1}"'.format(number, sms.number))
            self.assertEqual(sms.text, message, 'Sent SMS has invalid text. Expected "{0}", got "{1}"'.format(message, sms.text))
            self.assertIsInstance(sms.reference, int, 'Sent SMS reference type incorrect. Expected "{0}", got "{1}"'.format(int, type(sms.reference)))
            self.assertEqual(sms.reference, ref, 'Sent SMS reference incorrect. Expected "{0}", got "{1}"'.format(ref, sms.reference))
        await self.modem.close()
    
    async def test_receiveSmsTextMode(self):
        """ Tests receiving SMS messages in text mode """
        callbackInfo = [False, '', '', -1, None, '', None]
        def smsReceivedCallbackFuncText(sms):
            try:
                self.assertIsInstance(sms, gsmmodem.modem.ReceivedSms)
                self.assertEqual(sms.number, callbackInfo[1], 'SMS sender number incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[1], sms.number))
                self.assertEqual(sms.text, callbackInfo[2], 'SMS text incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[2], sms.text))
                self.assertIsInstance(sms.time, datetime, 'SMS received time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.time)))
                self.assertEqual(sms.time, callbackInfo[4], 'SMS received time incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[4], sms.time))
                self.assertEqual(sms.status, gsmmodem.modem.Sms.STATUS_RECEIVED_UNREAD)
                self.assertEqual(sms.smsc, None, 'Text-mode SMS should not have any SMSC information')
            finally:
                callbackInfo[0] = True

        self.initModem(smsReceivedCallbackFunc=smsReceivedCallbackFuncText)
        self.modem.smsTextMode = True # Set modem to text mode
        self.assertTrue(self.modem.smsTextMode)
        for number, message, index, smsTime, smsc, pdu, tpdu_length, ref, mem in self.tests:            
            # Wait for the handler function to finish
            callbackInfo[0] = False # "done" flag
            callbackInfo[1] = number
            callbackInfo[2] = message
            callbackInfo[3] = index
            callbackInfo[4] = smsTime
            
            # Time string as returned by modem in text modem
            tzDelta = smsTime.utcoffset()
            if tzDelta.days >= 0:
                tzValStr = '+{0:0>2}'.format(int(tzDelta.seconds / 60 / 15)) # calculate offset in 0.25 hours
            if tzDelta.days < 0: # negative
                tzValStr = '-{0:0>2}'.format(int((tzDelta.days * -3600 * 24 - tzDelta.seconds) / 60 / 15))
            textModeStr = smsTime.strftime('%y/%m/%d,%H:%M:%S') + tzValStr
            def writeCallbackFunc(data):
                """ Intercept the "read stored message" command """        
                def writeCallbackFunc2(data):                    
                    self.assertEqual('AT+CMGR={0}\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGR={0}'.format(index), data))
                    set_response_sequence(['+CMGR: "REC UNREAD","{0}",,"{1}"\r\n'.format(number, textModeStr), '{0}\r\n'.format(message), 'OK\r\n'])
                    def writeCallbackFunc3(data):
                        self.assertEqual('AT+CMGD={0},0\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0}'.format(index), data))
                    set_writeCallbackFunc(writeCallbackFunc3)
                if self.modem._smsMemReadDelete != mem:
                    self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                    set_writeCallbackFunc(writeCallbackFunc2)
                else:
                    # Modem does not need to change read memory
                    writeCallbackFunc2(data)
            set_writeCallbackFunc(writeCallbackFunc)
            # Fake a "new message" notification
            set_response_sequence(['+CMTI: "{0}",{1}\r\n'.format(mem, index)])
            # Wait for the handler function to finish
            while callbackInfo[0] == False:
                time.sleep(0.1)
        await self.modem.close()
        
    async def test_receiveSmsPduMode(self):
        """ Tests receiving SMS messages in PDU mode """
        callbackInfo = [False, '', '', -1, None, '', None]
        def smsReceivedCallbackFuncPdu(sms):
            try:
                self.assertIsInstance(sms, gsmmodem.modem.ReceivedSms)
                self.assertEqual(sms.number, callbackInfo[1], 'SMS sender number incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[1], sms.number))
                self.assertEqual(sms.text, callbackInfo[2], 'SMS text incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[2], sms.text))
                self.assertIsInstance(sms.time, datetime, 'SMS received time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.time)))
                self.assertEqual(sms.time, callbackInfo[4], 'SMS received time incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[4], sms.time))
                self.assertEqual(sms.status, gsmmodem.modem.Sms.STATUS_RECEIVED_UNREAD)
                self.assertEqual(sms.smsc, callbackInfo[5], 'PDU-mode SMS SMSC number incorrect. Expected: "{0}", got: "{1}"'.format(callbackInfo[5], sms.smsc))
            finally:
                callbackInfo[0] = True

        self.initModem(smsReceivedCallbackFunc=smsReceivedCallbackFuncPdu)
        self.modem.smsTextMode = False # Set modem to PDU mode
        self.assertFalse(self.modem.smsTextMode)
        for pduAddressText in self.testsPduAddressText:
            for number, message, index, smsTime, smsc, pdu, tpdu_length, ref, mem in self.tests:
                if smsc == None or pdu == None:
                    continue # not enough info for a PDU test, skip it
                # Wait for the handler function to finish
                callbackInfo[0] = False # "done" flag
                callbackInfo[1] = number
                callbackInfo[2] = message
                callbackInfo[3] = index
                callbackInfo[4] = smsTime
                callbackInfo[5] = smsc
            
                def writeCallbackFunc(data):
                    def writeCallbackFunc2(data):
                        """ Intercept the "read stored message" command """
                        self.assertEqual('AT+CMGR={0}\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGR={0}'.format(index), data))
                        set_response_sequence(['+CMGR: 0,{0},{1}\r\n'.format(pduAddressText, tpdu_length), '{0}\r\n'.format(pdu), 'OK\r\n']                )
                        def writeCallbackFunc3(data):
                            self.assertEqual('AT+CMGD={0},0\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0}'.format(index), data))
                        set_writeCallbackFunc(writeCallbackFunc3)
                    if self.modem._smsMemReadDelete != mem:
                        self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                        set_writeCallbackFunc(writeCallbackFunc2)
                    else:
                        # Modem does not need to change read memory
                        writeCallbackFunc2(data)
                set_writeCallbackFunc(writeCallbackFunc)
                # Fake a "new message" notification
                set_response_sequence(['+CMTI: "SM",{0}\r\n'.format(index)])
                # Wait for the handler function to finish
                while callbackInfo[0] == False:
                    time.sleep(0.1)
        await self.modem.close()

    async def test_sendSms_refCount(self):
        """ Test the SMS reference counter operation when sending SMSs """
        self.initModem(None)
        
        ref = 0
        def writeCallbackFunc(data):
            if data.startswith('AT+CMGS'):
                self.modem.serial.flushResponseSequence = False
                set_response_sequence(['> \r\n', '+CMGS: {0}\r\n'.format(ref), 'OK\r\n'])
            else:
                self.modem.serial.flushResponseSequence = True
        set_writeCallbackFunc(writeCallbackFunc)
        
        ref = 0
        sms = self.modem.sendSms("+27820000000", 'Test message')
        firstRef = sms.reference
        self.assertEqual(firstRef, 0)
        # Ensure the reference counter is incremented each time an SMS is sent
        ref = 1
        sms = self.modem.sendSms("+27820000000", 'Test message 2')
        reference = sms.reference
        self.assertEqual(sms.reference, firstRef + 1)
        # Ensure the reference counter rolls over once 255 is reached
        ref = 255
        self.modem._smsRef = 255
        sms = self.modem.sendSms("+27820000000", 'Test message 3')
        ref = 0
        self.assertEqual(sms.reference, 255)
        sms = self.modem.sendSms("+27820000000", 'Test message 4')
        self.assertEqual(sms.reference, 0)
        await self.modem.close()
    
    async def test_sendSms_waitForDeliveryReport(self):
        """ Test waiting for the status report when sending SMSs """
        self.initModem(None)
        causeTimeout = [False]
        def writeCallbackFunc(data):
            if data.startswith('AT+CMGS'):
                self.modem.serial.flushResponseSequence = False
                if causeTimeout[0]:
                    set_response_sequence(['> \r\n', '+CMGS: 183\r\n', 'OK\r\n'])
                else:
                    # Fake a delivery report notification after sending SMS
                    set_response_sequence(['> \r\n', '+CMGS: 183\r\n', 'OK\r\n', 0.1, '+CDSI: "SM",3\r\n'])
            elif data.startswith('AT+CMGR'):
                # Provide a fake status report - these are tested by the TestSmsStatusReports class
                set_response_sequence(['+CMGR: 0,,24\r\n', '07917248014000F506B70AA18092020000317071518590803170715185418000\r\n', 'OK\r\n'])
            else:
                self.modem.serial.flushResponseSequence = True
        set_writeCallbackFunc(writeCallbackFunc)
        # Prepare send SMS response as well as "delivered" notification
        self.modem._smsRef = 183
        sms = self.modem.sendSms('0829200000', 'Test message', waitForDeliveryReport=True)
        self.assertIsInstance(sms, gsmmodem.modem.SentSms)
        self.assertNotEqual(sms.report, None, 'Sent SMS\'s "report" attribute should not be None')
        self.assertIsInstance(sms.report, gsmmodem.modem.StatusReport)
        self.assertEqual(sms.status, gsmmodem.modem.SentSms.DELIVERED, 'Sent SMS status should have been {0} ("DELIVERED"), but is: {1}'.format(gsmmodem.modem.SentSms.DELIVERED, sms.status))
        # Now test timeout event when waiting for delivery report
        causeTimeout[0] = True
        self.modem._smsRef = 183
        # Set deliveryTimeout to 0.05 - should timeout very quickly
        self.assertRaises(gsmmodem.exceptions.TimeoutException, self.modem.sendSms, **{'destination': '0829200000', 'text': 'Test message', 'waitForDeliveryReport': True, 'deliveryTimeout': 0.05})
        await self.modem.close()
    
    async def test_sendSms_reply(self):
        """ Test the reply() method of the ReceivedSms class """
        self.initModem(None)
        
        def writeCallbackFunc(data):
            if data.startswith('AT+CMGS'):
                self.modem.serial.flushResponseSequence = False
                set_response_sequence(['> \r\n', '+CMGS: 0\r\n', 'OK\r\n'])
            else:
                self.modem.serial.flushResponseSequence = True
        set_writeCallbackFunc(writeCallbackFunc)
        
        receivedSms = gsmmodem.modem.ReceivedSms(self.modem, gsmmodem.modem.ReceivedSms.STATUS_RECEIVED_READ, '+27820000000', datetime(2013, 3, 8, 15, 2, 16, tzinfo=SimpleOffsetTzInfo(2)), 'Text message', '+9876543210')
        sms = receivedSms.reply('This is the reply')
        self.assertIsInstance(sms, gsmmodem.modem.SentSms)
        self.assertEqual(sms.number, receivedSms.number)
        self.assertEqual(sms.text, 'This is the reply')
        await self.modem.close()
        
    async def test_sendSms_noCgmsResponse(self):
        """ Test GsmModem.sendSms() but issue an invalid response from the modem """
        self.initModem(None)
        # Modem is just going to respond with "OK" to the send SMS command
        self.assertRaises(gsmmodem.exceptions.CommandError, self.modem.sendSms, '+27820000000', 'Test message')
        await self.modem.close()

class TestStoredSms(IsolatedAsyncioTestCase):
    """ Tests processing/accessing SMS messages stored on the SIM card """
    
    async def initModem(self, textMode, smsReceivedCallbackFunc):
        global FAKE_MODEM
        # Override the pyserial import
        mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = mockSerial
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --', smsReceivedCallbackFunc=smsReceivedCallbackFunc)
        self.modem.smsTextMode = textMode
        await self.modem.connect()
        FAKE_MODEM = None
    
    async def asyncSetUp(self):
        self.modem = None
    
    async def asyncTearDown(self):
        if self.modem != None:
            await self.modem.close()
    
    def initFakeModemResponses(self, textMode):
        global FAKE_MODEM
        FAKE_MODEM = copy(fakemodems.GenericTestModem())
        modem = gsmmodem.modem.GsmModem('--weak ref object--')
        self.expectedMessages = [ReceivedSms(modem, Sms.STATUS_RECEIVED_UNREAD, '+27748577604', datetime(2013, 1, 28, 14, 51, 42, tzinfo=SimpleOffsetTzInfo(2)), 'Hello raspberry pi', None),
                                 ReceivedSms(modem, Sms.STATUS_RECEIVED_READ, '+2784000153099999', datetime(2013, 2, 7, 1, 31, 44, tzinfo=SimpleOffsetTzInfo(2)), 'New and here to stay! Don\'t just recharge SUPACHARGE and get your recharged airtime+FREE CellC to CellC mins & SMSs+Free data to use anytime. T&C apply. Cell C', None),
                                 ReceivedSms(modem, Sms.STATUS_RECEIVED_READ, '+27840001463', datetime(2013, 2, 7, 6, 24, 2, tzinfo=SimpleOffsetTzInfo(2)), 'Standard Bank: Your accounts are no longer FICA compliant. Please bring ID & proof of residence to any branch to reactivate your accounts. Queries? 0860003422.')]       
        if textMode:
            FAKE_MODEM.responses['AT+CMGL="REC UNREAD"\r'] = ['+CMGL: 0,"REC UNREAD","+27748577604",,"13/01/28,14:51:42+08"\r\n', 'Hello raspberry pi\r\n',
                                                              'OK\r\n']
            FAKE_MODEM.responses['AT+CMGL="REC READ"\r'] = ['+CMGL: 1,"REC READ","+2784000153099999",,"13/02/07,01:31:44+08"\r\n', 'New and here to stay! Don\'t just recharge SUPACHARGE and get your recharged airtime+FREE CellC to CellC mins & SMSs+Free data to use anytime. T&C apply. Cell C\r\n',
                                                            '+CMGL: 2,"REC READ","+27840001463",,"13/02/07,06:24:02+08"\r\n', 'Standard Bank: Your accounts are no longer FICA compliant. Please bring ID & proof of residence to any branch to reactivate your accounts. Queries? 0860003422.\r\n',
                                                            'OK\r\n']
            allMessages = FAKE_MODEM.responses['AT+CMGL="REC UNREAD"\r'][:-1]
            allMessages.extend(FAKE_MODEM.responses['AT+CMGL="REC READ"\r'])
            FAKE_MODEM.responses['AT+CMGL="ALL"\r'] = allMessages
            FAKE_MODEM.responses['AT+CMGL="STO UNSENT"\r'] = FAKE_MODEM.responses['AT+CMGL="STO SENT"\r'] = ['OK\r\n']
            FAKE_MODEM.responses['AT+CMGL=0\r'] = FAKE_MODEM.responses['AT+CMGL=1\r'] = FAKE_MODEM.responses['AT+CMGL=2\r'] = FAKE_MODEM.responses['AT+CMGL=3\r'] = FAKE_MODEM.responses['AT+CMGL=4\r'] = ['ERROR\r\n']
        else:
            FAKE_MODEM.responses['AT+CMGL=0\r'] = ['+CMGL: 0,0,,35\r\n', '07917248014000F3240B917247587706F400003110824115248012C8329BFD06C9C373B8B82C97E741F034\r\n',
                                                   'OK\r\n'] 
            FAKE_MODEM.responses['AT+CMGL=1\r'] = ['+CMGL: 1,1,,161\r\n', '07917248010080F020109172480010359099990000312070101344809FCEF21D14769341E8B2BC0CA2BF41737A381F0211DFEE131DA4AECFE92079798C0ECBCF65D0B40A0D0E9141E9B1080ABBC9A073990ECABFEB7290BC3C4687E5E73219144ECBE9E976796594168BA06199CD1E82E86FD0B0CC660F41EDB47B0E3281A6CDE97C659497CB2072981E06D1DFA0FABC0C0ABBF3F474BBEC02514D4350180E67E75DA06199CD060D01\r\n',
                                                   '+CMGL: 2,1,,159\r\n', '07917248010080F0240B917248001064F30000312070604220809F537AD84D0ECBC92061D8BDD681B2EFBA1C141E8FDF75377D0E0ACBCB20F71BC47EBBCF6539C8981C0641E3771BCE4E87DD741708CA2E87E76590589E769F414922C80482CBDF6F33E86D06C9CBF334B9EC1E9741F43728ECCE83C4F2B07B8C06D1DF2079393CA6A7ED617A19947FD7E5A0F078FCAEBBE97317285A2FCBD3E5F90F04C3D96030D88C2693B900\r\n',
                                                   'OK\r\n']
            allMessages = FAKE_MODEM.responses['AT+CMGL=0\r'][:-1]
            allMessages.extend(FAKE_MODEM.responses['AT+CMGL=1\r'])
            FAKE_MODEM.responses['AT+CMGL=4\r'] = allMessages
            FAKE_MODEM.responses['AT+CMGL=2\r'] = FAKE_MODEM.responses['AT+CMGL=3\r'] = ['OK\r\n']
            FAKE_MODEM.responses['AT+CMGL="REC UNREAD"\r'] = FAKE_MODEM.responses['AT+CMGL="REC READ"\r'] = FAKE_MODEM.responses['AT+CMGL="STO UNSENT"\r'] = FAKE_MODEM.responses['AT+CMGL="STO SENT"\r'] = FAKE_MODEM.responses['AT+CMGL="ALL"\r'] = ['ERROR\r\n']
            FAKE_MODEM.responses['AT+CMGR=0\r'] = ['+CMGR: 0,,35\r\n', '07917248014000F3240B917247587706F400003110824115248012C8329BFD06C9C373B8B82C97E741F034\r\n', 'OK\r\n']

    def test_listStoredSms_pdu(self):
        """ Tests listing/reading SMSs that are currently stored on the SIM card (PDU mode) """
        self.initFakeModemResponses(textMode=False)
        self.initModem(False, None)
        # Test getting all messages
        def writeCallbackFunc(data):
            self.assertEqual('AT+CMGL=4\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGL=4', data))
        set_writeCallbackFunc(writeCallbackFunc)
        messages = self.modem.listStoredSms()
        self.assertIsInstance(messages, list)
        self.assertEqual(len(messages), 3, 'Invalid number of messages returned; expected 3, got {0}'.format(len(messages)))
        
        for i in range(len(messages)):
            message = messages[i]
            expected = self.expectedMessages[i]
            self.assertIsInstance(message, expected.__class__)
            self.assertEqual(message.number, expected.number)
            self.assertEqual(message.status, expected.status)
            self.assertEqual(message.text, expected.text)
            self.assertEqual(message.time, expected.time)
        del messages
        
        # Test filtering
        tests = ((Sms.STATUS_RECEIVED_UNREAD, 1), (Sms.STATUS_RECEIVED_READ, 2), (Sms.STATUS_STORED_SENT, 0), (Sms.STATUS_STORED_UNSENT, 0))
        for status, numberOfMessages in tests:
            def writeCallbackFunc2(data):
                self.assertEqual('AT+CMGL={0}\r'.format(status), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGL={0}'.format(status), data))
            set_writeCallbackFunc(writeCallbackFunc2)
            messages = self.modem.listStoredSms(status=status)
            self.assertIsInstance(messages, list)
            self.assertEqual(len(messages), numberOfMessages, 'Invalid number of messages returned for status: {0}; expected {1}, got {2}'.format(status, numberOfMessages, len(messages)))        
            del messages
        
        # Test deleting messages after retrieval
        # Test deleting all messages
        expectedFilter = [4, ['1,4']]
        delCount = [0]
        def writeCallbackFunc3(data):
            self.assertEqual('AT+CMGL={0}\r'.format(expectedFilter[0]), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGL={0}'.format(expectedFilter[0]), data))
            def writeCallbackFunc4(data):
                self.assertEqual('AT+CMGD={0}\r'.format(expectedFilter[1][delCount[0]]), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0}'.format(expectedFilter[1][delCount[0]]), data))
                delCount[0] += 1
            set_writeCallbackFunc(writeCallbackFunc4)
        set_writeCallbackFunc(writeCallbackFunc3)
        messages = self.modem.listStoredSms(status=Sms.STATUS_ALL, delete=True)
        self.assertIsInstance(messages, list)
        self.assertEqual(len(messages), 3, 'Invalid number of messages returned; expected 3, got {0}'.format(len(messages)))
        
        # Test deleting filtered messages
        expectedFilter[0] = 1
        expectedFilter[1] = ['1,0', '2,0']
        delCount[0] = 0
        set_writeCallbackFunc(writeCallbackFunc3)
        messages = self.modem.listStoredSms(status=Sms.STATUS_RECEIVED_READ, delete=True)
        
        # Test error handling if an invalid line is added between PDU data (line should be ignored)
        set_writeCallbackFunc()
        self.modem.serial.modem.responses['AT+CMGL=4\r'].insert(1, 'AFSDLF SDKFJSKDLFJLKSDJF SJDLKFSKLDJFKSDFS\r\n')
        messages = self.modem.listStoredSms()
        self.assertIsInstance(messages, list)
        self.assertEqual(len(messages), 3, 'Invalid number of messages returned; expected 3, got {0}'.format(len(messages)))

    def test_listStoredSms_text(self):
        """ Tests listing/reading SMSs that are currently stored on the SIM card (text mode) """
        self.initFakeModemResponses(textMode=True)
        self.initModem(True, None)
        
        # Test getting all messages
        def writeCallbackFunc(data):
            self.assertEqual('AT+CMGL="ALL"\r', data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGL="ALL"', data))
        set_writeCallbackFunc(writeCallbackFunc)
        messages = self.modem.listStoredSms()
        self.assertIsInstance(messages, list)
        self.assertEqual(len(messages), 3, 'Invalid number of messages returned; expected 3, got {0}'.format(len(messages)))
        
        for i in range(len(messages)):
            message = messages[i]
            expected = self.expectedMessages[i]
            self.assertIsInstance(message, expected.__class__)
            self.assertEqual(message.number, expected.number)
            self.assertEqual(message.status, expected.status)
            self.assertEqual(message.text, expected.text)
            self.assertEqual(message.time, expected.time)
        del messages
        
        # Test filtering
        tests = ((Sms.STATUS_RECEIVED_UNREAD, 'REC UNREAD', 1), (Sms.STATUS_RECEIVED_READ, 'REC READ', 2), (Sms.STATUS_STORED_SENT, 'STO SENT', 0), (Sms.STATUS_STORED_UNSENT, 'STO UNSENT', 0))
        for status, statusStr, numberOfMessages in tests:
            def writeCallbackFunc2(data):
                self.assertEqual('AT+CMGL="{0}"\r'.format(statusStr), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGL="{0}"'.format(statusStr), data))
            set_writeCallbackFunc(writeCallbackFunc2)
            messages = self.modem.listStoredSms(status=status)
            self.assertIsInstance(messages, list)
            self.assertEqual(len(messages), numberOfMessages, 'Invalid number of messages returned for status: {0}; expected {1}, got {2}'.format(status, numberOfMessages, len(messages)))
            del messages
        
        # Test deleting messages after retrieval
        # Test deleting all messages
        expectedFilter = ['ALL', ['1,4']]
        delCount = [0]
        def writeCallbackFunc3(data):
            self.assertEqual('AT+CMGL="{0}"\r'.format(expectedFilter[0]), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGL="{0}"'.format(expectedFilter[0]), data))
            def writeCallbackFunc4(data):
                self.assertEqual('AT+CMGD={0}\r'.format(expectedFilter[1][delCount[0]]), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0}'.format(expectedFilter[1][delCount[0]]), data))
                delCount[0] += 1
            set_writeCallbackFunc(writeCallbackFunc4)
        set_writeCallbackFunc(writeCallbackFunc3)
        messages = self.modem.listStoredSms(status=Sms.STATUS_ALL, delete=True)
        self.assertIsInstance(messages, list)
        self.assertEqual(len(messages), 3, 'Invalid number of messages returned; expected 3, got {0}'.format(len(messages)))
        
        # Test deleting filtered messages
        expectedFilter[0] = 'REC READ'
        expectedFilter[1] = ['1,0', '2,0']
        delCount[0] = 0
        set_writeCallbackFunc(writeCallbackFunc3)
        messages = self.modem.listStoredSms(status=Sms.STATUS_RECEIVED_READ, delete=True)
        
        # Test error handling when specifying an invalid SMS status value
        set_writeCallbackFunc()
        self.assertRaises(ValueError, self.modem.listStoredSms, **{'status': 99})
    
    def test_processStoredSms(self):
        """ Tests processing and then "receiving" SMSs that are currently stored on the SIM card """
        self.initFakeModemResponses(textMode=False)
        
        expectedMessages = copy(self.expectedMessages)
        unread = expectedMessages.pop(0)
        expectedMessages.append(unread)
        
        i = [0]
        def smsCallbackFunc(sms):
            expected = expectedMessages[i[0]]
            self.assertIsInstance(sms, ReceivedSms)
            self.assertEqual(sms.number, expected.number)
            self.assertEqual(sms.status, expected.status)
            self.assertEqual(sms.text, expected.text)
            self.assertEqual(sms.time, expected.time)
            i[0] += 1
        
        self.initModem(False, smsCallbackFunc)
        
        commandsWritten = [False, False]
        def writeCallbackFunc(data):
            if data.startswith('AT+CMGL'):
                commandsWritten[0] = True
            elif data.startswith('AT+CMGD'):
                commandsWritten[1] = True
        set_writeCallbackFunc(writeCallbackFunc)
        
        self.modem.processStoredSms()
        self.assertTrue(commandsWritten[0], 'AT+CMGL command not written to modem')
        self.assertTrue(commandsWritten[1], 'AT+CMGD command not written to modem')
        self.assertEqual(i[0], 3, 'Message received callback count incorrect; expected 3, got {0}'.format(i[0]))
        
        # Test unread only
        commandsWritten[0] = commandsWritten[1] = False
        i[0] = 0
        expectedMessages = [unread]
        self.modem.processStoredSms(unreadOnly=True)
        self.assertTrue(commandsWritten[0], 'AT+CMGL command not written to modem')
        self.assertTrue(commandsWritten[1], 'AT+CMGD command not written to modem')
        self.assertEqual(i[0], 1, 'Message received callback count incorrect; expected 1, got {0}'.format(i[0]))
    
    def test_deleteStoredSms(self):
        self.initFakeModemResponses(textMode=True)
        self.initModem(True, None)
        
        tests = (1,2,3)
        for index in tests:        
            def writeCallbackFunc(data):
                self.assertEqual('AT+CMGD={0},0\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0},0'.format(index), data))
            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.deleteStoredSms(index)
        # Test switching SMS memory
        tests = ((5, 'TEST1'), (32, 'ME'))
        for index, mem in tests:
            def writeCallbackFunc(data):
                self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CMGD={0},0\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0},0'.format(index), data))
                set_writeCallbackFunc(writeCallbackFunc2)
            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.deleteStoredSms(index, memory=mem)
            
    def test_deleteMultipleStoredSms(self):
        self.initFakeModemResponses(textMode=True)
        self.initModem(True, None)
        
        tests = (4,3,2,1)
        for delFlag in tests:        
            # Test getting all messages
            def writeCallbackFunc(data):
                self.assertEqual('AT+CMGD=1,{0}\r'.format(delFlag), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD=1,{0}'.format(delFlag), data))
            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.deleteMultipleStoredSms(delFlag)
        # Test switching SMS memory
        tests = ((4, 'TEST1'), (4, 'ME'))
        for delFlag, mem in tests:
            def writeCallbackFunc(data):
                self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CMGD=1,{0}\r'.format(delFlag), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD=1,{0}'.format(delFlag), data))
                set_writeCallbackFunc(writeCallbackFunc2)
            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.deleteMultipleStoredSms(delFlag, memory=mem)
        # Test default delFlag value
        delFlag = 4
        def writeCallbackFunc3(data):
            self.assertEqual('AT+CMGD=1,{0}\r'.format(delFlag), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD=1,{0}'.format(delFlag), data))
        set_writeCallbackFunc(writeCallbackFunc3)
        self.modem.deleteMultipleStoredSms()
        # Test invalid delFlag values
        tests = (0, 5, -3)
        for delFlag in tests:
            self.assertRaises(ValueError, self.modem.deleteMultipleStoredSms, **{'delFlag': delFlag})
    
    def test_readStoredSms_pdu(self):
        """ Tests reading stored SMS messages (PDU mode) """
        self.initFakeModemResponses(textMode=False)
        self.initModem(False, None)
        
        # Test basic reading
        index = 0
        def writeCallbackFunc(data):
            self.assertEqual('AT+CMGR={0}\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGR={0}'.format(index), data))
        set_writeCallbackFunc(writeCallbackFunc)
        message = self.modem.readStoredSms(index)
        expected = self.expectedMessages[index]
        self.assertIsInstance(message, expected.__class__)
        self.assertEqual(message.number, expected.number)
        self.assertEqual(message.status, expected.status)
        self.assertEqual(message.text, expected.text)
        self.assertEqual(message.time, expected.time)
        
        # Test switching SMS memory
        tests = ((0, 'TEST1'), (0, 'ME'))
        for index, mem in tests:
            def writeCallbackFunc(data):
                self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                def writeCallbackFunc2(data):
                    self.assertEqual('AT+CMGR={0}\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGR={0}'.format(index), data))
                set_writeCallbackFunc(writeCallbackFunc2)
            set_writeCallbackFunc(writeCallbackFunc)
            self.modem.readStoredSms(index, memory=mem)
            expected = self.expectedMessages[index]
            self.assertIsInstance(message, expected.__class__)
            self.assertEqual(message.number, expected.number)
            self.assertEqual(message.status, expected.status)
            self.assertEqual(message.text, expected.text)
            self.assertEqual(message.time, expected.time)


class TestSmsStatusReports(IsolatedAsyncioTestCase):
    """ Tests receiving SMS status reports """
        
    async def initModem(self, smsStatusReportCallback):
        # Override the pyserial import        
        self.mockSerial = MockSerialPackage()
        gsmmodem.serial_comms.serial = self.mockSerial
        self.modem = gsmmodem.modem.GsmModem('-- PORT IGNORED DURING TESTS --', smsStatusReportCallback=smsStatusReportCallback)        
        await self.modem.connect()
    
    async def test_receiveStatusReportTextMode(self):
        """ Tests receiving SMS status reports in text mode """
        
        tests = ((57, 'SR',
                  '+CMGR: ,6,20,"0870000000",129,"13/04/29,19:58:00+04","13/04/29,19:59:00+04",0',
                  Sms.STATUS_RECEIVED_UNREAD, # message read status 
                  '0870000000', # number
                  20, # reference
                  datetime(2013, 4, 29, 19, 58, 0, tzinfo=SimpleOffsetTzInfo(1)), # sentTime
                  datetime(2013, 4, 29, 19, 59, 0, tzinfo=SimpleOffsetTzInfo(1)), # deliverTime
                  StatusReport.DELIVERED), # delivery status
                 )
        
        callbackDone = [False]
        
        for index, mem, notification, msgStatus, number, reference, sentTime, deliverTime, deliveryStatus in tests:            
            def smsStatusReportCallbackFuncText(sms):
                try:
                    self.assertIsInstance(sms, gsmmodem.modem.StatusReport)
                    self.assertEqual(sms.status, msgStatus, 'Status report read status incorrect. Expected: "{0}", got: "{1}"'.format(msgStatus, sms.status))
                    self.assertEqual(sms.number, number, 'SMS sender number incorrect. Expected: "{0}", got: "{1}"'.format(number, sms.number))                    
                    self.assertEqual(sms.reference, reference, 'Status report SMS reference number incorrect. Expected: "{0}", got: "{1}"'.format(reference, sms.reference))
                    self.assertIsInstance(sms.timeSent, datetime, 'SMS sent time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.timeSent)))
                    self.assertEqual(sms.timeSent, sentTime, 'SMS sent time incorrect. Expected: "{0}", got: "{1}"'.format(sentTime, sms.timeSent))
                    self.assertIsInstance(sms.timeFinalized, datetime, 'SMS finalized time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.timeFinalized)))
                    self.assertEqual(sms.timeFinalized, deliverTime, 'SMS finalized time incorrect. Expected: "{0}", got: "{1}"'.format(deliverTime, sms.timeFinalized))
                    self.assertEqual(sms.deliveryStatus, deliveryStatus, 'SMS delivery status incorrect. Expected: "{0}", got: "{1}"'.format(deliveryStatus, sms.deliveryStatus))                
                    self.assertEqual(sms.smsc, None, 'Text-mode SMS should not have any SMSC information')
                finally:
                    callbackDone[0] = True
            self.initModem(smsStatusReportCallback=smsStatusReportCallbackFuncText)
            self.modem.smsTextMode = True
            def writeCallbackFunc(data):
                def writeCallbackFunc2(data):                    
                    self.assertEqual('AT+CMGR={0}\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGR={0}'.format(index), data))
                    set_response_sequence(['{0}\r\n'.format(notification), 'OK\r\n'])
                    def writeCallbackFunc3(data):
                        self.assertEqual('AT+CMGD={0},0\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0}'.format(index), data))
                    set_writeCallbackFunc(writeCallbackFunc3)
                if self.modem._smsMemReadDelete != mem:
                    self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                    set_writeCallbackFunc(writeCallbackFunc2)
                else:
                    # Modem does not need to change read memory
                    writeCallbackFunc2(data)
            set_writeCallbackFunc(writeCallbackFunc)
            # Fake a "new status report" notification
            set_response_sequence(['+CDSI: "{0}",{1}\r\n'.format(mem, index)])
            # Wait for the handler function to finish
            while callbackDone[0] == False:
                time.sleep(0.1)
        await self.modem.close()
        
    def test_receiveSmsPduMode_problemCases(self):
        """ Test receiving PDU-mode SMS using data captured from failed operations/bug reports """
        # AT+CMGR response from ZTE modem breaks incoming message read - simply test that we can parse it properly
        zteResponse = ['+CMGR: ,,27\r\n', '0297F1061C0F910B487228297020F5317062419272803170624192138000\r\n', 'OK\r\n']
        
        callbackInfo = [False, '', '', -1, None, '', None]
        def smsCallbackFunc1(sms):
            try:
                self.assertIsInstance(sms, gsmmodem.modem.StatusReport)
                # Since the +CMGR response did not include the SMS's status, see if the default fallback was loaded correctly
                self.assertEqual(sms.status, gsmmodem.modem.Sms.STATUS_RECEIVED_UNREAD)
            finally:
                callbackInfo[0] = True
        
        def writeCallback1(data):
            if data.startswith('AT+CMGR'):
                self.modem.serial.flushResponseSequence = True
                self.modem.serial.responseSequence = zteResponse

        self.initModem(smsStatusReportCallback=smsCallbackFunc1)
        # Fake a "new message" notification
        set_writeCallbackFunc(writeCallback1)
        set_response_sequence(['+CDSI: "SM",1\r\n'])
        # Wait for the handler function to finish
        while callbackInfo[0] == False:
            time.sleep(0.1)
        
    async def test_receiveStatusReportPduMode(self):
        """ Tests receiving SMS status reports in PDU mode """
        tests = ((3, 'SM',
                  ['+CMGR: 0,,24\r\n', '07917248014000F506B70AA18092020000317071518590803170715185418000\r\n', 'OK\r\n'],
                  Sms.STATUS_RECEIVED_UNREAD, # message read status 
                  '0829200000', # number
                  183, # reference
                  datetime(2013, 7, 17, 15, 58, 9, tzinfo=SimpleOffsetTzInfo(2)), # sentTime
                  datetime(2013, 7, 17, 15, 58, 14, tzinfo=SimpleOffsetTzInfo(2)), # deliverTime
                  StatusReport.DELIVERED), # delivery status
                 (1, 'SM', # This output was captured from a ZTE modem that seems to be broken (PDU is semi-invalid (SMSC length incorrect), and +CMGR output missing status)
                  ['+CMGR: ,,27\r\n', '0297F1061C0F910B487228297020F5317062419272803170624192138000\r\n', 'OK\r\n'],
                  Sms.STATUS_RECEIVED_UNREAD,
                  '+b08427829207025', # <-- note the broken number
                  28,
                  datetime(2013, 7, 26, 14, 29, 27, tzinfo=SimpleOffsetTzInfo(2)), # sentTime
                  datetime(2013, 7, 26, 14, 29, 31, tzinfo=SimpleOffsetTzInfo(2)), # deliverTime
                  StatusReport.DELIVERED),
                 )
        
        callbackDone = [False]
        
        for index, mem, responseSeq, msgStatus, number, reference, sentTime, deliverTime, deliveryStatus in tests:
            callbackDone[0] = False
            def smsStatusReportCallbackFuncText(sms):
                try:
                    self.assertIsInstance(sms, gsmmodem.modem.StatusReport)
                    self.assertEqual(sms.status, msgStatus, 'Status report read status incorrect. Expected: "{0}", got: "{1}"'.format(msgStatus, sms.status))
                    self.assertEqual(sms.number, number, 'SMS sender number incorrect. Expected: "{0}", got: "{1}"'.format(number, sms.number))
                    self.assertEqual(sms.reference, reference, 'Status report SMS reference number incorrect. Expected: "{0}", got: "{1}"'.format(reference, sms.reference))
                    self.assertIsInstance(sms.timeSent, datetime, 'SMS sent time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.timeSent)))
                    self.assertEqual(sms.timeSent, sentTime, 'SMS sent time incorrect. Expected: "{0}", got: "{1}"'.format(sentTime, sms.timeSent))
                    self.assertIsInstance(sms.timeFinalized, datetime, 'SMS finalized time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.timeFinalized)))
                    self.assertEqual(sms.timeFinalized, deliverTime, 'SMS finalized time incorrect. Expected: "{0}", got: "{1}"'.format(deliverTime, sms.timeFinalized))
                    self.assertEqual(sms.deliveryStatus, deliveryStatus, 'SMS delivery status incorrect. Expected: "{0}", got: "{1}"'.format(deliveryStatus, sms.deliveryStatus))                
                    self.assertEqual(sms.smsc, None, 'Text-mode SMS should not have any SMSC information')
                finally:
                    callbackDone[0] = True
            self.initModem(smsStatusReportCallback=smsStatusReportCallbackFuncText)
            self.modem.smsTextMode = False
            def writeCallbackFunc(data):
                def writeCallbackFunc2(data):                    
                    self.assertEqual('AT+CMGR={0}\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGR={0}'.format(index), data))
                    self.modem.serial.responseSequence = responseSeq
                    def writeCallbackFunc3(data):
                        self.assertEqual('AT+CMGD={0},0\r'.format(index), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CMGD={0}'.format(index), data))
                    set_writeCallbackFunc(writeCallbackFunc3)
                if self.modem._smsMemReadDelete != mem:
                    self.assertEqual('AT+CPMS="{0}"\r'.format(mem), data, 'Invalid data written to modem; expected "{0}", got: "{1}"'.format('AT+CPMS="{0}"'.format(mem), data))
                    set_writeCallbackFunc(writeCallbackFunc2)
                else:
                    # Modem does not need to change read memory
                    writeCallbackFunc2(data)
            set_writeCallbackFunc(writeCallbackFunc)
            # Fake a "new status report" notification
            set_response_sequence(['+CDSI: "{0}",{1}\r\n'.format(mem, index)])
            # Wait for the handler function to finish
            while callbackDone[0] == False:
                time.sleep(0.1)
        await self.modem.close()

    def test_receiveSmsPduMode_invalidPDUsRecordedFromModems(self):
        """ Test receiving PDU-mode SMS using data captured from failed operations/bug reports """
        tests = ((['+CMGR: 1,,26\r\n', '0006230E9126983575169498610103409544C26101034095448200\r\n', 'OK\r\n'], # see: babca/python-gsmmodem#15
                  Sms.STATUS_RECEIVED_READ, # message read status
                  '+62895357614989', # number
                  35, # reference
                  datetime(2016, 10, 30, 4, 59, 44, tzinfo=SimpleOffsetTzInfo(8)), # sentTime
                  datetime(2016, 10, 30, 4, 59, 44, tzinfo=SimpleOffsetTzInfo(7)), # deliverTime
                  StatusReport.DELIVERED), # delivery status
                 )

        callbackDone = [False]

        for modemResponse, msgStatus, number, reference, sentTime, deliverTime, deliveryStatus in tests:
            def smsCallbackFunc1(sms):
                try:
                    self.assertIsInstance(sms, gsmmodem.modem.StatusReport)
                    self.assertEqual(sms.status, msgStatus, 'Status report read status incorrect. Expected: "{0}", got: "{1}"'.format(msgStatus, sms.status))
                    self.assertEqual(sms.number, number, 'SMS sender number incorrect. Expected: "{0}", got: "{1}"'.format(number, sms.number))
                    self.assertEqual(sms.reference, reference, 'Status report SMS reference number incorrect. Expected: "{0}", got: "{1}"'.format(reference, sms.reference))
                    self.assertIsInstance(sms.timeSent, datetime, 'SMS sent time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.timeSent)))
                    self.assertEqual(sms.timeSent, sentTime, 'SMS sent time incorrect. Expected: "{0}", got: "{1}"'.format(sentTime, sms.timeSent))
                    self.assertIsInstance(sms.timeFinalized, datetime, 'SMS finalized time type invalid. Expected: datetime.datetime, got: {0}"'.format(type(sms.timeFinalized)))
                    self.assertEqual(sms.timeFinalized, deliverTime, 'SMS finalized time incorrect. Expected: "{0}", got: "{1}"'.format(deliverTime, sms.timeFinalized))
                    self.assertEqual(sms.deliveryStatus, deliveryStatus, 'SMS delivery status incorrect. Expected: "{0}", got: "{1}"'.format(deliveryStatus, sms.deliveryStatus))
                    self.assertEqual(sms.smsc, None, 'This SMS should not have any SMSC information')
                finally:
                    callbackDone[0] = True

            def writeCallback1(data):
                if data.startswith('AT+CMGR'):
                    self.modem.serial.flushResponseSequence = True
                    self.modem.serial.responseSequence = modemResponse

            self.initModem(smsStatusReportCallback=smsCallbackFunc1)
            # Fake a "new message" notification
            set_writeCallbackFunc(writeCallback1)
            self.modem.serial.flushResponseSequence = True
            set_response_sequence(['+CDSI: "SM",1\r\n'])
            # Wait for the handler function to finish
            while callbackDone[0] == False:
                time.sleep(0.1)

if __name__ == "__main__":
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    unittest.main()
