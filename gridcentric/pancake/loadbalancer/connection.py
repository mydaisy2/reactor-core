"""
The generic load balancer interface.
"""

import logging

import gridcentric.pancake.zookeeper.paths as paths

def get_connection(name, config, scale_manager):

    if name == "nginx":
        from gridcentric.pancake.loadbalancer.nginx import NginxLoadBalancerConfig
        from gridcentric.pancake.loadbalancer.nginx import NginxLoadBalancerConnection
        return NginxLoadBalancerConnection(name, scale_manager, NginxLoadBalancerConfig(config))

    elif name == "dnsmasq":
        from gridcentric.pancake.loadbalancer.dnsmasq import DnsmasqLoadBalancerConfig
        from gridcentric.pancake.loadbalancer.dnsmasq import DnsmasqLoadBalancerConnection
        return DnsmasqLoadBalancerConnection(name, scale_manager, DnsmasqLoadBalancerConfig(config))

    elif name == "tcp":
        from gridcentric.pancake.loadbalancer.tcp import TcpLoadBalancerConfig
        from gridcentric.pancake.loadbalancer.tcp import TcpLoadBalancerConnection
        return TcpLoadBalancerConnection(name, scale_manager, TcpLoadBalancerConfig(config))

    elif name == "none" or name == "":
        return LoadBalancerConnection()

    else:
        logging.error("Unknown load balancer: %s" % name)
        return LoadBalancerConnection()

class LoadBalancerConnection(object):
    def __init__(self, name, scale_manager):
        self._name          = name
        self._scale_manager = scale_manager

    def clear(self):
        pass
    def redirect(self, url, names, other, manager_ips):
        pass
    def change(self, url, names, public_ips, manager_ips, private_ips):
        pass
    def save(self):
        pass
    def metrics(self):
        # Returns { host : (weight, value) }
        return {}

    def _list_ips(self):
        return self._scale_manager.zk_conn.list_children(
                paths.loadbalancer_ips(self._name))

    def _find_ip(self, ips, data=''):
        locked = self._list_ips() or []
        for ip in ips:
            if not(ip in locked):
                if self._lock_ip(ip, data):
                    return ip
        return None

    def _lock_ip(self, ip, data=''):
        return self._scale_manager.zk_conn.trylock(
                paths.loadbalancer_ip(self._name, ip),
                default_value=data)

    def _update_ip(self, ip, data=''):
        return self._scale_manager.zk_conn.write(
                paths.loadbalancer_ip(self._name, ip),
                data)

    def _read_ip(self, ip):
        return self._scale_manager.zk_conn.read(
                paths.loadbalancer_ip(self._name, ip))

    def _forget_ip(self, ip):
        return self._scale_manager.zk_conn.delete(
                paths.loadbalancer_ip(self._name, ip))

class BackendIP(object):
    def __init__(self, ip, port=0, weight=1):
        self.ip     = ip
        self.port   = port
        self.weight = weight

class LoadBalancers(list):

    def clear(self):
        for lb in self:
            lb.clear()

    def redirect(self, url, names, other_url, manager_ips):
        for lb in self:
            lb.redirect(url, names, other_url, manager_ips)

    def change(self, url, names, public_ips, manager_ips, private_ips):
        for lb in self:
            lb.change(url, names, public_ips, manager_ips, private_ips)

    def save(self):
        for lb in self:
            lb.save()

    def metrics(self):
        # This is the only complex metric (that requires multiplexing).  We
        # combine the load balancer metrics by hostname, adding weights where
        # they are not unique.
        results = {}
        for lb in self:
            result = lb.metrics()
            for (host, value) in result.items():
                if not(host in results):
                    results[host] = value
                    continue

                for key in value:
                    (oldweight, oldvalue) = results[host][key]
                    (newweight, newvalue) = value[key]
                    weight = (oldweight + newweight)
                    value = ((oldvalue * oldweight) \
                          + (newvalue * newweight)) / weight
                    results[host][key] = (weight, value)

        return results
