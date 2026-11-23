# Security policy

This is a portfolio/demo project, not a hardened production service. Please report vulnerabilities privately via
GitHub's "Report a vulnerability" (Security Advisories) rather than a public issue.

Design guarantees and their tests are listed in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md); known limitations are listed there too.
Never commit credentials; the default passwords in `docker-compose.yml` are for local use only (services bind to 127.0.0.1).
