# Draft Room on your laptop

Everything needed is already in this folder. **No internet is required at any point** — not to
set it up, and not on draft night. The only thing your laptop needs that is not in here is
Python 3.12, and step 1 covers that.

Three steps, once. Then one file to double-click on draft night.

---

## Step 0 — get this folder onto the laptop

Copy the whole `draftroom-portable` folder to the laptop. A USB stick is the safest route.
It is about **280 MB**, so expect a minute or two.

Put it somewhere short and local, like `C:\draftroom-portable`.

- **Do not** put it in OneDrive or any synced folder. Sync can rename, lock or half-download
  files, and you would find out on draft night.
- **Do not** run it from the USB stick itself. Copy it to the laptop's own drive.
- If you see a `.venv` folder in there, delete it before copying. It belongs to the machine that
  built the bundle and its paths are wrong everywhere else. `Setup.bat` removes one automatically
  if it finds it, so this is a belt-and-braces note rather than something you have to remember.

**How you know it worked:** the folder on the laptop contains `Setup.bat`, `Verify.bat`,
`DraftNight.bat`, and folders named `backend`, `data`, `tools`, `wheels`.

---

## Step 1 — install Python 3.12 (about 3 minutes)

Skip this if the laptop already has Python **3.12** specifically. To check, open the Start menu,
type `cmd`, press Enter, and type:

```
py -3.12 --version
```

If it prints `Python 3.12.something`, you are done with this step.

Otherwise:

1. Go to **<https://www.python.org/downloads/release/python-31210/>**
2. Scroll to the bottom, to the table headed **Files**.
3. Click **"Windows installer (64-bit)"**. It downloads a file ending in `.exe`.
4. Run it.
5. **On the first screen, tick the box at the bottom that says "Add python.exe to PATH".**
   This is the one thing that matters and it is easy to miss. If you skip it, `Setup.bat` will
   tell you Python was not found even though you just installed it.
6. Click **"Install Now"**. It takes about two minutes.
7. When it finishes, click **Close**.

**Why 3.12 and not the newest:** the packages in the `wheels` folder are built for 3.12
specifically. A newer Python will not accept them, and the error message is not obvious about
why. `Setup.bat` checks the version and stops with a clear message rather than letting you find
out later.

**If you cannot install software on this laptop**, stop here and tell me. There is another route,
but it needs to be built differently and it should not be improvised on the day.

---

## Step 2 — run `Setup.bat` (about 2 minutes)

Double-click **`Setup.bat`** in the folder.

**What you will see:** a black window that prints `[Setup] Found: py -3.12`, then the Python
version, then a long list of lines beginning `Processing .\wheels\...`. That list is expected and
is roughly 60 lines long.

**How you know it worked:** the last thing it prints is

```
============================================================
[Setup] Done. Now run Verify.bat to prove it actually works.
============================================================
```

Press any key to close it.

**If it says Python 3.12 was not found:** you either skipped step 1, or you missed the "Add
python.exe to PATH" tick box. Re-run the Python installer, choose **Modify**, and make sure that
box is ticked.

**Run this only once.** Running it again is harmless but pointless.

---

## Step 3 — run `Verify.bat` (about 3 minutes) — do not skip this

Double-click **`Verify.bat`**.

This is the step that proves the tool actually works on your machine rather than merely
appearing to. It runs three things in order:

1. **The test suite** — 834 tests. Takes about two minutes and prints a lot of dots.
2. **The invariant gate** — the model's own sanity checks on the real board.
3. **A real board build** — it prints the top 10 players by draft value.

**How you know it worked:** it ends with

```
==============================================================
[Verify] ALL THREE PASSED. This bundle works on this machine.
         Now turn wifi OFF and run this file once more.
==============================================================
```

and just above that you will see a top 10 starting with **Jahmyr Gibbs** and **Bijan Robinson**,
and a list of five research notes (Pierce, Charbonnet, Tyson, Jacobs, Nacua). If you see those
names, the data made the trip intact.

### Then do exactly what it says: turn wifi off and run it again

This is the real test, and it takes three more minutes. Draft night has no network, and the only
way to know the tool survives that is to try it. Turn the laptop's wifi off, double-click
`Verify.bat` again, and confirm it still ends with **ALL THREE PASSED**.

**If it fails on any of the three, do not use this bundle on draft night.** Send me the last 20
lines of the window and I will fix it. There is time.

---

## Draft night — 2026-09-08

Double-click **`DraftNight.bat`**.

1. It asks for your draft slot. Type the number you drew (1 to 10) and press Enter.
   Do not guess — if the draw has not happened yet, launch it anyway with any number and change
   it in the app afterwards. Your seat is always marked `(YOU)`.
2. It starts the server and waits for it to come up. Takes about five seconds.
3. It opens the board in Chrome automatically, in a clean window with no tabs or address bar.

**How you know it worked:** the board fills with players and the header says **CC Boys Draft
Room**.

**Wifi should be OFF.** The server installs and verifies a guard before it starts: if anything
in the code tried to reach the internet, it would raise rather than quietly work. That guard is
the reason it is safe to run with no network — it is not merely tolerated, it is enforced.

### The two gestures you need

- **Left click a player's name** — records the pick against whoever is on the clock. This is the
  one you will use all night.
- **Right click a name**, or click the small `▾` next to it — pick a specific team instead. This
  is for catching up when you have fallen behind, or entering someone else's pick out of order.

**To undo:** `Ctrl+Z`. Or click the `x` next to the most recent pick, which undrafts it
immediately with no confirmation.

---

## Practising before the night

Practise as much as you like, but **not on the real log**. `DraftNight.bat` writes to
`data/drafts/draft.jsonl`, which is the file the real draft opens against. Fake picks left in it
would open your real draft with players already gone, and the only symptom is a board that looks
subtly wrong in a room full of people.

For a rehearsal, open `cmd` in this folder and run:

```
set PYTHONPATH=%CD%\backend
.venv\Scripts\python.exe -m draftroom.server --draft --port 8484 --my-slot 1 --log-path data\drafts\practice.jsonl
```

Then open <http://127.0.0.1:8484> in your browser. Everything you click goes to `practice.jsonl`
and the real log is untouched.

If you ever do end up with test picks in the real log, the app tells you at startup — it prints
how many picks it found and names the players — and it prints the exact command to archive them.
It never refuses to start, because a half-finished draft looks identical from the outside to a
log with junk in it, and refusing would be worse.

---

## If something goes wrong on the night

**The board is empty or every player shows no projection.** The `data` folder did not travel
completely. This is what `Verify.bat` exists to catch, which is why step 3 is not optional.

**It says the port is in use (`10048`).** A previous copy is still running. Open Task Manager,
end every `python.exe`, and run `DraftNight.bat` again.

**The browser window shows nothing.** The server may still be starting. Wait ten seconds and
refresh. If it is still blank, look at the minimized window titled `draftroom server` for the
error.

**Anything else:** the board is the only record of the draft, so if the tool is not cooperating,
write the picks on paper and keep going. Nothing is lost — the picks can be entered afterwards
and the log rebuilt.
