# Contributing to the ChurchBridge platform

Thanks for your interest. This covers the platform repository — the web client,
FastAPI backend, translation pipeline, sanctuary display, and mobile listener.

## What is most useful

**Field reports.** If you are running ChurchBridge in an actual service, that is
the most valuable thing you can send. What broke, what the captions did that
they shouldn't have, what the room was like, what the audio source was.
Sanctuary conditions are hard to reproduce and impossible to invent.

**Issues and questions.** Bug reports, deployment problems, and questions about
pipeline behaviour are all welcome in the issue tracker.

**Discussion before code.** For anything beyond a small fix, open an issue
first. The ordering and gating rules in the pipeline are load-bearing in ways
that are not obvious from a single file — `display_ready`, the caption merge
chain, and the deferred-release timers interact, and a change that looks local
often is not. A conversation up front saves rework.

## Code contributions and the CLA

ChurchBridge is deliberately kept under **single copyright ownership**. That is
what makes it possible to publish the source under a noncommercial license while
still licensing it commercially, and what keeps the project cleanly
transferable.

Accepted code contributions therefore require a **contributor license agreement
assigning copyright in the contribution to Daniel Haines**. Without it, a merged
pull request would make the contributor a co-owner of part of the codebase, and
neither the noncommercial license nor any commercial license could be offered
cleanly over the whole.

Say so on the issue and the CLA will be sent. Please don't open a pull request
with substantial code before it is in place — it cannot be merged, and that is a
frustrating way for both of us to find out. This is the same arrangement most
single-owner source-available projects use.

## Running the tests

Server tests, from the repository root:

```
server\.venv\Scripts\python.exe -m pytest tests\server -q
```

Benchmark harness tests:

```
server\.venv\Scripts\python.exe -m pytest tests\benchmark -q
```

Web client end-to-end tests, from `client/`:

```
npm run test:e2e
```

`TESTING_AND_BENCHMARKS.md` has the full picture, including environment setup,
focused Playwright runs, and the pipeline benchmark.

## Things that must not enter this repository

- **Credentials.** `.env` is ignored and must stay that way. Configuration is
  documented by variable *name* only; values never belong in the repo or in
  documentation.
- **Recordings.** Captured audio, live session recordings, and benchmark
  captures are ignored. Service audio contains real congregations and real
  preaching, and it does not belong in version control.
- **Model weights and Bible data.** Both come from outside the repository. See
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
- **Generated logs and temporary artifacts.** If something appears under
  `logs/` or as `tmp_*`, it should be ignored rather than committed.

If you add a dependency, add it to the third-party notices in the same change.

## Licensing questions

Questions about what the license permits belong in
[`LICENSE-FAQ.md`](LICENSE-FAQ.md) or an issue. Commercial licensing inquiries
should go to the contact in the README, not the issue tracker.

## Security

Do not report security issues in public issues. Use GitHub's private
vulnerability reporting on this repository.
