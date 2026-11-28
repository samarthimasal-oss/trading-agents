# Rollback runbook

Use this when a config change, code update or state restore must be undone.
Run every command from the repository root. The system is PAPER/dry-run by
default; never force-push or reset a shared branch.

## 1. Stop new risk first

Stop the runner from the dashboard or `POST /api/v1/runner/stop`. If a runner
is running in a terminal, `Ctrl+C` is enough. Do not copy or delete `state/`
while it is still writing.

If positions are open and the change may affect exits, flatten them from the
Risk Centre (or `POST /api/v1/risk/emergency-close` with the confirmation
phrase) **before** restoring state. A rollback must never leave an old position
unmanaged.

## 2. Restore the config

The tuner writes `config.json.bak_pre_tune` before any accepted change. The
pre-upgrade backup is under `backups/2026-11-02/config.json`.

```powershell
# prefer the most recent tuner backup
Copy-Item config.json.bak_pre_tune config.json

# or the pre-upgrade backup
Copy-Item backups/2026-11-02/config.json config.json

python -m pytest tests/test_config_schema.py tests/test_rollback.py -q
```

`config.json` is local and never committed. If neither backup exists, restore
the values from the last known-good settings export; do not reconstruct them
from a report, because reports record only a config hash.

## 3. Roll back code

Prefer a revert commit so the history stays auditable:

```powershell
git log --oneline -5
git revert <bad-commit>
git push origin upgrade/performance-reliability
```

To run an older release, switch to its tag/branch instead of resetting:

```powershell
git switch <tag-or-branch>
python -m pytest -q
```

Do **not** use `git reset --hard`, `git clean -fdx`, or a force-push without
explicit approval. Those can destroy local config/state that is not tracked.

## 4. Restore state and guardian lockout

Stop the runner, make a safety copy of the current state, then restore only the
files that belong to the rollback:

```powershell
Copy-Item state/agents.sqlite state/agents.sqlite.before-rollback
Copy-Item state/live.json state/live.json.before-rollback
Copy-Item state/executor_state.json state/executor_state.json.before-rollback

Copy-Item backups/2026-11-02/state/agents.sqlite state/agents.sqlite
Copy-Item backups/2026-11-02/state/live.json state/live.json
Copy-Item backups/2026-11-02/state/executor_state.json state/executor_state.json
```

`state/agents.sqlite` contains the journal and the persisted guardian row. If
the restored row is an old lockout that should not apply today, flatten any
open positions first and clear it from the Risk Centre or:

```powershell
# authenticated; requires the dashboard token and mutation permission
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/v1/risk/reset-guardian
```

Token files (`state/ea_token.txt`, the dashboard token) are credentials. Restore
them only if the EA/dashboard clients still use the same values; otherwise
regenerate the EA token with `python main.py ea-token --force` and update the
dashboard token in its local config.

## 5. Verify before restarting

```powershell
python -m pytest -q
python scripts/audit_secrets.py
git status -sb
```

`python main.py check` additionally requires a running MT5 terminal and is the
final connectivity/symbol check. Start the runner in PAPER mode first and watch
the funnel before considering any live mode.

## 6. Drill record

The config and guardian-state restore paths are covered by
`tests/test_rollback.py` (2026-11-28): a patched config is restored byte-for-byte
from `config.json.bak_pre_tune`, and a guardian lockout is restored from a
copied `agents.sqlite`. The MT5/EA mid-trade rollback drill still requires the
terminal and an attached `TradingAgentsEA`; it is not claimed as tested.
