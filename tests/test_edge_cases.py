"""Edge cases: wrong API key, exhausted quota, deleted shop, re-run on same day, analysis-only run."""
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
from radar.store import Store  # noqa: E402


def setup(tmp: Path, port: int) -> Path:
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["limits"]["requests_per_second"] = 500
    p = tmp / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    os.environ["ETSY_BASE_URL"] = f"http://127.0.0.1:{port}/v3/application"
    return p


def day(d: int) -> str:
    return (mock_etsy.START + timedelta(days=d)).isoformat()


def run_all() -> None:
    srv, port = mock_etsy.serve()
    base = ROOT / "tests" / "_edge"

    # 1. wrong key format (no shared secret) -> clear error, exit code 1, no crash
    cfg = setup(base / "badkey", port)
    os.environ["ETSY_API_KEY"] = "onlykeystring"
    os.environ.pop("ETSY_SHARED_SECRET", None)
    mock_etsy.STATE.update(day=0, calls=0, limit=5000)
    rc = main(["--data-dir", str(base / "badkey" / "out"), "--config", str(cfg), "--date", day(0)])
    assert rc == 1, rc
    print("1 ok: verkeerde key -> exit 1")

    # 2. missing key entirely
    os.environ.pop("ETSY_API_KEY", None)
    try:
        main(["--data-dir", str(base / "badkey" / "out"), "--config", str(cfg), "--date", day(0)])
        raise AssertionError("had moeten falen")
    except Exception as exc:  # noqa: BLE001
        assert "ETSY_API_KEY ontbreekt" in str(exc), exc
    print("2 ok: ontbrekende key -> duidelijke melding")

    # 3. quota nearly exhausted -> optional calls stop, report still produced with a note
    os.environ["ETSY_API_KEY"] = "k:s"
    cfg = setup(base / "quota", port)
    mock_etsy.STATE.update(day=0, calls=0, limit=380)
    rc = main(["--data-dir", str(base / "quota" / "out"), "--config", str(cfg), "--date", day(0)])
    feed = json.loads((base / "quota" / "out" / "feed" / "latest.json").read_text())
    assert rc == 0 and feed["run"]["notes"], (rc, feed["run"])
    print("3 ok: quotum op -> rapport met notitie:", feed["run"]["notes"][0][:80])

    # 4. multi-day incl. deleted shop (PrintShop005 disappears on day 4) + same-day re-run
    cfg = setup(base / "multi", port)
    out = base / "multi" / "out"
    for d in range(6):
        mock_etsy.STATE.update(day=d, calls=0, limit=5000)
        assert main(["--data-dir", str(out), "--config", str(cfg), "--date", day(d)]) == 0
    mock_etsy.STATE.update(day=5, calls=0)
    assert main(["--data-dir", str(out), "--config", str(cfg), "--date", day(5)]) == 0  # re-run same day
    st = Store(out)
    dropped = st.q("SELECT shop_name, status, reject_reason FROM shops WHERE shop_name='PrintShop005'")
    n_snap = st.one("SELECT COUNT(*) FROM shop_snap WHERE date=?", (day(5),))
    n_shops = st.one("SELECT COUNT(*) FROM shops WHERE status='tracked'")
    st.close(save=False)
    print("4 ok: verwijderde shop ->", [dict(r) for r in dropped], "| snapshots dag 5:", n_snap, "/", n_shops)

    # 5. analysis only
    assert main(["--data-dir", str(out), "--config", str(cfg), "--date", day(5), "--skip-collect", "--no-images"]) == 0
    print("5 ok: alleen analyse")
    srv.shutdown()
    shutil.rmtree(base)


if __name__ == "__main__":
    run_all()
