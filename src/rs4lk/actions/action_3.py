from __future__ import annotations

import ipaddress
import itertools
import logging
import os
import random
import shlex
import time
from io import BytesIO

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab
from Kathara.model.Machine import Machine

from . import action_utils
from .. import utils
from ..foundation.actions.action import Action
from ..foundation.configuration.vendor_configuration import VendorConfiguration
from ..globals import RESOURCES_FOLDER
from ..model.topology import Topology, INTERNET_AS_NUM


class Action3(Action):
    def verify(self, config: VendorConfiguration, topology: Topology, net_scenario: Lab) -> (bool, str):
        candidate = topology.get(config.get_local_as())
        candidate_client_name = f"as{candidate.identifier}_client"
        _, candidate_client_iface_idx = candidate.get_node_by_name(candidate_client_name)
        candidate_device = net_scenario.get_machine(candidate.name)
        candidate_client = net_scenario.get_machine(candidate_client_name)
        candidate_assigned_ips = set(
            itertools.chain.from_iterable(map(lambda x: x['All_Prefixes'], config.interfaces.values()))
        )

        logging.info(f"Copying spoofing check script into candidate client `{candidate_client_name}`...")
        with open(os.path.join(RESOURCES_FOLDER, "host_spoof_check.py"), "rb") as py_script:
            content = BytesIO(py_script.read())
        Kathara.get_instance().update_lab_from_api(net_scenario)
        Kathara.get_instance().copy_files(candidate_client, {'/host_spoof_check.py': content})

        all_announced_networks = {4: set(), 6: set()}
        # Get all providers
        providers_routers = list(filter(lambda x: x[1].is_provider(), topology.all()))
        if len(providers_routers) == 0:
            logging.warning("No providers found, skipping check...")
            return True

        for _, provider in providers_routers:
            logging.info(f"Reading networks from provider AS{provider.identifier}...")
            device_networks = action_utils.get_bgp_networks(net_scenario.get_machine(provider.name))
            all_announced_networks[4].update(device_networks[4])
            all_announced_networks[6].update(device_networks[6])

        # Remove default
        all_announced_networks[4] = set(filter(lambda x: x.prefixlen != 0, all_announced_networks[4]))
        all_announced_networks[6] = set(filter(lambda x: x.prefixlen != 0, all_announced_networks[6]))

        logging.info("Aggregating networks...")
        utils.aggregate_v4_6_networks(all_announced_networks)
        logging.debug(f"Resulting networks are: {all_announced_networks}")

        passed_checks = []
        for v, networks in all_announced_networks.items():
            logging.info(f"Performing check on IPv{v}...")

            if not networks:
                logging.warning(f"No networks announced in IPv{v}, skipping...")
                continue

            default_net = ipaddress.IPv4Network("0.0.0.0/0") if v == 4 else ipaddress.IPv6Network("::/0")

            spoofing_net = action_utils.get_non_overlapping_network(v, networks)
            logging.info(f"Chosen network to spoof is {spoofing_net}.")
            spoofing_hosts = spoofing_net.hosts()

            logging.info(f"Setting IPv{v} addresses on AS{INTERNET_AS_NUM} (Internet)...")
            internet_router = topology.get(INTERNET_AS_NUM)
            internet_router_client_name = f"as{INTERNET_AS_NUM}_client"
            _, internet_router_client_iface_idx = internet_router.get_node_by_name(internet_router_client_name)
            internet_router_device = net_scenario.get_machine(internet_router.name)
            internet_router_ip = ipaddress.ip_interface(f"{next(spoofing_hosts)}/{spoofing_net.prefixlen}")
            self._ip_addr_add(internet_router_device, internet_router_client_iface_idx, internet_router_ip)

            spoofed_src_ip = next(spoofing_hosts)
            internet_router_client = net_scenario.get_machine(internet_router_client_name)
            internet_router_client_ip = ipaddress.ip_interface(f"{spoofed_src_ip}/{spoofing_net.prefixlen}")
            self._ip_addr_add(internet_router_client, 0, internet_router_client_ip)
            self._ip_route_add(internet_router_client, default_net, internet_router_ip.ip, 0)

            for _, provider in providers_routers:
                # Peek one provider network
                if len(provider.local_networks[v]) == 0:
                    logging.warning(f"AS{provider.identifier} does not announce networks in IPv{v}, skipping...")
                    continue

                while True:
                    provider_net = random.choice(list(provider.local_networks[v]))
                    logging.info(f"Selected network {provider_net} on AS{provider.identifier}.")
                    if (2 ** provider_net.prefixlen) - 2 > 5:
                        break
                provider_net_hosts = provider_net.hosts()

                provider_client_name = f"as{provider.identifier}_client"
                _, provider_client_iface_idx = provider.get_node_by_name(provider_client_name)
                provider_device = net_scenario.get_machine(provider.name)
                provider_ip = ipaddress.ip_interface(f"{next(provider_net_hosts)}/{provider_net.prefixlen}")
                self._ip_addr_add(provider_device, provider_client_iface_idx, provider_ip)

                provider_client_addr = next(provider_net_hosts)

                provider_client = net_scenario.get_machine(provider_client_name)
                provider_client_ip = ipaddress.ip_interface(f"{provider_client_addr}/{provider_net.prefixlen}")
                self._ip_addr_add(provider_client, 0, provider_client_ip)
                self._ip_route_add(provider_client, default_net, provider_ip.ip, 0)

                _, candidate_iface_idx = provider.get_node_by_name(candidate.name)
                # We can surely pop since there is only one public IP towards the candidate router
                (cand_peering_ip, _) = provider.neighbours[candidate_iface_idx].get_ips(is_public=True)[v].pop()

                # Get the announced candidate networks towards this provider
                candidate_nets = action_utils.get_neighbour_bgp_networks(provider_device, cand_peering_ip.ip)
                candidate_nets = utils.aggregate_networks(candidate_nets)

                if not candidate_nets:
                    logging.warning(
                        f"No networks advertised by candidate to AS{provider.identifier} on IPv{v}, skipping..."
                    )
                    continue

                # Select one network
                candidate_net = random.choice(list(candidate_nets))
                logging.info(f"Selected network {candidate_net} on candidate AS.")
                candidate_client_ip = self._get_non_overlapping_address(candidate_net, candidate_assigned_ips)
                candidate_ip = self._get_non_overlapping_address(candidate_net,
                                                                 candidate_assigned_ips.union({candidate_client_ip}))

                # Set the interface IP on the candidate client
                self._ip_addr_add(candidate_client, 0, candidate_client_ip)
                self._ip_route_add(candidate_client, default_net, candidate_ip.ip, 0)

                # Set the interface IP on the candidate
                self._vendor_ip_add(candidate_device, config,
                                    candidate_client_iface_idx, candidate_ip)

                logging.info("Waiting 20s before performing check...")
                time.sleep(20)
                result = self._perform_spoofing_check(candidate_client,
                                                      candidate_client_ip.ip, spoofed_src_ip, provider_client_addr)
                passed_checks.append(result)
                if result:
                    logging.success(f"Check passed on IPv{v} with provider AS{provider.identifier}!")
                else:
                    logging.warning(f"Check not passed on IPv{v} with provider AS{provider.identifier}!")

                self._ip_addr_del(provider_device, provider_client_iface_idx, provider_ip)
                self._ip_addr_del(provider_client, 0, provider_client_ip)
                self._ip_route_del(provider_client, default_net, provider_ip.ip, 0)

                self._vendor_ip_del(candidate_device, config,
                                    candidate_client_iface_idx, candidate_ip)
                self._ip_addr_del(candidate_client, 0, candidate_client_ip)
                self._ip_route_del(candidate_client, default_net, candidate_ip.ip, 0)

            self._ip_addr_del(internet_router_device, internet_router_client_iface_idx, internet_router_ip)
            self._ip_addr_del(internet_router_client, 0, internet_router_client_ip)
            self._ip_route_del(internet_router_client, default_net, internet_router_ip.ip, 0)

        return all(passed_checks)

    @staticmethod
    def _get_non_overlapping_address(network: ipaddress.IPv4Network | ipaddress.IPv6Network,
                                     assigned_ips: set[ipaddress.IPv4Interface | ipaddress.IPv6Interface]
                                     ) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface:
        net_hosts = network.hosts()

        while True:
            selected_ip_iface = ipaddress.ip_interface(f"{next(net_hosts)}/{network.prefixlen}")

            if selected_ip_iface not in assigned_ips:
                break

        return selected_ip_iface

    @staticmethod
    def _ip_addr_add(device: Machine, iface_idx: int, ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface) -> None:
        logging.info(f"Setting IP Address={ip} in device `{device.name}` on interface eth{iface_idx}.")

        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"ip addr add {ip} dev eth{iface_idx}"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _ip_addr_del(device: Machine, iface_idx: int, ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface) -> None:
        logging.info(f"Deleting IP Address={ip} in device `{device.name}` on interface eth{iface_idx}.")

        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"ip addr del {ip} dev eth{iface_idx}"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _vendor_ip_add(device: Machine, config: VendorConfiguration,
                       iface_idx: int, ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface) -> None:
        logging.info(f"Setting IP Address={ip} in device `{device.name}` on interface with idx={iface_idx}.")

        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(config.command_set_iface_ip(iface_idx, ip)),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _vendor_ip_del(device: Machine, config: VendorConfiguration,
                       iface_idx: int, ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface) -> None:
        logging.info(f"Removing IP Address={ip} in device `{device.name}` on interface with idx={iface_idx}.")

        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(config.command_unset_iface_ip(iface_idx, ip)),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _ip_route_add(device: Machine,
                      net: ipaddress.IPv4Network | ipaddress.IPv6Network,
                      via_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                      via_iface_idx: int) -> None:
        logging.info(f"Setting IP Route={net} in device `{device.name}` on interface "
                     f"eth{via_iface_idx} via IP={via_ip}.")

        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"ip route add {net} via {via_ip} dev eth{via_iface_idx}"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _ip_route_del(device: Machine,
                      net: ipaddress.IPv4Network | ipaddress.IPv6Network,
                      via_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                      via_iface_idx: int) -> None:
        logging.info(f"Deleting IP Route={net} in device `{device.name}` on interface "
                     f"eth{via_iface_idx} via IP={via_ip}.")

        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"ip route del {net} via {via_ip} dev eth{via_iface_idx}"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _perform_spoofing_check(device: Machine,
                                candidate_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                                spoof_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                                dst_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        logging.info(f"Performing spoof check with IPs=({candidate_ip}, {spoof_ip}, {dst_ip})...")

        v = candidate_ip.version
        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"/usr/bin/python3 /host_spoof_check.py {candidate_ip} {spoof_ip} {dst_ip} {v}"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        result = None
        while result is None:
            time.sleep(2)
            try:
                (result, _) = next(exec_output)
            except StopIteration:
                pass

        return result.decode('utf-8').strip() == "1"

    def name(self) -> str:
        return "spoofing"

    def display_name(self) -> str:
        return "Anti-Spoofing Check"
