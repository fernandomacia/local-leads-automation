# CLAUDE.md — local-leads-automation

## Project Context

Lead generation tool for a web developer selling SEO repair services and custom WordPress themes. The goal is to find local businesses with poor websites and reach out with personalized messages.

**User profile:** Experienced web developer, comfortable with Python and automation. Not an expert in scraping or AI, but learns quickly.

## Target Pipeline

```
Google Maps → extract businesses
           → detect WordPress / analyze web quality
           → save email / instagram / website
           → generate personalized AI message
           → assisted manual outreach
```

## Module Architecture

```
main.py                      # Pipeline orchestrator (CLI entry point)
app.py                       # Streamlit dashboard — launches pipeline and shows results
scraper/
  maps_scraper.py            # Extracts lead, website, phone, address from Google Maps
  web_analyzer.py            # CMS detection, email/socials extraction, SEO scoring
ai/
  message_generator.py       # Generates personalized outreach emails via local LLM
api/
  client.py                  # Saves leads.json and optionally POSTs to external API
data/
  leads.json                 # Single output: all fields from all phases (flat JSON)
config.py                    # Constants and configuration (scraper, LLM, sender identity)
```

## Tech Stack

- **Python 3.13** with venv
- **Playwright** — Google Maps scraping (real browser to avoid blocks)
- **requests + BeautifulSoup** — lead website analysis
- **pandas** — data handling in the Streamlit dashboard
- **Streamlit** — web UI for running the pipeline and reviewing results
- **transformers + bitsandbytes** — local LLM inference (Qwen2.5-7B, 4-bit quantized)
- **python-dotenv** — environment variable management (`.env`)

---

## Working Standards

### Language
- Claude communicates with the user **in Spanish**
- `CLAUDE.md` and `README.md` are written **in English**
- All internal code documentation (docstrings, comments) is written **in English**

### Code Comments & Documentation
- **Language:** English
- **Style:** Concise, clear, professional, and modern. Avoid verbosity and redundancy. Document the *why*, not the *what*.
- **Standard:** Google Style Docstrings for Python
- Only document functions with non-trivial logic — trivial ones need no docstring
- Inline comments only when the *why* is non-obvious; never explain the *what*

```python
def analyze_website(url: str) -> dict:
    """Fetch and analyze a website for WordPress signals and SEO issues.

    Args:
        url: The website URL to analyze.

    Returns:
        A dict with keys: is_wordpress, email, instagram, seo_score.
    """
```

### Code
- Efficient, modern, and professional — leverage Python 3.10+ features
- Not verbose: prefer concise expressions over long blocks
- No unnecessary defensive error handling — errors should be visible for debugging
- No premature abstractions — if something is used once, a class is not needed
- Each module does one thing; `main.py` orchestrates

---

## Development Practices

### Before Starting Implementation
- **Understand the context** — Read existing code structure
- **Plan the approach** — Discuss architecture before coding
- **Follow patterns** — Use established conventions from the project
- **Security first** — Always sanitize/escape user input

### When Creating Features
- ✅ Create separate, modular files
- ✅ Use meaningful class/function names
- ✅ Add error handling (try-catch, validation)
- ✅ Document with concise comments
- ✅ Make code reusable and maintainable

### Checklist for Code Review
- [ ] Code is clean and professional
- [ ] English comments are concise and elegant
- [ ] Error handling is in place
- [ ] Files are properly organized following the feature structure
- [ ] Functions have a single responsibility

### Work Sessions
- Build in phases: make it work first, then polish
- Current phase: **Phase 4 complete — CLI + Streamlit dashboard shipped; preparing Phase 5**
- At the end of each phase, update this file with lessons learned

### Scraping
- Use `time.sleep()` with realistic values (3–6s between actions), never less
- `HEADLESS = True` in `config.py`; set to `False` only for debugging
- Pass `max_results=N` to `scrape()` to limit results during testing

### Outreach
- Final sending is semi-manual (not mass automated) to comply with GDPR
- AI-generated messages are drafts to review before sending

---

## Commit Conventions

Follow **Conventional Commits** format for all commit messages, always ready to copy-paste:

```
type(scope): Short description

- Bullet point 1 describing the main change
- Bullet point 2 (if needed)
```

Also list the files to include in the commit:

```
Files to include:

scraper/maps_scraper.py
```

### Types
| Type | Use |
|------|-----|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation updates |
| `style` | Code formatting, missing semicolons, etc. |
| `refactor` | Code restructuring without feature changes |
| `perf` | Performance improvements |
| `test` | Tests additions/updates |
| `chore` | Build config, dependencies, etc. |

### Commit Workflow

When asked for commits, **NEVER execute commits automatically**. Instead:

1. **Review Changes** — List all modified and new files, identify their scopes
2. **Analyze Strategy** — Determine if changes form a cohesive feature or separate concerns; order commits by least coupling first
3. **Process one commit at a time:**
   - Present the commit (message + file list, ready to copy-paste)
   - Wait for confirmation before moving to the next commit

---

## Project Phases

- [x] Phase 0: Basic Maps scraper prototype
- [x] Phase 1: Robust Maps scraper with scroll, more fields, rate limiting
- [x] Phase 2: Web analyzer (CMS detection, email/socials extraction, SEO scoring)
- [x] Phase 3: Message generation with local LLM (Qwen2.5-7B, 4-bit), full pipeline
- [x] Phase 4: CLI (`--profession`, `--city`, `--max`, `--no-headless`) + Streamlit dashboard + API client + JSON output
