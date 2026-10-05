# Security policy

Governed AI Gateway is built around security controls, so reports about them are welcome and taken seriously.

## Reporting a vulnerability

Please report privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Do not open a public issue or pull request for a security problem.

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement within 5 business days and a fix or mitigation plan once the report is confirmed. Reporters are credited in the changelog unless they prefer not to be.

## Supported versions

Only the latest release (currently 0.7.x) receives security fixes.

## In scope

- Bypassing budgets, tokens-per-minute limits, spend-anomaly pauses, or policy-as-code rules
- Using another team's or app's key, or recovering API keys from storage
- Sensitive content reaching a provider or the logs past the PII hooks, or being logged when content logging is off
- Tampering with the audit trail undetected
- MCP tool calls outside a team's allow-list or velocity limits

## Out of scope

- The hosted demo console's simulated services. It runs in your browser against fakes, with no real accounts or data.
- Vulnerabilities in third-party dependencies with no impact on this project's use of them (report those upstream).
- Findings that need an already-compromised host, admin credentials, or a deliberately insecure configuration.

The design assumptions and known gaps are documented in [docs/threat-model.md](docs/threat-model.md).
