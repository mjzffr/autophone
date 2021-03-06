# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import glob
import json
import logging
import os
import re
import tempfile
import time
import urlparse

import requests
from thclient import (TreeherderClient, TreeherderJobCollection)

from s3 import S3Error

LEAK_RE = re.compile('\d+ bytes leaked \((.+)\)$')
CRASH_RE = re.compile('.+ application crashed \[@ (.+)\]$')

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

def timestamp_now():
    return int(time.mktime(datetime.datetime.now().timetuple()))


class TestState(object):
    COMPLETED = 'completed'
    PENDING = 'pending'
    RUNNING = 'running'


class AutophoneTreeherder(object):

    def __init__(self, worker_subprocess, options, s3_bucket=None, mailer=None,
                 shared_lock=None):
        assert options, "options is required."
        assert shared_lock, "shared_lock is required."

        self.options = options
        self.s3_bucket = s3_bucket
        self.mailer = mailer
        self.shared_lock = shared_lock
        self.worker = worker_subprocess
        logger.debug('AutophoneTreeherder')

        self.url = self.options.treeherder_url
        if not self.url:
            logger.debug('AutophoneTreeherder: no treeherder url')
            return

        self.server = self.options.treeherder_server
        self.protocol = self.options.treeherder_protocol
        self.host = self.options.treeherder_server
        self.credentials = self.options.treeherder_credentials
        self.retries = self.options.treeherder_retries
        self.retry_wait = self.options.treeherder_retry_wait
        self.bugscache_uri = '%s/api/bugscache/' % self.url

        logger.debug('AutophoneTreeherder: %s' % self)

    def __str__(self):
        # Do not publish sensitive information
        whitelist = ('url',
                     'server',
                     'protocol',
                     'host',
                     'retries',
                     'retry_wait',
                     'bugscache_uri')
        d = {}
        for attr in whitelist:
            d[attr] = getattr(self, attr)
        return '%s' % d

    def post_request(self, machine, project, job_collection):
        logger.debug('AutophoneTreeherder.post_request: %s' % job_collection.__dict__)
        logger.debug('AutophoneTreeherder shared_lock.acquire')
        self.shared_lock.acquire()
        try:
            client = TreeherderClient(protocol=self.protocol, host=self.server)

            for attempt in range(1, self.retries+1):
                try:
                    client.post_collection(
                        project,
                        self.credentials[project]['consumer_key'],
                        self.credentials[project]['consumer_secret'],
                        job_collection)
                    return
                except requests.exceptions.Timeout:
                    msg = ('Attempt %d to post result to '
                           'Treeherder timed out.\n\n\n' % attempt)
                    logger.error(msg)
                    if self.mailer:
                        self.mailer.send('Attempt %d for Phone %s failed to post to Treeherder' %
                                         (attempt, machine), msg)
                    time.sleep(self.retry_wait)
                except Exception, e:
                    logger.exception('Error submitting request to Treeherder')
                    if self.mailer:
                        self.mailer.send('Error submitting request to Treeherder',
                                         'Phone: %s\n'
                                         'TreeherderClientError: %s\n'
                                         'TreeherderJobCollection %s\n' % (
                                             machine,
                                             e,
                                             job_collection.to_json()))
                    return
            logger.error('Error submitting request to Treeherder')
            if self.mailer:
                self.mailer.send('Error submitting request to Treeherder',
                                 'Phone: %s\n'
                                 'TreeherderClientError: %s\n'
                                 'TreeherderJobCollection %s\n' % (
                                     machine,
                                     e,
                                     job_collection.to_json()))
        finally:
            logger.debug('AutophoneTreeherder shared_lock.release')
            self.shared_lock.release()

    def submit_pending(self, machine, build_url, project, revision_hash, tests=[]):
        """Submit tests pending notifications to Treeherder

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision_hash: Treeherder revision hash of build.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_pending: %s' % tests)
        if not self.url or not revision_hash:
            logger.debug('AutophoneTreeherder.submit_pending: no url/revision hash')
            return

        tjc = TreeherderJobCollection(job_type='update')

        for t in tests:
            t.message = None
            t.submit_timestamp = timestamp_now()
            t.job_details = []

            logger.info('creating Treeherder job %s for %s %s, '
                        'revision_hash: %s' % (
                            t.job_guid, t.name, project,
                            revision_hash))

            logger.debug('AutophoneTreeherder.submit_pending: '
                         'test config_file=%s, config sections=%s' % (
                             t.config_file, t.cfg.sections()))

            tj = tjc.get_job()
            tj.add_revision_hash(revision_hash)
            tj.add_project(project)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.PENDING)
            tj.add_submit_timestamp(t.submit_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_start_timestamp(t.submit_timestamp)
            tj.add_end_timestamp(t.submit_timestamp)
            #
            tj.add_machine(machine)
            tj.add_build_url(build_url)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            # Fake the buildername from buildbot...
            tj.add_artifact('buildapi', 'json', {
                'buildername': t.get_buildername(project)})
            # Create a 'privatebuild' artifact for storing information
            # regarding the build.
            tj.add_artifact('privatebuild', 'json', {
                'build_url': build_url,
                'config_file': t.config_file,
                'chunk': t.chunk})
            tjc.add(tj)

        logger.debug('AutophoneTreeherder.submit_pending: tjc: %s' % (
            tjc.to_json()))

        self.post_request(machine, project, tjc)

    def submit_running(self, machine, build_url, project, revision_hash, tests=[]):
        """Submit tests running notifications to Treeherder

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision_hash: Treeherder revision hash of build.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_running: %s' % tests)
        if not self.url or not revision_hash:
            logger.debug('AutophoneTreeherder.submit_running: no url/revision hash')
            return

        tjc = TreeherderJobCollection(job_type='update')

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_running: '
                         'for %s %s' % (t.name, project))

            t.submit_timestamp = timestamp_now()
            t.start_timestamp = timestamp_now()

            tj = tjc.get_job()
            tj.add_revision_hash(revision_hash)
            tj.add_project(project)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.RUNNING)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_end_timestamp(t.start_timestamp)
            #
            tj.add_machine(machine)
            tj.add_build_url(build_url)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            tj.add_artifact('buildapi', 'json', {
                'buildername': t.get_buildername(project)})
            tj.add_artifact('privatebuild', 'json', {
                'build_url': build_url,
                'config_file': t.config_file,
                'chunk': t.chunk})
            tjc.add(tj)

        logger.debug('AutophoneTreeherder.submit_running: tjc: %s' %
                     tjc.to_json())

        self.post_request(machine, project, tjc)

    def submit_complete(self, machine, build_url, project, revision_hash,
                        tests=None):
        """Submit test results for the worker's current job to Treeherder.

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision_hash: Treeherder revision hash of build.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_complete: %s' % tests)

        if not self.url or not revision_hash:
            logger.debug('AutophoneTreeherder.submit_complete: no url/revision hash')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_complete '
                         'for %s %s' % (t.name, project))

            t.job_details.append({
                'value': os.path.basename(t.config_file),
                'content_type': 'text',
                'title': 'Config'})
            t.job_details.append({
                'url': build_url,
                'value': os.path.basename(build_url),
                'content_type': 'link',
                'title': 'Build'})

            t.end_timestamp = timestamp_now()
            # A usercancelled job may not have a start_timestamp
            # since it may have been cancelled before it started.
            if not t.start_timestamp:
                t.start_timestamp = t.end_timestamp

            if t.test_result.failed == 0:
                failed = '0'
            else:
                failed = '<em class="testfail">%s</em>' % t.test_result.failed

            t.job_details.append({
                'value': "%s/%s/%s" % (t.test_result.passed, failed, t.test_result.todo),
                'content_type': 'raw_html',
                'title': "%s-%s" % (t.job_name, t.job_symbol)
            })

            if hasattr(t, 'phonedash_url'):
                t.job_details.append({
                    'url': t.phonedash_url,
                    'value': 'graph',
                    'content_type': 'link',
                    'title': 'phonedash'
                    })

            tj = tjc.get_job()

            # Attach logs
            if self.s3_bucket:
                # We must make certain that S3 keys for uploaded files
                # are unique. We can create a unique log_identifier as
                # follows: For Unittests, t._log's basename contains a
                # unique name based on the actual Unittest name, chunk
                # and machine id. For Non-Unittests, the test classname,
                # chunk and machine id can be used.

                if t._log:
                    log_identifier = os.path.splitext(os.path.basename(t._log))[0]
                else:
                    log_identifier = "%s-%s-%s-%s" % (
                        t.name, os.path.basename(t.config_file), t.chunk,
                        machine)
                # We must make certain the key is unique even in the
                # event of retries.
                log_identifier = '%s-%s' % (log_identifier, t.job_guid)

                key_prefix = os.path.dirname(
                    urlparse.urlparse(build_url).path)
                key_prefix = re.sub('/tmp$', '', key_prefix)

                # Logcat
                fname = '%s-logcat.log' % log_identifier
                lname = 'logcat'
                key = "%s/%s" % (key_prefix, fname)
                with tempfile.NamedTemporaryFile(suffix='logcat.txt') as f:
                    try:
                        for line in t.logcat.get(full=True):
                            f.write('%s\n' % line)
                    except Exception, e:
                        logger.exception('Error reading logcat %s' % fname)
                        t.job_details.append({
                            'value': 'Failed to read %s: %s' % (fname, e),
                            'content_type': 'text',
                            'title': 'Error'})
                    try:
                        url = self.s3_bucket.upload(f.name, key)
                        tj.add_log_reference(lname, url,
                                             parse_status='parsed')
                        t.job_details.append({
                            'url': url,
                            'value': lname,
                            'content_type': 'link',
                            'title': 'artifact uploaded'})
                    except S3Error, e:
                        logger.exception('Error uploading logcat %s' % fname)
                        t.job_details.append({
                            'value': 'Failed to upload %s: %s' % (fname, e),
                            'content_type': 'text',
                            'title': 'Error'})
                # Upload directory containing ANRs, tombstones and other items
                # to be uploaded.
                if t.upload_dir:
                    for f in glob.glob(os.path.join(t.upload_dir, '*')):
                        try:
                            lname = os.path.basename(f)
                            fname = '%s-%s' % (log_identifier, lname)
                            url = self.s3_bucket.upload(f, "%s/%s" % (
                                key_prefix, fname))
                            t.job_details.append({
                                'url': url,
                                'value': lname,
                                'content_type': 'link',
                                'title': 'artifact uploaded'})
                        except S3Error, e:
                            logger.exception('Error uploading artifact %s' % fname)
                            t.job_details.append({
                                'value': 'Failed to upload artifact %s: %s' % (fname, e),
                                'content_type': 'text',
                                'title': 'Error'})

                logurl = None
                # UnitTest Log
                if t._log and os.path.exists(t._log):
                    fname = '%s.log' % log_identifier
                    lname = os.path.basename(t._log)
                    key = "%s/%s" % (key_prefix, fname)
                    try:
                        logurl = self.s3_bucket.upload(t._log, key)
                        tj.add_log_reference(fname, logurl,
                                             parse_status='parsed')
                        t.job_details.append({
                            'url': logurl,
                            'value': lname,
                            'content_type': 'link',
                            'title': 'artifact uploaded'})
                    except Exception, e:
                        logger.exception('Error %s uploading log %s' % (
                            e, fname))
                        t.job_details.append({
                            'value': 'Failed to upload log %s: %s' % (fname, e),
                            'content_type': 'text',
                            'title': 'Error'})
                # Since we are submitting results to Treeherder, we flush
                # the worker's log before uploading the log to
                # Treeherder. When we upload the log, it will contain
                # results for a single test run with possibly an error
                # message from the previous test if the previous log
                # upload failed.
                try:
                    self.worker.filehandler.flush()
                    fname = '%s-autophone.log' % log_identifier
                    lname = 'Autophone Log'
                    key = "%s/%s" % (key_prefix, fname)
                    url = self.s3_bucket.upload(self.worker.logfile, key)
                    self.worker.filehandler.close()
                    os.unlink(self.worker.logfile)
                    tj.add_log_reference(fname, url,
                                         parse_status='parsed')
                    t.job_details.append({
                        'url': url,
                        'value': lname,
                        'content_type': 'link',
                        'title': 'artifact uploaded'})
                    if not logurl:
                        logurl = url
                except Exception, e:
                    logger.exception('Error %s uploading %s' % (
                        e, fname))
                    t.job_details.append({
                        'value': 'Failed to upload Autophone log: %s' % e,
                        'content_type': 'text',
                        'title': 'Error'})

            tj.add_revision_hash(revision_hash)
            tj.add_project(project)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.COMPLETED)
            tj.add_result(t.test_result.status)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            tj.add_end_timestamp(t.end_timestamp)
            tj.add_machine(machine)
            tj.add_build_url(build_url)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            error_lines = []
            for failure in t.test_result.failures:
                line = ''
                status = failure['status']
                test = failure['test']
                text = failure['text']
                if not (status or test or text):
                    continue
                if status and test and text:
                    line = '%s | %s | %s' % (status, test, text)
                elif test and text:
                    line = '%s | %s' % (test, text)
                elif text:
                    line = text
                # XXX Need to update the log parser to return the line
                # numbers of the errors.
                if line:
                    error_lines.append({"line": line, "linenumber": 1})

            text_log_summary = {
                'header': {
                    'slave': machine,
                    'revision': revision_hash
                },
                'step_data': {
                    'all_errors': error_lines,
                    'steps': [
                        {
                            'name': 'step',
                            'started_linenumber': 1,
                            'finished_linenumber': 1,
                            'duration': t.end_timestamp - t.start_timestamp,
                            'finished': '%s' % datetime.datetime.fromtimestamp(t.end_timestamp),
                            'errors': error_lines,
                            'error_count': len(error_lines),
                            'order': 0,
                            'result': t.test_result.status
                        },
                    ],
                    'errors_truncated': False
                    },
                'logurl': logurl
                }

            tj.add_artifact('text_log_summary', 'json', json.dumps(text_log_summary))
            logger.debug('AutophoneTreeherder.submit_complete: text_log_summary: %s' % json.dumps(text_log_summary))

            tj.add_artifact('Job Info', 'json', {'job_details': t.job_details})
            tj.add_artifact('buildapi', 'json', {
                'buildername': t.get_buildername(project)})

            tj.add_artifact('privatebuild', 'json', {
                'build_url': build_url,
                'config_file': t.config_file,
                'chunk': t.chunk})
            tjc.add(tj)

            message = 'TestResult: %s %s %s' % (t.test_result.status, t.name, build_url)
            if t.message:
                message += ', %s' % t.message
            logger.info(message)

        logger.debug('AutophoneTreeherder.submit_completed: tjc: %s' %
                     tjc.to_json())

        self.post_request(machine, project, tjc)
