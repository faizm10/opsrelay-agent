# Security policy

## Reporting a vulnerability

Please don't open a public issue. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/faizm10/opsrelay-agent/security/advisories/new)
with the affected component, steps to reproduce and the impact you expect. You'll get an
acknowledgement within a few days, and credit in the fix unless you'd rather not.

## Supported versions

Only `main` is supported; fixes land there and deploy from it.

## Scope notes

- The repair agent opens pull requests but never merges them. A report that makes it merge, push to
  a protected branch or leak repository secrets is in scope.
- The dashboard is multi-tenant: reading or changing another workspace's sites, incidents,
  repositories or Better Stack settings is in scope.
