#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Copy znah/tt09 WASM viewer engine into visuals/ and optionally serve over HTTP.

The WebGL viewer needs a *layout pack* (gates, wires, bbox) in ``visuals/gds/circuit_pack.bin``.
That file is **not** Verilog and **not** produced by ``run_sim.py`` — it comes from exporting
your post-P&R GDS with the znah/tt09 ``parse_gds`` flow (or any tool that emits the same format).
Default sync updates only ``gates.wasm`` so repo copies of ``sim.js`` / ``swissgl.js``
(WebGL fixes, debug modes) are not overwritten. Use ``--full-engine-sync`` to replace
those from upstream (patches are reapplied).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_URL = "https://github.com/znah/tt09.git"
_ENGINE_ROOT_KEEP = frozenset({"gates.wasm", "sim.js", "swissgl.js"})


def _rmtree_git_maybe_locked(path: Path) -> None:
    """Remove .git; on Windows cmd rmdir is more reliable than shutil when packs are read-only."""
    if not path.is_dir():
        return
    if sys.platform == "win32":
        rc = subprocess.call(
            ["cmd", "/c", "rmdir", "/s", "/q", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc != 0 and path.exists():
            print(
                f"WARNING: could not remove {path} (in use?). "
                "Close Cursor/IDE from that folder and delete .git manually if you want it gone.",
                file=sys.stderr,
            )
        return
    try:
        shutil.rmtree(path)
    except OSError:
        if path.exists():
            print(f"WARNING: could not remove {path}", file=sys.stderr)


def _prune_engine_cache(cache_dir: Path) -> None:
    """Keep only WASM engine files; drop .git, gds/, demos, sources (layout packs are not from upstream)."""
    git_dir = cache_dir / ".git"
    if git_dir.is_dir():
        _rmtree_git_maybe_locked(git_dir)
    for path in list(cache_dir.iterdir()):
        if path.name == "gds" and path.is_dir():
            shutil.rmtree(path)
            continue
        if path.is_file() and path.name in _ENGINE_ROOT_KEEP:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()


def _find_source(visuals_dir: Path, prefer: Path | None) -> Path:
    if prefer and (prefer / "gates.wasm").is_file():
        return prefer
    cached = visuals_dir / "_znah_tt09"
    if (cached / "gates.wasm").is_file():
        _prune_engine_cache(cached)
        return cached
    if shutil.which("git") is None:
        print("ERROR: git not found, or place znah/tt09 checkout at", cached, file=sys.stderr)
        sys.exit(1)
    print("Cloning znah/tt09 (WASM viewer engine)...")
    if cached.exists():
        shutil.rmtree(cached)
    rc = subprocess.call(["git", "clone", "--depth", "1", REPO_URL, str(cached)])
    if rc != 0 or not (cached / "gates.wasm").is_file():
        sys.exit(rc or 1)
    _prune_engine_cache(cached)
    return cached


def _patch_sim_js_wasm_load(sim_js: Path) -> None:
    """instantiateStreaming() needs Content-Type: application/wasm; many static servers
    (e.g. VS Code Live Server) send application/octet-stream instead — WASM never loads."""
    text = sim_js.read_text(encoding="utf-8")
    old = "        const wasm = await WebAssembly.instantiateStreaming(fetch('gates.wasm'));"
    new = (
        "        const _gates_wasm_resp = await fetch('gates.wasm');\n"
        "        const wasm = await WebAssembly.instantiate(await _gates_wasm_resp.arrayBuffer());"
    )
    if old not in text:
        if "instantiateStreaming" not in text:
            return
        print("WARNING: sim.js layout changed; could not patch WASM load.", file=sys.stderr)
        return
    sim_js.write_text(text.replace(old, new, 1), encoding="utf-8")


def _patch_sim_js_glsl_scope(sim_js: Path) -> None:
    """Standalone sim.js must use this.glsl — bare glsl was only valid in the original inline script."""
    text = sim_js.read_text(encoding="utf-8")
    replacements = [
        (
            "array2tex(glsl, data.wire_rects.a,",
            "array2tex(this.glsl, data.wire_rects.a,",
        ),
        (
            "array2tex(glsl, data.wire_infos.a,",
            "array2tex(this.glsl, data.wire_infos.a,",
        ),
        (
            "this.state = glsl({}, {data:this.main.state,",
            "this.state = this.glsl({}, {data:this.main.state,",
        ),
        (
            "this.heat = glsl({}, {data:this.main.heat,",
            "this.heat = this.glsl({}, {data:this.main.heat,",
        ),
    ]
    new_text = text
    for old, new in replacements:
        if old in new_text:
            new_text = new_text.replace(old, new, 1)
    new_text = new_text.replace("sim.step(", "this.step(")
    new_text = new_text.replace(
        "while (c=this.main.update_all()){ console.log(c)};",
        "while (this.main.update_all()) {}",
        1,
    )
    new_text = new_text.replace(
        "if (d.offset) {",
        "if (typeof d.offset === 'number') {",
        1,
    )
    draw_scr_sp = (
        "rayTail:Math.sqrt(speed), \n            Blend:'d*(1-sa)+s', VP:`"
    )
    draw_scr_nl = "rayTail:Math.sqrt(speed),\n            Blend:'d*(1-sa)+s', VP:`"
    draw_scr_fix = (
        "rayTail:Math.sqrt(speed),\n            NoDepthWrite: true,\n            Blend:'d*(1-sa)+s', VP:`"
    )
    if "NoDepthWrite: true" not in new_text:
        if draw_scr_sp in new_text:
            new_text = new_text.replace(draw_scr_sp, draw_scr_fix, 1)
        elif draw_scr_nl in new_text:
            new_text = new_text.replace(draw_scr_nl, draw_scr_fix, 1)
    if new_text != text:
        sim_js.write_text(new_text, encoding="utf-8")


def _patch_swissgl_no_depth_write(swissgl_js: Path) -> None:
    """VGA overlay must not write the depth buffer or the next circuit draw fails the depth test."""
    text = swissgl_js.read_text(encoding="utf-8")
    if "NoDepthWrite" in text and "options.NoDepthWrite" in text:
        return
    new_text = text
    old_set = (
        "'Clear', 'Blend', 'View', 'Grid', 'Mesh', 'Aspect', 'DepthTest', 'AlphaCoverage', 'Face'\n])"
    )
    new_set = (
        "'Clear', 'Blend', 'View', 'Grid', 'Mesh', 'Aspect', 'DepthTest', 'AlphaCoverage', 'Face',\n"
        "    'NoDepthWrite',\n])"
    )
    if old_set in new_text:
        new_text = new_text.replace(old_set, new_set, 1)
    old_mask = "    gl.depthMask(!(options.DepthTest == 'keep'));\n    if (haveClear) {"
    new_mask = (
        "    if (options.NoDepthWrite) {\n"
        "        gl.depthMask(false);\n"
        "    } else {\n"
        "        gl.depthMask(!(options.DepthTest == 'keep'));\n"
        "    }\n"
        "    if (haveClear) {"
    )
    if old_mask in new_text:
        new_text = new_text.replace(old_mask, new_mask, 1)
    if new_text != text:
        swissgl_js.write_text(new_text, encoding="utf-8")


def _patch_swissgl_webgl_context(swissgl_js: Path) -> None:
    """MSAA + ANGLE/D3D11 can fail compiling huge instanced shaders on some Windows setups."""
    text = swissgl_js.read_text(encoding="utf-8")
    old = "canvas_gl.getContext('webgl2', {alpha:false, antialias:true})"
    new = (
        "canvas_gl.getContext('webgl2', {alpha:false, antialias:false, stencil:false, "
        "premultipliedAlpha:true, powerPreference:'high-performance'})"
    )
    if old not in text:
        return
    swissgl_js.write_text(text.replace(old, new, 1), encoding="utf-8")


def _patch_swissgl_instanced_batch(swissgl_js: Path) -> None:
    """Split huge drawArraysInstanced into chunks; ANGLE/D3D11 often fails one giant instanced draw."""
    text = swissgl_js.read_text(encoding="utf-8")
    if "uniform int InstanceBase;" in text and "maxInstChunk" in text:
        return
    new_text = text
    if "uniform int InstanceBase;" not in new_text:
        old_u = "uniform int MeshMode;\nuniform ivec4 View;"
        new_u = "uniform int MeshMode;\nuniform int InstanceBase;\nuniform ivec4 View;"
        if old_u in new_text:
            new_text = new_text.replace(old_u, new_u, 1)
    if "InstanceID + InstanceBase" not in new_text:
        new_text = new_text.replace(
            "      int ii = InstanceID;",
            "      int ii = InstanceID + InstanceBase;",
            1,
        )
    old_draw = """    // setup uniforms and textures
    Object.entries(prog.setters).forEach(([name, f])=>f(uniforms[name]));
    // draw
    gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, vertN, instN);
"""
    new_draw = """    // ANGLE/D3D11 can fail compiling dynamic shaders for very large instance counts
    // (GL_INVALID_OPERATION in triggerDrawCallProgramRecompilation). Chunk draws.
    const maxInstChunk = 1024;
    for (let instBase = 0; instBase < instN; instBase += maxInstChunk) {
        const chunkInst = Math.min(maxInstChunk, instN - instBase);
        uniforms.InstanceBase = instBase;
        Object.entries(prog.setters).forEach(([name, f])=>f(uniforms[name]));
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, vertN, chunkInst);
    }
"""
    if old_draw in new_text:
        new_text = new_text.replace(old_draw, new_draw, 1)
    if new_text != text:
        swissgl_js.write_text(new_text, encoding="utf-8")


def _migrate_legacy_layout_pack(gds_dir: Path) -> None:
    """One-time: old setups used the Tiny Tapeout tile name as the pack filename."""
    new_p = gds_dir / "circuit_pack.bin"
    if new_p.is_file():
        return
    legacy = gds_dir / "09_tt_um_znah_vga_ca.bin"
    if legacy.is_file():
        shutil.copy2(legacy, new_p)
        print(f"NOTE: copied {legacy.name} -> {new_p.name} (use circuit_pack.bin for your own exports).")


def _sync_engine(source: Path, dest: Path, *, full_engine_sync: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "gates.wasm", dest / "gates.wasm")
    sim_path = dest / "sim.js"
    sg_path = dest / "swissgl.js"
    env_full = os.environ.get("FULL_ENGINE_SYNC", "").lower() in ("1", "true", "yes")
    missing_js = not sim_path.is_file() or not sg_path.is_file()
    need_js = full_engine_sync or env_full or missing_js
    if need_js:
        shutil.copy2(source / "sim.js", sim_path)
        shutil.copy2(source / "swissgl.js", sg_path)
        _patch_swissgl_no_depth_write(sg_path)
        _patch_swissgl_webgl_context(sg_path)
        _patch_swissgl_instanced_batch(sg_path)
        _patch_sim_js_wasm_load(sim_path)
        _patch_sim_js_glsl_scope(sim_path)
        why = (
            "--full-engine-sync"
            if full_engine_sync
            else ("FULL_ENGINE_SYNC" if env_full else "missing sim.js or swissgl.js")
        )
        print(f"Engine: copied sim.js + swissgl.js from upstream ({why}); gates.wasm updated.")
    else:
        print(
            "Engine: updated gates.wasm only; kept visuals/sim.js and visuals/swissgl.js. "
            "Use --full-engine-sync or FULL_ENGINE_SYNC=1 to overwrite JS from znah."
        )
    gds_dir = dest / "gds"
    gds_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_layout_pack(gds_dir)
    print(
        f"Synced into {dest}; viewer loads {gds_dir / 'circuit_pack.bin'} "
        "(export from your post-P&R GDS; see znah/tt09 parse_gds)."
    )


def run_http_server(visuals_dir: Path, port: int) -> None:
    """Serve visuals_dir over HTTP (blocks until Ctrl+C)."""
    root = visuals_dir.resolve()
    prev_cwd = os.getcwd()
    os.chdir(root)

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {
            **dict(SimpleHTTPRequestHandler.extensions_map),
            ".wasm": "application/wasm",
        }

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        url = f"http://127.0.0.1:{port}/index.html"
        print(f"Serving {root}")
        print(f"Open: {url}")
        print(
            "Note: Cursor/VS Code Simple Browser often cannot reach this URL (404 or empty). "
            "Use Chrome/Edge, or Live Server at http://127.0.0.1:5500/visuals/index.html"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    finally:
        os.chdir(prev_cwd)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--source", type=Path, default=None)
    p.add_argument("--sync-only", action="store_true")
    p.add_argument(
        "--full-engine-sync",
        action="store_true",
        help="also overwrite sim.js and swissgl.js from upstream (default: wasm only)",
    )
    p.add_argument(
        "--serve-only",
        action="store_true",
        help="only start HTTP server (no clone/sync); use after run_sim.py or --sync-only",
    )
    args = p.parse_args()

    visuals_dir = Path(__file__).resolve().parent
    if not (visuals_dir / "index.html").is_file():
        print(f"ERROR: Missing {visuals_dir / 'index.html'}", file=sys.stderr)
        return 1

    if args.serve_only and args.sync_only:
        print("ERROR: use only one of --serve-only and --sync-only.", file=sys.stderr)
        return 1

    if args.serve_only:
        if not (visuals_dir / "gates.wasm").is_file():
            print(
                "ERROR: gates.wasm missing under visuals/. Run: python run_sim.py",
                file=sys.stderr,
            )
            return 1
        run_http_server(visuals_dir, args.port)
        return 0

    source = _find_source(visuals_dir, args.source)
    _sync_engine(source, visuals_dir, full_engine_sync=args.full_engine_sync)
    if args.sync_only:
        return 0

    run_http_server(visuals_dir, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
