#!/usr/bin/env python

"""
Demo: Send Simple SMS Demo

Simple demo to send sms via gsmmodem package

Note: you need to modify at least the SMS_DESTINATION variable (check SMS_TEXT, PORT, BAUDRATE, and PIN also) for this to work
"""

import asyncio
import logging

from gsmmodem.modem import GsmModem, SentSms

# PORT = 'COM5' # ON WINDOWS, Port is from COM1 to COM9 ,
# We can check using the 'mode' command in cmd
PORT = '/dev/ttyUSB1'
BAUDRATE = 115200
SMS_TEXT = 'A good teacher is like a candle, it consumes itself to light the way for others.'
SMS_DESTINATION = 'YOUR PHONE NUMBER HERE'
PIN = None  # SIM card PIN (if any)


async def main():
    print('Initializing modem...')
    # Uncomment the following line to see what the modem is doing:
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    modem = GsmModem(PORT, BAUDRATE)
    await modem.connect(PIN)
    await modem.waitForNetworkCoverage(10)
    print('Sending SMS to: {0}'.format(SMS_DESTINATION))

    response = await modem.sendSms(SMS_DESTINATION, SMS_TEXT, True)
    if type(response) == SentSms:
        print('SMS Delivered.')
    else:
        print('SMS Could not be sent')

    print('Closing modem...')
    await modem.close()


if __name__ == '__main__':
    asyncio.run(main())
