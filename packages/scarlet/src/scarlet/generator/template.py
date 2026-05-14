"""CLAUDE.md skeleton template.

The default template Scarlet uses when no project-specific template is
provided in `.scarlet.yml`. Sections marked with `<!-- BEGIN MANUAL -->`
/ `<!-- END MANUAL -->` are preserved across regenerations; auto sections
are refreshed every time.
"""

DEFAULT_CLAUDE_MD_TEMPLATE = """# Feature: {feature_display_name}

<!-- BEGIN MANUAL: description -->
{description_placeholder}
<!-- END MANUAL: description -->

<!-- BEGIN MANUAL: vocabulary -->
## Vocabulary

<!-- Domain terms specific to this feature. AI/humans fill this in. -->
<!-- END MANUAL: vocabulary -->

## Public API (import from `{import_alias}`)

<!-- BEGIN AUTO: public_api -->
{public_api}
<!-- END AUTO: public_api -->

## Key files

<!-- BEGIN AUTO: key_files -->
{key_files}
<!-- END AUTO: key_files -->

<!-- BEGIN MANUAL: conventions -->
## Conventions and patterns

<!-- Invariants, idioms, and "how things are done" in this feature.
     Include the *why* for every "don't do X" rule so future devs can
     judge edge cases. -->
<!-- END MANUAL: conventions -->

## Consumers

<!-- BEGIN AUTO: consumers -->
{consumers}
<!-- END AUTO: consumers -->

<!-- BEGIN MANUAL: common_tasks -->
## Common tasks

<!-- Step-by-step recipes for the most frequent operations on this feature.
     Example: "Adding a new field to a quote: 1. Add to FastAPI model, 2. ..." -->
<!-- END MANUAL: common_tasks -->

<!-- BEGIN MANUAL: gotchas -->
## Known issues and gotchas

<!-- Non-obvious invariants, deprecated patterns, subtle race conditions.
     Every "don't fix it" must include the *why*. -->
<!-- END MANUAL: gotchas -->

## See also

<!-- BEGIN AUTO: see_also -->
{see_also}
<!-- END AUTO: see_also -->

<!-- Last synced: {timestamp} -->
<!-- Regenerate: scarlet describe {feature_name} -->
"""

DEFAULT_DESCRIPTION_PLACEHOLDER = "<!-- One-paragraph description of what this feature does. -->"
