#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from setuptools import setup

setup(
    name='shoutd',
    version='0.1.1',
    description='shoutd',
    author='xhs',
    author_email='chef@dark.kitchen',
    url='https://github.com/xhs/shoutd',
    packages=['shoutd'],
    scripts=['bin/shoutd', 'bin/shoutdctl'],
    install_requires=['structlog', 'pyroute2', 'docopt', 'raftkit', 'requests'],
    classifiers=[
        'Programming Language :: Python :: 3'
    ]
)
