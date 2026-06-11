import unittest

from resource_updater.resources import cpu_to_millis


class ResourceParsingTests(unittest.TestCase):
    def test_cpu_to_millis_accepts_fractional_millicores(self) -> None:
        self.assertEqual(cpu_to_millis("0.0250m"), 0)

    def test_cpu_to_millis_returns_none_for_invalid_millicores(self) -> None:
        self.assertIsNone(cpu_to_millis("not-a-cpum"))


if __name__ == "__main__":
    unittest.main()
