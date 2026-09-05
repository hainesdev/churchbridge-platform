# Third-party notices

ChurchBridge includes and depends on third-party software and assets. This file
records what they are and the terms under which they are redistributed.

ChurchBridge's own code is licensed separately — see [`LICENSE`](LICENSE) and
[`LICENSE-FAQ.md`](LICENSE-FAQ.md). Nothing in this file changes those terms, and
the ChurchBridge license does not apply to the components listed here.

## Redistributed assets

### DeepFilterNet3 (Core ML model)

- **Source:** https://huggingface.co/aufklarer/DeepFilterNet3-CoreML
- **Author:** aufklarer
- **License:** Apache-2.0
- **Base model:** [Rikorose/DeepFilterNet3](https://github.com/Rikorose/DeepFilterNet),
  © 2021 Hendrik Schröter, MIT or Apache-2.0 at the user's option
- **Location:** `client/public/models/DeepFilterNet3-CoreML/`
- **Full license text:** [`client/public/models/DeepFilterNet3-CoreML/LICENSE`](client/public/models/DeepFilterNet3-CoreML/LICENSE)

Used for real-time speech enhancement. The platform serves these assets to the
iOS app at runtime; the iOS repository does not contain a copy.

These are an INT8-palettized Core ML conversion of DeepFilterNet3 published by a
third party, redistributed here **unmodified** — byte-for-byte identical to the
upstream files. The conversion is aufklarer's work, not ChurchBridge's.

The original authors ask that use of the DeepFilterNet3 model be cited:

> Schröter, H., Rosenkranz, T., Escalante-B., A. N., and Maier, A.
> "DeepFilterNet: Perceptually Motivated Real-Time Speech Enhancement."
> INTERSPEECH, 2023.

## Dependencies

Direct dependencies and their licenses, verified against PyPI and the npm
registry on 2026-09-05. **No dependency carries GPL or AGPL terms**, so none
conflicts with releasing the combined work under PolyForm Noncommercial.

### Python (`server/requirements.txt`)

| Package | License |
| --- | --- |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| websockets | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| deepgram-sdk | MIT |
| anthropic | MIT |
| redis | MIT |
| aioredis | MIT |
| aiosqlite | MIT |
| numpy | BSD-3-Clause, with bundled 0BSD / MIT / Zlib components |
| pydantic | MIT |
| python-multipart | Apache-2.0 |
| httpx | BSD-3-Clause |
| pydub | MIT |
| google-cloud-speech | Apache-2.0 |

### JavaScript (`client/package.json`)

| Package | License |
| --- | --- |
| next | MIT |
| react, react-dom | MIT |
| framer-motion | MIT |
| tailwindcss, @tailwindcss/postcss | MIT |
| @playwright/test | Apache-2.0 |
| eslint, eslint-config-next | MIT |
| typescript | Apache-2.0 |

### Scope and caveats

- This table covers **direct** dependencies. Transitive dependencies are not
  individually enumerated; the npm tree in particular is large. Anyone
  redistributing a built artifact should generate a full attribution report
  from the lockfiles.
- **ffmpeg is not a dependency of this repository**, but `pydub` shells out to
  it when converting audio. Depending on how it was built, a local ffmpeg may
  be GPL-licensed. It is invoked as an external program rather than linked or
  redistributed, so it does not affect this project's licensing — but anyone
  packaging ffmpeg *with* ChurchBridge would need to consider its terms.
- Apple frameworks used by the iPhone app are covered in the
  `churchbridge-ios` repository's own notices.

## Bible text

Public-domain translations (ASV, KJV, WEB, Reina-Valera Antigua 1909) carry no
restriction. **Reina-Valera 1960 is copyrighted** by Sociedades Bíblicas Unidas
/ American Bible Society. Source Bible data is not redistributed by this
repository; `data/source_bibles/` is gitignored and has never been committed.

## Benchmark source material

Sermon audio used as benchmark input is third-party copyrighted material and is
not distributed by this repository. `tests/audio/1`, `tests/audio/2`,
`tests/audio/3`, `tests/audio/captured/`, and `retrieved_live_recordings/` are
gitignored, and were removed from the repository's history on 2026-09-05.

## Third-party tooling, not redistributed

Used in development but not included in this repository or any build:

- **Docker-OSX** (https://github.com/sickcodes/Docker-OSX) — macOS
  virtualization used to run Xcode for iOS builds. Upstream project, used as-is,
  not forked or rebranded.
