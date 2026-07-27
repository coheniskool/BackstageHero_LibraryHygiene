# Spec: Chorus Lookup Reliability — Backoff and Concurrent-Run Protection

**Parent spec**: SPEC-library-enrichment.md (this document only amends `chorus_client.py`'s request behavior and `chorus_cache.py`/`gui.py`'s concurrency handling; sidecar format, chart/score parsing, and everything else there is unchanged and not restated here).

## Objective

A live run tonight (2026-07-27, `M:\_Organized`, ~7,600 songs) showed the Chorus-lookup path failing almost completely:

- **6,086** `Chorus lookup error` log lines in one session, nearly all `429 Too Many Requests` or `503 Service Unavailable` from `https://api.enchor.us/search/advanced` — essentially every lookup failed, not an occasional one.
- **49** `Could not write Chorus cache ... [WinError 32] The process cannot access the file because it is being used by another process: '...backstagehero_chorus_cache.json.tmp' -> '...backstagehero_chorus_cache.json'`, recurring every 1-2 seconds for extended stretches.

Both are root-caused below. Neither is a transient fluke — both are structural and will recur on every large-library run.

**User**: same solo hobbyist as the parent spec, running the GUI (`gui.py`) against a real ~7,600-song library on Windows, with "Enrich after scan" enabled.

**Success looks like**: a library-wide enrichment run against a rate-limiting Chorus API degrades gracefully (retries with backoff, eventually succeeds or cleanly gives up per song) instead of spamming errors for the whole run, and two enrichment passes can never race on the same cache file.

## Root Cause 1 — No backoff/retry in `chorus_client.py`

`chorus_client.search_by_artist_title()` (chorus_client.py:30-104) makes exactly one `requests.post()` call per song, with no delay before or after, no inspection of the `429`/`503` status beyond `raise_for_status()`, and no `Retry-After` handling. `library_enrichment.py`'s per-song loop (library_enrichment.py:197-217) calls this synchronously, once per song, back-to-back for the whole library with zero pacing.

Against a real library this means: the first handful of requests may succeed, the server starts returning 429/503 almost immediately, and every subsequent request for the rest of the run repeats the same failure — the loop never slows down or backs off, so it never recovers mid-run. The exception handler at chorus_client.py:102-104 swallows this correctly (never aborts the library scan), but "never crashes" and "actually works" are different things, and right now only the former is true.

## Root Cause 2 — No concurrency guard on enrichment runs

`gui.py:_maybe_start_enrichment()` (gui.py:1788-1799) is invoked after every scan settles, from two call sites (gui.py:1939 and gui.py:2962), and unconditionally starts a **new** `threading.Thread(target=self._run_enrichment)`. There is no check for "is an enrichment thread already running" anywhere in this path, and no lock guarding `library_enrichment.enrich_library()`.

Each call to `enrich_library()` (library_enrichment.py:167-190) builds its own `chorus_cache.CachedChorusClient(cache_path=cache_path)`, defaulting `cache_path` to `library_path / 'backstagehero_chorus_cache.json'` (library_enrichment.py:189) — the same file for every run against the same library, with no path uniquification (e.g., no PID/timestamp suffix). `CachedChorusClient._save()` (chorus_cache.py:55-64) writes to a fixed `<cache>.tmp` name and does `os.replace(tmp_path, self.cache_path)` **after every single lookup** (chorus_cache.py:75), not batched.

If two `enrich_library()` calls are in flight at once against the same library — e.g., the user re-scans (manually re-picks the folder, or a background-mode resume triggers a fresh scan) while a prior enrichment pass from an earlier scan hasn't finished — both threads' `CachedChorusClient` instances race to write the identical `<path>.tmp` and `os.replace` it onto the identical destination. Windows enforces exclusive file access during that open+replace window, so one thread's `os.replace` fails with `WinError 32` while the other thread's handle is still open. This isn't cosmetic: beyond the log spam, each instance holds its own private in-memory `_entries` dict loaded at thread-start, so whichever thread's `_save()` wins last silently discards any cache entries the other thread had already persisted — a real data-loss path, not just noisy logging.

Two independent bugs compound each other here: heavy rate-limiting from Root Cause 1 makes each `enrich_library()` pass fast per song (failed requests return quickly), which shortens the window during which a second concurrent run would be "wasted work" anyway — but it does not prevent the race, and a slow/successful run is exactly the case where losing cache entries to a second run matters most.

## Behavior Change

1. `chorus_client.search_by_artist_title()` gains bounded retry-with-backoff on `429`/`503`/connection-level transient errors, honoring a server-supplied `Retry-After` header when present.
2. Enrichment runs become mutually exclusive per process: a second `_maybe_start_enrichment()` call while one is already running is a no-op (skipped, logged), not a second concurrent pass.

## Implementation

