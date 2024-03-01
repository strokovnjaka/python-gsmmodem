#!/usr/bin/env python


"""\
Simple script to assist with identifying a GSM modem
The debug information obtained by this script (when using -d) can be used
to aid test cases (since I don't have access to every modem in the world ;-) )

@author: Francois Aucamp <francois.aucamp@gmail.com>
"""
import asyncio
import sys

from gsmmodem.modem import GsmModem
from gsmmodem.exceptions import PinRequiredError, IncorrectPinError

def parseArgs():
    """ Argument parser for Python 2.7 and above """
    from argparse import ArgumentParser
    parser = ArgumentParser(description='Identify and debug attached GSM modem')
    parser.add_argument('port', metavar='PORT', help='port to which the GSM modem is connected; a number or a device name.')
    parser.add_argument('-b', '--baud', metavar='BAUDRATE', default=115200, help='set baud rate')
    parser.add_argument('-p', '--pin', metavar='PIN', default=None, help='SIM card PIN')
    parser.add_argument('-d', '--debug',  action='store_true', help='dump modem debug information (for python-gsmmodem development)')
    parser.add_argument('-w', '--wait', type=int, default=0, help='Wait for modem to start, in seconds')
    return parser.parse_args()

async def main():
    args = parseArgs()
    print ('args:',args)
    modem = GsmModem(args.port, args.baud)

    print('Connecting to GSM modem on {0}...'.format(args.port))
    try:
        await modem.connect(args.pin, waitingForModemToStartInSeconds=args.wait)
    except PinRequiredError:
        sys.stderr.write('Error: SIM card PIN required. Please specify a PIN with the -p argument.\n')
        return
    except IncorrectPinError:
        sys.stderr.write('Error: Incorrect SIM card PIN entered.\n')
        return

    if args.debug:
        # Print debug info
        print('\n== MODEM DEBUG INFORMATION ==\n')
        print('ATI', await modem.write('ATI', parseError=False))
        print('AT+CGMI:', await modem.write('AT+CGMI', parseError=False))
        print('AT+CGMM:', await modem.write('AT+CGMM', parseError=False))
        print('AT+CGMR:', await modem.write('AT+CGMR', parseError=False))
        print('AT+CFUN=?:', await modem.write('AT+CFUN=?', parseError=False))
        print('AT+WIND=?:', await modem.write('AT+WIND=?', parseError=False))
        print('AT+WIND?:', await modem.write('AT+WIND?', parseError=False))
        print('AT+CPMS=?:', await modem.write('AT+CPMS=?', parseError=False))
        print('AT+CNMI=?:', await modem.write('AT+CNMI=?', parseError=False))
        print('AT+CVHU=?:', await modem.write('AT+CVHU=?', parseError=False))
        print('AT+CSMP?:', await modem.write('AT+CSMP?', parseError=False))
        print('AT+GCAP:', await modem.write('AT+GCAP', parseError=False))
        print('AT+CPIN?', await modem.write('AT+CPIN?', parseError=False))
        print('AT+CLAC:', await modem.write('AT+CLAC', parseError=False))
        print()
    else:
        # Print basic info
        print('\n== MODEM INFORMATION ==\n')
        print('Manufacturer:', await modem.manufacturer())
        print('Model:', await modem.model())
        print('Revision:', await modem.revision())
        print('\nIMEI:', await modem.imei())
        print('IMSI:', await modem.imsi())
        print('\nNetwork:', await modem.networkName())
        print('Signal strength:', await modem.signalStrength())
        print()

if __name__ == '__main__':
    asyncio.run(main())

