from __future__ import annotations

import unittest

from run_demo_goal_dns import _DEMO_DNS_SERVERS, _inject_demo_dns


class DirectDemoDnsTests(unittest.TestCase):
    def test_injects_public_dns_into_docker_run(self) -> None:
        command = ["docker", "run", "-d", "example/image:latest"]
        result = _inject_demo_dns(command)

        self.assertEqual(result[:6], ["docker", "run", "--dns", "1.1.1.1", "--dns", "8.8.8.8"])
        self.assertEqual(result[6:], ["-d", "example/image:latest"])
        self.assertEqual(_DEMO_DNS_SERVERS, ("1.1.1.1", "8.8.8.8"))

    def test_does_not_change_non_run_commands(self) -> None:
        command = ["docker", "version"]
        self.assertEqual(_inject_demo_dns(command), command)

    def test_does_not_duplicate_existing_dns(self) -> None:
        command = ["docker", "run", "--dns", "9.9.9.9", "example/image:latest"]
        self.assertEqual(_inject_demo_dns(command), command)


if __name__ == "__main__":
    unittest.main()