### chorus_client.py
- Add a small retry loop inside `search_by_artist_title()` (or a wrapping helper) around the `requests.post()` call:
  - Retry on `429` and `503` (and on `requests.exceptions.ConnectionError`/`Timeout`) up to a fixed small cap (e.g. 3 attempts).
  - On `429`/`503`, prefer the response's `Retry-After` header (seconds or HTTP-date) when present; otherwise use exponential backoff with a low base (e.g. 1s, 2s, 4s) and a cap.
  - After exhausting retries, fall through to today's behavior: log and return `None` — never raise out of this function, matching the existing "never abort a library-wide run" contract in its docstring.
  - Keep `CHORUS_REQUEST_TIMEOUT_SECONDS` as the per-attempt timeout, not a total-across-retries budget.
- This makes a *single* song's lookup slower in the failure case, which is correct — it's the same trade the codebase already made for `_handle_background_throttle`'s download-side backoff (gui.py:2565-2609); Chorus lookups currently have no equivalent.

### gui.py
- Add a re-entrancy guard around `_maybe_start_enrichment` / `_run_enrichment`, e.g. an instance flag (`self._enrichment_running`) set under a lock before starting the thread and cleared in a `finally` inside `_run_enrichment`. If already set, `_maybe_start_enrichment` logs at `info` level ("Library enrichment already running; skipping") and returns without starting a new thread.
- This is a per-process guard, not a cross-process file lock — sufficient for the actual failure mode observed (same GUI process, two scan-triggered threads), and consistent with the rest of the codebase's single-process assumption (chorus_cache.py's own docstring already states "single-process CLI/subprocess usage per spec, no concurrent-writer scenario in scope" — this closes the gap where that assumption was violated in practice).
- Out of scope for this spec: hardening against two *separate processes* (e.g. GUI + `library_enricher.py` CLI) both targeting the same library simultaneously. Not what tonight's log shows, and cross-process locking is a materially bigger change (file lock or lock file with staleness handling) — flagged as an Open Question below rather than built speculatively.

### chorus_cache.py
- No structural change required once the concurrency guard above prevents two `CachedChorusClient` instances from ever targeting the same `cache_path` concurrently. Leaving `_save()`'s per-lookup atomic-write pattern as-is (it's correct for the single-writer case).

## Testing Strategy

- `tests/test_chorus_client.py` (new or extended): mock `requests.post` to return a `429` with `Retry-After: 2` then a `200`, assert the function retries and returns the successful result; assert it sleeps/waits roughly the advertised duration (inject a fake clock/sleep, no real `time.sleep` in tests). Add a case for retries exhausted → returns `None`, still no raise.
- `tests/test_gui_enrichment_integration.py` (existing file, per earlier commits touching this area): add a test that calls `_maybe_start_enrichment()` twice back-to-back while the first `enrich_library()` call is still blocked (e.g. via a monkeypatched slow/blocking stub), and assert only one thread actually calls into `library_enrichment.enrich_library()`.
- Regression: existing `tests/test_chorus_cache.py` coverage should be unaffected — no change to `CachedChorusClient`'s save/load contract.

## Boundaries

### Always Do
- Keep `search_by_artist_title()`'s "never raises" contract intact through the retry loop — every exit path still returns a dict or `None`.
- Keep the retry cap small and bounded (no unbounded backoff loops) — this is called once per song in a loop over thousands of songs; a runaway retry policy on a systemically-down API would make a library scan take hours instead of minutes.
- Log when the re-entrancy guard actually skips a run, so a "why didn't enrichment re-run after I rescanned" question is answerable from log.txt.

### Ask First
- Any change to the Chorus API request shape/body itself (chorus_client.py:45-69) — out of scope, unrelated to this reliability fix.
- Introducing a cross-process lock file — bigger surface area (staleness/crash recovery), covered in Open Questions, not to be built opportunistically as part of this fix.

### Never Do
- Do not remove or weaken the top-level `except Exception` in `search_by_artist_title()` — the retry loop lives inside that boundary, not instead of it.
- Do not make the re-entrancy guard block/queue a second run — skip-and-log only, per today's "enrichment is optional, best-effort" philosophy stated in `_maybe_start_enrichment`'s own docstring.

## Open Questions

1. **Cross-process protection** (GUI + CLI `library_enricher.py` racing on the same library) is a real but separate risk — not evidenced in tonight's log, not built here. Worth a future spec if the user runs the CLI tool manually while the GUI's background mode is also active.
2. **Should the Chorus API be rate-limited proactively** (e.g., a fixed minimum delay between requests) rather than purely reactively (retry after a 429 already happened)? Reactive retry is the smaller change and matches tonight's evidence; a proactive throttle would reduce the number of 429s in the first place but adds a tunable that has no data behind it yet. Left for the user to weigh in on if reactive retry alone doesn't clear up the error rate.
3. **Should `_run_enrichment`'s failure/skip states surface in the GUI status bar** rather than only `log.txt`? Today's design deliberately keeps this silent-unless-logged (gui.py:1788-1796's own docstring: "nothing the user needs to watch live") — not changing that philosophy here, just flagging it stayed unchanged.

---

**Next phase**: `/plan` to break this into tasks (chorus_client retry logic + tests, gui.py re-entrancy guard + tests), then `/build`.
