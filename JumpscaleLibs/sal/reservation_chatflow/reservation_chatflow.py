import netaddr
from Jumpscale import j
from Jumpscale.servers.gedis.GedisChatBot import StopChatFlow
import random
import requests
import time
import json


class Network:
    def __init__(self, network, expiration, bot, reservations):
        self._network = network
        self._expiration = expiration
        self.name = network.name
        self._used_ips = []
        self._is_dirty = False
        self._sal = j.sal.reservation_chatflow
        self._bot = bot
        self._fill_used_ips(reservations)

        currencies = reservations[0].data_reservation.currencies
        if currencies:
            self.currency = currencies[0]
        else:
            self.currency = "TFT"

    def _fill_used_ips(self, reservations):
        for reservation in reservations:
            if reservation.next_action != "DEPLOY":
                continue
            for kubernetes in reservation.data_reservation.kubernetes:
                if kubernetes.network_id == self._network.name:
                    self._used_ips.append(kubernetes.ipaddress)
            for container in reservation.data_reservation.containers:
                for nc in container.network_connection:
                    if nc.network_id == self._network.name:
                        self._used_ips.append(nc.ipaddress)

    def add_node(self, node):
        network_resources = self._network.network_resources
        used_ip_ranges = set()
        for network_resource in network_resources:
            if network_resource.node_id == node.node_id:
                return
            used_ip_ranges.add(network_resource.iprange)
            for peer in network_resource.peers:
                used_ip_ranges.add(peer.iprange)
        else:
            network_range = netaddr.IPNetwork(self._network.iprange)
            for idx, subnet in enumerate(network_range.subnet(24)):
                if str(subnet) not in used_ip_ranges:
                    break
            else:
                self._bot.stop("Failed to find free network")
            j.sal.zosv2.network.add_node(self._network, node.node_id, str(subnet))
            self._is_dirty = True

    def get_node_range(self, node):
        for network_resource in self._network.network_resources:
            if network_resource.node_id == node.node_id:
                return network_resource.iprange
        self._bot.stop(f"Node {node.node_id} is not part of network")

    def update(self, tid, currency=None):
        if self._is_dirty:
            reservation = j.sal.zosv2.reservation_create()
            reservation.data_reservation.networks.append(self._network._ddict)
            reservation_create = self._sal.reservation_register(reservation, self._expiration, tid, currency=currency)
            rid = reservation_create.reservation_id
            wallet = j.sal.reservation_chatflow.payments_show(self._bot, reservation_create)
            if wallet:
                j.sal.zosv2.billing.payout_farmers(wallet, reservation_create)

            j.sal.reservation_chatflow.payment_wait(self._bot, rid)
            return self._sal.reservation_wait(self._bot, rid)
        return True

    def ask_ip_from_node(self, node, message):
        ip_range = self.get_node_range(node)
        freeips = []
        hosts = netaddr.IPNetwork(ip_range).iter_hosts()
        next(hosts)  # skip ip used by node
        for host in hosts:
            ip = str(host)
            if ip not in self._used_ips:
                freeips.append(ip)
        ip_address = self._bot.drop_down_choice(message, freeips)
        self._used_ips.append(ip_address)
        return ip_address


