#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Run simulation (cocotb) and prepare the znah/tt09 WASM viewer under visuals/.

Layout:
  src/     — Verilog
  test/    — Makefile, tb, cocotb tests
  visuals/ — viewer + synced gates.wasm, sim.js, swissgl.js; gds/circuit_pack.bin = your GDS export (not from sim)

Install: pip install -r test/requirements.txt
         Optional (PNG from VCD): pip install -r visuals/requirements.txt

Examples:
  python run_sim.py
  python run_sim.py --serve           # same, then start viewer (Chrome/Edge; Ctrl+C stops)
  python run_sim.py --serve --port 9000
  python run_sim.py --full-visuals-sync  # also overwrite sim.js/swissgl.js from znah (default: wasm only)
  VIZ=0 python run_sim.py              # skip syncing WASM/GDS into visuals/
  WAVES=1 python run_sim.py
  GATES=yes PDK_ROOT=/path/to/pdk python run_sim.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _maybe_reexec_with_venv() -> None:
    root = Path(__file__).resolve().parent
    venv_python = (
        root / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else root / ".venv" / "bin" / "python"
    )
    if not venv_python.is_file():
        return
    try:
        if Path(sys.executable).resolve().samefile(venv_python.resolve()):
            return
    except (FileNotFoundError, OSError):
        pass
    script = Path(__file__).resolve()
    rc = subprocess.call(
        [str(venv_python), "-u", str(script), *sys.argv[1:]],
        cwd=str(root),
    )
    raise SystemExit(rc)


def _require_iverilog(sim: str) -> int:
    if sim == "icarus" and shutil.which("iverilog") is None:
        print("ERROR: iverilog is not on PATH.", file=sys.stderr)
        return 1
    return 0


def _rtl_sources(root: Path) -> list[Path]:
    src_dir = root / "src"
    test_dir = root / "test"
    return [
        src_dir / "project.v",
        src_dir / "hvsync_generator.v",
        test_dir / "tb.v",
    ]


def _gate_sources(root: Path, pdk_root: Path) -> list[Path]:
    test_dir = root / "test"
    return [
        pdk_root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "verilog" / "primitives.v",
        pdk_root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "verilog" / "sky130_fd_sc_hd.v",
        test_dir / "gate_level_netlist.v",
        test_dir / "tb.v",
    ]


def _run_simulation(*, full_visuals_sync: bool = False) -> int:
    root = Path(__file__).resolve().parent
    test_dir = root / "test"
    src_dir = root / "src"

    sim = os.environ.get("SIM", "icarus").lower()
    viz = os.environ.get("VIZ", "1") != "0"
    waves = os.environ.get("WAVES", "0") == "1"
    gates = os.environ.get("GATES", "no").lower() == "yes"

    err = _require_iverilog(sim)
    if err:
        return err

    defines: dict[str, str | int] = {"SIM": 1}
    compile_args: list[str] = [f"-I{src_dir}"]

    if gates:
        pdk_root_env = os.environ.get("PDK_ROOT")
        if not pdk_root_env:
            print("ERROR: GATES=yes requires PDK_ROOT to be set.", file=sys.stderr)
            return 1
        pdk_root = Path(pdk_root_env)
        sources = _gate_sources(root, pdk_root)
        defines.update(
            {
                "GL_TEST": 1,
                "FUNCTIONAL": 1,
                "USE_POWER_PINS": 1,
                "UNIT_DELAY": "#1",
            }
        )
        sim_build = test_dir / "sim_build" / "gl"
    else:
        sources = _rtl_sources(root)
        sim_build = test_dir / "sim_build" / "rtl"

    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        print("ERROR: Missing required simulation files:", file=sys.stderr)
        for path in missing:
            print(f"  - {path}", file=sys.stderr)
        return 1

    try:
        from cocotb_tools.check_results import get_results
        from cocotb_tools.runner import get_runner
    except ModuleNotFoundError:
        rc = _run_with_make(test_dir, sim, gates, waves)
        if rc != 0:
            return rc
        if viz:
            _sync_visuals(root, full_visuals_sync=full_visuals_sync)
        return 0

    runner = get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel="tb",
        build_dir=sim_build,
        clean=True,
        timescale=("1ns", "1ps"),
        waves=waves,
        defines=defines,
        build_args=compile_args,
    )

    results_path = runner.test(
        hdl_toplevel="tb",
        test_module="test",
        test_dir=test_dir,
        build_dir=sim_build,
        results_xml="results.xml",
        waves=waves,
    )

    try:
        _, num_failed = get_results(Path(results_path))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if num_failed:
        print(f"ERROR: {num_failed} test(s) failed (see {results_path}).", file=sys.stderr)
        return 1

    print("All tests passed.")
    if viz:
        _sync_visuals(root, full_visuals_sync=full_visuals_sync)
    return 0


