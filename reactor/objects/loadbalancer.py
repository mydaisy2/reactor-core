from reactor.zookeeper.objects import DatalessObject

class Loadbalancers(DatalessObject):

    def tree(self, name):
        return self._get_child(name, clazz=DatalessObject)
