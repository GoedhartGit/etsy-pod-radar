"""End-to-end simulation: runs the radar for N days against the fake Etsy API and checks the outputs.

Usage: python tests/run_simulation.py [days] [out_dir]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_etsy  # noqa: E402
from radar.main import main  # noqa: E402


def run(days: int = 15, out_dir: str | None = None) -> Path:
    srv, port = mock_etsy.serve()
    os.environ["ETSY_API_KEY"] = "testkeystring"
    os.environ["ETSY_SHARED_SECRET"] = "testsecret"
    os.environ["ETSY_BASE_URL"] = f"http://127.0.0.1:{port}/v3/application"
    out = Path(out_dir or ROOT / "tests" / "_sim_out")
    if out.exists():
        shutil.rmtree(out)
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["limits"]["requests_per_second"] = 500
    cfg["limits"]["new_shops_per_day"] = 25
    cfg["extra_shops"] = ["PrintShop001", "DoesNotExistShop"]
    cfg_path = ROOT / "tests" / "_sim_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    for d in range(days):
        mock_etsy.STATE["day"] = d
        mock_etsy.STATE["calls"] = 0
        day = (mock_etsy.START + timedelta(days=d)).isoformat()
        rc = main(["--data-dir", str(out), "--config", str(cfg_path), "--date", day])
        feed = json.loads((out / "feed" / "latest.json").read_text())
        assert rc == 0, f"exit code {rc} op dag {d}"
        assert feed["date"] == day
        print(f"dag {d:2d} {day}: shops={feed['counts']['shops_tracked']:3d} "
              f"listings={feed['counts']['listings_analyzed']:5d} briefs={len(feed['briefs']):2d} "
              f"calls={feed['run']['api_calls']:4d} rising={len(feed['rising_terms'])} "
              f"basis={feed['momentum_basis']}")
    srv.shutdown()
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    run(n, sys.argv[2] if len(sys.argv) > 2 else None)
