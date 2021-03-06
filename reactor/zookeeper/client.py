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

import threading

from reactor.atomic import Atomic

from . connection import ZookeeperConnection

class ZookeeperClient(Atomic):

    """ A simple wrapper around connections that allows for reconnection, etc. """

    def __init__(self, zk_servers):
        super(ZookeeperClient, self).__init__()
        self._zk_conn = None
        self._zk_servers = zk_servers
        self._lock = threading.Lock()

    def __del__(self):
        self.disconnect()

    @Atomic.sync
    def servers(self):
        return self._zk_servers

    @Atomic.sync
    def _set_connection(self, zk_conn, zk_servers):
        self.disconnect()
        self._zk_conn = zk_conn
        self._zk_servers = zk_servers
        return self._zk_conn

    @Atomic.sync
    def _get_connection(self, zk_servers):
        if self._zk_servers == zk_servers:
            return self._zk_conn
        return None

    def connect(self, zk_servers=None):
        if zk_servers is None:
            zk_servers = self.servers()

        while True:
            conn = self._get_connection(zk_servers)
            if conn is not None:
                return conn

            # Attempt the connection, but ensure we
            # aren't holding on the lock during the
            # timeout period. This means we could waste
            # a little bit of effort if multiple attempts
            # are happening simultaneously.
            zk_conn = ZookeeperConnection(zk_servers)
            self._set_connection(zk_conn, zk_servers)

    @Atomic.sync
    def disconnect(self):
        if self._zk_conn:
            self._zk_conn.close()
            self._zk_conn = None

    @Atomic.sync
    def connected(self):
        return self._zk_conn != None

    def reconnect(self, zk_servers=None):
        self.disconnect()
        self.connect(zk_servers=zk_servers)
