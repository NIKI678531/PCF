# AGENTS.md

## Purpose
This file provides essential guidance for AI coding agents working in this repository. It summarizes project structure, conventions, and actionable tips to ensure agents are immediately productive.

---

## Project Overview
- **Domain:** PCF (Portfolio Composition File) generation and management
- **Main entry point:** `main.py`
- **Core modules:**
  - `pcf_master.py`: Orchestrates DB connections, PCF status, data sync, and generation
  - `pcf.py`: Abstract base class for PCF types
  - `lni_pcf.py`: Implements leveraged and inverse fund PCFs
  - `scripts/scan_logic_bugs.py`: Static LLM logic review and report generation

## Key Conventions
- **Logging:** Use `logging.getLogger('Main')` for consistent logging
- **Database:** Use `pymssql` for SQL Server connections; DB names managed via Enum
- **Inheritance:** Extend `Pcf` for new fund types; override sync/generation methods
- **Scheduling:** Use `schedule` library for task scheduling

## Pitfalls & Gaps
- No requirements.txt or dependency management file
- Logic scan workflow, scanner regression tests, and dividend reader tests exist; other business generation logic still lacks automated tests
- Workflow usage: [docs/logic-bug-scan.md](docs/logic-bug-scan.md)
- Hardcoded log file path in `main.py`
- Placeholder logic in several methods

## Actionable Guidance for Agents
- If adding dependencies, create a `requirements.txt` and document them
- If adding new fund types, subclass `Pcf` and override required methods
- Avoid hardcoding paths or sensitive info; use config or environment variables
- If adding documentation, prefer linking to files (README, docs/) instead of duplicating content here
- If adding tests, use `pytest` or `unittest` and place in a `tests/` directory
- The logic scanner uses only the Python standard library; keep its tests offline and never import or execute the production application during a scan

---

**Update this file if project conventions or structure change.**
