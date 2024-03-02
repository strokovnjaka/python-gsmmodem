#!/usr/bin/env python


"""\
Simple script to send an SMS message

@author: Francois Aucamp <francois.aucamp@gmail.com>
"""
import asyncio
import sys, logging

from gsmmodem.modem import GsmModem, SentSms
from gsmmodem.exceptions import TimeoutException, PinRequiredError, IncorrectPinError

def parseArgs():
    """ Argument parser for Python 2.7 and above """
    from argparse import ArgumentParser
    parser = ArgumentParser(description='Simple script for sending SMS messages')
    parser.add_argument('-i', '--port', metavar='PORT', help='port to which the GSM modem is connected; a number or a device name.')
    parser.add_argument('-b', '--baud', metavar='BAUDRATE', default=115200, help='set baud rate')
    parser.add_argument('-p', '--pin', metavar='PIN', default=None, help='SIM card PIN')
    parser.add_argument('-d', '--deliver', action='store_true', help='wait for SMS delivery report')
    parser.add_argument('-w', '--wait', type=int, default=0, help='Wait for modem to start, in seconds')
    parser.add_argument('--CNMI', default='', help='Set the CNMI of the modem, used for message notifications')
    parser.add_argument('--debug', action='store_true', help='turn on debug (serial port dump)')
    parser.add_argument('destination', metavar='DESTINATION', help='destination mobile number')
    parser.add_argument('message', nargs='?', metavar='MESSAGE', help='message to send, defaults to stdin-prompt')
    return parser.parse_args()

async def main():
    args = parseArgs()
    if args.port == None:
        sys.stderr.write('Error: No port specified. Please specify the port to which the GSM modem is connected using the -i argument.\n')
        return

    modem = GsmModem(args.port, args.baud, AT_CNMI=args.CNMI)
    if args.debug:
        # enable dump on serial port
        logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)
    
    print('Connecting to GSM modem on {0}...'.format(args.port))
    try:
        await modem.connect(args.pin, waitingForModemToStartInSeconds=args.wait)
    except PinRequiredError:
        sys.stderr.write('Error: SIM card PIN required. Please specify a PIN with the -p argument.\n')
        return
    except IncorrectPinError:
        sys.stderr.write('Error: Incorrect SIM card PIN entered.\n')
        return
    print('Checking for network coverage...')
    try:
        await modem.waitForNetworkCoverage(5)
    except TimeoutException:
        print('Network signal strength is not sufficient, please adjust modem position/antenna and try again.')
        await modem.close()
        return
    else:
        if args.message is None:
            print('\nPlease type your message and press enter to send it:')
            text = raw_input('> ')
        else:
            text = args.message
        if args.deliver:
            print ('\nSending SMS and waiting for delivery report...')
        else:
            print('\nSending SMS message...')
        try:
            sms = await modem.sendSms(args.destination, text, waitForDeliveryReport=args.deliver)
        except TimeoutException:
            sys.stderr.write('Failed to send message: the send operation timed out')
            await modem.close()
            return
        else:
            await modem.close()
            if sms.report:
                print('Message sent{0}'.format(' and delivered OK.' if sms.status == SentSms.DELIVERED else ', but delivery failed.'))
            else:
                print('Message sent.')

if __name__ == '__main__':
    asyncio.run(main())
