import time
import datetime
import re
import random
import uuid
import base64

import ldap
import ldap.modlist as modlist

from gridcentric.pancake.config import SubConfig

COMPUTER_ATTRS = [
    "operatingsystem",
    "countrycode",
    "cn",
    "lastlogoff",
    "dscorepropagationdata",
    "usncreated",
    "objectguid",
    "iscriticalsystemobject",
    "serviceprincipalname",
    "whenchanged",
    "localpolicyflags",
    "accountexpires",
    "primarygroupid",
    "badpwdcount",
    "objectclass",
    "instancetype",
    "objectcategory",
    "whencreated",
    "lastlogon",
    "useraccountcontrol",
    "samaccountname",
    "operatingsystemversion",
    "samaccounttype",
    "adspath",
    "serverreferencebl",
    "dnshostname",
    "pwdlastset",
    "ridsetreferences",
    "logoncount",
    "codepage",
    "name",
    "usnchanged",
    "badpasswordtime",
    "objectsid",
    "distinguishedname",
]

COMPUTER_RECORD = {
    'objectclass' : ['top', 'person', 'organizationalPerson', 'user', 'computer'],
}

class LdapConnection:
    def __init__(self, domain, username, password):
        self.domain   = domain
        self.username = username
        self.password = password
        self.con      = None

    def _wrap_and_retry(fn):
        def _wrapped_fn(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except ldap.LDAPError:
                self.con = None
                return fn(self, *args, **kwargs)
        _wrapped_fn.__name__ = fn.__name__
        _wrapped_fn.__doc__ = fn.__doc__
        return _wrapped_fn

    def _open(self):
        if not(self.con):
            ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
            self.con = ldap.initialize("ldaps://%s:636" % self.domain)
            self.con.set_option(ldap.OPT_REFERRALS, 0)
            self.con.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
            self.con.set_option(ldap.OPT_X_TLS, ldap.OPT_X_TLS_DEMAND)
            self.con.set_option(ldap.OPT_X_TLS_DEMAND, True)
            self.con.simple_bind_s("%s@%s" % (self.username, self.domain), self.password)
        return self.con

    def __del__(self):
        try:
            if self.con:
                self.con.unbind()
        except:
            pass

    @_wrap_and_retry
    def list_machines(self):
        dom      = ",".join(map(lambda x: 'dc=%s' % x, self.domain.split(".")))
        filter   = '(objectclass=computer)'
        machines = self._open().search_s(dom, ldap.SCOPE_SUBTREE, filter, COMPUTER_ATTRS)
        rval     = {}

        # Synthesize the machines into a simple form.
        for machine in machines:
            if machine[0]:
                for cn in machine[1]['cn']:
                    rval[cn.lower()] = machine[1]
        return rval

    def check_logged_on(self, machine):
        lastLogoff = int(machine.get('lastLogoff', ['0'])[0])
        lastLogon  = int(machine.get('lastLogon', ['0'])[0])
        if lastLogon > lastLogoff:
            return True
        else:
            return False

    def check_dead(self, machine):
        # Compute expiry (one month in the past).
        now = datetime.datetime.now()
        expiry = datetime.datetime(now.year, now.month-1, now.day,
                                   now.hour, now.minute, now.second)

        # Check the last update time.
        changed = machine.get('whenChanged', [''])[0]
        m = re.match("(\d\d\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d).\d*Z", changed)
        if m:
            dt = datetime.datetime(int(m.group(1)),
                                   int(m.group(2)),
                                   int(m.group(3)),
                                   int(m.group(4)),
                                   int(m.group(5)),
                                   int(m.group(6)))

            if dt > expiry:
                return False

        # Check the last logon time.
        lastLogon = int(machine.get('lastLogon', ['0'])[0])
        dt = datetime.datetime.fromtimestamp(lastLogon / 1000000)
        if dt > expiry:
            return False

        return True

    @_wrap_and_retry
    def clean_machines(self, template):
        template.replace("#", "\d")
        machines = self.list_machines()

        for (name, machine) in machines.items():
            if re.match(template, name):
                if not(self.check_logged_on(machine)) or \
                    self.check_dead(machine):
                    self.remove_machine(name)

    @_wrap_and_retry
    def create_machine(self, template):
        machines = self.list_machines()
        index = template.find("#")
        if index < 0:
            return False

        # Extract a maximum substring.
        size = 1
        while template[index:index+size] == ("#" * size):
            size += 1
        size -= 1

        # Compute an integer in the range we want.
        fmt = "%%0%sd" % size
        maximum = pow(10, size)
        while True:
            # Create the random name.
            n = random.randint(0, maximum-1)
            name = template.replace("#" * size, fmt % n)
            if not(name.lower() in machines):
                break

        # Generate a password.  
        # TODO: We need some way to deal with password strength requirements.
        # We'll probably need to pass in a password schema through the configs
        # since the strength requirements will vary between deployments. For
        # now we ensure the generated password will meet the default strength
        # requirements.
        password = str(uuid.uuid4())[:8] + '!'
        quoted_password = '"' + password + '"'
        utf_password = password.encode('utf-16-le')
        utf_quoted_password = quoted_password.encode('utf-16-le')
        base64_password = base64.b64encode(utf_password)

        # Generate the queries for creating the account.
        new_record = {}
        new_record.update(COMPUTER_RECORD.items())
        new_record['cn']             = name.upper()
        new_record['description']    = ''
        new_record['dNSHostName']    = '%s.%s' % (name, self.domain)
        new_record['sAMAccountName'] = '%s$' % name
        new_record['servicePrincipalName'] = [
            'HOST/%s' % name.upper(),
            'HOST/%s.%s' % (name, self.domain),
            'TERMSRV/%s' % name.upper(),
            'TERMSRV/%s.%s' % (name, self.domain),
            'RestrictedKrbHost/%s' % name.upper(),
            'RestrictedKrbHost/%s.%s' % (name, self.domain)
        ]

        password_change_attr = [(ldap.MOD_REPLACE, 'unicodePwd', utf_quoted_password)]
        account_enabled_attr = [(ldap.MOD_REPLACE, 'userAccountControl', '4096')]

        dom   = ",".join(map(lambda x: 'dc=%s' % x, self.domain.split(".")))
        descr = "cn=%s,cn=Computers,%s" % (name, dom)

        connection = self._open()

        # Create the new account.
        connection.add_s(descr, modlist.addModlist(new_record))
        # Set the account password.
        connection.modify_s(descr, password_change_attr)
        # Enable the computer account.
        connection.modify_s(descr, account_enabled_attr)

        return (name, base64_password)

class WindowsConfig(SubConfig):

    def domain(self):
        return self._get("domain", '')

    def username(self):
        return self._get("username", '')

    def password(self):
        return self._get("password", '')

    def template(self):
        return self._get("template", "gc#############")

class WindowsConnection:

    def __init__(self):
        self.connections = {}

    def _get_connection(self, config):
        if not(config.domain()) or \
           not(config.username()) or \
           not(config.password()):
            return False

        # Retrieve the cached connection.
        key = (config.domain(), config.username(), config.password())
        if not(self.connections.has_key(key)):
            self.connections[key] = \
                LdapConnection(config.domain(), 
                               config.username(),
                               config.password())
        return self.connections[key]

    def start_params(self, config_view):
        config = WindowsConfig(config_view)

        connection = self._get_connection(config)
        if not(connection):
            return {}

        # Clean existing machines and create a new one.
        connection.clean_machines(config.template())
        info = connection.create_machine(config.template())
        if info:
            # Return the relevant information about the machine.
            return { "name" : info[0], "machinepassword" : info[1] }
        else:
            return {}
