# Corey Mathie, 2026
"""Console money reads en-US with a leading "$" in any locale, and figures are marked translate="no".

Page translation then leaves "$1,100" alone. The browser check under fr-FR is in scripts/demo_smoke.py; these are
the static and formatter checks."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent.parent / "demo"
JS = {p.name: p.read_text() for p in DEMO.glob("*.js")}


def test_every_locale_call_names_en_us():
    calls = [(name, m) for name, src in JS.items() for m in re.findall(r"toLocale\w*\([^)]*\)", src)]
    assert calls
    assert all('"en-US"' in m for _, m in calls), [c for c in calls if '"en-US"' not in c[1]]
    assert not any("Intl.NumberFormat" in src for src in JS.values())


def test_figures_carry_translate_no():
    ui, screens = JS["ui.js"], JS["screens.js"]
    svgs = re.findall(r"<svg [^>]*class=\"(?:chart|spark)", ui + screens)
    assert svgs and all('translate="no"' in s for s in svgs)
    kpi = re.search(r"function kpi\(.*?\n\}", screens, re.S).group(0)
    for cls in ("v", "kpi-foot", "s"):
        assert f'class="{cls}" translate="no"' in kpi, cls
    for name, src in JS.items():  # every numeric cell and KPI value, in every screen
        for cls in re.findall(r'class="((?:[^"$]* )?(?:num|v)(?: [^"$]*)?)"(?! translate)', src):
            pytest.fail(f'{name}: class="{cls}" without translate="no"')
    assert "export const figs" in ui and "figs(esc(n.detail))" in screens


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    return run.stdout


def test_formatters_print_dollars(tmp_path):
    (tmp_path / "ui.mjs").write_text(JS["ui.js"])
    out = _node(
        f"import {{ usd, figs }} from {json.dumps((tmp_path / 'ui.mjs').as_uri())};"
        "console.log(JSON.stringify([usd(1100, 2), usd(940, 0), usd(5.4, 2), usd(0.0123), usd(12345.678),"
        " figs('cost $1,099 and $1.5M')]))"
    )
    assert json.loads(out) == [
        "$1,100.00",
        "$940",
        "$5.40",
        "$0.0123",
        "$12,345.68",
        'cost <span translate="no">$1,099</span> and <span translate="no">$1.5M</span>',
    ]
