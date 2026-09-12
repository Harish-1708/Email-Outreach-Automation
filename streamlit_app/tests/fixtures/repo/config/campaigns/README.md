# Fixture campaign overrides

Deliberately EMPTY of per-campaign YAML files.

`Sample_Campaign` is intentionally "unconfigured" — it has no override
file — so it exercises the default-settings path that several page tests
assert against (schedule defaults, a status-only commit payload, etc.).

If a test needs a campaign WITH overrides, add a new fixture campaign
(templates folder + its own YAML here) rather than giving Sample_Campaign
an override file, which would silently change what those tests exercise.
