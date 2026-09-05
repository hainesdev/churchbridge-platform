# Third-party notices

ChurchBridge includes and depends on third-party software and assets. This file
records what they are and the terms under which they are redistributed.

ChurchBridge's own code is licensed separately — see [`LICENSE`](LICENSE) and
[`LICENSE-FAQ.md`](LICENSE-FAQ.md). Nothing in this file changes those terms, and
the ChurchBridge license does not apply to the components listed here.

> **This inventory is incomplete.** It currently covers only assets redistributed
> directly in this repository. Runtime dependencies pulled from package managers
> (Python, npm) and the Apple frameworks used by the iOS app are not yet
> enumerated. Completing it is a prerequisite for making this repository public.

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

## Still to enumerate

Before publication, this file needs to cover:

- **Python dependencies** — FastAPI and the rest of the server requirements.
- **JavaScript dependencies** — Next.js, React, and the client package tree.
- **Apple frameworks and iOS app assets** — see the `churchbridge-ios`
  repository.
- **Bible texts.** Public-domain translations (ASV, KJV, WEB, RVA) can be
  attributed here. **RVR1960 is copyrighted** (Sociedades Bíblicas Unidas /
  American Bible Society) and is deliberately not redistributed in this
  repository; source Bible data is gitignored. Any arrangement covering
  server-side use of RVR1960 belongs in this file once settled.

## Third-party tooling, not redistributed

Used in development but not included in this repository or any build:

- **Docker-OSX** (https://github.com/sickcodes/Docker-OSX) — macOS
  virtualization used to run Xcode for iOS builds. Upstream project, used as-is,
  not forked or rebranded.
