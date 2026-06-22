"""Attribution and licensing for the data travelplanner auto-fetches.

travelplanner bundles no third-party data; it downloads it at runtime. Two open
datasets back the auto-sourced timetable, and both require attribution when their
data is used or redistributed:

- OpenFlights (airports + route network), under the Open Database License (ODbL).
- GTFS schedule feeds discovered through the Mobility Database catalog; each feed
  is under its own license (the publishing agency's), recorded per feed.

This module is the single place that records those obligations, so the CLI can
print them (`travelplanner attribution`) and a downstream application embedding
the planner can render the same notice (`data_sources` + `render`).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Attribution:
    """One data source's credit: who to name, the license, and where to find it."""

    name: str
    license: str = ""
    source_url: str = ""
    license_url: str = ""

    def line(self) -> str:
        """A compact one-line credit (name, license, most specific URL)."""
        out = self.name
        if self.license:
            out += f": {self.license}"
        url = self.license_url or self.source_url
        if url:
            out += f" <{url}>"
        return out


# The flight network. OpenFlights' airport/route databases are published under
# the Open Database License; using or redistributing the data requires crediting
# OpenFlights and keeping any derived database under the same terms.
OPENFLIGHTS = Attribution(
    name="OpenFlights",
    license="Open Database License (ODbL) v1.0",
    source_url="https://openflights.org/data.html",
    license_url="https://opendatacommons.org/licenses/odbl/1-0/",
)

# The GTFS feed catalog. MobilityData publishes the catalog metadata under CC0;
# the feeds it points to each carry the publishing agency's own license, which is
# the credit that varies per trip (see feed_attribution).
MOBILITY_DATABASE = Attribution(
    name="Mobility Database (MobilityData) -- GTFS feed catalog",
    license="catalog metadata under CC0 1.0; each feed under its own license",
    source_url="https://mobilitydatabase.org/",
    license_url="https://creativecommons.org/publicdomain/zero/1.0/",
)

# The road network. Driving routes (the `drive` command and road=True access legs)
# run over OpenStreetMap extracts fetched from Geofabrik; OSM data is under the
# ODbL and must credit "OpenStreetMap contributors".
OPENSTREETMAP = Attribution(
    name="OpenStreetMap contributors (extracts via Geofabrik)",
    license="Open Database License (ODbL) v1.0",
    source_url="https://www.openstreetmap.org/copyright",
    license_url="https://opendatacommons.org/licenses/odbl/1-0/",
)


def feed_attribution(feed) -> Attribution:
    """Credit for one catalog feed: the publishing agency and its license URL.

    Reads the catalog Feed's public fields (provider/name/country/url/license_url)
    without importing transit_catalog, so this stays the single licensing module.
    The license itself lives with the feed publisher; the catalog records a license
    URL for some feeds, not all.
    """
    name = feed.provider or feed.name or f"feed {feed.id}"
    if feed.country:
        name = f"{name} ({feed.country})"
    return Attribution(
        name=name,
        license="",                 # the agency's own terms; URL below when listed
        source_url=feed.url,
        license_url=feed.license_url,
    )


def data_sources(*, feeds=None, air: bool = True, ground: bool = True,
                 road: bool = True) -> list[Attribution]:
    """The attributions for the data sources a trip drew on.

    `feeds` is the catalog feeds actually selected (each adds its own credit);
    `air`/`ground`/`road` say which line-haul/road data the result used, so a trip
    credits only the datasets it actually drew from (a flight-only result does not
    credit OpenStreetMap).
    """
    out: list[Attribution] = []
    if air:
        out.append(OPENFLIGHTS)
    if ground:
        out.append(MOBILITY_DATABASE)
        for f in feeds or ():
            out.append(feed_attribution(f))
    if road:
        out.append(OPENSTREETMAP)
    return out


def render(attribs) -> str:
    """A readable multi-line credit block for a list of attributions."""
    blocks: list[str] = []
    for a in attribs:
        lines = [a.name]
        if a.source_url:
            lines.append(f"    source:  {a.source_url}")
        if a.license and a.license_url:
            lines.append(f"    license: {a.license}  {a.license_url}")
        elif a.license_url:
            lines.append(f"    license: {a.license_url}")
        elif a.license:
            lines.append(f"    license: {a.license}")
        else:
            lines.append("    license: not listed in the catalog; "
                         "see the feed publisher")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
