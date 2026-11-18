import ipaddress
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MediaAccessTests(unittest.TestCase):
    def test_relays_allow_private_lan_and_tailscale_but_not_public_addresses(self):
        for relative in ("mediamtx.yml", "services/pi-cctv-relay.yml"):
            with self.subTest(config=relative):
                config = (ROOT / relative).read_text()
                networks = json.loads(re.search(r"^\s+ips:\s*(\[.*\])$", config, re.MULTILINE).group(1))
                ranges = [ipaddress.ip_network(value) for value in networks]
                def allowed(address):
                    ip = ipaddress.ip_address(address)
                    return any(ip.version == net.version and ip in net for net in ranges)
                for address in ("127.0.0.1", "192.168.10.8", "172.18.0.2", "100.118.121.50"):
                    self.assertTrue(allowed(address), address)
                for address in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
                    self.assertFalse(allowed(address), address)
                self.assertRegex(config, r"(?m)^\s+- action: read$")
                self.assertRegex(config, r"(?m)^moq: no$")

    def test_compose_keeps_same_relay_network_restrictions(self):
        config = (ROOT / "mediamtx.yml").read_text()
        networks = json.loads(re.search(r"^\s+ips:\s*(\[.*\])$", config, re.MULTILINE).group(1))
        compose = (ROOT / "docker-compose.yml").read_text()
        value = re.search(r'^\s+MTX_AUTHINTERNALUSERS_0_IPS: "([^"]+)"', compose, re.MULTILINE).group(1)
        self.assertEqual(value.split(','), networks)


if __name__ == "__main__":
    unittest.main()
