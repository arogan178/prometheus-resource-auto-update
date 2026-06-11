import html
import re
from pathlib import Path
from typing import List, Optional

from resource_updater.models import Recommendation, SkippedRecommendation

NAV_ALL_RUNS = "All report runs"
NAV_RUN_ENVIRONMENTS = "Environments in this run"
LINK_VIEW_DASHBOARD = "View dashboard"
LINK_MARKDOWN = "Markdown report"
LINK_VIEW_ENVIRONMENTS = "View environments"


def _direction(delta: int) -> str:
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


def generate_html_report(
    output_file: Path,
    env: str,
    title: str,
    applied_recs: List[Recommendation],
    prometheus_days: str,
    prometheus_percentile: str,
    skipped_recs: Optional[List[SkippedRecommendation]] = None,
    report_home_href: Optional[str] = None,
    run_index_href: Optional[str] = None,
) -> None:
    from resource_updater.resources import (
        cpu_to_millis,
        memory_to_mi,
        format_val,
        get_request_tightening_policy,
        get_limit_buffer_policy,
    )

    skipped_recs = skipped_recs or []
    cpu_factor, mem_factor = get_request_tightening_policy()
    cpu_limit_factor, mem_limit_factor = get_limit_buffer_policy()
    cpu_req_pct = int(cpu_factor * 100)
    mem_req_pct = int(mem_factor * 100)
    cpu_lim_pct = int(cpu_limit_factor * 100)
    mem_lim_pct = int(mem_limit_factor * 100)
    prom_pct_display = int(float(prometheus_percentile) * 100)

    records = []
    ns_stats = {}
    totals = {
        "cur_cpu": 0,
        "tgt_cpu": 0,
        "cur_mem": 0,
        "tgt_mem": 0,
        "cur_limit_cpu": 0,
        "tgt_limit_cpu": 0,
        "cur_limit_mem": 0,
        "tgt_limit_mem": 0,
    }

    for rec in applied_recs:
        c_cur = cpu_to_millis(rec.cpu_cur) or 0
        c_tgt = cpu_to_millis(rec.cpu_tgt) or 0
        m_cur = memory_to_mi(rec.mem_cur) or 0
        m_tgt = memory_to_mi(rec.mem_tgt) or 0
        lc_cur = cpu_to_millis(rec.cpu_limit_cur) or 0
        lc_tgt = cpu_to_millis(rec.cpu_limit_tgt) or 0
        lm_cur = memory_to_mi(rec.mem_limit_cur) or 0
        lm_tgt = memory_to_mi(rec.mem_limit_tgt) or 0

        impact = (
            (abs(c_tgt - c_cur) + abs(lc_tgt - lc_cur)) * 1.024
            + abs(m_tgt - m_cur)
            + abs(lm_tgt - lm_cur)
        )
        records.append(
            {
                "impact": impact,
                "rec": rec,
                "c_cur": c_cur,
                "c_tgt": c_tgt,
                "m_cur": m_cur,
                "m_tgt": m_tgt,
                "lc_cur": lc_cur,
                "lc_tgt": lc_tgt,
                "lm_cur": lm_cur,
                "lm_tgt": lm_tgt,
            }
        )

        st = ns_stats.setdefault(
            rec.namespace,
            {
                "count": 0,
                "cur_cpu": 0,
                "tgt_cpu": 0,
                "cur_mem": 0,
                "tgt_mem": 0,
                "cur_limit_cpu": 0,
                "tgt_limit_cpu": 0,
                "cur_limit_mem": 0,
                "tgt_limit_mem": 0,
            },
        )
        st["count"] += 1
        st["cur_cpu"] += c_cur
        st["tgt_cpu"] += c_tgt
        st["cur_mem"] += m_cur
        st["tgt_mem"] += m_tgt
        st["cur_limit_cpu"] += lc_cur
        st["tgt_limit_cpu"] += lc_tgt
        st["cur_limit_mem"] += lm_cur
        st["tgt_limit_mem"] += lm_tgt

        totals["cur_cpu"] += c_cur
        totals["tgt_cpu"] += c_tgt
        totals["cur_mem"] += m_cur
        totals["tgt_mem"] += m_tgt
        totals["cur_limit_cpu"] += lc_cur
        totals["tgt_limit_cpu"] += lc_tgt
        totals["cur_limit_mem"] += lm_cur
        totals["tgt_limit_mem"] += lm_tgt

    records.sort(key=lambda r: r["impact"], reverse=True)

    def cell(cur: int, tgt: int, unit: str) -> str:
        delta = tgt - cur
        direction = _direction(delta)
        arrow = {"up": "&#9650;", "down": "&#9660;", "flat": "&#8226;"}[direction]
        if delta == 0:
            sub = "no change"
        else:
            sign = "+" if delta > 0 else "-"
            sub = f"{sign}{format_val(abs(delta), unit)}"
        return (
            f'<div class="cell {direction}" data-sort="{delta}">'
            f'<span class="vals">{html.escape(format_val(cur, unit))} '
            f'&rarr; {html.escape(format_val(tgt, unit))}</span>'
            f'<span class="delta">{arrow} {html.escape(sub)}</span></div>'
        )

    def card(label: str, cur: int, tgt: int, unit: str) -> str:
        delta = tgt - cur
        direction = _direction(delta)
        if delta == 0:
            sub = "no change"
        else:
            sign = "+" if delta > 0 else "-"
            verb = "increase" if delta > 0 else "savings"
            sub = f"{sign}{format_val(abs(delta), unit)} {verb}"
        return (
            f'<div class="card {direction}">'
            f'<div class="card-label">{html.escape(label)}</div>'
            f'<div class="card-value">{html.escape(format_val(cur, unit))} '
            f'&rarr; {html.escape(format_val(tgt, unit))}</div>'
            f'<div class="card-delta">{html.escape(sub)}</div></div>'
        )

    def notes_html(rec: Recommendation) -> str:
        items = []
        items.append(
            f"Prometheus {html.escape(str(prometheus_days))}d P{prom_pct_display}: "
            f"CPU {html.escape(str(rec.prom_cpu_p95 or 'n/a'))}, "
            f"memory {html.escape(str(rec.prom_mem_p95 or 'n/a'))}"
        )
        items.append(
            f"Limit pctl: CPU {html.escape(str(rec.prom_cpu_p99 or 'n/a'))}, "
            f"memory {html.escape(str(rec.prom_mem_p99 or 'n/a'))}"
        )
        for label, val in (
            ("Baseline", rec.baseline_summary),
            ("CPU request", rec.cpu_request_reason),
            ("CPU limit", rec.cpu_limit_reason),
            ("Memory request", rec.mem_request_reason),
            ("Memory limit", rec.mem_limit_reason),
        ):
            if val:
                items.append(f"{label}: {html.escape(str(val))}")
        lis = "".join(f"<li>{item}</li>" for item in items)
        return f"<ul>{lis}</ul>"

    rows = []
    for r in records:
        rec = r["rec"]
        action = html.escape(str(rec.sizing_action or "change"))
        rows.append(
            "<tr>"
            f'<td data-sort="{html.escape(rec.namespace)}">{html.escape(rec.namespace)}</td>'
            f'<td data-sort="{html.escape(rec.deployment)}">'
            f'<code>{html.escape(rec.deployment)}</code></td>'
            f'<td data-sort="{action}"><span class="tag">{action}</span></td>'
            f'<td>{cell(r["c_cur"], r["c_tgt"], "m")}</td>'
            f'<td>{cell(r["m_cur"], r["m_tgt"], "Mi")}</td>'
            f'<td>{cell(r["lc_cur"], r["lc_tgt"], "m")}</td>'
            f'<td>{cell(r["lm_cur"], r["lm_tgt"], "Mi")}</td>'
            f'<td class="details"><details><summary>view</summary>{notes_html(rec)}</details></td>'
            "</tr>"
        )
    patchable_rows = "\n".join(rows)

    ns_sorted = sorted(
        ns_stats.items(),
        key=lambda x: (
            (
                abs(x[1]["tgt_cpu"] - x[1]["cur_cpu"])
                + abs(x[1]["tgt_limit_cpu"] - x[1]["cur_limit_cpu"])
            )
            * 1.024
            + abs(x[1]["tgt_mem"] - x[1]["cur_mem"])
            + abs(x[1]["tgt_limit_mem"] - x[1]["cur_limit_mem"])
        ),
        reverse=True,
    )
    ns_rows = "\n".join(
        "<tr>"
        f'<td data-sort="{html.escape(ns)}"><code>{html.escape(ns)}</code></td>'
        f'<td data-sort="{st["count"]}">{st["count"]}</td>'
        f'<td>{cell(st["cur_cpu"], st["tgt_cpu"], "m")}</td>'
        f'<td>{cell(st["cur_mem"], st["tgt_mem"], "Mi")}</td>'
        f'<td>{cell(st["cur_limit_cpu"], st["tgt_limit_cpu"], "m")}</td>'
        f'<td>{cell(st["cur_limit_mem"], st["tgt_limit_mem"], "Mi")}</td>'
        "</tr>"
        for ns, st in ns_sorted
    )

    skipped_section = ""
    if skipped_recs:
        cat_counts = {}
        for s in skipped_recs:
            cat_counts[s.category] = cat_counts.get(s.category, 0) + 1
        chips = "".join(
            f'<span class="chip">{html.escape(cat)} '
            f'<b>{count}</b></span>'
            for cat, count in sorted(cat_counts.items())
        )
        skip_rows = "\n".join(
            "<tr>"
            f'<td data-sort="{html.escape(s.namespace)}"><code>{html.escape(s.namespace)}</code></td>'
            f'<td data-sort="{html.escape(s.deployment)}"><code>{html.escape(s.deployment)}</code></td>'
            f'<td data-sort="{html.escape(s.repo)}">{html.escape(s.repo)}</td>'
            f'<td data-sort="{html.escape(s.category)}"><span class="tag warn">{html.escape(s.category)}</span></td>'
            f"<td>{html.escape(s.reason)}</td>"
            "</tr>"
            for s in sorted(
                skipped_recs, key=lambda x: (x.category, x.namespace, x.deployment)
            )
        )
        skipped_section = f"""
    <section>
      <h2>Skipped / Unpatchable Recommendations</h2>
      <div class="chips">{chips}</div>
      <div class="table-wrap">
        <table class="sortable">
          <thead><tr>
            <th>Namespace</th><th>Deployment</th><th>Repo</th>
            <th>Category</th><th>Reason</th>
          </tr></thead>
          <tbody>{skip_rows}</tbody>
        </table>
      </div>
    </section>"""

    cards_html = (
        card("CPU Requests", totals["cur_cpu"], totals["tgt_cpu"], "m")
        + card("Memory Requests", totals["cur_mem"], totals["tgt_mem"], "Mi")
        + card("CPU Limits", totals["cur_limit_cpu"], totals["tgt_limit_cpu"], "m")
        + card("Memory Limits", totals["cur_limit_mem"], totals["tgt_limit_mem"], "Mi")
    )

    subtitle = (
        f"{len(applied_recs)} patchable deployment(s) across "
        f"{len(ns_stats)} namespace(s)"
    )
    if skipped_recs:
        subtitle += f" &middot; {len(skipped_recs)} skipped/unpatchable"
    policy = (
        f"Request policy: CPU {cpu_req_pct}%, memory {mem_req_pct}% &middot; "
        f"Limit buffers: CPU {cpu_lim_pct}%, memory {mem_lim_pct}%"
    )
    nav = _nav_links(
        [
            (NAV_RUN_ENVIRONMENTS, run_index_href, False),
            (NAV_ALL_RUNS, report_home_href, True),
        ]
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{_CSS}
</style>
</head>
<body>
<header>
  {nav}
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">{subtitle}</p>
  <p class="policy">Totals below cover all namespaces in the {html.escape(env.upper())} environment. {policy}</p>
</header>

<section>
  <div class="cards">{cards_html}</div>
</section>

<section>
  <h2>Namespace Impact</h2>
  <div class="table-wrap">
    <table class="sortable">
      <thead><tr>
        <th>Namespace</th><th>Deployments</th>
        <th>CPU Req</th><th>Mem Req</th><th>CPU Limit</th><th>Mem Limit</th>
      </tr></thead>
      <tbody>{ns_rows}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>Patchable Changes</h2>
  <input type="text" id="filter" class="filter" placeholder="Filter deployments...">
  <div class="table-wrap">
    <table class="sortable" id="patchable">
      <thead><tr>
        <th>Namespace</th><th>Deployment</th><th>Action</th>
        <th>CPU Req</th><th>Mem Req</th><th>CPU Limit</th><th>Mem Limit</th>
        <th>Details</th>
      </tr></thead>
      <tbody>{patchable_rows}</tbody>
    </table>
  </div>
</section>
{skipped_section}

<footer>Generated by goldi-oss &middot; dry-run report</footer>
<script>
{_JS}
</script>
</body>
</html>"""

    output_file.write_text(doc, encoding="utf-8")


def _relative_href(path: Optional[Path], base_dir: Path) -> str:
    if not path:
        return ""
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _nav_links(links: list[tuple[str, Optional[str], bool]]) -> str:
    rendered = []
    for label, href, primary in links:
        if not href:
            continue
        class_name = "button primary" if primary else "button"
        rendered.append(
            f'<a class="{class_name}" href="{html.escape(href)}">{html.escape(label)}</a>'
        )
    if not rendered:
        return ""
    return f'<nav class="nav-actions" data-report-nav>{"".join(rendered)}</nav>'


def _dashboard_nav_html() -> str:
    return (
        '<nav data-report-nav style="display:flex;justify-content:flex-end;gap:8px;'
        'padding:16px 32px 0;font:13px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">'
        f'<a href="index.html" style="text-decoration:none;border:1px solid #2a2f3c;'
        f'color:#e6e9ef;background:#1d212c;border-radius:8px;padding:7px 11px;">'
        f'{html.escape(NAV_RUN_ENVIRONMENTS)}</a>'
        f'<a href="../index.html" style="text-decoration:none;border:1px solid #58a6ff;'
        f'color:#58a6ff;background:#1d212c;border-radius:8px;padding:7px 11px;">'
        f'{html.escape(NAV_ALL_RUNS)}</a>'
        "</nav>"
    )


def _ensure_dashboard_nav(report_dir: Path) -> None:
    nav = _dashboard_nav_html()
    nav_pattern = re.compile(r'<nav data-report-nav[^>]*>.*?</nav>\s*', re.DOTALL)
    for path in sorted(report_dir.glob("resource_changes_*.html")):
        content = path.read_text(encoding="utf-8")
        if nav_pattern.search(content):
            content = nav_pattern.sub(nav + "\n", content, count=1)
        elif "<body>" in content:
            content = content.replace("<body>", "<body>\n" + nav, 1)
        else:
            content = nav + content
        path.write_text(content, encoding="utf-8")


def generate_report_index(
    report_dir: Path,
    results,
    title: str,
    repo_type: str,
    target_envs: str,
) -> Path:
    index_file = report_dir / "index.html"
    cards = []
    total_patchable = 0
    total_skipped = 0
    total_failed = 0

    for result in sorted(results, key=lambda r: r.env):
        total_patchable += result.applied_count
        total_skipped += result.skipped_count
        total_failed += result.failed
        html_href = _relative_href(result.html_output_file, report_dir)
        md_href = _relative_href(result.output_file, report_dir)
        html_link = (
            f'<a class="button primary" href="{html.escape(html_href)}">{LINK_VIEW_DASHBOARD}</a>'
            if html_href
            else '<span class="button disabled">No dashboard</span>'
        )
        md_link = (
            f'<a class="button" href="{html.escape(md_href)}">{LINK_MARKDOWN}</a>'
            if md_href
            else '<span class="button disabled">No markdown</span>'
        )
        cards.append(
            f"""
      <article class="report-card">
        <div class="report-env">{html.escape(result.env.upper())}</div>
        <div class="metrics">
          <span><b>{result.applied_count}</b> patchable</span>
          <span><b>{result.skipped_count}</b> skipped/unpatchable</span>
          <span><b>{result.failed}</b> failed</span>
        </div>
        <div class="actions">{html_link}{md_link}</div>
      </article>"""
        )

    nav = _nav_links([(NAV_ALL_RUNS, "../index.html", True)])

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{_CSS}
</style>
</head>
<body>
<header>
  {nav}
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">{html.escape(target_envs)}{f" &middot; repo type: {html.escape(repo_type)}" if repo_type else ""}</p>
  <p class="policy">Pick an environment to review changes visually, or open the Markdown report when you need to paste results into an LLM.</p>
</header>

<section>
  <div class="cards">
    <div class="card flat">
      <div class="card-label">Patchable</div>
      <div class="card-value">{total_patchable}</div>
      <div class="card-delta">deployment(s)</div>
    </div>
    <div class="card flat">
      <div class="card-label">Skipped / Unpatchable</div>
      <div class="card-value">{total_skipped}</div>
      <div class="card-delta">recommendation(s)</div>
    </div>
    <div class="card flat">
      <div class="card-label">Failed</div>
      <div class="card-value">{total_failed}</div>
      <div class="card-delta">repo processing failure(s)</div>
    </div>
  </div>
</section>

<section>
  <h2>Environments in this run</h2>
  <div class="report-grid">{"".join(cards)}</div>
</section>

<footer>Generated by goldi-oss &middot; dry-run run overview</footer>
</body>
</html>"""
    index_file.write_text(doc, encoding="utf-8")
    _ensure_dashboard_nav(report_dir)
    return index_file


def generate_report_file_index(report_dir: Path) -> Path:
    index_file = report_dir / "index.html"
    reports = {}

    for path in sorted(report_dir.glob("resource_changes_*.*")):
        if path.suffix not in {".html", ".md"}:
            continue
        parts = path.stem.split("_")
        env = parts[2] if len(parts) >= 3 else path.stem
        reports.setdefault(env, {})[path.suffix] = path

    cards = []
    for env, files in sorted(reports.items()):
        html_href = _relative_href(files.get(".html"), report_dir)
        md_href = _relative_href(files.get(".md"), report_dir)
        html_link = (
            f'<a class="button primary" href="{html.escape(html_href)}">{LINK_VIEW_DASHBOARD}</a>'
            if html_href
            else '<span class="button disabled">No dashboard</span>'
        )
        md_link = (
            f'<a class="button" href="{html.escape(md_href)}">{LINK_MARKDOWN}</a>'
            if md_href
            else '<span class="button disabled">No markdown</span>'
        )
        cards.append(
            f"""
      <article class="report-card">
        <div class="report-env">{html.escape(env.upper())}</div>
        <div class="metrics">
          <span>Counts unavailable for this older run.</span>
        </div>
        <div class="actions">{html_link}{md_link}</div>
      </article>"""
        )

    nav = _nav_links([(NAV_ALL_RUNS, "../index.html", True)])

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resource Update Reports - {html.escape(report_dir.name)}</title>
<style>
{_CSS}
</style>
</head>
<body>
<header>
  {nav}
  <h1>Dry-run report run</h1>
  <p class="subtitle">{html.escape(report_dir.name)}</p>
  <p class="policy">Rebuilt from report files in this folder. Pick an environment to review changes visually, or open the Markdown report for LLM copy/paste.</p>
</header>

<section>
  <h2>Environments in this run</h2>
  <div class="report-grid">{"".join(cards)}</div>
</section>

<footer>Generated by goldi-oss &middot; dry-run run overview</footer>
</body>
</html>"""
    index_file.write_text(doc, encoding="utf-8")
    _ensure_dashboard_nav(report_dir)
    return index_file


def generate_report_history_index(reports_root: Path) -> Path:
    index_file = reports_root / "index.html"
    run_dirs = []
    for path in reports_root.iterdir():
        if not path.is_dir():
            continue
        if (path / "index.html").exists():
            run_dirs.append(path)
            continue
        if list(path.glob("resource_changes_*.html")) or list(
            path.glob("resource_changes_*.md")
        ):
            generate_report_file_index(path)
            run_dirs.append(path)
    run_dirs.sort(key=lambda path: path.name, reverse=True)

    total_dashboards = 0
    total_markdown_reports = 0
    rows = []
    for run_dir in run_dirs:
        _ensure_dashboard_nav(run_dir)
        dashboards = len(list(run_dir.glob("resource_changes_*.html")))
        markdown_reports = len(list(run_dir.glob("resource_changes_*.md")))
        total_dashboards += dashboards
        total_markdown_reports += markdown_reports
        rows.append(
            "<tr>"
            f'<td><code>{html.escape(run_dir.name)}</code></td>'
            f'<td data-sort="{dashboards}">{dashboards}</td>'
            f'<td data-sort="{markdown_reports}">{markdown_reports}</td>'
            f'<td><a class="button primary small" href="{html.escape(run_dir.name)}/index.html">{LINK_VIEW_ENVIRONMENTS}</a></td>'
            "</tr>"
        )

    empty_state = ""
    if not rows:
        empty_state = '<p class="policy">No report runs found yet.</p>'

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resource Update Reports</title>
<style>
{_CSS}
</style>
</head>
<body>
<header>
  <h1>Resource Update Reports</h1>
  <p class="subtitle">Bookmark this page to reopen any dry-run</p>
  <p class="policy">Each row is one dry-run folder. Open it to pick UAT, STG, PRD, or DEV, then review the HTML dashboard or Markdown report.</p>
</header>

<section>
  <div class="cards">
    <div class="card flat">
      <div class="card-label">Dry-run folders</div>
      <div class="card-value">{len(run_dirs)}</div>
      <div class="card-delta">saved run(s)</div>
    </div>
    <div class="card flat">
      <div class="card-label">HTML dashboards</div>
      <div class="card-value">{total_dashboards}</div>
      <div class="card-delta">visual report(s)</div>
    </div>
    <div class="card flat">
      <div class="card-label">Markdown reports</div>
      <div class="card-value">{total_markdown_reports}</div>
      <div class="card-delta">LLM copy/paste file(s)</div>
    </div>
  </div>
</section>

<section>
  <h2>How to share</h2>
  <div class="info-box">
    <p>Share the whole <code>resource_update_reports</code> folder. Recipients should open this file: <code>resource_update_reports/index.html</code>.</p>
    <ul>
      <li>All links are relative, so reports work offline after copy or unzip.</li>
      <li>Zip the folder for Slack, email, or attach it to a ticket.</li>
      <li>Commit the folder to git if the team should keep a shared history.</li>
      <li>For local preview in a browser, run <code>python3 -m http.server</code> from the repo root.</li>
    </ul>
  </div>
</section>

<section>
  <h2>Dry-run folders</h2>
  {empty_state}
  <div class="table-wrap">
    <table class="sortable">
      <thead><tr><th>Folder</th><th>Dashboards</th><th>Markdown</th><th>Open</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
</section>

<footer>Generated by goldi-oss &middot; report home</footer>
<script>
{_JS}
</script>
</body>
</html>"""
    index_file.write_text(doc, encoding="utf-8")
    return index_file


_CSS = """
:root {
  --bg: #0f1117; --panel: #171a23; --panel2: #1d212c; --border: #2a2f3c;
  --text: #e6e9ef; --muted: #9aa3b2; --up: #f0883e; --down: #3fb950; --flat: #8b949e;
  --accent: #58a6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 60px; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
header { padding: 28px 32px 16px; border-bottom: 1px solid var(--border); }
h1 { margin: 0 0 6px; font-size: 22px; }
.nav-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.subtitle { margin: 0; color: var(--text); font-weight: 600; }
.policy { margin: 6px 0 0; color: var(--muted); font-size: 12.5px; }
section { padding: 20px 32px 4px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 8px 0 12px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; border-left: 4px solid var(--flat); }
.card.up { border-left-color: var(--up); }
.card.down { border-left-color: var(--down); }
.card-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.card-value { font-size: 18px; font-weight: 700; margin: 4px 0; }
.card-delta { font-size: 12.5px; color: var(--muted); }
.card.up .card-delta { color: var(--up); }
.card.down .card-delta { color: var(--down); }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; background: var(--panel); }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; white-space: nowrap; }
th { background: var(--panel2); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; cursor: pointer; user-select: none; position: sticky; top: 0; }
th:hover { color: var(--text); }
tbody tr:hover { background: var(--panel2); }
td code { background: var(--panel2); padding: 2px 6px; border-radius: 5px; font-size: 12.5px; }
.cell { display: flex; flex-direction: column; }
.cell .vals { font-variant-numeric: tabular-nums; }
.cell .delta { font-size: 11.5px; color: var(--flat); }
.cell.up .delta { color: var(--up); }
.cell.down .delta { color: var(--down); }
.tag { background: var(--panel2); border: 1px solid var(--border); border-radius: 20px; padding: 2px 10px; font-size: 11.5px; }
.tag.warn { border-color: var(--up); color: var(--up); }
.chips { margin-bottom: 12px; display: flex; flex-wrap: wrap; gap: 8px; }
.chip { background: var(--panel); border: 1px solid var(--border); border-radius: 20px; padding: 4px 12px; font-size: 12.5px; color: var(--muted); }
.chip b { color: var(--text); }
.filter { width: 320px; max-width: 100%; margin-bottom: 12px; padding: 8px 12px; background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 8px; }
.details summary { cursor: pointer; color: var(--accent); }
.details ul { margin: 8px 0 0; padding-left: 18px; white-space: normal; max-width: 520px; }
.details li { color: var(--muted); margin-bottom: 3px; }
footer { padding: 24px 32px 0; color: var(--muted); font-size: 12px; }
.report-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.report-card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.report-env { font-size: 20px; font-weight: 800; margin-bottom: 10px; }
.metrics { display: flex; flex-direction: column; gap: 4px; color: var(--muted); margin-bottom: 14px; }
.metrics b { color: var(--text); }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
.button { display: inline-flex; align-items: center; justify-content: center; text-decoration: none; color: var(--text); background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; font-size: 12.5px; }
.button.primary { border-color: var(--accent); color: var(--accent); }
.button.small { padding: 4px 8px; }
.button.disabled { color: var(--muted); opacity: .65; }
.info-box { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; color: var(--muted); }
.info-box p { margin: 0 0 10px; color: var(--text); }
.info-box ul { margin: 0; padding-left: 18px; }
.info-box li { margin-bottom: 4px; }
.info-box code { background: var(--panel2); padding: 2px 6px; border-radius: 5px; font-size: 12.5px; color: var(--text); }
"""

_JS = """
document.querySelectorAll('table.sortable th').forEach((th, idx) => {
  th.addEventListener('click', () => {
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const asc = !(th.dataset.asc === 'true');
    table.querySelectorAll('th').forEach(h => h.removeAttribute('data-asc'));
    th.dataset.asc = asc;
    const getKey = (row) => {
      const c = row.children[idx];
      if (!c) return '';
      const inner = c.querySelector('[data-sort]');
      const raw = (inner ? inner.getAttribute('data-sort') : c.getAttribute('data-sort')) ?? c.textContent;
      const num = parseFloat(raw);
      return isNaN(num) ? raw.toString().toLowerCase() : num;
    };
    rows.sort((a, b) => {
      const ka = getKey(a), kb = getKey(b);
      if (ka < kb) return asc ? -1 : 1;
      if (ka > kb) return asc ? 1 : -1;
      return 0;
    });
    rows.forEach(r => tbody.appendChild(r));
  });
});
const filter = document.getElementById('filter');
if (filter) {
  filter.addEventListener('input', () => {
    const q = filter.value.toLowerCase();
    document.querySelectorAll('#patchable tbody tr').forEach(row => {
      row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
}
"""
