# Copyright 2013 GridCentric Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import signal
import shutil
import glob
import re
import time
import logging
import subprocess
import tempfile

from mako.template import Template

from reactor.atomic import Atomic
from reactor.atomic import AtomicRunnable
from reactor.config import Config
from reactor.utils import sha_hash
from reactor.loadbalancer.utils import read_pid
from reactor.loadbalancer.connection import LoadBalancerConnection
from reactor.loadbalancer.netstat import connection_count

class NginxLogReader(object):

    def __init__(self, log_filename, log_filter=None):
        super(NginxLogReader, self).__init__()
        self.logfile = None
        self.log_filename = log_filename
        self.log_filter = None
        if log_filter != None:
            self.log_filter = re.compile(log_filter)
        self.connected = False

    def connect(self):
        # Re-open the file by name.
        self.logfile = open(self.log_filename, 'r')

        # Seek to the end of the file, always.
        self.logfile.seek(0, 2)
        self.connected = True

    def nextline(self):
        # Open the file initially.
        if not(self.connected):
            try:
                self.connect()
            except IOError:
                return None

        line = self.logfile.readline().strip()
        while self.log_filter != None and line:
            # Apply the filter to the line.
            m = self.log_filter.match(line)
            if m != None:
                return m.groups()
            else:
                line = self.logfile.readline().strip()

        # If we got nothing, make sure we've got
        # the right file open (due to rotation) and
        # seek to the end where we should still be.
        if not(line):
            try:
                self.connect()
            except IOError:
                logging.warn("Unable to reopen Nginx log.")

        return line

class NginxLogWatcher(AtomicRunnable):
    """
    This will monitor the nginx access log.
    """

    LOG_FILTER = "reactor> " \
               + "\[([^\]]*)\]" \
               + "[^<]*" \
               + "<([^>]*?)>" \
               + "[^<]*" \
               + "<([^>]*?)>" \
               + "[^<]*" \
               + "<([^>]*?)>" \
               + ".*"

    def __init__(self, access_logfile):
        super(NginxLogWatcher, self).__init__()
        self.log = NginxLogReader(access_logfile, log_filter=self.LOG_FILTER)
        self.last_update = time.time()
        self.record = {}

    @Atomic.sync
    def swap(self):
        # Swap out the records.
        now = time.time()
        delta = now - self.last_update
        self.last_update = now
        cur = self.record
        self.record = {}
        return (cur, delta)

    def pull(self):
        (record, delta) = self.swap()

        # Compute the response times.
        for host in record:
            hits = record[host][0]
            metrics = \
                {
                "rate" : (hits, hits / delta),
                "response" : (hits, record[host][2] / hits),
                "bytes" : (hits, record[host][1] / delta),
                }
            record[host] = [metrics]

        return record

    @Atomic.sync
    def run(self):
        while self.is_running():
            line = self.log.nextline()
            if not(line):
                # No updates.
                self._wait(1.0)
            else:
                # We have some information.
                (_, host, body, response) = line
                try:
                    if not(host in self.record):
                        self.record[host] = [0, 0, 0.0]
                    self.record[host][0] += 1
                    self.record[host][1] += int(body)
                    self.record[host][2] += float(response)
                except ValueError:
                    continue

class NginxManagerConfig(Config):

    pid_file = Config.string(label="Pid file",
        default="/var/run/nginx.pid",
        description="The nginx pid file.")

    config_path = Config.string(label="Configuration path",
        default="/etc/nginx/conf.d",
        description="The configuration directory for nginx.")

    site_path = Config.string(label="Sites-enabled path",
        default="/etc/nginx/sites-enabled",
        description="The site path for nginx.")

class NginxEndpointConfig(Config):

    sticky_sessions = Config.boolean(label="Use Sticky Sessions", default=False,
        description="Enables nginx sticky sessions.")

    keepalive = Config.integer(label="Keepalive Connections", default=0,
        validate=lambda self: self.keepalive >= 0 or \
            Config.error("Keepalive must be non-negative."),
        description="Number of backend connections to keep alive.")

    ssl = Config.boolean(label="Use SSL", default=False,
        description="Configures nginx to handle SSL.")

    ssl_certificate = Config.text(label="SSL Certificate", default=None,
        description="An SSL certification in PEM format.")

    ssl_key = Config.text(label="SSL Key", default=None,
        description="An SSL key (not password protected).")

    redirect = Config.string(label="Redirect", default=None,
        description="A 301 redirect to use when no backends are available.")

