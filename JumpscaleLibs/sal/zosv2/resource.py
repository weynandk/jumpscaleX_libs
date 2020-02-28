from Jumpscale import j
from decimal import Decimal, getcontext


class ResourceParser:
    def __init__(self, explorer, reservation):
        self._actor_directory = explorer.actors_get("tfgrid.directory")
        self._actor_workloads = explorer.actors_get("tfgrid.workloads")
        self._reservation = reservation

    def calculate_used_resources(self):
        resource_units_per_node = []

        workloads_per_nodes = self._workloads_per_node()
        for node_id, workloads in workloads_per_nodes.items():
            resource_units = ResourceUnitsNode(node_id=node_id)
            for _type, workload in workloads.items():
                if _type == "container":
                    for container in workload:
                        ru = self._process_container(container)
                        ru.set_workload_id(container.workload_id)
                        resource_units + ru
                elif _type == "zdb":
                    for zdb in workload:
                        ru = self._process_zdb(zdb)
                        ru.set_workload_id(zdb.workload_id)
                        resource_units + ru
                elif _type == "kubernetes":
                    for k8s in workload:
                        ru = self._process_kubernetes(k8s)
                        ru.set_workload_id(k8s.workload_id)
                        resource_units + ru
                elif _type == "volume":
                    for volume in workload:
                        ru = self._process_volume(volume)
                        ru.set_workload_id(volume.workload_id)
                        resource_units + ru

            resource_units_per_node.append(resource_units)

        return resource_units_per_node

    def calculate_used_resources_cost(self):
        resource_units_per_node = self.calculate_used_resources()
        for resource_unit_node in resource_units_per_node:
            node = self._actor_directory.nodes.get(resource_unit_node.node_id, False)
            farm = self._actor_directory.farms.get(node.farm_id, False)

            wallet_address = farm.wallet_addresses[0]

            total_cru_cost = resource_unit_node.CRU * farm.resource_prices[0].cru
            total_sru_cost = resource_unit_node.SRU * farm.resource_prices[0].sru
            total_hru_cost = resource_unit_node.HRU * farm.resource_prices[0].hru
            total_mru_cost = resource_unit_node.MRU * farm.resource_prices[0].mru
            total_cost = total_cru_cost + total_sru_cost + total_hru_cost + total_mru_cost

            print(f"To pay to farmer: with id {node.farm_id}\n")
            print(f"CRU: resources used: {resource_unit_node.CRU}, Total amount to pay for CRU: {total_cru_cost}")
            print(f"SRU: resources used: {resource_unit_node.SRU}, Total amount to pay for SRU: {total_sru_cost}")
            print(f"HRU: resources used: {resource_unit_node.HRU}, Total amount to pay for HRU: {total_hru_cost}")
            print(f"MRU: resources used: {resource_unit_node.MRU}, Total amount to pay for MRU: {total_mru_cost} \n")
            print(f"Total to pay: {total_cost} in TFT to wallet address: {wallet_address}\n")

            resource_unit_node.set_cru_cost(total_cru_cost)
            resource_unit_node.set_sru_cost(total_sru_cost)
            resource_unit_node.set_hru_cost(total_hru_cost)
            resource_unit_node.set_mru_cost(total_mru_cost)
            resource_unit_node.set_total_cost(total_cost)

            resource_unit_node.set_farm_wallet(wallet_address)
            resource_unit_node.set_farm_id(node.farm_id)

        return resource_units_per_node

    def payout_farmers(self, resource_units_per_node, reservation_id):
        tfchain_client = j.clients.tfchain.get("tfchain")
        tfchain_wallet = tfchain_client.wallets.get("main")
        transactions = []
        for resource_unit_node in resource_units_per_node:
            resource_unit_node.workload_id.sort()
            workload_ids = [str(wid) for wid in resource_unit_node.workload_id]
            msg = "-".join(workload_ids)
            msg = "{}-{}".format(reservation_id, msg)
            (txn, submitted) = tfchain_wallet.coins_send(
                recipient=resource_unit_node.farm_wallet, amount=str(resource_unit_node.TOTAL_COST), data=msg,
            )
            if submitted:
                transactions.append(txn)
                continue
        return transactions

    def validate_reservation_payment(self, reservation_id):
        getcontext().prec = 9
        me_tid = j.tools.threebot.me.default.tid
        # load all farms belonging to our threebot id
        farms = self._actor_directory.farms.owned_by(me_tid).farms
        farm_ids = set()
        for farm in farms:
            farm_ids.add(farm.id)
        # load reservation and parse resource units
        resource_units_per_node = self.calculate_used_resources_cost()
        # tuple of the message and amount expected in a tx
        expected_txes = set()
        for resource_unit_node in resource_units_per_node:
            if resource_unit_node.farm_id in farm_ids:
                resource_unit_node.workload_id.sort()
                workload_ids = [str(wid) for wid in resource_unit_node.workload_id]
                msg = "-".join(workload_ids)
                msg = "{}-{}".format(reservation_id, msg)
                amount = Decimal(resource_unit_node.TOTAL_COST)
                expected_txes.add((msg, amount))
        # load the transactions in our wallet, for every workload in one of the owned farms, whe need a trandaction
        # with the right amount of tft
        tfchain_client = j.clients.tfchain.get("tfchain")
        tfchain_wallet = tfchain_client.wallets.get("main")
        for tx in tfchain_wallet.transactions:
            value = Decimal(0)
            for co in tx.coin_outputs:
                if co.condition.unlockhash in tfchain_wallet.addresses:
                    value += co.value.value
            for (msg, amount) in expected_txes:
                if msg == tx.data.value.decode():
                    # verify amount
                    if amount == str(value):
                        # good tx, remove from expected set
                        expected_txes.remove((msg, amount))
        # verification is now done, if teh expected tx set is empty, it means
        # all required txes have been created and processed.
        return len(expected_txes) == 0

    def _iterate_over_workloads(self):
        for _type in ["zdbs", "volumes", "containers", "networks"]:
            for workload in getattr(self._reservation.data_reservation, _type):
                yield _type[:-1], workload
        if hasattr(self._reservation.data_reservation, "kubernetes"):
            for workload in getattr(self._reservation.data_reservation, "kubernetes"):
                yield "kubernetes", workload

    def _workloads_dict(self):
        return {"network": [], "zdb": [], "volume": [], "container": [], "kubernetes": []}

    def _workloads_per_node(self):
        """
        Separate individual workloads in a reservation based on the farm they are going to be deployed on
        """
        mapping = {}

        for _typ, workload in self._iterate_over_workloads():
            if _typ == "network":
                for nr in workload.network_resources:
                    if not nr.node_id in mapping:
                        mapping[nr.node_id] = self._workloads_dict()
                    mapping[nr.node_id]["network"].append(nr)
            else:
                if not workload.node_id in mapping:
                    mapping[workload.node_id] = self._workloads_dict()
                mapping[workload.node_id][_typ].append(workload)

        return mapping

    def _process_volume(self, volume):
        if volume.type == "SSD":
            return ResourceUnitsNode(SRU=volume.size)
        elif volume.type == "HDD":
            return ResourceUnitsNode(HRU=volume.size)

    def _process_container(self, container):
        return ResourceUnitsNode(CRU=container.capacity.cpu, MRU=(container.capacity.memory / 1024))

    def _process_zdb(self, zdb):
        if zdb.disk_type == "SSD":
            return ResourceUnitsNode(SRU=zdb.size)
        elif zdb.disk_type == "HDD":
            return ResourceUnitsNode(HRU=zdb.size)

    def _process_kubernetes(self, k8s):
        if k8s.size == 1:
            return ResourceUnitsNode(CRU=1, MRU=2, SRU=50)
        elif k8s.size == 2:
            return ResourceUnitsNode(CRU=2, MRU=4, SRU=100)


