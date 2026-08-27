r"""Build a self-contained folder that runs the draft assistant on another Windows machine.

WHY THIS EXISTS (ledger #17, Marc 2026-08-27)
----------------------------------------------
The tool lived only on the workstation it was built on, and the draft is in person on a laptop.
A ``git clone`` is not close to sufficient: ``.gitignore`` excludes ``data/raw/`` (~126 MB of
cached projections and ADP -- the actual board), ``data/manual/`` (the hand-downloaded
FantasyPros CSVs) and ``backend/draftroom/static/`` (the built frontend). Only seven small
human-decision files under ``data/`` are tracked. A clone therefore produces code that cannot
build a board, and the failure would surface as a subtly empty screen rather than a clear error.

WHY THIS AND NOT A PYINSTALLER BUNDLE
--------------------------------------
A frozen .exe looks tidier and was rejected on purpose. It introduces an **untested packaging
path into draft-phase startup** -- the one code path in this repo whose regression cost is the
draft itself -- less than two weeks out, and it would run code no test in this suite has ever
executed. What ships here is the *same* interpreter, the *same* packages and the *same* entry
point that the 834-test suite exercises, moved to a different disk. The only thing that changes
is where ``sys.path`` points.

WHY THE WHEELS ARE VENDORED
----------------------------
``Setup.bat`` installs with ``--no-index``, from a ``wheels/`` folder inside the bundle. So the
laptop needs **no network at any point**, not even during setup. That matters beyond
convenience: draft night runs with wifi physically off, and a setup step that quietly requires
the internet is a step that can fail on the one evening it cannot be retried. It also freezes
the dependency set at versions that are known-good here, which is this repo's standing rule --
pin what is installed and proven, never what is newest.

WHY THERE IS NO ``pip install -e .``
-------------------------------------
The launchers put ``backend`` on ``PYTHONPATH`` instead. An editable install would drag in
setuptools, build isolation and a wheel build on the target machine -- three more things that
can fail, for no gain. Nothing in the running app imports ``draftroom`` from site-packages.

The layout below is NOT arbitrary. Several modules resolve the data directory as
``Path(__file__).resolve().parents[3] / "data"`` (``playing_time.py``, ``injury_research.py``,
``decisions.py``), so ``backend/draftroom/<pkg>/<mod>.py`` must sit exactly three levels under
the bundle root with ``data/`` beside it. Flattening the tree silently breaks every
human-decision file at once.

    <bundle>/
      backend/draftroom/...      the app, including static/ (the built frontend)
      data/                      raw cache, manual CSVs, decision files
      tools/                     invariants + the prep/availability tooling
      tests/                     so the bundle can prove itself on the target machine
      wheels/                    every dependency, as wheels, for an offline install
      requirements-frozen.txt    pinned from what is actually installed here
      Setup.bat                  run ONCE
      Verify.bat                 proves it works, offline
      DraftNight.bat             the thing to double-click on the night
      README-LAPTOP.md           the click-by-click

Usage:
    .venv\Scripts\python.exe tools\make_portable.py
    .venv\Scripts\python.exe tools\make_portable.py --out D:\draftroom-portable --skip-wheels
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Copied wholesale. `tests/` travels deliberately: the bundle's whole claim is "this works on
#: your machine", and the only way to *prove* that on the target is to run the suite there.
TREES = ("backend", "tools", "tests")

#: Everything under data/ that the board needs, including the gitignored parts that are the
#: entire reason a clone is not enough.
DATA_TREES = ("raw", "manual")

ROOT_FILES = ("pyproject.toml", "CLAUDE.md", "FEEDBACK_LEDGER.md")

#: Never travels: caches, the source venv (absolute paths baked in, would not run anyway), the
#: node toolchain (the frontend is shipped BUILT), and live draft logs -- a stale log would open
#: the board mid-draft with players already gone, which is the exact hazard CLAUDE.md documents.
EXCLUDE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", "node_modules", ".venv", "*.egg-info",
)


def _log(msg: str) -> None:
    print(f"[make_portable] {msg}", flush=True)


def _size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1_048_576
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


SETUP_BAT = r"""@echo off
rem ============================================================================
rem Setup.bat -- run this ONCE, then never again.
rem
rem Creates a private Python environment inside this folder and installs every
rem dependency from the bundled wheels/ directory. NO INTERNET IS USED OR
rem NEEDED: pip runs with --no-index, so it cannot reach out even if a network
rem is available.
rem ============================================================================
setlocal
cd /d "%~dp0"