class Connection(LoadBalancerConnection):

    """ Nginx """

    _MANAGER_CONFIG_CLASS = NginxManagerConfig
    _ENDPOINT_CONFIG_CLASS = NginxEndpointConfig
    _SUPPORTED_URLS = {
        "(http|https)://([a-zA-Z0-9]+[a-zA-Z0-9.]*)(:[0-9]+|)(/.*|)": \
            lambda m: (m.group(1), m.group(2), m.group(3), m.group(4)),
        "(http|https)://(:[0-9]+|)": \
            lambda m: (m.group(1), None, m.group(2), None)
    }

    def __init__(self, **kwargs):
        super(Connection, self).__init__(**kwargs)
        self.tracked = {}
        template_file = os.path.join(os.path.dirname(__file__), 'nginx.template')
        self.template = Template(filename=template_file)
        self.log_reader = NginxLogWatcher("/var/log/nginx/access.log")
        self.log_reader.start()

        if kwargs.get('zkobj') is not None:
            # Remove all sites configurations.
            # We want to start with a clean slate in case
            # there was state leftover from before.
            for conf in glob.glob(
                os.path.join(self._manager_config().site_path, "reactor.*")):
                try:
                    os.remove(conf)
                except OSError:
                    pass

    def __del__(self):
        self.log_reader.stop()
        self.log_reader.join()

    def _generate_ssl(self, uniq_id, config):
        key = config.ssl_key
        cert = config.ssl_certificate

        prefix = os.path.join(tempfile.gettempdir(), uniq_id)
        try:
            os.makedirs(prefix)
        except OSError:
            pass
        raw_file = os.path.join(prefix, "raw")
        key_file = os.path.join(prefix, "key")
        csr_file = os.path.join(prefix, "csr")
        crt_file = os.path.join(prefix, "crt")

        if key:
            # Save the saved key.
            f = open(key_file, 'w')
            f.write(key)
            f.close()
        elif not os.path.exists(key_file):
            # Genereate a new random key.
            subprocess.check_call(\
                "openssl genrsa -des3 -out %s -passout pass:1 1024" % \
                (raw_file), shell=True, close_fds=True)
            subprocess.check_call(\
                "openssl rsa -in %s -passin pass:1 -out %s" % \
                (raw_file, key_file), shell=True, close_fds=True)

        if cert:
            # Save the saved certificate.
            f = open(crt_file, 'w')
            f.write(cert)
            f.close()
        elif not os.path.exists(crt_file):
            # Generate a new certificate.
            subprocess.check_call(\
                "openssl req -new -key %s -batch -out %s" % \
                (key_file, csr_file), shell=True, close_fds=True)
            subprocess.check_call(\
                "openssl x509 -req -in %s -signkey %s -out %s" % \
                (csr_file, key_file, crt_file), shell=True, close_fds=True)

        # Return the certificate and key.
        return (crt_file, key_file)

    def change(self, url, backends, config=None):
        # We use a simple hash of the URL as the file name for the configuration file.
        uniq_id = sha_hash(url)
        conf_filename = "reactor.%s.conf" % uniq_id

        # Grab the endpoint configuration.
        config = self._endpoint_config(config)

        # Check for a removal.
        if len(backends) == 0 and not config.redirect:
            # Remove the connection from our tracking list.
            if uniq_id in self.tracked:
                del self.tracked[uniq_id]

            try:
                full_conf_file = os.path.join(
                    self._manager_config().site_path, conf_filename)
                if os.path.exists(full_conf_file):
                    os.remove(full_conf_file)
            except OSError:
                logging.warn("Unable to remove file: %s", conf_filename)
            return

        # Parse the given URL.
        (scheme, netloc, listen, path) = self.url_info(url)

        if listen:
            listen = int(listen)
        elif scheme == "http":
            listen = 80
        elif scheme == "https":
            listen = 443

        # Ensure that there is a path.
        path = path or "/"

        # Add the connection to our tracking list, and
        # compute the specification for the template.
        ipspecs = []
        self.tracked[uniq_id] = []
        extra = ''

        # Figure out if we're doing SSL.
        if config.ssl:
            # Try to either extract existing keys and certificates, or
            # we dynamically generate a local cert here (for testing).
            (ssl_certificate, ssl_key) = self._generate_ssl(uniq_id, config)

            # Since we are front-loading SSL, just use raw HTTP to connect
            # to the backends. If the user doesn't want this, they should disable
            # the SSL key in the nginx config.
            scheme = "http"
        else:
            # Don't use any SSL backend.
            (ssl_certificate, ssl_key) = (None, None)

        # Collect all the backend IPs.
        for backend in backends:
            ipspecs.append("%s:%d weight=%d" % (backend.ip, backend.port, backend.weight))
            self.tracked[uniq_id].append((backend.ip, backend.port))

        # Compute any extra bits for the template.
        if self._endpoint_config(config).sticky_sessions:
            extra += '    sticky;\n'
        if self._endpoint_config(config).keepalive:
            extra += '    keepalive %d single;\n' % self._endpoint_config(config).keepalive

        # Check if we're doing a redirect.
        if len(ipspecs) == 0:
            redirect = config.redirect
        else:
            redirect = False

        # Render our given template.
        conf = self.template.render(id=uniq_id,
                                    url=url,
                                    netloc=netloc,
                                    path=path,
                                    scheme=scheme,
                                    listen=str(listen),
                                    ipspecs=ipspecs,
                                    redirect=redirect,
                                    ssl=config.ssl,
                                    ssl_certificate=ssl_certificate,
                                    ssl_key=ssl_key,
                                    extra=extra)

        # Write out the config file.
        config_file = open(os.path.join(
            self._manager_config().site_path, conf_filename), 'wb')
        config_file.write(conf)
        config_file.close()

    def save(self):
        # Copy over our base configuration.
        shutil.copyfile(os.path.join(os.path.dirname(__file__), 'reactor.conf'),
                        os.path.join(self._manager_config().config_path, 'reactor.conf'))

        # Send a signal to NginX to reload the configuration
        # (Note: we might need permission to do this!!)
        pid = read_pid(self._manager_config().pid_file)
        if pid:
            os.kill(pid, signal.SIGHUP)
        else:
            subprocess.call(
                ["service", "nginx", "start"],
                close_fds=True)

    def metrics(self):
        # Grab the log records.
        records = self.log_reader.pull()

        # Grab the active connections.
        active_connections = connection_count()

        for connection_list in self.tracked.values():
            for (ip, port) in connection_list:
                active = active_connections.get((ip, port), 0)

                # Save an additional record.
                hostinfo = "%s:%d" % (ip, port)
                if not(hostinfo in records):
                    records[hostinfo] = []
                records[hostinfo].append({ "active": (1, active) })

        return records

    def drop_session(self, client, backend):
        raise NotImplementedError()
