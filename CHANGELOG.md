# Changelog

[简体中文](CHANGELOG.zh-CN.md)

BFBT keeps a behavior-oriented changelog. Release dates use UTC.

## Unreleased

### Added

- Controlled Showcase contracts, ResearchIntent semantic freeze, and action-class boundaries.
- Read-only `bfbt doctor` and verified `showcase inspect/build/prepare` workflows.
- Multi-period static presentation, cost/risk/provenance qualifications, and opening-margin paths
  derived from immutable runs.
- A39 offline acceptance, presentation assets, and public contribution/security documentation.
- Independent English and Simplified-Chinese HTML artifacts plus the A40 public-release contract.
- Hosted, self-contained English and Simplified-Chinese examples for all three report layers.

### Changed

- The root README now describes the A01–A40 architecture, implemented capabilities, bounded
  Showcase, and explicit exclusions.
- Brand, Python distribution, import namespace, CLI, and repository links migrated completely to
  BFBT/`bfbt`; English is the default public entry point with separate Chinese documents.
- Public contribution, security, changelog, documentation-map, and Showcase-preview surfaces use
  English-first presentation with linked Simplified-Chinese counterparts where applicable.
- The README research architecture now uses dedicated static English and Chinese editorial diagrams
  with a vertical hierarchy and an explicit human decision gate.
- Python 3.10 compatibility now uses an explicit `StrEnum` fallback and `timezone.utc`, matching the
  declared interpreter floor and CI matrix.
- Report localization preserves executable JavaScript, embedded JSON, CSS, and preformatted code;
  the public README and Showcase tour link directly to the corresponding hosted report language.

## 0.1.0

- Standalone repository baseline.
- Quick Research, Fast Matrix, Event Engine, bounded-memory recovery, immutable artifacts, and
  interactive audit reports.
