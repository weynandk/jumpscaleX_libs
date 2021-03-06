from Jumpscale import j

JSBASE = j.baseclasses.object


class BaseKVMComponent(j.baseclasses.object):
    def __init__(self, controller):
        self.controller = controller
        JSBASE.__init__(self)

    @property
    def is_created(self):
        return True

    @property
    def is_started(self):
        return True

    def create(self):
        return NotImplementedError()

    def start(self):
        return NotImplementedError()

    def delete(self):
        return NotImplementedError()

    def stop(self):
        return NotImplementedError()

    def to_xml(self):
        raise j.exceptions.NotImplemented()

    @classmethod
    def from_xml(cls, controller, xml):
        raise j.exceptions.NotImplemented()

    @classmethod
    def get_by_name(cls, controller, name):
        raise j.exceptions.NotImplemented()
