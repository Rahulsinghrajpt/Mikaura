# MikAura Logging Unification — Implementation Plan (Client Feedback Addressed)

## Client Feedback Resolution

| Client Concern | Resolution |
|----------------|------------|
| "regular logger is still alive in real path" | Migrating `tracer_file_finder.py` (21 calls) and `s3_utils.py` (14 calls) to MikAura primary path |
| "import-time warnings/errors still go through regular logger" | **Accepted** — client agrees stdlib is OK for "very early import/init" |
| "helper fallbacks still do logger.info/warning/error when status_logger is missing" | Fallbacks are defensive-only (dead code in prod). Acceptable per client: "keep stdlib logger only for... true local fallback" |
| "not fully Aura for everything yet" | After this PR, every runtime log in every helper goes through MikAura when available |

### Design Decisions (Closed)
- **Pattern**: `_finder_*()` / `_vip_*()` wrappers matching `pipeline_info_helper.py`. `status_logger` is optional (backward compat) but always passed in prod.
- **Gold standard**: `stale_data_check` — MikAura required, no fallback. Other lambdas converge over time; this PR eliminates the two **fully stdlib** modules first.
- **Constructor injection**: `status_logger` passed to `__init__()`, stored as `self._status_logger`.

## Changes

### Phase 1 — `tracer_file_finder.py` → MikAura primary
- Add MikAura import guard + `_finder_*()` wrappers
- Add `status_logger` param to `TracerFileFinder.__init__()` + all methods
- Replace all 21 `logger.*()` calls with `_finder_*(self._status_logger, ...)`

### Phase 2 — `s3_utils.py` → MikAura primary
- Add `_vip_*()` wrappers
- Add `status_logger` param to `VIPDataBucket.__init__()`
- Replace all 14 `self.logger.*()` calls with `_vip_*(self._status_logger, ...)`

### Phase 3 — Snyk scan + docs update

## Status: EXECUTING
