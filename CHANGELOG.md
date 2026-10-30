# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `options.watermark` stamps a draft on every format: a diagonal text over each
  PDF page, Word's text watermark in each .docx header, the print header and
  footer of each .xlsx sheet, a fixed element in HTML. The result echoes the
  watermark it applied (`watermark`), so a caller can tell a stamped draft from a
  render by an older worker that ignored the option.
- The result names what produced the bytes (`renderer`: Python, zlib and the
  rendering libraries' versions), so a caller regenerating a kept artifact can
  tell a reproducibility bug from an upgraded library.

### Changed

- The Docker base image pins its Debian release (`python:3.12-slim-trixie`):
  zlib compresses every .xlsx/.docx, and a silent jump to the next release
  would change the bytes of renders that callers keep as evidence.
- .xlsx/.docx members are written as Unix entries on every host. The container
  already did; a Windows developer machine now produces the same bytes.

### Fixed

- A change to the worker's consumer settings (`DOCGEN_ACK_WAIT_SECONDS`, the
  `max_deliver` backstop, `max_ack_pending` from the render pool size) now
  reaches the existing JetStream consumer when the worker restarts. nats-py
  applies them only when it creates the durable, so until now a new value
  needed the consumer deleted by hand. The worker logs the old and new values.
