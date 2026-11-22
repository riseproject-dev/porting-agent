# Repository process

These rules are mandatory for every change to this repository.

- Update `docs/` in the same change as any implementation change, so the docs stay accurate to the code.
- Record every known place where the code does not yet follow the lifecycle table in `docs/state-machine/known-gaps.md`, and remove the entry in the change that fixes it.
- Keep the website free of milestones and other temporary implementation-plan details. Describe current behavior.
- Never mention AI, assistants, or tools in commit messages, and never add Co-Authored-By or session trailers.
- Never use the U+2014 character.
- Fix the pipeline, never the resources it manages by hand.