class Chatflow(j.baseclasses.object):
    __jslocation__ = "j.sal.reservation_chatflow"

    def _init(self, **kwargs):
        j.data.bcdb.get("tfgrid_solutions")
        self._explorer = j.clients.explorer.default

    def validate_user(self, user_info):
        if not j.core.myenv.config.get("THREEBOT_CONNECT", False):
            error_msg = """
            This chatflow is not supported when Threebot is in dev mode.
            To enable Threebot connect : `j.me.encryptor.tools.threebotconnect_enable()`
            """
            raise j.exceptions.Runtime(error_msg)
        if not user_info["email"]:
            raise j.exceptions.Value("Email shouldn't be empty")
        if not user_info["username"]:
            raise j.exceptions.Value("Name of logged in user shouldn't be empty")
        return self._explorer.users.get(name=user_info["username"], email=user_info["email"])

    def nodes_get(
        self, number_of_nodes, farm_id=None, farm_name=None, cru=None, sru=None, mru=None, hru=None, currency="TFT"
    ):
        # get nodes without public ips
        nodes = j.sal.zosv2.nodes_finder.nodes_by_capacity(
            farm_id=farm_id, farm_name=farm_name, cru=cru, sru=sru, mru=mru, hru=hru, currency=currency
        )
        nodes = filter(j.sal.zosv2.nodes_finder.filter_is_up, nodes)

        # to avoid using the same node with different networks
        nodes = list(nodes)
        nodes_selected = []
        for i in range(number_of_nodes):
            try:
                node = random.choice(nodes)
                while node in nodes_selected:
                    node = random.choice(nodes)
            except IndexError:
                raise StopChatFlow("Failed to find resources for this reservation")
            nodes.remove(node)
            nodes_selected.append(node)
        return nodes_selected

    def validate_node(self, nodeid, query=None):
        try:
            node = self._explorer.nodes.get(nodeid)
        except requests.exceptions.HTTPError:
            raise j.exceptions.NotFound(f"Node {nodeid} doesn't exists please enter a valid nodeid")
        if not j.sal.zosv2.nodes_finder.filter_is_up(node):
            raise j.exceptions.NotFound(f"Node {nodeid} doesn't seem to be up please choose another nodeid")

        if query:
            for unit, value in query.items():
                freevalue = getattr(node.total_resources, unit) - getattr(node.used_resources, unit)
                if freevalue < value:
                    raise j.exceptions.Value(
                        f"Node {nodeid} does not have enough available resources for this request, please choose another one"
                    )
        return node

    def network_select(self, bot, customer_tid):
        reservations = j.sal.zosv2.reservation_list(tid=customer_tid, next_action="DEPLOY")
        networks = self.network_list(customer_tid, reservations)
        names = []
        for n in networks.keys():
            names.append(n)
        if not names:
            res = "<h2> You don't have any networks, please use the network chatflow to create one</h2>"
            res = j.tools.jinja2.template_render(text=res)
            bot.stop(res)
        while True:
            result = bot.single_choice("Choose a network", names)
            if result not in networks:
                continue
            network, expiration = networks[result]
            return Network(network, expiration, bot, reservations)

    def ip_range_get(self, bot):
        """
        bot: Gedis chatbot object from chatflow
        return ip_range from user or generated one
        """
        ip_range_choose = ["Configure IP range myself", "Choose IP range for me"]
        iprange_user_choice = bot.single_choice(
            "To have access to the threebot, the network must be configured", ip_range_choose
        )
        if iprange_user_choice == "Configure IP range myself":
            ip_range = bot.string_ask("Please add private IP Range of the network")
        else:
            first_digit = random.choice([172, 10])
            if first_digit == 10:
                second_digit = random.randint(0, 255)
            else:
                second_digit = random.randint(16, 31)
            ip_range = str(first_digit) + "." + str(second_digit) + ".0.0/16"
        return ip_range

    def network_create(
        self, network_name, reservation, ip_range, customer_tid, ip_version, expiration=None, currency=None
    ):
        """
        bot: Gedis chatbot object from chatflow
        reservation: reservation object from schema
        ip_range: ip range for network eg: "10.70.0.0/16"
        node: list of node objects from explorer

        return reservation (Object) , config of network (dict)
        """
        network = j.sal.zosv2.network.create(reservation, ip_range, network_name)
        node_subnets = netaddr.IPNetwork(ip_range).subnet(24)
        network_config = dict()
        access_nodes = j.sal.zosv2.nodes_finder.nodes_search()
        use_ipv4 = ip_version == "IPv4"

        if use_ipv4:
            nodefilter = j.sal.zosv2.nodes_finder.filter_public_ip4
        else:
            nodefilter = j.sal.zosv2.nodes_finder.filter_public_ip6
        for node in filter(nodefilter, access_nodes):
            access_node = node
            break
        else:
            raise j.exceptions.NotFound("Could not find available access node")

        j.sal.zosv2.network.add_node(network, access_node.node_id, str(next(node_subnets)))
        wg_quick = j.sal.zosv2.network.add_access(network, access_node.node_id, str(next(node_subnets)), ipv4=use_ipv4)

        network_config["wg"] = wg_quick
        j.sal.fs.writeFile(f"/sandbox/cfg/wireguard/{network_name}.conf", f"{wg_quick}")

        # register the reservation
        expiration = expiration or j.data.time.epoch + (60 * 60 * 24)
        reservation_create = self.reservation_register(reservation, expiration, customer_tid, currency=currency)

        network_config["rid"] = reservation_create.reservation_id
        network_config["reservation_create"] = reservation_create

        return network_config

    def reservation_register(self, reservation, expiration, customer_tid, expiration_provisioning=1000, currency=None):
        """
        Register reservation

        :param reservation: Reservation object to register
        :type  reservation: object
        :param expiration: epoch time when the reservation should be canceled automaticly
        :type  expiration: int
        :param customer_tid: Id of the customer making the reservation
        :type  customer_tid: int
        :param expiration_provisioning: timeout on the deployment of the provisioning in seconds
        :type  expiration_provisioning: int

        :return: reservation_create object
        :rtype: Obj
        """
        expiration_provisioning += j.data.time.epoch
        reservation_create = j.sal.zosv2.reservation_register(
            reservation,
            expiration,
            expiration_provisioning=expiration_provisioning,
            customer_tid=customer_tid,
            currencies=[currency],
        )
        rid = reservation_create.reservation_id
        reservation.id = rid

        if j.core.myenv.config.get("DEPLOYER") and customer_tid:
            # create a new object from deployed_reservation with the reservation and the tid
            deployed_rsv_model = j.clients.bcdbmodel.get(url="tfgrid.deployed_reservation.1", name="tfgrid_workloads")
            deployed_reservation = deployed_rsv_model.new()
            deployed_reservation.reservation_id = rid
            deployed_reservation.customer_tid = customer_tid
            deployed_reservation.save()
        return reservation_create

    def reservation_wait(self, bot, rid):
        def is_finished(reservation):
            count = 0
            count += len(reservation.data_reservation.volumes)
            count += len(reservation.data_reservation.zdbs)
            count += len(reservation.data_reservation.containers)
            count += len(reservation.data_reservation.kubernetes)
            for network in reservation.data_reservation.networks:
                count += len(network.network_resources)
            return len(reservation.results) >= count

        def is_expired(reservation):
            return reservation.data_reservation.expiration_provisioning < j.data.time.epoch

        reservation = self._explorer.reservations.get(rid)
        while True:
            self._reservation_failed(bot, reservation)
            if is_finished(reservation):
                return reservation.results
            if is_expired(reservation):
                res = f"# Sorry your reservation ```{reservation.id}``` failed to deploy in time:\n"
                for x in reservation.results:
                    if x.state == "ERROR":
                        res += f"\n### {x.category}: ```{x.message}```\n"
                link = f"{self._explorer.url}/reservations/{reservation.id}"
                res += f"<h2> <a href={link}>Full reservation info</a></h2>"
                j.sal.zosv2.reservation_cancel(rid)
                bot.stop(res)
            time.sleep(1)
            reservation = self._explorer.reservations.get(rid)

    def payment_wait(self, bot, rid):
        # wait to check payment is actually done next_action changed from:PAY
        def is_expired(reservation):
            return reservation.data_reservation.expiration_provisioning < j.data.time.epoch

        reservation = self._explorer.reservations.get(rid)
        while True:
            if reservation.next_action != "PAY":
                return
            if is_expired(reservation):
                res = f"# Sorry your reservation ```{reservation.id}``` failed to deploy in time:\n"
                for x in reservation.results:
                    if x.state == "ERROR":
                        res += f"\n### {x.category}: ```{x.message}```\n"
                link = f"{self._explorer.url}/reservations/{reservation.id}"
                res += f"<h2> <a href={link}>Full reservation info</a></h2>"
                j.sal.zosv2.reservation_cancel(rid)
                bot.stop(res)
            time.sleep(1)
            reservation = self._explorer.reservations.get(rid)

    def _reservation_failed(self, bot, reservation):
        failed = j.sal.zosv2.reservation_failed(reservation)
        if failed:
            res = f"# Sorry your reservation ```{reservation.id}``` has failed :\n"
            for x in reservation.results:
                if x.state == "ERROR":
                    res += f"\n### {x.category}: ```{x.message}```\n"
            link = f"{self._explorer.url}/reservations/{reservation.id}"
            res += f"<h2> <a href={link}>Full reservation info</a></h2>"
            j.sal.zosv2.reservation_cancel(reservation.id)
            bot.stop(res)

    def network_list(self, tid, reservations=None):
        if not reservations:
            reservations = j.sal.zosv2.reservation_list(tid=tid, next_action="DEPLOY")
        networks = dict()
        names = set()
        for reservation in sorted(reservations, key=lambda r: r.id, reverse=True):
            if reservation.next_action != "DEPLOY":
                continue
            rnetworks = reservation.data_reservation.networks
            expiration = reservation.data_reservation.expiration_reservation
            currency = reservation.data_reservation.currencies[0]
            for network in rnetworks:
                if network.name in names:
                    continue
                names.add(network.name)
                remaning = expiration - j.data.time.epoch
                network_name = network.name + f" ({currency}) - ends in: " + j.data.time.secondsToHRDelta(remaning)
                networks[network_name] = (network, expiration)

        return networks

    def wallets_list(self):
        """[summary]
        List all stellar client wallets from bcdb. Based on explorer instance only either wallets with network type TEST or STD are returned
        rtype: list
        """
        if "devnet" in self._explorer.url or "testnet" in self._explorer.url:
            network_type = "TEST"
        else:
            network_type = "STD"

        wallets_list = j.clients.stellar.find(network=network_type)
        wallets = dict()
        for wallet in wallets_list:
            wallets[wallet.name] = wallet
        return wallets

    def payments_show(self, bot, reservation_create_resp):
        """
        Show valid payment options in chatflow available. All available wallets possible are shown or usage of 3bot app is shown
        where a QR code is viewed for the user to scan and continue with their payment
        :rtype: wallet in case a wallet is used
        """
        if not (reservation_create_resp.escrow_information and reservation_create_resp.escrow_information.details):
            return
        escrow_info = j.sal.zosv2.reservation_escrow_information_with_qrcodes(reservation_create_resp)

        escrow_address = escrow_info["escrow_address"]
        escrow_asset = escrow_info["escrow_asset"]
        total_amount = escrow_info["total_amount"]

        wallets = self.wallets_list()
        wallet_names = []
        for w in wallets.keys():
            wallet_names.append(w)
        wallet_names.append("3bot app")

        message = f"""
Billing details:
<h4> Escrow address: </h4>  {escrow_address} \n
<h4> Escrow asset: </h4>  {escrow_asset} \n
<h4> Total amount: </h4>  {total_amount} \n
<h4> Choose a wallet name to use for payment or proceed with payment through 3bot app </h4>
"""
        while True:
            result = bot.single_choice(message, wallet_names)
            if result not in wallet_names:
                continue
            if result == "3bot app":
                self.escrow_qr_show(bot, escrow_info)
                return
            else:
                wallet = wallets[result]
                return wallet

    def escrow_qr_show(self, bot, escrow_info):
        """
        Show in chatflow the QR code with the details of the escrow information for payment
        """
        escrow_address = escrow_info["escrow_address"]
        escrow_asset = escrow_info["escrow_asset"]
        farmer_payments = escrow_info["farmer_payments"]
        total_amount = escrow_info["total_amount"]
        qrcode = escrow_info["qrcode"]

        message_text = f"""
<h4> Escrow address: </h4>  {escrow_address} \n
<h4> Escrow asset: </h4>  {escrow_asset} \n
<h4> Total amount: </h4>  {total_amount} \n

<h4>Payment details:</h4> \n
"""
        for payment in farmer_payments:
            message_text += f"""
Farmer id : {payment['farmer_id']} , Amount :{payment['total_amount']}
"""

        bot.qrcode_show(
            qrcode,
            title=f"Scan the following with your application or enter the information below manually and proceed with the payment",
            msg=message_text,
            scale=4,
            update=True,
        )

    def reservation_save(self, rid, name, url, form_info=None):
        form_info = form_info or []
        rsv_model = j.clients.bcdbmodel.get(url=url, name="tfgrid_solutions")
        reservation = rsv_model.new()
        reservation.rid = rid
        reservation.name = name
        reservation.form_info = form_info

        explorer = j.clients.explorer.explorer
        reservation.explorer = explorer.url
        reservation.save()

    def solution_name_add(self, bot, model):
        name_exists = False
        while not name_exists:
            solution_name = bot.string_ask("Please add a name for your solution")
            find = model.find(name=solution_name)
            if len(find) > 0:
                res = "# Please choose another name because this name already exist"
                res = j.tools.jinja2.template_render(text=res)
                bot.md_show(res)
            else:
                return solution_name

    def solutions_get(self, url):
        try:
            model = j.clients.bcdbmodel.get(url=url, name="tfgrid_solutions")
        except:
            return []
        solutions = model.find()
        reservations = []
        explorer = j.clients.explorer.explorer

        for solution in solutions:
            if solution.explorer and solution.explorer != explorer.url:
                continue
            reservation = explorer.reservations.get(solution.rid)
            solution_type = url.replace("tfgrid.solutions.", "").replace(".1", "")
            reservations.append(
                {
                    "name": solution.name,
                    "reservation": reservation._ddict_json_hr,
                    "type": solution_type,
                    "form_info": json.dumps(solution.form_info),
                }
            )
        return reservations

    def reservation_cancel_for_solution(self, url, solution_name):
        model = j.clients.bcdbmodel.get(url=url, name="tfgrid_solutions")
        solutions = model.find(name=solution_name)
        for solution in solutions:
            j.sal.zosv2.reservation_cancel(solution.rid)
            solution.delete()
