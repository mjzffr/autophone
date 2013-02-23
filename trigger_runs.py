# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging
import pytz
import re
import socket

import builds

def from_iso_date_or_datetime(s):
    datefmt = '%Y-%m-%d'
    datetimefmt = datefmt + 'T%H:%M:%S'
    try:
        d = datetime.datetime.strptime(s, datetimefmt)
    except ValueError:
        d = datetime.datetime.strptime(s, datefmt)
    return d


def main(args, options):
    logging.info('Looking for builds...')
    if args[0] == 'latest':
        cache_build_dir = (builds.BuildCache(options.repos,
                                             options.buildtypes).
                           find_latest_build(options.build_location))
        if not cache_build_dir:
            return 1
        commands = ['triggerjobs %s' % cache_build_dir]
    else:
        if re.match('\d{14}', args[0]):
            # build id
            build_time = datetime.datetime.strptime(args[0], '%Y%m%d%H%M%S')
            start_time = build_time
            end_time = build_time
        else:
            start_time = from_iso_date_or_datetime(args[0])
            if len(args) > 1:
                end_time = from_iso_date_or_datetime(args[1])
            else:
                end_time = datetime.datetime.now()
        if not start_time.tzinfo:
            start_time = start_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        if not end_time.tzinfo:
            end_time = end_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        cache_build_dir_list = (builds.BuildCache(options.repos,
                                                  options.buildtypes).
                                find_builds(start_time, end_time,
                                            options.build_location))
        if not cache_build_dir_list:
            return 1
        commands = ['triggerjobs %s' % cache_build_dir for cache_build_dir in
                    cache_build_dir_list]
    logging.info('Connecting to autophone server...')
    commands.append('exit')
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((options.ip, options.port))
    logging.info('- %s' % s.recv(1024).strip())
    for c in commands:
        logging.info('%s' % c)
        s.sendall(c + '\n')
        logging.info('- %s' % s.recv(1024).strip())
    return 0

            
if __name__ == '__main__':
    import errno
    import sys
    from optparse import OptionParser

    usage = '''%prog [options] <datetime, date/datetime, or date/datetime range>
Triggers one or more test runs.

The argument(s) should be one of the following:
- a build ID, e.g. 20120403063158
- a date/datetime, e.g. 2012-04-03 or 2012-04-03T06:31:58
- a date/datetime range, e.g. 2012-04-03T06:31:58 2012-04-05
- the string "latest"

If a build ID is given, a test run is initiated for that, and only that,
particular build.

If a single date or datetime is given, test runs are initiated for all builds
with build IDs between the given date/datetime and now.

If a date/datetime range is given, test runs are initiated for all builds
with build IDs in the given range.

If "latest" is given, test runs are initiated for the most recent build.'''
    parser = OptionParser(usage=usage)
    parser.add_option('-i', '--ip', action='store', type='string', dest='ip',
                      default='127.0.0.1',
                      help='IP address of autophone controller; defaults to localhost')
    parser.add_option('-p', '--port', action='store', type='int', dest='port',
                      default=28001,
                      help='port of autophone controller; defaults to 28001')
    parser.add_option('-b', '--build-location', action='store', type='string',
                      dest='build_location', default='nightly',
                      help='build location to search for builds, defaults to nightly;'
                      ' can be "tinderbox" for both m-c and m-i')
    parser.add_option('-v', '--verbose', action='store_true', dest='verbose',
                      default=False, help='verbose output')
    parser.add_option('--repo',
                      dest='repos',
                      action='append',
                      help='The repos to test. '
                      'One of mozilla-central, mozilla-inbound, mozilla-aurora, '
                      'mozilla-beta. To specify multiple repos, specify them '
                      'with additional --repo options. Defaults to mozilla-central.')
    parser.add_option('--buildtype',
                      dest='buildtypes',
                      action='append',
                      help='The build types to test. '
                      'One of opt or debug. To specify multiple build types, '
                      'specify them with additional --buildtype options. '
                      'Defaults to opt.')
    (options, args) = parser.parse_args()
    if len(args) > 2:
        parser.print_help()
        sys.exit(errno.EINVAL)

    if not options.repos:
        options.repos = ['mozilla-central']

    if not options.buildtypes:
        options.buildtypes = ['opt']

    if options.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(level=log_level)

    sys.exit(main(args, options))