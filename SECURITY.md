# Security Policy

## Supported versions

Security fixes are made on the latest release and the `main` branch. Older releases may not receive separate patches.

| Version | Supported |
|---|---|
| 1.0.x | Yes |
| Older versions | No |

## Reporting a vulnerability

Please do not open a public issue for a suspected security vulnerability. Use GitHub's **Report a vulnerability** button on the repository's Security page instead:

<https://github.com/The-Sequence/aero-on-apple-silicon/security/advisories/new>

Include the affected version, macOS and guest Windows versions, reproduction steps, the expected and observed behavior, and any relevant logs. Remove passwords, license keys, personal paths, and other sensitive information from reports and logs.

You should receive an acknowledgement within seven days. Confirmed issues will be tracked privately until a fix or mitigation is ready.

## Scope

Reports about this project's scripts, patches, prebuilt runtime, guest helper tools, or update/download verification are in scope. Vulnerabilities in upstream QEMU, DXVK, MoltenVK, VMware software, Microsoft Windows, Homebrew, or SPICE should also be reported to the appropriate upstream project.
