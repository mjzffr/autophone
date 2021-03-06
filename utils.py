# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# get_remote_content modelled on treeherder/etc/common.py

import httplib
import json
import logging
import os
import random
import re
import time
import urllib2
import urlparse
import uuid

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

def get_remote_text(url):
    """Return the string containing the contents of a remote url if the
    HTTP response code is 200, otherwise return None.

    :param url: url of content to be retrieved.
    """
    conn = None

    try:
        scheme = urlparse.urlparse(url).scheme
        if not scheme:
            raise Exception('required scheme missing in url %s' % url)

        if scheme.startswith('file'):
            conn = urllib2.urlopen(url)
            return conn.read()

        while True:
            conn = urllib2.urlopen(url)
            code = conn.getcode()
            if code == 200:
                content = conn.read()
                return content
            if code != 503:
                logger.warning("Unable to open url %s : %s" % (
                    url, httplib.responses[code]))
                return None
            # Server is too busy. Wait and try again.
            # See https://bugzilla.mozilla.org/show_bug.cgi?id=1146983#c10
            logger.warning("HTTP 503 Server Too Busy: url %s" % url)
            conn.close()
            time.sleep(60 + random.randrange(0,30,1))
    except urllib2.HTTPError, e:
        logger.warning('%s Unable to open %s' % (e, url))
        return None
    except Exception:
        logger.exception('Unable to open %s' % url)
        return None
    finally:
        if conn:
            conn.close()

    return content


def get_remote_json(url):
    """Return the json representation of the contents of a remote url if
    the HTTP response code is 200, otherwise return None.

    :param url: url of content to be retrieved.
    """
    content = get_remote_text(url)
    if content:
        content = json.loads(content)
    return content


def get_build_data(build_url):
    """Return a dict containing information parsed from a build's .txt
    file.

    Returns None if the file does not exist or does not contain build
    data, otherwise returns a dict with keys:

       'id'       : build id of form 'CCYYMMDDHHSS'
       'changeset': url to changeset
       'repo'     : build repository
       'revision' : revision

    :param build_url: string containing url to the firefox build.
    """
    build_prefix, build_ext = os.path.splitext(build_url)
    build_txt = build_prefix + '.txt'
    content = get_remote_text(build_txt)
    if not content:
        return None

    lines = content.splitlines()
    if len(lines) < 1:
        return None

    buildid_regex = re.compile(r'([\d]{14})$')
    changeset_regex = re.compile(r'.*/([^/]*)/rev/(.*)')

    buildid_match = buildid_regex.match(lines[0])
    changeset_match = changeset_regex.match(lines[1])
    if not buildid_match or not changeset_match:
        return None

    build_data = {
        'id' : lines[0],
        'changeset' : lines[1],
        'repo' : changeset_match.group(1),
        'revision' : changeset_match.group(2),
    }
    return build_data


def get_treeherder_revision_hash(treeherder_url, repo, revision):
    """Return the Treeherder revision_hash.

    :param treeherder_url: url to the treeherder server.
    :param repo: repository name for the revision.
    :param revision: revision id for the changeset.
    """
    if not treeherder_url or not repo or not revision:
        return None

    revurl = '%s/api/project/%s/revision-lookup/?revision=%s' % (
        treeherder_url, repo, revision)
    revision_lookup = get_remote_json(revurl)
    if not revision_lookup:
        return None

    if revision not in revision_lookup:
        return None

    return revision_lookup[revision]['revision_hash']


def generate_guid():
    return str(uuid.uuid4())
