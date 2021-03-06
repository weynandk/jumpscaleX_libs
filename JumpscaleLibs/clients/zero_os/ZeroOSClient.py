from Jumpscale import j
from JumpscaleLibsExtra.sal_zos.node.Node import Node

from .protocol.Client import Client as ProtocolClient


class ZeroOSClient(j.baseclasses.object_config, Node):
    _SCHEMATEXT = """
    @url = jumpscale.zos.client.1
    name** = "" (S)
    host = "127.0.0.1" (S)
    port = 6379 (ipport)
    unixsocket = "" (S)
    password = ""  (S)
    db = 0 (I)
    ssl = true (B)
    timeout = 120 (I)
    """

    client = None

    def _init(self, **kwargs):
        self.client = ProtocolClient(
            host=self.host,
            port=self.port,
            unixsocket=self.unixsocket,
            password=self.password,
            db=self.db,
            ssl=self.ssl,
            timeout=self.timeout,
        )
        Node.__init__(self, client=self.client)

    def _update_trigger(self, key, value):
        try:
            setattr(self.client, key, value)
        except AttributeError:
            # not a client attribute
            pass

        # force re-creation of the redis client
        # when the config is changed
        if key in ["host", "port", "password", "unixsocket"]:
            self.client._redis = None