def _run_with_make(test_dir: Path, sim: str, gates: bool, waves: bool) -> int:
    make_exe = shutil.which("make")
    if make_exe is None:
        print(
            "ERROR: cocotb_tools is unavailable and 'make' is not on PATH.\n"
            "Install cocotb>=2 or run in an environment with make.",
            file=sys.stderr,
        )
        return 1

    env = os.environ.copy()
    env["SIM"] = sim
    if gates:
        env["GATES"] = "yes"
    if waves:
        env["WAVES"] = "1"

    print("INFO: cocotb_tools not found, using Makefile fallback.")
    rc = subprocess.call([make_exe, "-B"], cwd=str(test_dir), env=env)
    if rc != 0:
        return rc

    results_xml = test_dir / "sim_build" / ("gl" if gates else "rtl") / "results.xml"
    if not results_xml.is_file():
        print(f"ERROR: results.xml not found at {results_xml}", file=sys.stderr)
        return 1

    try:
        xml_root = ET.parse(results_xml).getroot()
    except ET.ParseError as exc:
        print(f"ERROR: Could not parse {results_xml}: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for testcase in xml_root.iter("testcase"):
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            failed += 1

    if failed:
        print(f"ERROR: {failed} test(s) failed (see {results_xml}).", file=sys.stderr)
        return 1

    print("All tests passed.")
    return 0


def _sync_visuals(root: Path, *, full_visuals_sync: bool = False) -> None:
    script = root / "visuals" / "serve_circuit_viewer.py"
    if not script.is_file():
        print(f"WARNING: {script} not found, skipping visuals sync.", file=sys.stderr)
        return
    cmd = [str(sys.executable), str(script), "--sync-only"]
    if full_visuals_sync:
        cmd.append("--full-engine-sync")
    rc = subprocess.call(cmd, cwd=str(root))
    if rc == 0:
        print(
            "Visuals: synced WASM engine into visuals/ — ensure visuals/gds/circuit_pack.bin exists "
            "(export from your layout; see znah/tt09 parse_gds). Open via Live Server or: python run_sim.py --serve"
        )
    else:
        print("WARNING: visuals/serve_circuit_viewer.py --sync-only failed.", file=sys.stderr)


def _serve_visuals(root: Path, port: int) -> int:
    script = root / "visuals" / "serve_circuit_viewer.py"
    if not script.is_file():
        print(f"ERROR: {script} not found.", file=sys.stderr)
        return 1
    return subprocess.call(
        [str(sys.executable), str(script), "--serve-only", "--port", str(port)],
        cwd=str(root),
    )


def main() -> int:
    _maybe_reexec_with_venv()
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Run cocotb simulation; sync WASM viewer under visuals/.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="After tests pass, start the viewer HTTP server (use Chrome/Edge; Ctrl+C to stop).",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve (default: 8765).")
    parser.add_argument(
        "--full-visuals-sync",
        action="store_true",
        help="Overwrite sim.js and swissgl.js from znah when syncing (default: only gates.wasm).",
    )
    args = parser.parse_args()

    rc = _run_simulation(full_visuals_sync=args.full_visuals_sync)
    if rc != 0:
        return rc
    if args.serve:
        return _serve_visuals(root, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