echo.
echo [Setup] Looking for Python 3.12...

set "PYEXE="
py -3.12 -c "import sys" >nul 2>&1 && set "PYEXE=py -3.12"
if not defined PYEXE (
    python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>&1 && set "PYEXE=python"
)

if not defined PYEXE (
    echo.
    echo [Setup] ERROR: Python 3.12 was not found on this machine.
    echo.
    echo   The bundled packages are built specifically for Python 3.12 on 64-bit
    echo   Windows. A different version ^(3.11, 3.13...^) will NOT work with them,
    echo   and the error you would get instead is confusing, so this stops here.
    echo.
    echo   Install Python 3.12 from:
    echo     https://www.python.org/downloads/release/python-31210/
    echo   Choose "Windows installer ^(64-bit^)" and TICK "Add python.exe to PATH".
    echo   Then run this file again.
    echo.
    pause
    exit /b 1
)

echo [Setup] Found: %PYEXE%
%PYEXE% --version

echo.
rem A .venv copied from another machine is WORSE than none: pyvenv.cfg and every
rem Scripts\*.exe shim embed the absolute path of the machine that built it, so it
rem appears to exist and then fails in ways that look like a code bug. `python -m venv`
rem over the top does NOT fully repair that. Remove it and build clean.
if exist ".venv" (
    echo [Setup] Removing an existing .venv ^(a copied one has the wrong paths baked in^)...
    rmdir /s /q ".venv"
)

echo [Setup] Creating the private environment in .venv ...
%PYEXE% -m venv .venv
if errorlevel 1 (
    echo [Setup] ERROR: could not create the environment. See the message above.
    pause
    exit /b 1
)

