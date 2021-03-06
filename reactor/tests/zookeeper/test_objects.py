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

import array

from reactor.zookeeper.objects import JSONObject
from reactor.zookeeper.objects import RawObject
from reactor.zookeeper.objects import BinObject

def _test_obj(obj):
    if isinstance(obj, JSONObject):
        return { "a" : { "b" : [ "c", "d", "e" ] } }
    elif isinstance(obj, RawObject):
        return "foo"
    elif isinstance(obj, BinObject):
        return array.array("b", [1, 1, 2, 3, 5, 8])
    else:
        raise NotImplementedError()

def test_create(zk_conn, zk_object):
    assert not zk_conn.exists(zk_object._path)
    zk_object._set_data()
    assert zk_conn.exists(zk_object._path)

def test_save_contents(zk_object):
    test_obj = _test_obj(zk_object)
    zk_object._set_data(test_obj)
    assert zk_object._get_data() == test_obj

def test_watch_contents(zk_conn, zk_object):
    test_obj = _test_obj(zk_object)
    watch_ref = [False]
    def watch_fired(value):
        watch_ref[0] = value
    assert not zk_object._get_data(watch=watch_fired)
    zk_object._set_data(test_obj)
    zk_conn.sync()
    assert watch_ref[0] == test_obj
    zk_object._set_data(None)
    zk_conn.sync()
    assert watch_ref[0] != test_obj

def test_watch_removed(zk_conn, zk_object):
    test_obj = _test_obj(zk_object)
    watch_ref = [False]
    def watch_fired(value):
        watch_ref[0] = value
    assert not zk_object._get_data(watch=watch_fired)
    zk_object._unwatch()
    zk_object._set_data(test_obj)
    zk_conn.sync()
    assert watch_ref[0] == False

def test_list(zk_object):
    assert zk_object._list_children() == []
    child = zk_object._get_child("child")
    child._set_data()
    assert zk_object._list_children() == ["child"]
    child._delete()
    assert zk_object._list_children() == []

def test_watch_children(zk_conn, zk_object):
    watch_ref = [False]
    def watch_fired(value):
        watch_ref[0] = value
    assert zk_object._list_children(watch=watch_fired) == []
    child = zk_object._get_child("child")
    child._set_data()
    zk_conn.sync()
    assert watch_ref[0] == ["child"]

def test_watch_children(zk_conn, zk_object):
    watch_ref = [False]
    def watch_fired(value):
        watch_ref[0] = value
    assert zk_object._list_children(watch=watch_fired) == []
    zk_object._unwatch()
    child = zk_object._get_child("child")
    child._set_data()
    zk_conn.sync()
    assert watch_ref[0] == False

def test_delete(zk_conn, zk_object):
    assert not zk_conn.exists(zk_object._path)
    zk_object._set_data(None)
    assert zk_conn.exists(zk_object._path)
