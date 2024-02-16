#!/usr/bin/env python

"""\
Demo: handle incoming calls

Simple demo app that listens for incoming calls, displays the caller ID,
optionally answers the call and plays sone DTMF tones (if supported by modem),
and hangs up the call.

Note: check PORT, BAUDRATE, and PIN variables to make this work
"""

import asyncio
import logging

PORT = '/dev/ttyUSB1'
BAUDRATE = 115200
PIN = None # SIM card PIN (if any)

from gsmmodem.modem import GsmModem, IncomingCall
from gsmmodem.exceptions import InterruptedException

async def handleIncomingCall(call: IncomingCall):
    if call.ringCount == 1:
        print('Incoming call from:', call.number)
    elif call.ringCount >= 3:
        if call.dtmfSupport:
            print('Answering call and playing some DTMF tones...')
            await call.answer()
            # Wait for a bit - some older modems struggle to send DTMF tone immediately after answering a call
            asyncio.sleep(2.0)
            try:
                await call.sendDtmfTone('9515999955951')
            except InterruptedException as e:
                # Call was ended during playback
                print('DTMF playback interrupted: {0} ({1} Error {2})'.format(e, e.cause.type, e.cause.code))
            finally:
                if call.answered:
                    print('Hanging up call.')
                    await call.hangup()
        else:
            print('Modem has no DTMF support - hanging up call.')
            await call.hangup()
    else: 
        # The second ring
        print(' Call from {0} is still ringing...'.format(call.number))

async def main():
    print('Initializing modem...')
    # Uncomment the following line to see what the modem is doing:
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    modem = GsmModem(PORT, BAUDRATE, incomingCallCallbackFunc=handleIncomingCall)
    await modem.connect(PIN)
    print('Waiting for incoming calls... [ctrl-c to exit]')
    try:
        asyncio.sleep(60*60) # you have an hour to receive the call
    finally:
        print('Closing modem...')
        await modem.close()

if __name__ == '__main__':
    asyncio.run(main())
