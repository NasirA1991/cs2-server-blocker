# Contributing

Thanks for helping improve CS2 Server Blocker. This is a small Linux project
with a privileged helper, so clear reproductions and careful changes matter
more than volume.

## Before you start

- Read the [README](README.md), [usage guide](docs/USAGE.md) and
  [design notes](docs/design.md) for the area you are changing.
- Check existing issues and pull requests before opening a new one.
- For a security problem, follow [SECURITY.md](SECURITY.md) instead of opening
  a public issue.
- Documentation-only fixes are welcome; they do not need a matching code change.

## Reporting a bug

Include:

- the CS2 Server Blocker version (`cs2blockctl version`),
- the distribution and version,
- whether the GUI or CLI is affected,
- the exact command or UI action,
- the output of `cs2blockctl doctor`, `sudo cs2blockctl doctor` and
  `cs2blockctl status`,
- relevant `journalctl` output, with personal data removed.

A minimal reproduction is more useful than a screenshot alone. See
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for the diagnostics the
project normally asks for.

## Development setup

The prerequisites and test commands are in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). In short:

```bash
git clone https://github.com/NasirA1991/cs2-server-blocker.git
cd cs2-server-blocker
make test
make test-netns
```

`make test-netns` uses an isolated network namespace and is safe for the host
firewall. If `ipset` is not installed, it runs the nftables path and reports
that the iptables path was skipped.

## Pull requests

- Keep the change focused. Separate documentation, refactoring and behaviour
  changes where practical.
- Do not change application behaviour in a documentation-only change.
- Add or update tests for behaviour changes. The project uses Python's
  `unittest` and the existing test helpers.
- Keep privileged operations in the helper. The GUI and CLI must not gain root
  execution beyond the documented helper/polkit path.
- Do not add a raw command, path, IP address or nftables snippet as helper
  input. The helper accepts validated POP codes, modes and documented options.
- Preserve the root-owned installed-code checks and the rule that only
  `inet cs2blocker` (or the `CS2BLOCKER` chain in iptables fallback mode) is
  modified.
- Update the man page, README and relevant docs when commands, options,
  dependencies or limitations change.
- Run `git diff --check` and the relevant tests before submitting.

A pull request should explain what changed, why it is needed, and how it was
tested. If a test cannot run on the contributor's machine, say so explicitly.

## Documentation contributions

- Verify commands against the implementation, not only against older docs.
- Keep the README an entry point; put detailed procedures in `docs/`.
- Use relative links for repository files and GitHub-compatible Markdown.
- Do not document planned features as if they already exist.
- Keep the existing technical warnings about ICMP versus SDR latency, the
  host-wide firewall and per-process scoping.
- `docs/phase0-research.md` is historical research; add a note rather than
  rewriting its measurements.

## Releases

Release artifacts are built by GitHub Actions, not by contributors. Do not add
local package binaries, generated archives or signing keys to a pull request.
The maintainer handles version bumps, tags and AUR publication as described in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

By contributing, you agree that your contribution is licensed under the
project's MIT license. See [LICENSE](LICENSE).
