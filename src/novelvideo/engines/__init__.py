"""Local-first media and text engines (ported from MacGen's adapters).

- Video: Higgsfield CLI (cloud, default) or h3.c (local, Metal).
- Image: Draw Things HTTP API (local, default) or Higgsfield CLI (cloud).
- Text: MTPLX (local OpenAI-compatible server) or OpenRouter.

Engine binaries, URLs and models are environment configuration, never
constants in callers.
"""
