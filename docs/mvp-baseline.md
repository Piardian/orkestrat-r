# MVP Baseline

- Baseline commit: `7ea9518e48e7cf13685ddb887f623e10100d7110`
- Baseline tag: `stable-baseline-pre-goal-mvp`
- Active branch: `mvp-goal-system`

## Rollback Commands

### 1. Temporarily return to stable baseline

```powershell
git checkout stable-baseline-pre-goal-mvp
```

### 2. Return to the MVP branch

```powershell
git checkout mvp-goal-system
```

### 3. Discard incorrect local changes

```powershell
git restore .
git clean -fd
```

### 4. Create a recovery branch from the baseline tag

```powershell
git checkout -b recovery-from-baseline stable-baseline-pre-goal-mvp
```

## Notes

- The stable baseline was verified with compile and smoke checks before tagging.
- `.env`, runtime outputs, caches, and generated artifacts are kept out of Git by `.gitignore`.
- No changes were made to the working agent-core behavior in this phase.
- Goal IDs are generated using UTC date stamps, not local time.
