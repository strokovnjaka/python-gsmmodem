#!/usr/bin/env python

"""\
Demo: read own phone number

Note: check PORT, BAUDRATE, and PIN variables to make this work
"""

import asyncio
import logging

PORT = '/dev/vmodem0'
BAUDRATE = 115200
PIN = None # SIM card PIN (if any)

from gsmmodem.modem import GsmModem

async def main():
    print('Initializing modem...')
    # Uncomment the following line to see what the modem is doing:
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    modem = GsmModem(PORT, BAUDRATE)
    await modem.connect(PIN)

    number = await modem.ownNumber()
    print(f"The SIM card phone number is {number}")

    # Uncomment the following block to change your own number.
    # await modem.setOwnNumber("+000123456789") # lease empty for removing the phone entry altogether

    # number = await modem.ownNumber()
    # print(f"The new phone number is {number}")

    print('Closing modem...')
    await modem.close()

if __name__ == '__main__':
    asyncio.run(main())
