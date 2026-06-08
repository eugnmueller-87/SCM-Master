"""Agent safety evaluation harness.

Test-only. Drives the REAL deterministic placement/sourcing logic with ONLY the
Claude call stubbed, so it runs offline (no ANTHROPIC_API_KEY) and is CI-safe.
Feeds the agent both reasonable and HOSTILE LLM advice and asserts the
deterministic layer holds the line ("LLM advises, deterministic code decides").
"""
