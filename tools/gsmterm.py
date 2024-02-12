#!/usr/bin/env python

"""\
Launch script for GSMTerm

@author: Francois Aucamp <francois.aucamp@gmail.com>
"""
from __future__ import print_function

import sys

from gsmtermlib.terminal import GsmTerm, RawTerm

def parseArgs():
    """ Argument parser for Python 2.7 and above """
    from argparse import ArgumentParser
    parser = ArgumentParser(description='User-friendly terminal for interacting with a connected GSM modem.')
    parser.add_argument('port', metavar='PORT', help='port to which the GSM modem is connected; a number or a device name.')
    parser.add_argument('-b', '--baud', metavar='BAUDRATE', default=115200, help='set baud rate')
    parser.add_argument('-r', '--raw',  action='store_true', help='switch to raw terminal mode')
    return parser.parse_args()

def main():
    args = parseArgs()
    if args.raw:
        gsmTerm = RawTerm(args.port, args.baud)
    else:
        gsmTerm = GsmTerm(args.port, args.baud)

    gsmTerm.start()
    gsmTerm.rxThread.join()
    print('Done.')

if __name__ == '__main__':
    main()

