import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from resource_updater.kustomize import find_resource_patch_file
from resource_updater.models import Recommendation, SkippedRecommendation
from resource_updater.processor import generate_markdown_report
from resource_updater.html_report import (
    NAV_ALL_RUNS,
    NAV_RUN_ENVIRONMENTS,
    generate_html_report,
    generate_report_file_index,
    generate_report_history_index,
    generate_report_index,
)


class DryRunReportingTests(unittest.TestCase):
    def test_find_resource_patch_file_accepts_nested_patches_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_dir = Path(temp_dir)
            (env_dir / "kustomization.yaml").write_text(
                "\n".join(
                    [
                        "patches:",
                        "- target:",
                        "    kind: Deployment",
                        "    name: workload-a",
                        "  path: resource-limits.yaml",
                    ]
                )
            )
            patch = env_dir / "resource-limits.yaml"
            patch.write_text(
                "\n".join(
                    [
                        "apiVersion: apps/v1",
                        "kind: Deployment",
                        "metadata:",
                        "  name: workload-a",
                        "spec:",
                        "  template:",
                        "    spec:",
                        "      containers:",
                        "      - name: app",
                        "        resources:",
                        "          requests:",
                        "            cpu: 100m",
                        "            memory: 128Mi",
                        "          limits:",
                        "            cpu: 200m",
                        "            memory: 256Mi",
                    ]
                )
            )

            self.assertEqual(find_resource_patch_file(env_dir, "workload-a"), patch)

    def test_find_resource_patch_file_falls_back_to_common_resource_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_dir = Path(temp_dir)
            (env_dir / "kustomization.yaml").write_text("resources:\n- deployment.yaml\n")
            patch = env_dir / "resource-limits.yaml"
            patch.write_text(
                "\n".join(
                    [
                        "apiVersion: apps/v1",
                        "kind: Deployment",
                        "metadata:",
                        "  name: workload-b",
                        "spec:",
                        "  template:",
                        "    spec:",
                        "      containers:",
                        "      - name: app",
                        "        resources:",
                        "          requests:",
                        "            cpu: 100m",
                        "            memory: 128Mi",
                        "          limits:",
                        "            cpu: 200m",
                        "            memory: 256Mi",
                    ]
                )
            )

            self.assertEqual(find_resource_patch_file(env_dir, "workload-b"), patch)

    def test_markdown_report_includes_skipped_recommendations(self) -> None:
        rec = Recommendation(
            namespace="app-stg01",
            deployment="workload-a",
            cpu_cur="100m",
            cpu_tgt="150m",
            mem_cur="128Mi",
            mem_tgt="256Mi",
            cpu_limit_cur="200m",
            mem_limit_cur="256Mi",
            cpu_limit_tgt="300m",
            mem_limit_tgt="512Mi",
            prom_cpu_p95="0.12",
            prom_mem_p95="160Mi",
        )
        skipped = SkippedRecommendation(
            namespace="app-stg01",
            deployment="workload-b",
            repo="sample-app",
            category="Missing kustomize patch",
            reason="Could not find a resource patch file",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "report.md"
            generate_markdown_report(
                output_file,
                "stg",
                "Dry Run Report",
                [rec],
                "30",
                "0.95",
                [skipped],
            )

            report = output_file.read_text()

        self.assertIn("1 patchable deployment(s)", report)
        self.assertIn("1 skipped/unpatchable recommendation(s)", report)
        self.assertIn("## Patchable Changes", report)
        self.assertIn("| Namespace | Deployment | Action | CPU Req |", report)
        self.assertIn("| `app-stg01` | `workload-a` |", report)
        self.assertNotIn("### `app-stg01 / workload-a`", report)
        self.assertIn("## Skipped / Unpatchable Recommendations", report)
        self.assertIn("workload-b", report)
        self.assertIn("Missing kustomize patch", report)

    def test_html_report_generates_self_contained_dashboard(self) -> None:
        rec = Recommendation(
            namespace="app-stg01",
            deployment="workload-a",
            cpu_cur="100m",
            cpu_tgt="150m",
            mem_cur="128Mi",
            mem_tgt="256Mi",
            cpu_limit_cur="200m",
            mem_limit_cur="256Mi",
            cpu_limit_tgt="300m",
            mem_limit_tgt="512Mi",
            prom_cpu_p95="0.12",
            prom_mem_p95="160Mi",
            sizing_action="mixed",
        )
        skipped = SkippedRecommendation(
            namespace="app-stg01",
            deployment="workload-b",
            repo="sample-app",
            category="Missing kustomize patch",
            reason="Could not find a resource patch file",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "report.html"
            generate_html_report(
                output_file,
                "stg",
                "Dry Run Report",
                [rec],
                "30",
                "0.95",
                [skipped],
                report_home_href="../index.html",
                run_index_href="index.html",
            )
            report = output_file.read_text()

        self.assertIn("<!DOCTYPE html>", report)
        self.assertIn('href="../index.html"', report)
        self.assertIn('href="index.html"', report)
        self.assertIn(NAV_ALL_RUNS, report)
        self.assertIn(NAV_RUN_ENVIRONMENTS, report)
        self.assertIn("Totals below cover all namespaces in the STG", report)
        self.assertIn("Patchable Changes", report)
        self.assertIn("workload-a", report)
        self.assertIn("Skipped / Unpatchable Recommendations", report)
        self.assertIn("workload-b", report)
        self.assertIn("table.sortable", report)

    def test_report_index_links_environment_dashboards_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir) / "2026-06-11_150541"
            report_dir.mkdir()
            stg_md = report_dir / "resource_changes_stg.md"
            stg_html = report_dir / "resource_changes_stg.html"
            prd_md = report_dir / "resource_changes_prd.md"
            prd_html = report_dir / "resource_changes_prd.html"
            for path in (stg_md, stg_html, prd_md, prd_html):
                path.write_text("report")

            index_file = generate_report_index(
                report_dir,
                [
                    SimpleNamespace(
                        env="stg",
                        applied_count=45,
                        skipped_count=40,
                        failed=0,
                        output_file=stg_md,
                        html_output_file=stg_html,
                    ),
                    SimpleNamespace(
                        env="prd",
                        applied_count=69,
                        skipped_count=50,
                        failed=1,
                        output_file=prd_md,
                        html_output_file=prd_html,
                    ),
                ],
                "Dry-run report run",
                "",
                "STG, PRD",
            )

            report = index_file.read_text()

        self.assertIn("Dry-run report run", report)
        self.assertIn("STG, PRD", report)
        self.assertIn('href="resource_changes_stg.html"', report)
        self.assertIn('href="resource_changes_stg.md"', report)
        self.assertIn('href="../index.html"', report)
        self.assertIn(NAV_ALL_RUNS, report)
        self.assertIn("Environments in this run", report)
        self.assertIn("114", report)
        self.assertIn("90", report)

    def test_report_history_index_links_timestamped_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_root = Path(temp_dir) / "resource_update_reports"
            newer = reports_root / "2026-06-11_150541"
            older = reports_root / "2026-06-11_142437"
            newer.mkdir(parents=True)
            older.mkdir(parents=True)
            (newer / "index.html").write_text("newer")
            (older / "index.html").write_text("older")
            (newer / "resource_changes_stg.html").write_text("dashboard")
            (newer / "resource_changes_stg.md").write_text("markdown")

            index_file = generate_report_history_index(reports_root)
            report = index_file.read_text()

        self.assertIn("Resource Update Reports", report)
        self.assertIn("Bookmark this page", report)
        self.assertIn("How to share", report)
        self.assertIn('<div class="card-value">2</div>', report)
        self.assertIn('href="2026-06-11_150541/index.html"', report)
        self.assertIn('href="2026-06-11_142437/index.html"', report)
        self.assertLess(
            report.index("2026-06-11_150541"),
            report.index("2026-06-11_142437"),
        )

    def test_report_history_index_backfills_loose_report_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_root = Path(temp_dir) / "resource_update_reports"
            run_dir = reports_root / "2026-06-11_153042"
            run_dir.mkdir(parents=True)
            (run_dir / "resource_changes_uat.html").write_text("dashboard")
            (run_dir / "resource_changes_uat.md").write_text("markdown")

            history_index = generate_report_history_index(reports_root)
            run_index = run_dir / "index.html"
            self.assertTrue(run_index.exists())
            run_report = run_index.read_text()
            history_report = history_index.read_text()

        self.assertIn("UAT", run_report)
        self.assertIn('href="resource_changes_uat.html"', run_report)
        self.assertIn('href="resource_changes_uat.md"', run_report)
        self.assertIn('href="../index.html"', run_report)
        self.assertIn("Counts unavailable for this older run.", run_report)
        self.assertIn('href="2026-06-11_153042/index.html"', history_report)

    def test_history_index_retrofits_home_nav_into_existing_dashboards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_root = Path(temp_dir) / "resource_update_reports"
            run_dir = reports_root / "2026-06-11_153042"
            run_dir.mkdir(parents=True)
            (run_dir / "index.html").write_text("<html><body>existing</body></html>")
            dashboard = run_dir / "resource_changes_uat.html"
            dashboard.write_text(
                "<!DOCTYPE html><html><body><header><h1>UAT</h1></header></body></html>"
            )

            generate_report_history_index(reports_root)
            dashboard_html = dashboard.read_text()

        self.assertIn("data-report-nav", dashboard_html)
        self.assertIn('href="../index.html"', dashboard_html)
        self.assertIn('href="index.html"', dashboard_html)
        self.assertIn(NAV_ALL_RUNS, dashboard_html)
        self.assertIn(NAV_RUN_ENVIRONMENTS, dashboard_html)

    def test_file_index_groups_loose_report_files_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir) / "2026-06-11_153042"
            report_dir.mkdir()
            (report_dir / "resource_changes_uat.html").write_text("dashboard")
            (report_dir / "resource_changes_stg.md").write_text("markdown")

            index_file = generate_report_file_index(report_dir)
            report = index_file.read_text()

        self.assertIn("UAT", report)
        self.assertIn("STG", report)
        self.assertIn('href="resource_changes_uat.html"', report)
        self.assertIn('href="resource_changes_stg.md"', report)


if __name__ == "__main__":
    unittest.main()
