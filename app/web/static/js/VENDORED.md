# Vendored files

| File | Package | Version | License | SHA-256 of the file |
|---|---|---|---|---|
| `htmx.min.js` | htmx.org | 2.0.11 | 0BSD | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `tests/e2e/vendor/axe.min.js` (tests only) | axe-core | 4.14.0 | MPL-2.0 | `20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878` |

To update: download the package tarball from registry.npmjs.org, check it against the registry's
`dist.integrity`, copy the file here, and update this table. `tests/unit/test_vendored.py` checks
that the files match it. Stay on htmx 2.x unless the plan changes; htmx 4 is a new major version.
