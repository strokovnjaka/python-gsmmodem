#!/usr/bin/env python

"""\
Demo: Simple USSD example

Simple demo app that initiates a USSD session, reads the string response and closes the session
(if it wasn't closed by the network)

Note: for this to work, a valid USSD string for your network must be used. Also check PORT, BAUDRATE, and PIN varaibles
"""

import asyncio
import logging

# PORT = 'COM5' # ON WINDOWS, Port is from COM1 to COM9, you can check using the 'mode' command in cmd
PORT = '/dev/ttyUSB1'
BAUDRATE = 115200
USSD_STRING = '*101#'
PIN = None # SIM card PIN (if any)

from gsmmodem.modem import GsmModem

async def main():
    print('Initializing modem...')
    # Uncomment the following line to see what the modem is doing:
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    modem = GsmModem(PORT, BAUDRATE)
    await modem.connect(PIN)
    await modem.waitForNetworkCoverage(10)
    print('Sending USSD string: {0}'.format(USSD_STRING))
    response = await modem.sendUssd(USSD_STRING) # response type: gsmmodem.modem.Ussd
    print('USSD reply received: {0}'.format(response.message))
    if response.sessionActive:
        print('Closing USSD session.')
        # At this point, you could also reply to the USSD message by using response.reply()
        await response.cancel()
    else:
        print('USSD session was ended by network.')
    print('Closing modem...')
    await modem.close()

if __name__ == '__main__':
    asyncio.run(main())
