"""Everything that draws, and nothing that collects.

Four modules, split along the seams the single file already had: `theme` for
the two questions every panel asks (which box characters, and is colour
wanted), `chart` for the plot and its arithmetic, `panels` for the individual
regions, and `dashboard` for the height budget that decides which of them fit.

The dependency runs one way. `ui/` reads `config`, `store`, `timeutil` and
`logging_setup`; nothing under it imports `poller`, `ga4` or `commands`. That
is the same rule from the other side: `PollerSet` deliberately has no UI
dependency so a future headless `collect` can reuse it whole, and a UI that
imported the collector would close the circle by hand.

Nothing here prints. **stdout belongs to the Live region** -- these modules
return renderables and the caller decides what happens to them, because a
single stray line written from inside a render tears the display for the rest
of the run.
"""