class ResourceUnitsNode:
    def __init__(self, node_id=None, SRU=0, HRU=0, MRU=0, CRU=0):
        self.node_id = node_id
        self.farm_id = None
        self.farm_wallet = None
        self.workload_id = []
        self.SRU = SRU
        self.HRU = HRU
        self.MRU = MRU
        self.CRU = CRU
        self.SRU_COST = 0
        self.HRU_COST = 0
        self.MRU_COST = 0
        self.CRU_COST = 0
        self.TOTAL_COST = 0

    def __add__(self, other):
        self.CRU += other.CRU
        self.HRU += other.HRU
        self.MRU += other.MRU
        self.SRU += other.SRU
        if other.workload_id is not None and other.workload_id not in self.workload_id:
            self.workload_id.extend(other.workload_id)

    def set_farm_wallet(self, farm_wallet):
        self.farm_wallet = farm_wallet

    def set_farm_id(self, farm_id):
        self.farm_id = farm_id

    def set_workload_id(self, workload_id):
        self.workload_id = [workload_id]

    def set_cru_cost(self, CRU_COST):
        self.CRU_COST = CRU_COST

    def set_sru_cost(self, SRU_COST):
        self.SRU_COST = SRU_COST

    def set_hru_cost(self, HRU_COST):
        self.HRU_COST = HRU_COST

    def set_mru_cost(self, MRU_COST):
        self.MRU_COST = MRU_COST

    def set_total_cost(self, TOTAL_COST):
        self.TOTAL_COST = TOTAL_COST

    def __str__(self):
        representation = f"Total of resourceunits for node: {self.node_id} \n SRU: {self.SRU} \n CRU: {self.CRU} \n MRU: {self.MRU} \n HRU: {self.HRU} \n"

        if self.SRU_COST > 0:
            representation += f"Total SRU cost: {self.SRU_COST} \n"
        if self.CRU_COST > 0:
            representation += f"Total CRU cost: {self.CRU_COST} \n"
        if self.HRU_COST > 0:
            representation += f"Total HRU cost: {self.HRU_COST} \n"
        if self.MRU_COST > 0:
            representation += f"Total MRU cost: {self.MRU_COST} \n"

        return representation

    def __repr__(self):
        return str(self)
