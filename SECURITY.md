# Security Policy

## Supported versions

This is a small single-purpose tool with no releases yet; only the latest
`main` is supported.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report it privately through GitHub's **Report a vulnerability** button under
this repository's **Security** tab (Security advisories → Private vulnerability
reporting). This keeps the report confidential until a fix is available.

Because `journalr` parses untrusted image files (PNG/JPEG) and produces
documents that may hold sensitive personal writing, the reports most valued
are: parser crashes or out-of-bounds reads on malformed images, any path where
entry content or metadata leaks outside the intended `0600` file, and anything
that weakens the offline / no-network guarantee.
