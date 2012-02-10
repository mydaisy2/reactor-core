#!/usr/bin/env python

"""
This defines the various paths used in zookeeper
"""

# The root path that all other paths hang off from.
ROOT = "/gridcentric/pancake"

# The path to the authorization hash used by the API to validate requests.
AUTH_HASH = "%s/auth" % (ROOT)
def auth_hash():
    return AUTH_HASH

# The main system configuration for all of the scale managers.
CONFIG = "%s/config" %(ROOT)
def config():
    return CONFIG

# The services subtree. Basically anything related to a particular
# service should be rooted here.
SERVICES = "%s/service" % (ROOT)
def services():
    return SERVICES

# The subtree for a particular service.
def service(name):
    return "%s/%s" %(SERVICES, name)

# A leaf node to determine if the service is already being managed.
def service_managed(name):
    return "%s/%s/managed" %(SERVICES, name)

# The ips that have been confirmed by the system for a particular service. An ip is
# confirmed once it sends a message to a pancake.
def confirmed_ips(name):
    return "%s/confirmed_ip" % (service(name))

# A particular ip that has been confirmed for the service.
def confirmed_ip(name, ip_address):
    return "%s/%s" %(confirmed_ips(name), ip_address)

# The instance ids that have been marked as having an issue relating to them. Usually this
# issue will be related to connectivity issue.
def marked_instances(name):
    return "%s/marked_ip" % (service(name))

# The particular instance id that has been marked for the service. This is a running counter
# and once it has reached some configurable value the system should attempt to clean it up because
# there is something wrong with it.
def marked_instance(name, instance_id):
    return "%s/%s" %(marked_instances(name), instance_id)

# New IPs currently not associated with any service are logged here
NEW_IPS = "%s/new-ips" % (ROOT)
def new_ips():
    return NEW_IPS

# A particular new ip
def new_ip(ip_address):
    return "%s/%s" %(NEW_IPS, ip_address)

# The agents subtree that holds the collected stats from the different agents.
AGENTS = "%s/agents" % (ROOT)
def agents():
    return AGENTS

def agent(agent_name):
    return "%s/%s" % (AGENTS, agent_name)

def agent_stats(agent_name, identifier):
    return "%s/%s" %(agent(agent_name), identifier)
