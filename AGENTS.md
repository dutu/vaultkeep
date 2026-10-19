# Agent instructions

# Project Documentation

## README — User Guide

Treat `README.md` as the primary operational user guide, not merely a documentation index.

Organize instructions in the order users need them:

1. Overview and purpose.
2. Requirements and prerequisites.
3. Installation.
4. Configuration and validation.
5. Starting and enabling the application.
6. Health checks and verification.
7. Everyday operation and maintenance.
8. Troubleshooting.
9. Updating to a newer version.
10. Uninstallation and optional data cleanup.

Adapt these sections to the functionality actually implemented.

- Provide numbered procedures with executable commands in the correct order.
- Include expected results and verification steps where useful.
- Make standard installation and operation possible without navigating multiple documents.
- Keep explanations concise and focused on user actions.
- Link to detailed references from the relevant sections.
- Do not mix architecture, implementation plans, design discussions, or development history into the README.
- During early development, document only currently supported procedures and clearly identify unavailable functionality.

## Supporting Documentation

Keep detailed documentation in separate topic-specific Markdown files under `docs/`.

- Update existing topic files when functionality changes; create new files only for genuinely new topics.
- Keep detailed configuration, CLI, troubleshooting, and advanced operational references separate from the README.
- Avoid duplicating extensive reference material in the README.
- Use relative links between related documents.

Suggested organization:

- `docs/configuration.md` — Complete configuration reference.
- `docs/cli.md` — Complete command reference.
- `docs/troubleshooting.md` — Detailed troubleshooting.
- `docs/design/` — Architecture, technical designs, and significant design decisions.
- `docs/plans/` — Implementation plans, task records, and design proposals.
- `docs/development/` — Developer setup, testing, and contributing instructions.

Create files and directories only when their content justifies them. Do not generate empty documentation structures.

## Development Documentation

- Maintain implementation plans and design documents separately from user instructions.
- Use topic-specific documents during development to capture technical details without expanding the README unnecessarily.
- Keep final designs and plans as settled specifications, not records of approval discussions.
- When functionality becomes available, update the relevant user documentation.
- Do not expose internal implementation details in the user guide unless users need them to operate or troubleshoot the application.

## Documentation Maintenance

- Keep README instructions synchronized with actual commands, options, paths, and supported behavior.
- When updating functionality, update affected documentation rather than adding a duplicate explanation elsewhere.
- Preserve useful technical reference details when simplifying the README.
- Verify local links and anchors after reorganizing documentation.
- Avoid unrelated documentation rewrites.

**Guiding principle: The README explains how to use the application; supporting references explain its options and operation; design and development documents explain how it is built.**