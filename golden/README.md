# golden/ — the byte-identity goldens

Exact stdout for 18 CLI invocations, each captured twice: `.piped.txt` (color
gate closed) and `.tty.txt` (gate forced open, i.e. a terminal). `tokenstat.py`
promises that a theme only moves a byte when you ask for one — flag-off output
is what it was before themes existed, and piped and `--json` output stay plain
under every palette. These files are that promise, pinned.

`ByteIdentityGoldenTests` in `test_tokenstat.py` owns them: it regenerates on
demand, compares otherwise, and fails with the case name and mode when a byte
moves.

```bash
# after reading the diff and deciding the change is intended:
TOKENSTAT_UPDATE_GOLDEN=1 python3 -m unittest test_tokenstat
```

Treat a golden diff as a code review, not an obstacle: an unexplained change
here is exactly the regression the harness exists to catch.

Why the bytes are stable:

- every fixture row carries an explicit timestamp, so nothing depends on today;
- `TZ` is pinned to UTC during the run (date columns render in local time);
- the themes folder is redirected to an empty temp dir, so a saved
  `~/.tokenstat/themes/config.json` on the machine cannot repaint a golden;
- the same test file proves the goldens are not vacuous: piped bytes contain no
  escapes, tty bytes paint 24-bit color except under `--json`, and every theme
  changes the tty bytes while leaving the piped bytes alone.
