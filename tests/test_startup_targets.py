import importlib.util
import sys
import unittest
from pathlib import Path


def load_auto_update_module():
    module_path = Path(__file__).resolve().parents[1] / "auto-update.py"
    spec = importlib.util.spec_from_file_location("auto_update", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load auto-update.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


auto_update = load_auto_update_module()


class StartupTargetTests(unittest.TestCase):
    def test_cpe_namespaces_select_uat_stg_prd(self) -> None:
        namespaces = [
            "payments-uat01",
            "core-uat01",
            "payments-stg01",
            "core-stg01",
            "payments-prd01",
            "core-prd01",
        ]

        self.assertEqual(
            auto_update.infer_dry_run_environments(namespaces),
            ["uat", "stg", "prd"],
        )

    def test_dev_namespaces_select_only_dev(self) -> None:
        namespaces = [
            "payments-dev01",
            "core-dev01",
        ]

        self.assertEqual(auto_update.infer_dry_run_environments(namespaces), ["dev"])

    def test_namespaces_for_environment_filters_by_suffix(self) -> None:
        namespaces = [
            "payments-uat01",
            "core-uat01",
            "payments-stg01",
        ]

        self.assertEqual(
            auto_update.namespaces_for_environment(namespaces, "uat"),
            ["core-uat01", "payments-uat01"],
        )

    def test_missing_cpe_environments_are_skipped(self) -> None:
        namespaces = [
            "payments-uat01",
            "core-prd01",
        ]

        self.assertEqual(
            auto_update.infer_dry_run_environments(namespaces),
            ["uat", "prd"],
        )

    def test_build_environment_targets_uses_matching_namespaces(self) -> None:
        namespaces = [
            "payments-uat01",
            "core-uat01",
            "payments-stg01",
        ]

        targets = auto_update.build_environment_targets(
            namespaces, ["uat", "stg", "prd"]
        )

        self.assertEqual([target.env for target in targets], ["uat", "stg"])
        self.assertEqual(
            targets[0].target_namespaces,
            ["core-uat01", "payments-uat01"],
        )


if __name__ == "__main__":
    unittest.main()
