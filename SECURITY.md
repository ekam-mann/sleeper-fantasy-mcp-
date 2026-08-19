# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
on this repository, rather than opening a public issue.

Include what you did, what happened, and what you expected. A proof of concept
helps but is not required.

## Scope

This project is a **read-only** client. It makes no write calls to any API — no
POST, PUT, PATCH or DELETE anywhere in the codebase — so it cannot modify a
league, submit a lineup, or make a transaction. The realistic risk surface is:

- **Credential handling.** The only secret is an optional OpenWeather API key
  (and an optional Anthropic key). Both load from environment variables or a
  gitignored `secrets.json`, never from source.
- **Dependencies.** The runtime surface is two packages: `mcp` and `httpx`.
- **Untrusted text.** `news_signals` reads third-party news prose. Treat any
  signal it returns as data, not instruction.

## Out of scope

Vulnerabilities in Sleeper, ESPN or OpenWeather themselves — report those to
the respective vendors.
