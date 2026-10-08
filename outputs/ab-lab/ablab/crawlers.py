"""Publisher-issued crawler ranges. A normal phone address is not in this set."""
import ipaddress
import json
from pathlib import Path


def _load():
    data = json.loads(Path(__file__).with_name('crawler_ranges.json').read_text(encoding='utf-8'))
    exact4 = set()
    ranges4, ranges6 = [], []
    for prefix in data['prefixes']:
        network = ipaddress.ip_network(prefix)
        if network.version == 4 and network.prefixlen == 32:
            exact4.add(network.network_address)
        elif network.version == 4:
            ranges4.append(network)
        else:
            ranges6.append(network)
    return frozenset(exact4), tuple(ranges4), tuple(ranges6)


_EXACT4, _RANGES4, _RANGES6 = _load()


def is_crawler_ip(address):
    """True when the address is inside a publisher's own crawler or fetcher range."""
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address = ipaddress.ip_address(address)
    if address.version == 4:
        return address in _EXACT4 or any(address in network for network in _RANGES4)
    return any(address in network for network in _RANGES6)
