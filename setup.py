#!/usr/bin/env python

""" python-gsmmodem installation script """

import sys
from distutils.core import Command
from setuptools import setup

test_command = [sys.executable, '-m', 'unittest', 'discover']
coverage_command = ['coverage', 'run', '-m', 'unittest', 'discover']

VERSION = "0.2"

class RunUnitTests(Command):
    """ run unit tests """

    user_options = []
    description = __doc__[1:]

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        import subprocess
        errno = subprocess.call(test_command)
        raise SystemExit(errno)

class RunUnitTestsCoverage(Command):
    """ run unit tests and report on code coverage using the 'coverage' tool """

    user_options = []
    description = __doc__[1:]

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        import subprocess
        errno = subprocess.call(coverage_command)
        if errno == 0:
            subprocess.call(['coverage', 'report'])
        raise SystemExit(errno)

setup(use_scm_version=True,
      cmdclass = {'test': RunUnitTests,
                  'coverage': RunUnitTestsCoverage})
