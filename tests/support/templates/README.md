The `generic`, `htmx`, and `form_helpers` HTML fixtures are snapshots of NetBox's
native object edit and form rendering templates. They allow the standalone Django
test harness to exercise the same template inheritance and field rendering as
NetBox without requiring a complete NetBox installation. Minimal compatible tags
are provided by `tests.support.form_helpers`.

Production uses NetBox's installed templates; these fixtures are not packaged with
the plugin. The standalone `base/layout.html` only provides the page blocks needed
by the tests and does not reproduce NetBox's navigation or styling.
