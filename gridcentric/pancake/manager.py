import ConfigParser
import logging
import threading
import time
import uuid
import hashlib
import bisect
import json
import traceback
from StringIO import StringIO

from gridcentric.pancake.config import ManagerConfig
from gridcentric.pancake.config import ServiceConfig
from gridcentric.pancake.service import Service
import gridcentric.pancake.loadbalancer.connection as lb_connection
from gridcentric.pancake.zookeeper.connection import ZookeeperConnection
from gridcentric.pancake.zookeeper.connection import ZookeeperException
import gridcentric.pancake.zookeeper.paths as paths
import gridcentric.pancake.ips as ips

def locked(fn):
    def wrapped_fn(self, *args, **kwargs):
        try:
            self.cond.acquire()
            return fn(self, *args, **kwargs)
        finally:
            self.cond.release()
    return wrapped_fn

class ScaleManager(object):

    def __init__(self, zk_servers):
        self.running    = False
        self.zk_servers = zk_servers
        self.config     = ManagerConfig("")
        self.cond       = threading.Condition()

        self.uuid   = str(uuid.uuid4()) # Manager uuid (generated).
        self.domain = None              # Pancake domain.

        self.services = {}        # Service map (name -> service)
        self.key_to_services = {} # Service map (key() -> [services...])

        self.managers = {}        # Forward map of manager keys.
        self.manager_keys = []    # Our local manager keys.
        self.key_to_manager = {}  # Reverse map for manager keys.
        self.key_to_owned = {}    # Service to ownership.

        self.load_balancer = None # Load balancer connections.

    @locked
    def serve(self):
        # Create a Zookeeper connection.
        self.zk_conn = ZookeeperConnection(self.zk_servers)

        # Register ourselves.
        self.manager_register(initial=True)

        # Create the loadbalancer connections.
        self.load_balancer = lb_connection.LoadBalancers()
        for name in self.config.loadbalancer_names():
            self.load_balancer.append(\
                lb_connection.get_connection(\
                    name, self.config.loadbalancer_config(name), self))

        # Read the domain.
        self.reload_domain(self.zk_conn.watch_contents(\
                                paths.domain(),
                                self.reload_domain,
                                default_value=self.domain))

        # Watch all IPs.
        self.zk_conn.watch_children(paths.new_ips(), self.register_ip)

        # Watch all managers and services.
        self.manager_change(self.zk_conn.watch_children(paths.managers(), self.manager_change))
        self.service_change(self.zk_conn.watch_children(paths.services(), self.service_change))
        
    @locked
    def manager_select(self, service):
        # Remember whether this was previous managed.
        managed = self.service_owned(service)

        # Find the closest key.
        keys = self.key_to_manager.keys()
        index = bisect.bisect(keys, service.key())
        if len(keys) == 0:
            logging.error("No scale manager available!")
            manager_key = None
        else:
            key = keys[index % len(self.key_to_manager)]
            manager_key = self.key_to_manager[key]

        # Check if this is us.
        self.key_to_owned[service.key()] = (manager_key == self.uuid)

        logging.info("Service %s owned by %s (%s)." % \
            (service.name, manager_key, \
            self.service_owned(service) and "That's me!" or "Not me!"))

        # Check if it is one of our own.
        # Start the service if necessary (now owned).
        if not(managed) and self.service_owned(service):
            self.start_service(service)

    @locked
    def manager_remove(self, service):
        if self.key_to_owned.has_key(service.key()):
            del self.key_to_owned[service.key()]

    @locked
    def service_owned(self, service):
        return self.key_to_owned.get(service.key(), False)

    @locked
    def service_change(self, services):
        logging.info("Services have changed: new=%s, existing=%s" %
                     (services, self.services.keys()))

        for service_name in services:
            if service_name not in self.services:
                self.create_service(service_name)

        services_to_remove = []
        for service_name in self.services:
            if service_name not in services:
                self.remove_service(service_name, unmanage=True)
                services_to_remove += [service_name]

        for service in services_to_remove:
            del self.services[service]

    @locked
    def manager_config_change(self, value):
        self.manager_register(initial=False)

    @locked
    def manager_register(self, initial=False):
        # Figure out our global IPs.
        global_ip = ips.find_global()
        logging.info("Manager %s has key %s." % (global_ip, self.uuid))

        # Reload our global config.
        self.config = ManagerConfig("")
        if initial:
            global_config = self.zk_conn.watch_contents(paths.config(),
                                                        self.manager_config_change,
                                                        str(self.config))
        else:
            global_config = self.zk_conn.read(paths.config())
        if global_config:
            self.config.reload(global_config)

        if global_ip:
            # Reload our local config.
            if initial:
                local_config = self.zk_conn.watch_contents(paths.manager_config(global_ip),
                                                           self.manager_config_change,
                                                           str(self.config))
            else:
                local_config = self.zk_conn.read(paths.manager_config(global_ip))
            if local_config:
                self.config.reload(local_config)

            # Register our IP.
            self.zk_conn.write(paths.manager_ip(global_ip), self.uuid, ephemeral=True)

        # Generate keys.
        while len(self.manager_keys) < self.config.keys():
            # Generate a random hash key to associate with this manager.
            self.manager_keys.append(hashlib.md5(str(uuid.uuid4())).hexdigest())
        while len(self.manager_keys) > self.config.keys():
            # Drop keys while we're too high.
            del self.manager_keys[len(self.manager_keys) - 1]

        # Write out our associated hash keys as an ephemeral node.
        key_string = ",".join(self.manager_keys)
        self.zk_conn.write(paths.manager_keys(self.uuid), key_string, ephemeral=True)

        if not(initial):
            # If we're not doing initial setup, refresh services.
            for service in self.services.values():
                self.manager_select(service)

    @locked
    def manager_change(self, managers):
        for manager in managers:
            if manager not in self.managers:
                # Read the key and update all mappings.
                keys = self.zk_conn.read(paths.manager_keys(manager)).split(",")
                logging.info("Found manager %s with %d keys." % (manager, len(keys)))
                self.managers[manager] = keys
                for key in keys:
                    self.key_to_manager[key] = manager

        managers_to_remove = []
        for manager in self.managers:
            if manager not in managers:
                # Remove all mappings.
                keys = self.managers[manager]
                logging.info("Removing manager %s with %d keys." % (manager, len(keys)))
                for key in keys:
                    if key in self.key_to_manager:
                        del self.key_to_manager[key]
                managers_to_remove.append(manager)
        for manager in managers_to_remove:
            if manager in self.managers:
                del self.managers[manager]

        # Recompute all service owners.
        for service in self.services.values():
            self.manager_select(service)

    @locked
    def create_service(self, service_name):
        logging.info("New service %s found to be managed." % service_name)

        # Create the object.
        service_path = paths.service(service_name)
        service_config = ServiceConfig(self.zk_conn.read(service_path))
        service = Service(service_name, service_config, self)
        self.add_service(service,
                         service_path=service_path,
                         service_config=service_config)

    @locked
    def add_service(self, service, service_path=None, service_config=''):
        self.services[service.name] = service
        service_key = service.key()
        self.key_to_services[service_key] = \
            self.key_to_services.get(service.key(),[]) + [service.name]

        if service_path:
            # Watch the config for this service.
            logging.info("Watching service %s." % (service.name))
            self.zk_conn.watch_contents(service_path,
                                        service.update_config,
                                        str(service_config))

        # Select the manager for this service.
        self.manager_select(service)

        # Update the loadbalancer for this service.
        self.update_loadbalancer(service)

    @locked
    def start_service(self, service):
        # This service is now being managed by us.
        service.manage()
        service.update()

    @locked
    def remove_service(self, service_name, unmanage=False):
        """
        This removes / unmanages the service.
        """
        logging.info("Removing service %s from manager %s" % (service_name, self.uuid))
        service = self.services.get(service_name, None)

        if service:
            # Update the loadbalancer for this service.
            self.update_loadbalancer(service, remove=True)

            logging.info("Unmanaging service %s" %(service_name))
            service_names = self.key_to_services.get(service.key(), [])
            if service_name in service_names:
                # Remove the service name from the list of services with the
                # same key. If the service name is not in the list, then it is
                # fine because we are just removing it anyway.
                service_names.remove(service_name)

            # Perform a full unmanage if this is required.
            if unmanage and self.service_owned(service):
                service.unmanage()

            self.manager_remove(service)

    @locked
    def confirmed_ips(self, service_name):
        """
        Returns a list of all the confirmed ips for the service.
        """
        ips = self.zk_conn.list_children(paths.confirmed_ips(service_name))
        if ips == None:
            ips = []
        return ips

    @locked
    def active_ips(self, service_name):
        """
        Returns all confirmed and static ips for the service.
        """
        ips = []
        ips += self.confirmed_ips(service_name)
        if service_name in self.services:
            ips += self.services[service_name].static_addresses()
        return ips

    @locked
    def drop_ip(self, service_name, ip_address):
        self.zk_conn.delete(paths.confirmed_ip(service_name, ip_address))
        self.zk_conn.delete(paths.ip_address(ip_address))

    @locked
    def register_ip(self, ips):
        def _register_ip(scale_manager, service, ip):
            logging.info("Service %s found for IP %s" % (service.name, ip))
            # We found the service that this IP address belongs. Confirm this
            # IP address and remove it from the new-ip address. Finally update
            # the loadbalancer.
            scale_manager.zk_conn.write(paths.confirmed_ip(service.name, ip), "")
            scale_manager.zk_conn.write(paths.ip_address(ip), service.name)
            scale_manager.zk_conn.delete(paths.new_ip(ip))
            scale_manager.update_loadbalancer(service)

        for ip in ips:
            for service in self.services.values():
                service_ips = service.addresses()
                if ip in service_ips:
                    _register_ip(self, service, ip)
                    break

    @locked
    def update_loadbalancer(self, service, remove = False):
        all_addresses = []
        names = []

        # Go through all services with the same keys.
        for service_name in self.key_to_services.get(service.key(), []):
            if remove and (self.services[service_name] == service):
                continue
            else:
                names.append(service_name)
                all_addresses += self.active_ips(service_name)

        logging.info("Updating loadbalancer for url %s with addresses %s" %
                     (service.service_url(), all_addresses))
        self.load_balancer.change(service.service_url(),
                                  service.config.port(),
                                  names, all_addresses)
        self.load_balancer.save()

    @locked
    def reload_loadbalancer(self):
        self.load_balancer.clear()
        for service in self.services.values():
            addresses = []
            names = []
            for service_name in self.key_to_services.get(service.key(), []):
                names.append(service_name)
                addresses += self.active_ips(service_name)
            self.load_balancer.change(service.service_url(), 
                                      service.config.port(),
                                      names, addresses)
        self.load_balancer.save()

    @locked
    def reload_domain(self, domain):
        self.domain = domain

    @locked
    def mark_instance(self, service_name, instance_id):
        # Increment the mark counter.
        remove_instance = False
        mark_counter = int(self.zk_conn.read(paths.marked_instance(service_name, instance_id), '0'))
        mark_counter += 1

        if mark_counter >= self.config.mark_maximum():
            # This instance has been marked too many times. There is likely
            # something really wrong with it, so we'll clean it up.
            remove_instance = True
            self.zk_conn.delete(paths.marked_instance(service_name, instance_id))
        else:
            # Just save the mark counter
            logging.info("Instance %s for service %s has been marked (count=%s)" %
                         (instance_id, service_name, mark_counter))
            self.zk_conn.write(paths.marked_instance(service_name, instance_id), str(mark_counter))

        return remove_instance

    @locked
    def update_metrics(self):
        # Update all the service metrics from the loadbalancer.
        metrics = self.load_balancer.metrics()

        metrics_by_key = {}
        service_addresses = {}
        for ip in metrics:
            for service in self.services.values():
                if not service.name in service_addresses:
                    service_addresses[service.name] = self.active_ips(service.name)
                service_ips = service_addresses[service.name]
                if not(service.key() in metrics_by_key):
                    metrics_by_key[service.key()] = []
                if ip in service_ips:
                    metrics_by_key[service.key()].append(metrics[ip])

        # Stuff all the metrics into Zookeeper.
        self.zk_conn.write(paths.manager_metrics(self.uuid), \
                           json.dumps(metrics_by_key), \
                           ephemeral=True)

        # Load all metrics.
        all_metrics = {}

        # Read the keys for all other managers.
        for manager in self.managers:
            # Skip re-reading the local metrics.
            if manager == self.uuid:
                manager_metrics = metrics_by_key
            else:
                manager_metrics = self.zk_conn.read(paths.manager_metrics(manager))
                if manager_metrics:
                    manager_metrics = json.loads(manager_metrics)
                else:
                    continue

            # Merge into the all_metrics dictionary.
            for key in manager_metrics:
                if not(key in all_metrics):
                    all_metrics[key] = []
                all_metrics[key].extend(manager_metrics[key])

        # Return all available global metrics.
        return all_metrics

    @locked
    def load_metrics(self, service, service_metrics={}):
        # Read any default metrics. We can override the source service
        # for metrics here (so, for example, a backend database server
        # can inheret a set of metrics given for the front server).
        # This, like many other things, is specified here by the name
        # of the service we are inheriting metrics for. If not given,
        # we default to the current service.
        source = service.config.source()
        if source:
            source_service = self.services.get(source, None)
            if source_service:
                metrics = service_metrics.get(source_service.key(), [])
            else:
                metrics = []
        else:
            metrics = service_metrics.get(service.key(), [])

        default_metrics = self.zk_conn.read(paths.service_custom_metrics(service.name))
        if default_metrics:
            try:
                # This should be a dictionary { "name" : (weight, value) }
                metrics.append(json.loads(default_metrics))
            except ValueError:
                logging.warn("Invalid custom metrics for %s." % (service.name))
      
        # Read other metrics for given hosts.
        for host in self.active_ips(service.name):
            host_metrics = self.zk_conn.read(paths.service_host_metrics(service.name, host))
            if host_metrics:
                try:
                    # This should be a dictionary { "name" : (weight, value) }
                    metrics.append(json.loads(host_metrics))
                except ValueError:
                    logging.warn("Invalid host metrics for %s:%s." % (service.name, host))

        # Return the metrics.
        return metrics

    @locked
    def health_check(self):
        # Save and load the current metrics.
        service_metrics = self.update_metrics()

        # Does a health check on all the services that are being managed.
        for service in self.services.values():
            # Do not kick the service if it is not currently owned by us.
            if not(self.service_owned(service)):
                continue

            try:
                metrics = self.load_metrics(service, service_metrics)

                # Update the live metrics.
                self.zk_conn.write(paths.service_live_metrics(service.name), \
                                   json.dumps(metrics), \
                                   ephemeral=True)

                # Run a health check on this service.
                service.health_check()

                # Do the service update.
                service.update(reconfigure=False, metrics=metrics)
            except:
                error = traceback.format_exc()
                logging.error("Error updating service %s: %s" % (service.name, error))

    def run(self):
        # Note that we are running.
        self.running = True

        while self.running:
            try:
                # Reconnect to the Zookeeper servers.
                self.serve()

                # Kick the loadbalancer on startup.
                self.reload_loadbalancer()

                # Perform continuous health checks.
                while self.running:
                    self.health_check()
                    if self.running:
                        time.sleep(self.config.health_check())

            except ZookeeperException:
                # Sleep on ZooKeeper exception and retry.
                logging.debug("Received ZooKeeper exception, retrying.")
                if self.running:
                    time.sleep(self.config.health_check())

    def clean_stop(self):
        self.running = False
