# Master Data db-seed image carrying the OAN country packs.
#
# OpenG2P's db-seed image bakes openg2p-data into /openg2p-data at build time,
# and the chart's geo-seed job loads geoSeed.countryPack from it. That image
# carries upstream's packs only, so this one is the same image with this
# repository in /openg2p-data instead: the loader, its scripts and their
# Python packages are upstream's, unchanged, and only the packs differ.
#
# Build from a clean checkout at the commit to ship, from the repository root:
#
#   docker build -f docker/master-data-db-seed/Dockerfile \
#     --label org.openg2p.pin.openg2p-data-branch=$(git rev-parse HEAD) \
#     -t <registry>/master-data-db-seed:<base tag>-oan.<n> .
#
# BASE is the upstream image matching the Master Data API that is deployed, by
# digest, so the loader always matches the API's schema. Change both together.
ARG BASE=openg2p/master-data-db-seed:0.0.0-develop.118@sha256:cfa91c1bcc1798a8a82454073ac5454c8099041511bf6d0c7380b492232d809f
FROM ${BASE}

RUN rm -rf /openg2p-data
COPY . /openg2p-data
