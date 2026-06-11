import unittest
from unittest.mock import patch

from resource_updater import k8s


class PrometheusAttributionTests(unittest.TestCase):
    def test_prometheus_data_is_keyed_by_workload_and_container(self) -> None:
        pod_workloads = {
            "workload-a-abc": "workload-a",
            "workload-b-def": "workload-b",
        }

        def fake_query(_prom_url: str, _token: str, query: str):
            if "container_cpu_usage_seconds_total" in query:
                if f"quantile_over_time({k8s.PROMETHEUS_LIMIT_PERCENTILE}" in query:
                    values = {
                        "workload-a-abc": "0.2",
                        "workload-b-def": "3.0",
                    }
                else:
                    values = {
                        "workload-a-abc": "0.1",
                        "workload-b-def": "2.0",
                    }
            else:
                if f"quantile_over_time({k8s.PROMETHEUS_LIMIT_PERCENTILE}" in query:
                    values = {
                        "workload-a-abc": "256",
                        "workload-b-def": "4096",
                    }
                else:
                    values = {
                        "workload-a-abc": "128",
                        "workload-b-def": "2048",
                    }

            return [
                {
                    "metric": {"pod": pod, "container": "app"},
                    "value": [0, value],
                }
                for pod, value in values.items()
            ]

        with (
            patch("resource_updater.k8s.PROMETHEUS_PERCENTILE", "0.95"),
            patch("resource_updater.k8s.PROMETHEUS_LIMIT_PERCENTILE", "0.99"),
            patch("resource_updater.k8s.query_prometheus", side_effect=fake_query),
        ):
            data = k8s.get_prometheus_resource_data(
                "app-stg01",
                "https://prometheus.example.test",
                "token",
                pod_workloads=pod_workloads,
            )

        self.assertEqual(data[("workload-a", "app")]["cpu_req"], 0.1)
        self.assertEqual(data[("workload-a", "app")]["mem_req"], 128)
        self.assertEqual(data[("workload-b", "app")]["cpu_req"], 2.0)
        self.assertEqual(data[("workload-b", "app")]["mem_req"], 2048)

    def test_pod_workload_map_resolves_replicaset_and_statefulset_owners(self) -> None:
        oc_data = {
            "items": [
                {
                    "kind": "ReplicaSet",
                    "metadata": {
                        "name": "workload-a-abc",
                        "ownerReferences": [
                            {"kind": "Deployment", "name": "workload-a"}
                        ],
                    },
                },
                {
                    "kind": "Pod",
                    "metadata": {
                        "name": "workload-a-abc-123",
                        "ownerReferences": [
                            {"kind": "ReplicaSet", "name": "workload-a-abc"}
                        ],
                    },
                },
                {
                    "kind": "Pod",
                    "metadata": {
                        "name": "workload-b-0",
                        "ownerReferences": [
                            {"kind": "StatefulSet", "name": "workload-b"}
                        ],
                    },
                },
            ]
        }

        with patch("resource_updater.k8s._load_json_output", return_value=oc_data):
            pod_workloads = k8s._pod_workload_map("app-stg01")

        self.assertEqual(pod_workloads["workload-a-abc-123"], "workload-a")
        self.assertEqual(pod_workloads["workload-b-0"], "workload-b")


if __name__ == "__main__":
    unittest.main()
