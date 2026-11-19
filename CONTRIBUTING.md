# Contributing

Thanks for your interest. Bug reports, feature ideas and pull requests are welcome.

## Reporting bugs

Use the bug report form. Include the output of `reader-assistant --version` and `reader-assistant info`,
how you installed the program, your desktop and session (GNOME on Wayland), the exact steps and the
full error. Do not paste text you read aloud if it is confidential.

## Development setup

You need Linux with a Wayland session, [uv](https://docs.astral.sh/uv/), and the system packages:

```bash
sudo apt install wl-clipboard libportaudio2
```

Then, from a clone of the repository:

```bash
git clone https://github.com/kursluzz/reader-assistant.git
cd reader-assistant
uv sync --all-extras          # all dependencies, including the voices, sound output and window
uv run pytest                 # the tests need no voice models and no network
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Run the application from the clone:

```bash
uv run reader-assistant info          # configuration and paths
uv run reader-assistant serve         # the speech service, in the foreground
uv run reader-assistant serve --gui   # the window, as a client of the service
```

Voice models are not in the repository. To hear real speech, download them as described in the
[README](README.md#voice-model-files), or point `RA_MODELS_DIR` at a directory that holds them.

The code is in `src/reader_assistant/`. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) describes the
modules, the IPC protocol and the window; the tests are in `tests/`.

## Pull requests

- Keep them focused: one change per pull request.
- Add or update tests for behaviour changes. The tests use fake engines and fake sound output; do not
  make them depend on voice models or real audio.
- Update `CHANGELOG.md` under `## [Unreleased]` for user-visible changes, and the README or `docs/`
  where the behaviour or the installation changes.
- Do not commit voice models, audio samples, logs, configuration files or anything from your
  `~/.local/share/reader-assistant` directory.
- Do not change the IPC protocol (`docs/CLIPBOARD.md` and `docs/ARCHITECTURE.md`) without discussing
  it in an issue first; existing clients must keep working.
- CI must pass: tests on Python 3.12 and 3.13, lint and types, and the build.

## Versions and releases

Contributors do not change version numbers in pull requests. A maintainer releases like this:

1. On a clean `main`, run `uv run bump-my-version bump patch` (or `minor` or `major`, following
   Semantic Versioning). It updates the version in `pyproject.toml`, the README and `uv.lock`, moves
   `[Unreleased]` in `CHANGELOG.md` to a dated version section, and makes a local commit and the tag
   `v<version>`. Check the result with `git show`.
2. Push the commit and the tag: `git push origin main --follow-tags`. Only the tag starts the release
   workflow. Ordinary pushes and pull requests run only the CI workflow.
3. The release workflow checks that the tag matches `pyproject.toml`, builds the wheel and the sdist,
   and smoke-tests both. It then waits for a maintainer to approve the `pypi` environment, publishes to
   PyPI with trusted publishing, and creates the GitHub Release with the changelog section as its notes.

The first release, 0.1.0, is the one exception: its version is already set, so `bump-my-version` is not
used. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [0.1.0] - <date>`, keep an empty
`## [Unreleased]` above it, commit, and create the tag with `git tag -a v0.1.0 -m "Release 0.1.0"`.

To preview a bump without changing anything: `uv run bump-my-version bump minor --dry-run --allow-dirty`.

## Licence

Reader Assistant is licensed under GPL-3.0-or-later (see [LICENSE](LICENSE)). By contributing you agree
that your contribution is licensed under the same licence. Voice models have their own licences and are
not part of this repository: the Hebrew voice is for non-commercial use only. Do not add a model without
checking its licence and recording it in [docs/ENGINES.md](docs/ENGINES.md).
