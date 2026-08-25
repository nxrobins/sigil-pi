# Security reporting

sigil-pi has no generally available supported release yet. `0.3.0` (like every release before
it) is an internal-alpha release and does not carry a production response SLA.

Do not disclose a suspected vulnerability in a public issue. Use the repository's private
vulnerability-reporting channel under the GitHub **Security** tab. Include the affected
version or artifact digest, deployment topology, prerequisites, reproduction, impact, and
whether customer data or credentials may have been exposed. Do not include live credentials
or customer content.

The maintainer receiving a report must acknowledge it privately, preserve evidence, assign a
severity and owner, and coordinate remediation/disclosure before public discussion. A v1.0
release remains blocked until a named security owner, response targets, escalation contacts,
and an exercised incident process are approved in the release sign-off.

The v1 security boundary and known blocking risks are documented in
`docs/security/threat-model.md`. The single-token legacy research endpoint (no tenant isolation), arbitrary
shell/host access, in-guest secrets, memory sidecars, multi-host state, and network
filesystems are outside the supported product boundary.
