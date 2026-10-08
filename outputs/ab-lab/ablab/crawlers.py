"""Official crawler IP ranges. User-Agent remains a claim; the address is the check."""
import ipaddress
import json
from pathlib import Path


def _load():
    data = json.loads(Path(__file__).with_name('crawler_ranges.json').read_text(encoding='utf-8'))
    version4, version6 = [], []
    for prefix in data['prefixes']:
        network = ipaddress.ip_network(prefix)
        (version4 if network.version == 4 else version6).append(network)
    return tuple(version4), tuple(version6)


_V4, _V6 = _load()


def is_crawler_ip(address):
    """True when the address is inside a publisher's own crawler range."""
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address = ipaddress.ip_address(address)
    return any(address in network for network in (_V4 if address.version == 4 else _V6))
