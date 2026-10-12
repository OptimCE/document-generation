# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A change to the worker's consumer settings (`DOCGEN_ACK_WAIT_SECONDS`, the
  `max_deliver` backstop, `max_ack_pending` from the render pool size) now
  reaches the existing JetStream consumer when the worker restarts. nats-py
  applies them only when it creates the durable, so until now a new value
  needed the consumer deleted by hand. The worker logs the old and new values.
