# Security Policy

## Threat model

Squelch is designed to run on a **trusted home LAN** and intentionally has **no authentication**: anyone who can reach the HTTP port can tune the radio, record, and delete data. See the *Security* section of the README for deployment guidance (VPN or authenticated reverse proxy for remote access — never port-forward Squelch directly).

Vulnerabilities that matter despite that model include:

- Path traversal / arbitrary file read or write through any API parameter
- Anything that lets a LAN client execute code on the host
- XSS via broadcast-controlled data (RDS/HD Radio text is attacker-influenced by anyone with a transmitter)

## Reporting a vulnerability

Please report vulnerabilities privately via [GitHub Security Advisories](https://github.com/altrhombus/squelch/security/advisories/new) rather than opening a public issue. You should receive a response within a week.

## Supported versions

Only the latest release (and `main`) receives fixes.
