# Themes

Staff-portal themes for the registries, seeded like every other piece of
reference data here: the registry's db-seed image checks this repository out and
renders the theme into SQL; nothing is baked into a UI image.

## `oan_theme`

One theme (`themes/oan_theme/theme.json`), one entry per registry (`farmer`,
`crop`, `livestock`, `da`). Each entry holds the registry's palette and its
background image (`images/<registry>.jpg`, JPEG, under 300 KB).

`scripts/themes/build_theme_sql.py <registry>` prints the SQL: it upserts the
`oan_theme` row in `registry_themes`, its values in `registry_theme_values` (the
image as a base64 data URI in the `dashboard_image` attribute) and points
`g2p_registry_configuration.registry_theme_id` at it. Ids are derived from the
theme id, so seeding twice changes nothing. `--check` validates every entry (CI).

The registry platform's theme model has no page-background attribute; the staff
portal paints its page background from a compiled stylesheet. `dashboard_image`
is the one image the theme can carry, and the portal shows it on the home page.

Provenance: the farmer, crop and livestock images are the backgrounds those
registries used to bake into their staff-ui images (crop was resized to 1200 px
wide); the DA image is a generated illustration.