echo.
echo [Setup] Installing dependencies from the bundled wheels ^(no internet^)...
".venv\Scripts\python.exe" -m pip install --upgrade --no-index --find-links wheels pip setuptools wheel >nul 2>&1
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheels -r requirements-frozen.txt
if errorlevel 1 (
    echo.
    echo [Setup] ERROR: the install failed. The most likely cause is a Python
    echo         version other than 3.12, or a 32-bit Python. Check the version
    echo         printed above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [Setup] Done. Now run Verify.bat to prove it actually works.
echo ============================================================
pause
"""

VERIFY_BAT = r"""@echo off
rem ============================================================================
rem Verify.bat -- proves this bundle works on THIS machine, with no network.
rem
rem Runs three things, in increasing order of what they prove:
rem   1. the full test suite
rem   2. the invariant gate (the model's own sanity checks)
rem   3. a real board build, printing the top of the board
rem
rem If all three pass, the tool works here. Turn wifi OFF and run it again --
rem it must pass identically, because draft night has no network.
rem ============================================================================
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo [Verify] No environment found. Run Setup.bat first.
    pause
    exit /b 1
)

echo.
echo === 1/3  Test suite =========================================
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 goto :failed

echo.
echo === 2/3  Invariant gate ======================================
".venv\Scripts\python.exe" tools\run_invariants.py
if errorlevel 1 goto :failed

echo.
echo === 3/3  Real board build ====================================
".venv\Scripts\python.exe" tools\portable_smoke.py
if errorlevel 1 goto :failed

echo.
echo ==============================================================
echo [Verify] ALL THREE PASSED. This bundle works on this machine.
echo          Now turn wifi OFF and run this file once more.
echo ==============================================================
pause
exit /b 0

:failed
echo.
echo ==============================================================
echo [Verify] FAILED. Read the output above -- do not use this
echo          bundle on draft night until it passes.
echo ==============================================================
pause
exit /b 1
"""

DRAFTNIGHT_BAT = r"""@echo off
rem ============================================================================
rem DraftNight.bat -- the thing to double-click on 2026-09-08.
rem
rem Starts the assistant in offline draft mode. The server installs and VERIFIES
rem a socket guard before it binds: any outbound non-localhost connection raises.
rem Safe to run with wifi physically off, which is how it is meant to be run.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8484"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:%PORT%"
set "PYTHONPATH=%~dp0backend"

if not exist "%VENV_PY%" (
    echo [DraftNight] No environment found. Run Setup.bat first, then Verify.bat.
    pause
    exit /b 1
)

rem Draft mode refuses to start without a slot -- no silent slot-1 assumption.
rem The draw happens at the table, and the slot can also be changed in the UI later.
:askslot
set "MYSLOT="
set /p "MYSLOT=[DraftNight] Enter your draft slot (1-10): "
if not defined MYSLOT goto :badslot
echo(!MYSLOT!| findstr /r "^[0-9][0-9]*$" >nul || goto :badslot
if !MYSLOT! GEQ 1 if !MYSLOT! LEQ 10 goto :slotok
:badslot
echo [DraftNight] Please enter a whole number from 1 to 10.
goto :askslot
:slotok

echo [DraftNight] Starting on %URL% (offline, slot !MYSLOT!) ...
start "draftroom server" /min "%VENV_PY%" -m draftroom.server --draft --port %PORT% --my-slot !MYSLOT!

echo [DraftNight] Waiting for it to come up...
set /a TRIES=0
:waitloop
set /a TRIES+=1
powershell -NoProfile -Command ^
    "try { $r = Invoke-WebRequest -Uri '%URL%/healthz' -UseBasicParsing -TimeoutSec 1; exit ($(if ($r.StatusCode -eq 200) {0} else {1})) } catch { exit 1 }"
if not errorlevel 1 goto :ready
if %TRIES% GEQ 40 (
    echo [DraftNight] It did not respond after 20 seconds.
    echo [DraftNight] Check the minimized "draftroom server" window for the error.
    goto :openanyway
)
timeout /t 1 /nobreak >nul
goto :waitloop

:ready
echo [DraftNight] Up.

:openanyway
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" ( start "" "%CHROME%" --app=%URL% --new-window & goto :done )
set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" ( start "" "%CHROME%" --app=%URL% --new-window & goto :done )
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" ( start "" "%EDGE%" --app=%URL% & goto :done )
set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" ( start "" "%EDGE%" --app=%URL% & goto :done )
start "" "%URL%"

:done
endlocal
"""

SMOKE_PY = r'''"""Prove the bundle can build a REAL board, not just import cleanly.

Deliberately stronger than a health check. An import test passes on a bundle whose data
directory never travelled, and the symptom of that on draft night is an empty-looking board
rather than an error -- which is precisely the failure mode that must not reach the room.

So this asserts on the things a missing/partial cache destroys: a valued pool of real size,
a plausible top of the board, and the availability/research files binding to actual players.
"""

from __future__ import annotations

import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BUNDLE / "backend"))


def main() -> int:
    from draftroom.validate.board import build_real_board

    print(f"bundle root: {BUNDLE}")
    print(f"data dir:    {BUNDLE / 'data'}  (exists: {(BUNDLE / 'data').exists()})")

    rb = build_real_board()
    ranked = sorted(
        [p for p in rb.players if p.is_ranked and p.dv is not None], key=lambda p: -p.dv
    )
    print(f"\nboard source: {rb.source}")
    print(f"players valued: {len(ranked)}")

    if len(ranked) < 150:
        print(
            f"\nFAIL: only {len(ranked)} players were valued. A complete cache values ~199. "
            "This means data/raw did not travel completely."
        )
        return 1

    print("\ntop 10 by draft value:")
    for i, p in enumerate(ranked[:10], start=1):
        print(f"  {i:>2}. {p.name:<24} {p.pos:<3} adp {p.adp:<6} dv {p.dv:.1f}")

    notes = getattr(rb, "research_notes", {}) or {}
    print(f"\nresearch notes bound to board players: {len(notes)}")
    for pid, n in notes.items():
        figure = "UNPRICED" if n.finding.is_unpriced else f"-{n.finding.games_missed:g}G"
        print(f"  {n.finding.player_name or pid:<24} {figure}")

    if not notes:
        print(
            "\nFAIL: no research notes bound. data/injury_research.json is missing or its "
            "player ids do not match this board."
        )
        return 1

    print("\nOK: the bundle built a real board with a real cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def build(out: Path, *, skip_wheels: bool) -> int:
    if out.exists():
        _log(f"removing existing {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for tree in TREES:
        src = REPO_ROOT / tree
        if not src.exists():
            _log(f"WARNING: {tree}/ not found, skipping")
            continue
        _log(f"copying {tree}/ ...")
        shutil.copytree(src, out / tree, ignore=EXCLUDE)

    static = out / "backend" / "draftroom" / "static"
    if not (static / "index.html").exists():
        _log(
            "ERROR: backend/draftroom/static/index.html is missing. The frontend has not been "
            "built. Run `cd frontend && npm run build` first -- the bundle ships the frontend "
            "BUILT so the laptop never needs Node."
        )
        return 1
    _log(f"frontend assets present ({_size_mb(static):.1f} MB)")

    data_out = out / "data"
    data_out.mkdir()
    for name in DATA_TREES:
        src = REPO_ROOT / "data" / name
        if not src.exists():
            _log(f"WARNING: data/{name}/ not found, skipping")
            continue
        _log(f"copying data/{name}/ ({_size_mb(src):.1f} MB) ...")
        shutil.copytree(src, data_out / name, ignore=EXCLUDE)
    for f in sorted((REPO_ROOT / "data").glob("*")):
        if f.is_file():
            shutil.copy2(f, data_out / f.name)
            _log(f"copying data/{f.name}")

    # Draft logs deliberately do NOT travel: a stale log opens the board mid-draft with players
    # already gone, and the only symptom is a board that looks subtly wrong in a room full of
    # people (CLAUDE.md). The directory is created empty so the first launch has somewhere to write.
    (data_out / "drafts").mkdir(exist_ok=True)
    (data_out / "drafts" / ".gitkeep").write_text("", encoding="utf-8")

    for f in ROOT_FILES:
        if (REPO_ROOT / f).exists():
            shutil.copy2(REPO_ROOT / f, out / f)

    _log("freezing the dependency set from THIS environment (pinned to what is proven here)")
    frozen = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--exclude-editable"],
        capture_output=True, text=True, check=True,
    ).stdout
    lines = [ln for ln in frozen.splitlines() if ln.strip() and not ln.startswith("-e")]
    (out / "requirements-frozen.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log(f"{len(lines)} pinned packages")

    if skip_wheels:
        _log("SKIPPING wheel download (--skip-wheels). The bundle will need internet at setup.")
    else:
        wheels = out / "wheels"
        wheels.mkdir()
        _log("downloading wheels for offline install (this takes a minute) ...")
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "download",
                "-r", str(out / "requirements-frozen.txt"),
                "-d", str(wheels),
                "--only-binary", ":all:",
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            _log("ERROR: wheel download failed:\n" + proc.stdout[-3000:] + proc.stderr[-3000:])
            return 1
        # pip itself is not in `pip freeze`, but Setup.bat upgrades it offline, so it has to be
        # in the folder or that line silently no-ops on an old pip.
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "pip", "setuptools", "wheel",
             "-d", str(wheels), "--only-binary", ":all:"],
            capture_output=True, text=True,
        )
        _log(f"{len(list(wheels.glob('*.whl')))} wheels ({_size_mb(wheels):.1f} MB)")

    readme_src = REPO_ROOT / "tools" / "portable_readme.md"
    if readme_src.exists():
        shutil.copy2(readme_src, out / "README-LAPTOP.md")
        _log("copying README-LAPTOP.md (the click-by-click)")
    else:
        _log("WARNING: tools/portable_readme.md not found -- the bundle ships with no guide")

    (out / "Setup.bat").write_text(SETUP_BAT, encoding="utf-8")
    (out / "Verify.bat").write_text(VERIFY_BAT, encoding="utf-8")
    (out / "DraftNight.bat").write_text(DRAFTNIGHT_BAT, encoding="utf-8")
    (out / "tools" / "portable_smoke.py").write_text(SMOKE_PY, encoding="utf-8")

    # Belt and braces against the trap Setup.bat also guards: a .venv that travelled from
    # another machine has absolute paths baked into pyvenv.cfg and every Scripts\*.exe shim, so
    # it LOOKS installed and then fails as though the code were broken. This runs last so it
    # also cleans up a venv created by testing the bundle in place.
    stale = out / ".venv"
    if stale.exists():
        _log("removing a .venv found in the bundle (it must never travel)")
        shutil.rmtree(stale, ignore_errors=True)

    _log("")
    _log(f"BUNDLE READY: {out}")
    _log(f"total size: {_size_mb(out):.1f} MB")
    _log("")
    for child in sorted(out.iterdir()):
        _log(f"  {child.name:<28} {_size_mb(child):>8.1f} MB")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "dist" / "draftroom-portable")
    ap.add_argument(
        "--skip-wheels", action="store_true",
        help="do not vendor wheels (faster to build; the target then needs internet at setup)",
    )
    args = ap.parse_args(argv)
    return build(args.out.resolve(), skip_wheels=args.skip_wheels)


if __name__ == "__main__":
    raise SystemExit(main())
